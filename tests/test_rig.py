import pytest

from tonetech.catalog import CATALOG
from tonetech.library import preset
from tonetech.rig import PatchError, Rig, RigState, apply_patch


def test_set_by_value_real_and_delta(state: RigState) -> None:
    tone = state.rig.find("b2").type.param("tone")
    state.patch([{"op": "set", "block": "b2", "param": "tone", "real": 2500}])
    assert abs(state.rig.find("b2").real("tone") - 2500) < 1
    state.patch([{"op": "set", "block": "b2", "param": "tone", "value": 1.0}])
    assert state.rig.find("b2").real("tone") == pytest.approx(tone.hi)
    state.patch([{"op": "set", "block": "b2", "param": "tone", "delta": -0.5}])
    assert state.rig.find("b2").params["tone"] == pytest.approx(0.5)


def test_values_are_clamped(state: RigState) -> None:
    state.patch([{"op": "set", "block": "b1", "param": "sustain", "value": 7}])
    assert state.rig.find("b1").params["sustain"] == 1.0
    state.patch([{"op": "set", "block": "b1", "param": "sustain", "real": -999}])
    assert state.rig.find("b1").params["sustain"] == 0.0


def test_bad_patch_leaves_rig_untouched(state: RigState) -> None:
    before = state.rig.to_json()
    with pytest.raises(PatchError):
        state.patch(
            [
                {"op": "set", "block": "b2", "param": "tone", "value": 0.1},  # valid
                {"op": "set", "block": "b2", "param": "nope", "value": 0.1},  # invalid
            ]
        )
    assert state.rig.to_json() == before
    assert not state.history


def test_add_remove_move(state: RigState) -> None:
    lines = state.patch([{"op": "add", "kind": "chorus", "at": 1}])
    assert lines == ["added b7 Chorus at position 1"]
    assert [b.kind for b in state.rig.blocks][:3] == ["comp", "chorus", "od"]
    state.patch([{"op": "move", "block": "b7", "to": 5}])
    assert state.rig.blocks[5].kind == "chorus"
    state.patch([{"op": "remove", "block": "b7"}])
    assert all(b.kind != "chorus" for b in state.rig.blocks)


def test_unknown_kind_and_op() -> None:
    rig = Rig()
    with pytest.raises(PatchError):
        apply_patch(rig, [{"op": "add", "kind": "tremolo"}])
    with pytest.raises(PatchError):
        apply_patch(rig, [{"op": "explode"}])
    with pytest.raises(PatchError):
        apply_patch(rig, ["not an op"])


def test_undo_redo_round_trip(state: RigState) -> None:
    original = state.rig.to_json()
    state.patch([{"op": "set", "block": "b3", "param": "gain", "value": 0.9}], source="agent")
    state.patch([{"op": "bypass", "block": "b4"}])
    assert state.can_undo and not state.can_redo
    state.undo()
    assert not state.rig.find("b4").bypass
    state.undo()
    assert state.rig.to_json() == original
    assert not state.can_undo
    state.redo()
    assert state.rig.find("b3").params["gain"] == pytest.approx(0.9)
    assert state.history[-1].lines[0].startswith("redid:")


def test_nudges_coalesce_into_one_undo_step(state: RigState) -> None:
    start = state.rig.find("b1").params["sustain"]
    for _ in range(5):
        state.nudge("b1", "sustain", 0.02)
    assert len(state.history) == 1
    assert state.history[0].lines[0].startswith("b1 Comp: Sustain")
    state.undo()
    assert state.rig.find("b1").params["sustain"] == pytest.approx(start)


def test_nudge_after_other_change_starts_new_step(state: RigState) -> None:
    state.nudge("b1", "sustain", 0.02)
    state.patch([{"op": "bypass", "block": "b2"}])
    state.nudge("b1", "sustain", 0.02)
    assert len(state.history) == 3


def test_json_round_trip_and_new_ids_do_not_collide() -> None:
    rig = preset("Plexi Crunch")
    clone = Rig.from_json(rig.to_json())
    assert clone.to_dict() == rig.to_dict()
    new = clone.new_block("eq")
    assert new.id not in {b.id for b in clone.blocks}


def test_from_dict_fills_params_added_later() -> None:
    data = {"name": "old", "blocks": [{"id": "b1", "kind": "delay", "params": {"time": 0.5}}]}
    rig = Rig.from_dict(data)
    assert set(rig.blocks[0].params) == {p.name for p in CATALOG["delay"].params}


def test_listeners_distinguish_param_from_structure(state: RigState) -> None:
    seen = []
    state.subscribe(lambda kind, payload: seen.append(kind))
    state.patch([{"op": "set", "block": "b1", "param": "level", "value": 0.7}])
    state.nudge("b1", "level", 0.01)
    state.patch([{"op": "bypass", "block": "b1"}])
    state.undo()
    assert seen == ["param", "param", "structure", "structure"]
