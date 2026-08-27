import numpy as np

from tonetech.analysis import analyze, describe


def _tone(freqs, sr=48000, seconds=1.0, amp=0.1):
    t = np.arange(int(sr * seconds)) / sr
    return sum(amp * np.sin(2 * np.pi * f * t) for f in freqs).astype(np.float32)


def test_silence_is_reported() -> None:
    a = analyze(np.zeros((1, 48000), dtype=np.float32), 48000)
    assert a.silent and a.words == ["silent"]
    assert "silent" in a.summary().lower()


def test_dark_signal_reads_dark() -> None:
    a = analyze(_tone([110, 220]), 48000)
    assert not a.silent
    assert a.centroid_hz < 400
    assert "dark" in a.words or "muddy" in a.words


def test_bright_signal_reads_bright_or_fizzy() -> None:
    a = analyze(_tone([4000, 7000, 9000]), 48000)
    assert a.centroid_hz > 3000
    assert "bright" in a.words or "fizzy" in a.words


def test_clipping_flag() -> None:
    a = analyze(np.clip(_tone([440], amp=3.0), -1, 1), 48000)
    assert a.clipping and "clipping" in a.words


def test_describe_balanced() -> None:
    bands = {k: 0.0 for k in ("sub", "low", "low_mid", "mid", "upper_mid", "presence", "air")}
    assert describe(bands, crest=12, centroid=1000, silent=False, peak_db=-6) == ["balanced"]
