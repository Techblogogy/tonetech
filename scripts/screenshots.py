"""Regenerate the README screenshots headlessly.

    PYTHONPATH=. .venv/bin/python scripts/screenshots.py

Runs the real app against the offline engine with a scripted stand-in for
the Claude client, so the pictures show real widgets and real patch output.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
from pathlib import Path

os.environ["TONETECH_HOME"] = tempfile.mkdtemp(prefix="tonetech-shots-")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.conftest import FakeClient, response, text_block, tool_block  # noqa: E402
from tonetech.agent import ToneTech  # noqa: E402
from tonetech.app import TechInput, ToneTechApp  # noqa: E402
from tonetech.engine import NullEngine  # noqa: E402
from tonetech.library import preset  # noqa: E402
from tonetech.rig import Rig, RigState  # noqa: E402

OUT = Path(__file__).resolve().parents[1] / "docs" / "screenshots"
SIZE = (132, 38)


async def wait_idle(app: ToneTechApp, pilot) -> None:
    for _ in range(100):
        await pilot.pause(0.05)
        if not app.busy:
            return
    raise RuntimeError("agent never finished")


async def type_and_send(app: ToneTechApp, pilot, text: str) -> None:
    app.query_one(TechInput).focus()
    await pilot.press(*text, "enter")
    await wait_idle(app, pilot)


def fake_levels(engine: NullEngine, inp: float = -21.0, out: float = -14.0) -> None:
    engine.levels.input_db = inp
    engine.levels.output_db = out


async def scene_board() -> None:
    state = RigState(preset("Bedroom Blues"))
    engine = NullEngine(state)
    fake_levels(engine)
    script = [
        response([tool_block("get_rig", {}, "t1")], "tool_use"),
        response([tool_block("analyze_input", {"seconds": 3}, "t2")], "tool_use"),
        response(
            [
                tool_block(
                    "apply_patch",
                    {
                        "ops": [
                            {"op": "set", "block": "b2", "param": "tone", "real": 2900},
                            {"op": "set", "block": "b3", "param": "presence", "real": 3.5},
                            {"op": "set", "block": "b3", "param": "gain", "real": 6.5},
                            {"op": "set", "block": "b4", "param": "high_cut", "real": 4600},
                        ],
                        "why": "more sag, less fizz",
                    },
                    "t3",
                )
            ],
            "tool_use",
        ),
        response(
            [
                text_block(
                    "Had a listen: the air band was sitting about 5 dB hot and the crest was low, so the fizz "
                    "was mostly the cab letting too much through. Closed the cab down to 4.6k, rolled the "
                    "Screamer's tone back and took a point off presence. Then pushed the amp gain up a notch so "
                    "it sags instead of the pedal doing all the work. Try leaning into a bend on the G string. "
                    "u undoes the lot if it's too dark."
                )
            ]
        ),
    ]
    app = ToneTechApp(state, engine)
    app.tech = ToneTech(state, engine, client=FakeClient(script), on_event=app._agent_event)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        await type_and_send(app, pilot, "more sag, less fizz")
        await pilot.press("escape", "j", "down")
        await pilot.pause()
        app.save_screenshot(str(OUT / "board.svg"))


async def scene_build() -> None:
    state = RigState(Rig(name="Untitled"))
    engine = NullEngine(state)
    fake_levels(engine, -24, -16)
    script = [
        response([tool_block("get_rig", {}, "t1")], "tool_use"),
        response(
            [
                tool_block(
                    "apply_patch",
                    {
                        "ops": [
                            {"op": "rename", "name": "Pulse Lead"},
                            {"op": "add", "kind": "comp", "params": {"sustain": 0.6, "attack": 0.4}},
                            {"op": "add", "kind": "fuzz", "params": {"fuzz": 0.72, "tone": 0.45, "level": 0.45}},
                            {"op": "add", "kind": "amp_black", "params": {"gain": 0.3, "mid": 0.5, "treble": 0.55}},
                            {"op": "add", "kind": "cab", "params": {"high_cut": 0.4, "body": 0.7}},
                            {"op": "add", "kind": "phaser", "params": {"rate": 0.35, "depth": 0.6, "feedback": 0.3}},
                            {"op": "add", "kind": "delay", "params": {"time": 0.72, "feedback": 0.48, "mix": 0.33, "tone": 0.35}},
                            {"op": "add", "kind": "reverb", "params": {"size": 0.7, "mix": 0.3}},
                        ],
                        "why": "Gilmour-style lead: fuzz into a clean amp, slow phase, long dark repeats",
                    },
                    "t2",
                )
            ],
            "tool_use",
        ),
        response(
            [
                text_block(
                    "Built you a Pulse-era lead. Compressor first for sustain, fuzz into a clean Blackface with "
                    "a scoop, cab closed down so the fuzz stays creamy, slow phaser, then 440ms repeats rolled "
                    "dark with a big plate behind it. Roll your guitar volume to about 7 for the verse and open "
                    "it up for the solo. Want me to swap the fuzz for the overdrive if it's too woolly?"
                )
            ]
        ),
    ]
    app = ToneTechApp(state, engine)
    app.tech = ToneTech(state, engine, client=FakeClient(script), on_event=app._agent_event)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        await type_and_send(app, pilot, "build me a Gilmour-style lead tone from scratch")
        await pilot.press("escape", "j", "j", "j", "j", "j")
        await pilot.pause()
        app.save_screenshot(str(OUT / "build.svg"))


async def scene_picker() -> None:
    state = RigState(preset("Plexi Crunch"))
    engine = NullEngine(state)
    fake_levels(engine, -19, -12)
    app = ToneTechApp(state, engine)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        await pilot.press("escape", "j", "j", "a")
        await pilot.pause()
        await pilot.press("down", "down", "down", "down", "down", "down", "down", "down", "down", "down")
        await pilot.pause()
        app.save_screenshot(str(OUT / "add-block.svg"))


async def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for scene in (scene_board, scene_build, scene_picker):
        await scene()
    for svg in sorted(OUT.glob("*.svg")):
        print(f"wrote {svg}")
        if "--png" in sys.argv:  # handy for previewing; needs librsvg
            png = Path(tempfile.gettempdir()) / svg.with_suffix(".png").name
            subprocess.run(["rsvg-convert", "-w", "1800", str(svg), "-o", str(png)], check=True)
            print(f"  preview {png}")


if __name__ == "__main__":
    asyncio.run(main())
