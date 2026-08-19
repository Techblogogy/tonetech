"""Catalog of effect blocks and how they map onto pedalboard plugins.

Every block exposes a handful of guitarist-facing knobs normalised to 0..1.
:class:`ParamSpec` converts between the knob position and engineering units,
and each :class:`BlockType` knows how to turn a set of knob values into a
list of pedalboard plugin settings. Building and live-updating share the
same ``settings`` function so the two can never drift apart.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable

from pedalboard import (
    Chorus,
    Clipping,
    Compressor,
    Delay,
    Distortion,
    Gain,
    HighpassFilter,
    HighShelfFilter,
    LowpassFilter,
    LowShelfFilter,
    NoiseGate,
    PeakFilter,
    Phaser,
    Plugin,
    Reverb,
)


class PatchError(ValueError):
    pass


@dataclass(frozen=True)
class ParamSpec:
    name: str
    label: str
    lo: float
    hi: float
    default: float  # in real units
    unit: str = ""
    curve: str = "lin"  # "lin" or "log"
    precision: int = 1

    def denorm(self, value: float) -> float:
        value = min(1.0, max(0.0, value))
        if self.curve == "log":
            return self.lo * (self.hi / self.lo) ** value
        return self.lo + (self.hi - self.lo) * value

    def norm(self, real: float) -> float:
        if self.curve == "log":
            real = min(self.hi, max(self.lo, real))
            return math.log(real / self.lo) / math.log(self.hi / self.lo)
        return (real - self.lo) / (self.hi - self.lo)

    @staticmethod
    def clamp(value: float) -> float:
        if math.isnan(value):
            raise PatchError("parameter value is NaN")
        return min(1.0, max(0.0, value))

    def format(self, value: float) -> str:
        real = self.denorm(value)
        if self.unit == "Hz":
            if real >= 1000:
                return f"{real / 1000:.2f}kHz"
            return f"{real:.0f}Hz"
        if self.unit == "" and (self.lo, self.hi) == (0, 1):
            return f"{value:.2f}"
        return f"{real:.{self.precision}f}{self.unit}"

    @property
    def default_norm(self) -> float:
        return self.clamp(self.norm(self.default))


@dataclass(frozen=True)
class BlockType:
    kind: str
    label: str
    category: str
    description: str
    params: tuple[ParamSpec, ...]
    plugins: tuple[type[Plugin], ...]
    settings: Callable[[dict[str, float]], list[dict[str, float]]]
    tags: tuple[str, ...] = ()

    def param(self, name: str) -> ParamSpec:
        for spec in self.params:
            if spec.name == name:
                return spec
        raise PatchError(f"{self.kind} has no parameter {name!r}; options: {[p.name for p in self.params]}")

    def defaults(self) -> dict[str, float]:
        return {p.name: p.default_norm for p in self.params}

    def real_values(self, params: dict[str, float]) -> dict[str, float]:
        return {p.name: p.denorm(params.get(p.name, p.default_norm)) for p in self.params}

    def build(self, params: dict[str, float]) -> list[Plugin]:
        settings = self.settings(self.real_values(params))
        return [cls(**kwargs) for cls, kwargs in zip(self.plugins, settings, strict=True)]

    def update(self, plugins: list[Plugin], params: dict[str, float]) -> None:
        settings = self.settings(self.real_values(params))
        for plugin, kwargs in zip(plugins, settings, strict=True):
            for attr, value in kwargs.items():
                setattr(plugin, attr, value)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "label": self.label,
            "category": self.category,
            "description": self.description,
            "params": [
                {
                    "name": p.name,
                    "label": p.label,
                    "range": [p.lo, p.hi],
                    "unit": p.unit,
                    "default": p.default,
                }
                for p in self.params
            ],
        }


P = ParamSpec
_BLOCKS: dict[str, BlockType] = {}


def _register(bt: BlockType) -> BlockType:
    _BLOCKS[bt.kind] = bt
    return bt


# --------------------------------------------------------------------------
# Dynamics
# --------------------------------------------------------------------------


def _gate(r: dict[str, float]) -> list[dict[str, float]]:
    return [dict(threshold_db=r["threshold"], ratio=10.0, attack_ms=1.0, release_ms=r["release"])]


_register(
    BlockType(
        kind="gate",
        label="Gate",
        category="dynamics",
        description="Noise gate. Kills hum and hiss between notes; set the threshold just above your noise floor.",
        params=(
            P("threshold", "Thresh", -80, -20, -55, "dB"),
            P("release", "Release", 20, 500, 120, "ms", "log", 0),
        ),
        plugins=(NoiseGate,),
        settings=_gate,
        tags=("noise", "hum", "high gain"),
    )
)


def _comp(r: dict[str, float]) -> list[dict[str, float]]:
    # 'sustain' is the guitarist's word for how hard the compressor works.
    threshold = -6.0 - 30.0 * r["sustain"]
    ratio = 1.5 + 6.5 * r["sustain"]
    makeup = (-threshold) * (1 - 1 / ratio) * 0.6 + r["level"]
    return [
        dict(threshold_db=threshold, ratio=ratio, attack_ms=r["attack"], release_ms=r["release"]),
        dict(gain_db=makeup),
    ]


_register(
    BlockType(
        kind="comp",
        label="Comp",
        category="dynamics",
        description="Optical-style compressor. Sustain sets threshold and ratio together; slow attack keeps pick transients.",
        params=(
            P("sustain", "Sustain", 0, 1, 0.45, ""),
            P("attack", "Attack", 1, 60, 12, "ms", "log", 0),
            P("release", "Release", 40, 600, 180, "ms", "log", 0),
            P("level", "Level", -12, 12, 0, "dB"),
        ),
        plugins=(Compressor, Gain),
        settings=_comp,
        tags=("clean", "country", "funk", "squash"),
    )
)

# --------------------------------------------------------------------------
# Drive
# --------------------------------------------------------------------------


def _boost(r: dict[str, float]) -> list[dict[str, float]]:
    return [dict(cutoff_frequency_hz=r["tight"]), dict(gain_db=r["gain"])]


_register(
    BlockType(
        kind="boost",
        label="Boost",
        category="drive",
        description="Clean boost with a low cut. Push the front of an amp harder without adding its own colour.",
        params=(
            P("gain", "Gain", 0, 24, 8, "dB"),
            P("tight", "Tight", 40, 400, 90, "Hz", "log", 0),
        ),
        plugins=(HighpassFilter, Gain),
        settings=_boost,
        tags=("solo", "push", "tighten"),
    )
)


def _od(r: dict[str, float]) -> list[dict[str, float]]:
    # Tube Screamer topology: mid hump in, soft clip, tone lowpass, level out.
    return [
        dict(cutoff_frequency_hz=720.0, gain_db=3.0 + 3.0 * (r["drive"] / 30.0), q=0.8),
        dict(cutoff_frequency_hz=110.0),
        dict(drive_db=r["drive"]),
        dict(cutoff_frequency_hz=r["tone"]),
        dict(gain_db=r["level"] - r["drive"] * 0.55),
    ]


_register(
    BlockType(
        kind="od",
        label="Overdrive",
        category="drive",
        description="Tube Screamer style overdrive: mid hump, soft clipping, tone is a low-pass. Great for pushing amps.",
        params=(
            P("drive", "Drive", 0, 30, 12, "dB", "lin", 0),
            P("tone", "Tone", 1200, 9000, 4200, "Hz", "log", 0),
            P("level", "Level", -12, 12, 0, "dB"),
        ),
        plugins=(PeakFilter, HighpassFilter, Distortion, LowpassFilter, Gain),
        settings=_od,
        tags=("blues", "srv", "tube screamer", "mid hump", "edge of breakup"),
    )
)


def _dist(r: dict[str, float]) -> list[dict[str, float]]:
    return [
        dict(cutoff_frequency_hz=80.0),
        dict(drive_db=r["gain"]),
        dict(threshold_db=-6.0 - 0.3 * r["gain"]),
        dict(cutoff_frequency_hz=r["tone"]),
        dict(gain_db=r["level"] - r["gain"] * 0.35 + 4.0),
    ]


_register(
    BlockType(
        kind="dist",
        label="Distortion",
        category="drive",
        description="Hard-clipping distortion box. Tighter and more compressed than the overdrive; think RAT or DS-1.",
        params=(
            P("gain", "Gain", 6, 45, 24, "dB", "lin", 0),
            P("tone", "Tone", 1000, 8000, 3200, "Hz", "log", 0),
            P("level", "Level", -12, 12, 0, "dB"),
        ),
        plugins=(HighpassFilter, Distortion, Clipping, LowpassFilter, Gain),
        settings=_dist,
        tags=("rock", "grunge", "rat", "ds-1", "metal"),
    )
)


def _fuzz(r: dict[str, float]) -> list[dict[str, float]]:
    return [
        dict(drive_db=20.0 + r["fuzz"] * 0.4),
        dict(threshold_db=-14.0 - r["fuzz"] * 0.25),
        dict(cutoff_frequency_hz=r["tone"]),
        dict(cutoff_frequency_hz=60.0),
        dict(gain_db=r["level"] - 2.0),
    ]


_register(
    BlockType(
        kind="fuzz",
        label="Fuzz",
        category="drive",
        description="Square-wave fuzz. Splatty and woolly; roll the guitar volume down to clean it up.",
        params=(
            P("fuzz", "Fuzz", 0, 100, 65, "", "lin", 0),
            P("tone", "Tone", 800, 7000, 2600, "Hz", "log", 0),
            P("level", "Level", -12, 12, 0, "dB"),
        ),
        plugins=(Distortion, Clipping, LowpassFilter, HighpassFilter, Gain),
        settings=_fuzz,
        tags=("hendrix", "gilmour", "big muff", "fuzz face", "doom"),
    )
)

# --------------------------------------------------------------------------
# Amps
# --------------------------------------------------------------------------


def _amp(voice: dict[str, float]) -> Callable[[dict[str, float]], list[dict[str, float]]]:
    def settings(r: dict[str, float]) -> list[dict[str, float]]:
        gain = r["gain"]
        drive_db = voice["drive_floor"] + gain * voice["drive_span"] / 10.0
        return [
            dict(cutoff_frequency_hz=voice["hp"]),
            dict(gain_db=voice["pre_gain"] + gain * 1.2),
            dict(cutoff_frequency_hz=voice["voice_hz"], gain_db=voice["voice_db"], q=0.7),
            dict(drive_db=drive_db),
            dict(cutoff_frequency_hz=voice["bass_hz"], gain_db=(r["bass"] - 5.0) * 2.4, q=0.7),
            dict(cutoff_frequency_hz=voice["mid_hz"], gain_db=(r["mid"] - 5.0) * 2.0 + voice["mid_offset"], q=0.9),
            dict(cutoff_frequency_hz=voice["treble_hz"], gain_db=(r["treble"] - 5.0) * 2.2, q=0.7),
            dict(cutoff_frequency_hz=4200.0, gain_db=(r["presence"] - 5.0) * 1.6, q=1.1),
            dict(gain_db=r["master"] - 6.0 - drive_db * 0.5),
        ]

    return settings


_AMP_PLUGINS = (
    HighpassFilter,
    Gain,
    PeakFilter,
    Distortion,
    LowShelfFilter,
    PeakFilter,
    HighShelfFilter,
    PeakFilter,
    Gain,
)
_AMP_PARAMS = (
    P("gain", "Gain", 0, 10, 4, "", "lin", 1),
    P("bass", "Bass", 0, 10, 5, "", "lin", 1),
    P("mid", "Mid", 0, 10, 5, "", "lin", 1),
    P("treble", "Treble", 0, 10, 5, "", "lin", 1),
    P("presence", "Presence", 0, 10, 5, "", "lin", 1),
    P("master", "Master", -12, 12, 0, "dB"),
)

_register(
    BlockType(
        kind="amp_tweed",
        label="Amp: Tweed",
        category="amp",
        description="Small 50s American combo. Breaks up early, warm and midrangey, sags when you dig in.",
        params=_AMP_PARAMS,
        plugins=_AMP_PLUGINS,
        settings=_amp(
            dict(hp=70, pre_gain=6, voice_hz=900, voice_db=2.5, drive_floor=4, drive_span=26,
                 bass_hz=120, mid_hz=650, mid_offset=1.5, treble_hz=2800)
        ),
        tags=("blues", "neil young", "keith richards", "warm", "early breakup"),
    )
)

_register(
    BlockType(
        kind="amp_black",
        label="Amp: Blackface",
        category="amp",
        description="60s American clean platform. Scooped mids, glassy top, tons of headroom; the pedal-friendly amp.",
        params=_AMP_PARAMS,
        plugins=_AMP_PLUGINS,
        settings=_amp(
            dict(hp=60, pre_gain=2, voice_hz=3200, voice_db=2.0, drive_floor=0, drive_span=16,
                 bass_hz=100, mid_hz=500, mid_offset=-2.5, treble_hz=3500)
        ),
        tags=("clean", "surf", "country", "john mayer", "sparkle", "pedal platform"),
    )
)

_register(
    BlockType(
        kind="amp_brit",
        label="Amp: Brit",
        category="amp",
        description="British stack. Aggressive upper mids, crunchy, gets saggy and huge with the gain up.",
        params=_AMP_PARAMS,
        plugins=_AMP_PLUGINS,
        settings=_amp(
            dict(hp=90, pre_gain=8, voice_hz=1800, voice_db=3.5, drive_floor=6, drive_span=32,
                 bass_hz=110, mid_hz=800, mid_offset=2.0, treble_hz=2600)
        ),
        tags=("rock", "ac/dc", "led zeppelin", "crunch", "plexi", "marshall"),
    )
)

_register(
    BlockType(
        kind="amp_hi",
        label="Amp: Hi-Gain",
        category="amp",
        description="Modern high-gain head. Tight low end, saturated, scooped enough for chugging.",
        params=_AMP_PARAMS,
        plugins=_AMP_PLUGINS,
        settings=_amp(
            dict(hp=110, pre_gain=12, voice_hz=1400, voice_db=2.0, drive_floor=18, drive_span=30,
                 bass_hz=130, mid_hz=700, mid_offset=-3.0, treble_hz=3000)
        ),
        tags=("metal", "djent", "rectifier", "5150", "chug"),
    )
)

# --------------------------------------------------------------------------
# Cabinet
# --------------------------------------------------------------------------


def _cab(r: dict[str, float]) -> list[dict[str, float]]:
    return [
        dict(cutoff_frequency_hz=r["low_cut"]),
        dict(cutoff_frequency_hz=150.0, gain_db=r["body"], q=0.9),
        dict(cutoff_frequency_hz=2400.0, gain_db=1.5, q=1.4),
        dict(cutoff_frequency_hz=r["high_cut"]),
        dict(cutoff_frequency_hz=r["high_cut"] * 0.9),
    ]


_register(
    BlockType(
        kind="cab",
        label="Cab",
        category="cab",
        description="Speaker cabinet emulation: low cut, body resonance, cone breakup bump and a steep high cut.",
        params=(
            P("low_cut", "Low cut", 50, 200, 85, "Hz", "log", 0),
            P("body", "Body", -6, 6, 1.5, "dB"),
            P("high_cut", "High cut", 3000, 9000, 5500, "Hz", "log", 0),
        ),
        plugins=(HighpassFilter, PeakFilter, PeakFilter, LowpassFilter, LowpassFilter),
        settings=_cab,
        tags=("speaker", "fizz", "always last"),
    )
)

# --------------------------------------------------------------------------
# EQ
# --------------------------------------------------------------------------


def _eq(r: dict[str, float]) -> list[dict[str, float]]:
    return [
        dict(cutoff_frequency_hz=120.0, gain_db=r["low"], q=0.7),
        dict(cutoff_frequency_hz=r["mid_freq"], gain_db=r["mid"], q=1.0),
        dict(cutoff_frequency_hz=3000.0, gain_db=r["high"], q=0.7),
    ]


_register(
    BlockType(
        kind="eq",
        label="EQ",
        category="eq",
        description="Three-band EQ with a sweepable mid. Fix boxiness (cut 400-600Hz) or mud (cut around 200Hz).",
        params=(
            P("low", "Low", -12, 12, 0, "dB"),
            P("mid", "Mid", -12, 12, 0, "dB"),
            P("mid_freq", "Mid freq", 200, 3000, 600, "Hz", "log", 0),
            P("high", "High", -12, 12, 0, "dB"),
        ),
        plugins=(LowShelfFilter, PeakFilter, HighShelfFilter),
        settings=_eq,
        tags=("boxy", "mud", "honk", "shape"),
    )
)

# --------------------------------------------------------------------------
# Modulation
# --------------------------------------------------------------------------


def _chorus(r: dict[str, float]) -> list[dict[str, float]]:
    return [dict(rate_hz=r["rate"], depth=r["depth"], centre_delay_ms=7.0, feedback=0.0, mix=r["mix"])]


_register(
    BlockType(
        kind="chorus",
        label="Chorus",
        category="mod",
        description="Analog-style chorus. Slow and shallow for width, fast and deep for 80s shimmer.",
        params=(
            P("rate", "Rate", 0.1, 5, 0.8, "Hz", "log", 2),
            P("depth", "Depth", 0, 1, 0.3, ""),
            P("mix", "Mix", 0, 1, 0.5, ""),
        ),
        plugins=(Chorus,),
        settings=_chorus,
        tags=("80s", "the cure", "nirvana", "andy summers", "shimmer"),
    )
)


def _phaser(r: dict[str, float]) -> list[dict[str, float]]:
    return [dict(rate_hz=r["rate"], depth=r["depth"], centre_frequency_hz=1300.0, feedback=r["feedback"], mix=0.5)]


_register(
    BlockType(
        kind="phaser",
        label="Phaser",
        category="mod",
        description="Four-stage phaser. Slow sweep for swirl, more feedback for a vocal, resonant peak.",
        params=(
            P("rate", "Rate", 0.05, 4, 0.5, "Hz", "log", 2),
            P("depth", "Depth", 0, 1, 0.6, ""),
            P("feedback", "Feedback", 0, 0.9, 0.3, ""),
        ),
        plugins=(Phaser,),
        settings=_phaser,
        tags=("gilmour", "evh", "funk", "swirl"),
    )
)

# --------------------------------------------------------------------------
# Time
# --------------------------------------------------------------------------


def _delay(r: dict[str, float]) -> list[dict[str, float]]:
    return [
        dict(delay_seconds=r["time"] / 1000.0, feedback=r["feedback"], mix=r["mix"]),
        dict(cutoff_frequency_hz=r["tone"]),
    ]


_register(
    BlockType(
        kind="delay",
        label="Delay",
        category="time",
        description="Digital delay with a darkening tone control on the output. 80-140ms is slapback, 350-450ms is ambient.",
        params=(
            P("time", "Time", 40, 1200, 380, "ms", "log", 0),
            P("feedback", "Feedback", 0, 0.9, 0.3, ""),
            P("mix", "Mix", 0, 1, 0.3, ""),
            P("tone", "Tone", 1500, 12000, 5000, "Hz", "log", 0),
        ),
        plugins=(Delay, LowpassFilter),
        settings=_delay,
        tags=("slapback", "rockabilly", "the edge", "u2", "ambient", "dotted eighth"),
    )
)


def _reverb(r: dict[str, float]) -> list[dict[str, float]]:
    mix = r["mix"]
    return [
        dict(
            room_size=r["size"],
            damping=r["damping"],
            wet_level=mix * 0.6,
            dry_level=1.0 - mix * 0.4,
            width=r["width"],
            freeze_mode=0.0,
        )
    ]


_register(
    BlockType(
        kind="reverb",
        label="Reverb",
        category="time",
        description="Plate/hall style reverb. Small size with high damping is a spring-ish room; big and bright is a wash.",
        params=(
            P("size", "Size", 0, 1, 0.5, ""),
            P("damping", "Damping", 0, 1, 0.5, ""),
            P("mix", "Mix", 0, 1, 0.25, ""),
            P("width", "Width", 0, 1, 1.0, ""),
        ),
        plugins=(Reverb,),
        settings=_reverb,
        tags=("plate", "spring", "hall", "ambient", "surf"),
    )
)


CATALOG: dict[str, BlockType] = dict(_BLOCKS)
CATEGORY_ORDER = ("dynamics", "drive", "amp", "cab", "eq", "mod", "time")


def catalog_summary() -> str:
    """Plain-text catalog used in the agent's system prompt."""
    lines = []
    for category in CATEGORY_ORDER:
        for bt in CATALOG.values():
            if bt.category != category:
                continue
            params = ", ".join(f"{p.name} ({p.lo:g}-{p.hi:g}{p.unit or ''})" for p in bt.params)
            lines.append(f"- {bt.kind} [{bt.label}]: {bt.description} Params: {params}.")
    return "\n".join(lines)
