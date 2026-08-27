import numpy as np

from tonetech.engine import NullEngine, build_chain
from tonetech.rig import RigState


def test_bypassed_blocks_are_left_out(state: RigState) -> None:
    n_active = len(build_chain(state.rig).board) - 1  # minus safety limiter
    assert n_active == len(state.rig.blocks)
    state.patch([{"op": "bypass", "block": "b2"}])
    assert len(build_chain(state.rig).board) - 1 == len(state.rig.blocks) - 1


def test_param_changes_update_live_plugins_without_rebuild(state: RigState, engine: NullEngine) -> None:
    before = engine._chain
    state.patch([{"op": "set", "block": "b5", "param": "time", "real": 500}])
    assert engine._chain is before
    delay_plugin = engine._chain.plugins_by_block["b5"][0]
    assert abs(delay_plugin.delay_seconds - 0.5) < 1e-3


def test_structure_changes_rebuild(state: RigState, engine: NullEngine) -> None:
    before = engine._chain
    state.patch([{"op": "add", "kind": "eq"}])
    assert engine._chain is not before
    assert "b7" in engine._chain.plugins_by_block


def test_null_engine_capture_is_audible_and_limited(engine: NullEngine) -> None:
    audio = engine.capture(1.0)
    assert audio.shape[0] == 1 and audio.shape[1] == 48000
    peak = float(np.abs(audio).max())
    assert 0.01 < peak <= 1.0
