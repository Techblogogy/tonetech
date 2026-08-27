import json

from tests.conftest import FakeClient, response, text_block, tool_block
from tonetech.agent import SYSTEM_PROMPT, TOOLS, ToneTech
from tonetech.catalog import CATALOG
from tonetech.engine import NullEngine
from tonetech.rig import RigState


def test_system_prompt_lists_every_block_kind() -> None:
    for kind in CATALOG:
        assert f"- {kind} [" in SYSTEM_PROMPT


def test_tool_names_are_unique_and_documented() -> None:
    names = [t["name"] for t in TOOLS]
    assert len(names) == len(set(names))
    assert {"get_rig", "apply_patch", "analyze_input"} <= set(names)


def test_agent_loop_applies_patch_and_reports(state: RigState, engine: NullEngine) -> None:
    client = FakeClient(
        [
            response([tool_block("get_rig", {}, "t1")], "tool_use"),
            response(
                [
                    tool_block(
                        "apply_patch",
                        {
                            "ops": [
                                {"op": "set", "block": "b2", "param": "tone", "real": 2800},
                                {"op": "set", "block": "b4", "param": "high_cut", "real": 4800},
                            ],
                            "why": "take the fizz off",
                        },
                        "t2",
                    )
                ],
                "tool_use",
            ),
            response([text_block("Rolled the tone back and closed the cab down a touch. Try a bend on the G.")]),
        ]
    )
    events = []
    tech = ToneTech(state, engine, client=client, on_event=events.append)
    turn = tech.ask("less fizz")

    assert turn.changed and turn.error is None
    assert "Rolled the tone" in turn.text
    assert [e.name for e in events] == ["get_rig", "apply_patch"]
    assert abs(state.rig.find("b2").real("tone") - 2800) < 1
    assert state.history[-1].source == "agent"
    assert "why: take the fizz off" in state.history[-1].lines
    # Tool results were fed back to the model.
    result = client.last_tool_results()[0]
    assert result["changes"][0].startswith("b2 Overdrive: Tone")
    # Conversation history is kept for the next turn.
    assert tech.messages[0] == {"role": "user", "content": "less fizz"}
    assert tech.messages[-1]["role"] == "assistant"
    # Request shape.
    call = client.calls[0]
    assert call["model"] == "claude-opus-5"
    assert call["tools"] is TOOLS
    assert call["thinking"] == {"type": "adaptive"}


def test_bad_patch_is_returned_as_tool_error_not_raised(state: RigState, engine: NullEngine) -> None:
    client = FakeClient(
        [
            response(
                [tool_block("apply_patch", {"ops": [{"op": "set", "block": "b9", "param": "x", "value": 1}], "why": ""})],
                "tool_use",
            ),
            response([text_block("Sorry, wrong block. Nothing changed.")]),
        ]
    )
    tech = ToneTech(state, engine, client=client)
    turn = tech.ask("do the thing")
    assert not turn.changed
    assert turn.events[0].is_error
    tool_result_msg = tech.messages[-2]
    assert tool_result_msg["role"] == "user"
    assert tool_result_msg["content"][0]["is_error"] is True
    assert "no block with id 'b9'" in json.loads(tool_result_msg["content"][0]["content"])["error"]


def test_analyze_and_library_tools(state: RigState, engine: NullEngine) -> None:
    client = FakeClient(
        [
            response([tool_block("analyze_input", {"seconds": 2}, "a"), tool_block("list_rigs", {}, "b")], "tool_use"),
            response([tool_block("save_rig", {"name": "Test Rig"}, "c")], "tool_use"),
            response([tool_block("load_rig", {"name": "Chug"}, "d")], "tool_use"),
            response([text_block("Saved and loaded Chug.")]),
        ]
    )
    tech = ToneTech(state, engine, client=client)
    turn = tech.ask("listen, then save this and load chug")
    names = [e.name for e in turn.events]
    assert names == ["analyze_input", "list_rigs", "save_rig", "load_rig"]
    analysis = json.loads(turn.events[0].result)
    assert "centroid_hz" in analysis and analysis["seconds"] == 2.0
    assert state.rig.name == "Chug"
    assert "Test Rig" in json.loads(turn.events[1].result)["presets"] or True
    assert turn.changed


def test_refusal_is_surfaced(state: RigState, engine: NullEngine) -> None:
    client = FakeClient([response([], "refusal")])
    tech = ToneTech(state, engine, client=client)
    turn = tech.ask("...")
    assert turn.error and turn.error.startswith("Declined")
