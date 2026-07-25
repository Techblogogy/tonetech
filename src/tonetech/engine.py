"""Live audio engine.

Pulls audio from an input device, runs it through the rig, and pushes it to
an output device from a background thread. Doing the loop in Python (rather
than letting pedalboard's duplex ``AudioStream.run`` do it) costs one buffer
of latency but gives us input/output metering and a capture ring buffer for
the analysis tool.

The engine subscribes to :class:`~tonetech.rig.RigState` and applies knob
moves live without rebuilding the chain; structural changes swap in a freshly
built board at the next buffer boundary.
"""

from __future__ import annotations

import collections
import threading
import time
from dataclasses import dataclass, field

import numpy as np
from pedalboard import Limiter, Pedalboard, Plugin

from tonetech.rig import Rig, RigState


@dataclass
class Levels:
    input_db: float = -120.0
    output_db: float = -120.0
    input_peak_db: float = -120.0
    output_peak_db: float = -120.0


FLOOR_DB = -120.0


def _rms_db(x: np.ndarray) -> float:
    return max(FLOOR_DB, float(20.0 * np.log10(max(float(np.sqrt(np.mean(x * x))), 1e-9))))


def _peak_db(x: np.ndarray) -> float:
    return max(FLOOR_DB, float(20.0 * np.log10(max(float(np.max(np.abs(x))), 1e-9))))


@dataclass
class BuiltChain:
    board: Pedalboard
    plugins_by_block: dict[str, list[Plugin]] = field(default_factory=dict)


def build_chain(rig: Rig, safety_limiter: bool = True) -> BuiltChain:
    """Turn a rig into a pedalboard. Bypassed blocks are simply left out."""
    board = Pedalboard([])
    by_block: dict[str, list[Plugin]] = {}
    for block in rig.blocks:
        plugins = block.type.build(block.params)
        by_block[block.id] = plugins
        if not block.bypass:
            board.append(Pedalboard(plugins))
    if safety_limiter:
        board.append(Limiter(threshold_db=-1.0, release_ms=80.0))
    return BuiltChain(board=board, plugins_by_block=by_block)


class BaseEngine:
    """Interface shared by the real engine and the offline one."""

    sample_rate: float = 48000.0
    running: bool = False
    error: str | None = None

    def __init__(self, state: RigState) -> None:
        self.state = state
        self.levels = Levels()
        self._chain = build_chain(state.rig)
        self._lock = threading.Lock()
        state.subscribe(self._on_state)

    # -- state sync --------------------------------------------------------
    def _on_state(self, kind: str, payload) -> None:
        if kind == "param":
            block_id, _ = payload
            try:
                block = self.state.rig.find(block_id)
            except Exception:
                self.rebuild()
                return
            plugins = self._chain.plugins_by_block.get(block_id)
            if plugins is None:
                self.rebuild()
                return
            block.type.update(plugins, block.params)
        else:
            self.rebuild()

    def rebuild(self) -> None:
        new_chain = build_chain(self.state.rig)
        with self._lock:
            self._chain = new_chain

    @property
    def board(self) -> Pedalboard:
        return self._chain.board

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        raise NotImplementedError

    def stop(self) -> None:
        raise NotImplementedError

    def capture(self, seconds: float = 3.0) -> np.ndarray:
        raise NotImplementedError

    def render(self, audio: np.ndarray, sample_rate: float) -> np.ndarray:
        """Offline: run a buffer through the current chain (used by tests and analysis)."""
        with self._lock:
            board = self._chain.board
        return board(audio.astype(np.float32, copy=False), sample_rate, reset=True)

    def describe(self) -> str:
        return "offline"


class NullEngine(BaseEngine):
    """No audio devices. Used for ``--no-audio``, tests and screenshots.

    ``capture`` synthesises a plucked-string test tone through the chain so
    the analysis tool still returns something meaningful.
    """

    def start(self) -> None:
        self.running = True

    def stop(self) -> None:
        self.running = False

    def capture(self, seconds: float = 3.0) -> np.ndarray:
        n = int(seconds * self.sample_rate)
        t = np.arange(n) / self.sample_rate
        signal = np.zeros(n)
        # A few Karplus-Strong-ish plucks on an A chord.
        for i, f0 in enumerate((110.0, 164.8, 220.0, 277.2, 329.6)):
            onset = int(i * 0.35 * self.sample_rate)
            if onset >= n:
                break
            env = np.exp(-(t[: n - onset]) * 2.2)
            tone = sum(np.sin(2 * np.pi * f0 * k * t[: n - onset]) / k for k in range(1, 9))
            signal[onset:] += 0.12 * env * tone
        return self.render(signal[np.newaxis, :].astype(np.float32), self.sample_rate)

    def describe(self) -> str:
        return "no audio (offline)"


class AudioEngine(BaseEngine):
    """Real-time engine using two pedalboard AudioStreams and a worker thread."""

    def __init__(
        self,
        state: RigState,
        input_device: str | None = None,
        output_device: str | None = None,
        buffer_size: int = 256,
        sample_rate: float | None = None,
        capture_seconds: float = 8.0,
    ) -> None:
        super().__init__(state)
        from pedalboard.io import AudioStream  # imported lazily: touching devices is slow

        self._AudioStream = AudioStream
        self.input_device = input_device or AudioStream.default_input_device_name
        self.output_device = output_device or AudioStream.default_output_device_name
        self.buffer_size = buffer_size
        self.sample_rate = sample_rate or 48000.0
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._capture_len = int(capture_seconds * self.sample_rate)
        self._ring: collections.deque[np.ndarray] = collections.deque()
        self._ring_samples = 0
        self.dropped = 0

    def describe(self) -> str:
        return f"{self.input_device} -> {self.output_device} @ {int(self.sample_rate)}Hz/{self.buffer_size}"

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="tonetech-audio", daemon=True)
        self._thread.start()
        # Give the thread a moment to fail fast on device errors.
        time.sleep(0.3)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        self.running = False

    def _run(self) -> None:
        AudioStream = self._AudioStream
        try:
            with AudioStream(
                input_device_name=self.input_device,
                sample_rate=self.sample_rate,
                buffer_size=self.buffer_size,
                num_input_channels=1,
            ) as inp, AudioStream(
                output_device_name=self.output_device,
                sample_rate=self.sample_rate,
                buffer_size=self.buffer_size,
                num_output_channels=2,
            ) as out:
                inp.ignore_dropped_input = True
                self.sample_rate = float(inp.sample_rate)
                self.running = True
                self.error = None
                while not self._stop.is_set():
                    chunk = inp.read(self.buffer_size)
                    if chunk.ndim == 1:
                        chunk = chunk[np.newaxis, :]
                    self.dropped = int(inp.dropped_input_frame_count or 0)
                    with self._lock:
                        board = self._chain.board
                    processed = board(chunk, self.sample_rate, reset=False)
                    if processed.shape[0] == 1:
                        processed = np.vstack([processed, processed])
                    self._meter(chunk, processed)
                    self._push_capture(processed)
                    out.write(processed.astype(np.float32, copy=False), self.sample_rate)
        except Exception as exc:  # noqa: BLE001 - surfaced to the UI
            self.error = f"{type(exc).__name__}: {exc}"
        finally:
            self.running = False

    def _meter(self, inp: np.ndarray, out: np.ndarray) -> None:
        decay = 0.6
        lv = self.levels
        lv.input_db = max(_rms_db(inp), lv.input_db - 6 * decay, FLOOR_DB)
        lv.output_db = max(_rms_db(out), lv.output_db - 6 * decay, FLOOR_DB)
        lv.input_peak_db = max(_peak_db(inp), lv.input_peak_db - 3 * decay, FLOOR_DB)
        lv.output_peak_db = max(_peak_db(out), lv.output_peak_db - 3 * decay, FLOOR_DB)

    def _push_capture(self, processed: np.ndarray) -> None:
        mono = processed.mean(axis=0)
        self._ring.append(mono)
        self._ring_samples += len(mono)
        while self._ring_samples > self._capture_len:
            self._ring_samples -= len(self._ring.popleft())

    def capture(self, seconds: float = 3.0) -> np.ndarray:
        """Return the most recent *seconds* of processed output (mono, 1xN)."""
        want = int(seconds * self.sample_rate)
        with self._lock:
            chunks = list(self._ring)
        if not chunks:
            return np.zeros((1, 0), dtype=np.float32)
        data = np.concatenate(chunks)[-want:]
        return data[np.newaxis, :].astype(np.float32)


def make_engine(state: RigState, no_audio: bool = False, **kwargs) -> BaseEngine:
    """Pick an engine. Falls back to :class:`NullEngine` if devices are unusable."""
    if no_audio:
        return NullEngine(state)
    try:
        engine = AudioEngine(state, **kwargs)
    except Exception as exc:  # noqa: BLE001
        fallback = NullEngine(state)
        fallback.error = f"audio unavailable ({type(exc).__name__}: {exc}); running offline"
        return fallback
    return engine
