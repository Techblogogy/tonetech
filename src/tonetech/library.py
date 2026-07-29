"""Saving and loading rigs, plus the bundled factory presets."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from tonetech.rig import Rig


def rigs_dir() -> Path:
    override = os.environ.get("TONETECH_HOME")
    base = Path(override) if override else Path.home() / ".tonetech"
    path = base / "rigs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "rig"


def save_rig(rig: Rig, name: str | None = None) -> Path:
    if name:
        rig.name = name
    path = rigs_dir() / f"{slugify(rig.name)}.json"
    path.write_text(rig.to_json())
    return path


def load_rig(name: str) -> Rig:
    path = rigs_dir() / f"{slugify(name)}.json"
    if not path.exists():
        if name in PRESETS:
            return preset(name)
        raise FileNotFoundError(f"no saved rig called {name!r}")
    return Rig.from_json(path.read_text())


def list_rigs() -> list[str]:
    saved = sorted(json.loads(p.read_text()).get("name", p.stem) for p in rigs_dir().glob("*.json"))
    return saved


# --------------------------------------------------------------------------
# Factory presets. Params are in real units; the loader normalises them.
# --------------------------------------------------------------------------

_PRESET_DEFS: dict[str, dict] = {
    "Bedroom Blues": {
        "notes": "Warm tweed at the edge of breakup with a touch of slap.",
        "blocks": [
            ("comp", {"sustain": 0.35, "attack": 15, "release": 200, "level": 0}),
            ("od", {"drive": 9, "tone": 3800, "level": 2}),
            ("amp_tweed", {"gain": 5.5, "bass": 5, "mid": 6, "treble": 5, "presence": 4.5, "master": 0}),
            ("cab", {"low_cut": 90, "body": 2, "high_cut": 5200}),
            ("delay", {"time": 120, "feedback": 0.12, "mix": 0.18, "tone": 4000}),
            ("reverb", {"size": 0.35, "damping": 0.6, "mix": 0.2, "width": 1.0}),
        ],
    },
    "Glassy Clean": {
        "notes": "Blackface pedal platform. Compressed, wide, sparkly.",
        "blocks": [
            ("comp", {"sustain": 0.55, "attack": 20, "release": 250, "level": 1}),
            ("amp_black", {"gain": 2.5, "bass": 6, "mid": 4, "treble": 6.5, "presence": 6, "master": 0}),
            ("cab", {"low_cut": 80, "body": 1, "high_cut": 6500}),
            ("chorus", {"rate": 0.6, "depth": 0.25, "mix": 0.4}),
            ("reverb", {"size": 0.55, "damping": 0.4, "mix": 0.3, "width": 1.0}),
        ],
    },
    "Plexi Crunch": {
        "notes": "Brit stack pushed with a boost. Roll the guitar volume down for cleans.",
        "blocks": [
            ("gate", {"threshold": -58, "release": 150}),
            ("boost", {"gain": 10, "tight": 120}),
            ("amp_brit", {"gain": 6.5, "bass": 5, "mid": 7, "treble": 6, "presence": 6, "master": -2}),
            ("cab", {"low_cut": 95, "body": 1.5, "high_cut": 5000}),
            ("reverb", {"size": 0.3, "damping": 0.7, "mix": 0.12, "width": 0.8}),
        ],
    },
    "Comfortably Wet": {
        "notes": "Fuzz into a clean amp, phaser, long dark repeats. Sustain for days.",
        "blocks": [
            ("comp", {"sustain": 0.6, "attack": 10, "release": 300, "level": 0}),
            ("fuzz", {"fuzz": 70, "tone": 2400, "level": -2}),
            ("amp_black", {"gain": 3, "bass": 5.5, "mid": 5, "treble": 5.5, "presence": 5, "master": 0}),
            ("cab", {"low_cut": 85, "body": 2.5, "high_cut": 4800}),
            ("phaser", {"rate": 0.35, "depth": 0.6, "feedback": 0.25}),
            ("delay", {"time": 440, "feedback": 0.42, "mix": 0.32, "tone": 3200}),
            ("reverb", {"size": 0.7, "damping": 0.5, "mix": 0.3, "width": 1.0}),
        ],
    },
    "Chug": {
        "notes": "Tight modern high gain. Gate is doing a lot of work here.",
        "blocks": [
            ("gate", {"threshold": -48, "release": 60}),
            ("od", {"drive": 3, "tone": 5000, "level": 6}),
            ("amp_hi", {"gain": 7.5, "bass": 5.5, "mid": 4, "treble": 6, "presence": 6.5, "master": -3}),
            ("cab", {"low_cut": 110, "body": 0.5, "high_cut": 6000}),
        ],
    },
}


def preset(name: str) -> Rig:
    spec = _PRESET_DEFS[name]
    rig = Rig(name=name, notes=spec["notes"])
    for kind, real_params in spec["blocks"]:
        block = rig.new_block(kind)
        for pname, real in real_params.items():
            block.params[pname] = block.type.param(pname).clamp(block.type.param(pname).norm(real))
        rig.blocks.append(block)
    return rig


PRESETS: tuple[str, ...] = tuple(_PRESET_DEFS)
