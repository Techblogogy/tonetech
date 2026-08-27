import pytest

from tests.conftest import FakeClient, response, text_block, tool_block
from tonetech.agent import ToneTech
from tonetech.app import BlockCard, PickerScreen, TechInput, ToneTechApp
from tonetech.engine import NullEngine
from tonetech.rig import RigState


@pytest.mark.asyncio
async def test_keyboard_drives_the_board(state: RigState, engine: NullEngine) -> None:
    app = ToneTechApp(state, engine)
    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.press("escape", "j", "down", "l", "l", "space")
        await pilot.pause()
        assert app.selected == 1 and app.knob == 1
        assert state.rig.find("b2").bypass
        assert len(app.query(BlockCard)) == 6
        await pilot.press("u")
        await pilot.pause()
        assert not state.rig.find("b2").bypass
        await pilot.press("d")
        await pilot.pause()
        assert len(app.query(BlockCard)) == 5
        await pilot.press("a")
        await pilot.pause()
        assert isinstance(app.screen, PickerScreen)
        await pilot.press("escape")


@pytest.mark.asyncio
async def test_asking_the_tech_updates_board_and_log(state: RigState, engine: NullEngine) -> None:
    client = FakeClient(
        [
            response(
                [tool_block("apply_patch", {"ops": [{"op": "add", "kind": "chorus"}], "why": "width"})],
                "tool_use",
            ),
            response([text_block("Added a chorus at the end for some width.")]),
        ]
    )
    app = ToneTechApp(state, engine)
    app.tech = ToneTech(state, engine, client=client, on_event=app._agent_event)
    async with app.run_test(size=(120, 36)) as pilot:
        app.query_one(TechInput).focus()
        await pilot.press(*"add chorus", "enter")
        for _ in range(50):
            await pilot.pause(0.05)
            if not app.busy:
                break
        assert not app.busy
        assert state.rig.blocks[-1].kind == "chorus"
        assert len(app.query(BlockCard)) == 7
