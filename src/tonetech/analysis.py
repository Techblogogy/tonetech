"""Signal analysis that turns a few seconds of guitar into words.

The agent calls this through the ``analyze_input`` tool so its suggestions
are grounded in what is actually coming out of the chain, not just in what
the knobs say.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any

import numpy as np

BANDS: tuple[tuple[str, float, float], ...] = (
    ("sub", 20, 80),
    ("low", 80, 250),
    ("low_mid", 250, 600),
    ("mid", 600, 1500),
    ("upper_mid", 1500, 3500),
    ("presence", 3500, 6000),
    ("air", 6000, 12000),
)


@dataclass
class Analysis:
    seconds: float
    rms_db: float
    peak_db: float
    crest_db: float
    centroid_hz: float
    bands_db: dict[str, float]
    clipping: bool
    silent: bool
    words: list[str]

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["bands_db"] = {k: round(v, 1) for k, v in self.bands_db.items()}
        for key in ("rms_db", "peak_db", "crest_db"):
            d[key] = round(d[key], 1)
        d["centroid_hz"] = round(self.centroid_hz)
        d["seconds"] = round(self.seconds, 2)
        return d

    def summary(self) -> str:
        if self.silent:
            return "No signal detected (input is silent). Check the input device and play something."
        bands = ", ".join(f"{k} {v:+.0f}" for k, v in self.bands_db.items())
        return (
            f"{self.seconds:.1f}s captured. RMS {self.rms_db:.0f} dBFS, peak {self.peak_db:.0f} dBFS, "
            f"crest {self.crest_db:.0f} dB, centroid {self.centroid_hz:.0f} Hz. "
            f"Band balance (dB rel. to mean): {bands}. "
            + ("Output is clipping. " if self.clipping else "")
            + "Reads as: " + ", ".join(self.words) + "."
        )


def _db(x: float) -> float:
    return float(20.0 * np.log10(max(x, 1e-9)))


def analyze(audio: np.ndarray, sample_rate: float) -> Analysis:
    """Analyse mono or multi-channel float audio (channels-first)."""
    if audio.ndim == 2:
        mono = audio.mean(axis=0)
    else:
        mono = audio
    mono = mono.astype(np.float64, copy=False)
    seconds = len(mono) / float(sample_rate)
    if len(mono) < 256:
        return Analysis(seconds, -120, -120, 0, 0, {b[0]: 0.0 for b in BANDS}, False, True, ["silent"])

    rms = float(np.sqrt(np.mean(mono**2)))
    peak = float(np.max(np.abs(mono)))
    rms_db, peak_db = _db(rms), _db(peak)
    silent = rms_db < -70
    crest = peak_db - rms_db

    # Welch-ish averaged spectrum.
    n = 4096
    window = np.hanning(n)
    hop = n // 2
    spec = np.zeros(n // 2 + 1)
    count = 0
    for start in range(0, max(1, len(mono) - n), hop):
        frame = mono[start : start + n]
        if len(frame) < n:
            break
        spec += np.abs(np.fft.rfft(frame * window)) ** 2
        count += 1
    if count == 0:
        frame = np.pad(mono, (0, n - len(mono)))
        spec = np.abs(np.fft.rfft(frame * window)) ** 2
        count = 1
    spec /= count
    freqs = np.fft.rfftfreq(n, 1.0 / sample_rate)
    total = float(spec.sum()) + 1e-18
    centroid = float((freqs * spec).sum() / total)

    band_power = {}
    for name, lo, hi in BANDS:
        mask = (freqs >= lo) & (freqs < hi)
        band_power[name] = float(spec[mask].sum() / max(1, mask.sum()))
    mean_power = float(np.mean(list(band_power.values()))) + 1e-18
    bands_db = {k: 10.0 * float(np.log10(max(v, 1e-18) / mean_power)) for k, v in band_power.items()}

    words = describe(bands_db, crest, centroid, silent, peak_db)
    return Analysis(
        seconds=seconds,
        rms_db=rms_db,
        peak_db=peak_db,
        crest_db=crest,
        centroid_hz=centroid,
        bands_db=bands_db,
        clipping=peak_db > -0.3,
        silent=silent,
        words=words,
    )


def describe(bands_db: dict[str, float], crest: float, centroid: float, silent: bool, peak_db: float) -> list[str]:
    """Map measurements to the words guitarists use."""
    if silent:
        return ["silent"]
    words: list[str] = []
    b = bands_db
    if b["low"] > 4 and b["low_mid"] > 2:
        words.append("muddy")
    elif b["low"] < -5:
        words.append("thin")
    if b["low_mid"] > 4:
        words.append("boxy")
    if b["mid"] > 4:
        words.append("honky")
    elif b["mid"] < -5 and b["low_mid"] < -3:
        words.append("scooped")
    if b["upper_mid"] > 4:
        words.append("forward")
    if b["presence"] > 4 or b["air"] > 4:
        words.append("fizzy" if b["air"] > 4 else "bright")
    elif b["presence"] < -6 and b["air"] < -8:
        words.append("dark")
    if crest < 8:
        words.append("compressed")
    elif crest > 18:
        words.append("dynamic")
    if peak_db > -0.3:
        words.append("clipping")
    if not words:
        words.append("balanced")
    return words
