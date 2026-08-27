import numpy as np
import pytest
from pedalboard import Pedalboard

from tonetech.catalog import CATALOG, ParamSpec


@pytest.mark.parametrize("kind", sorted(CATALOG))
def test_every_block_builds_updates_and_passes_audio(kind: str) -> None:
    bt = CATALOG[kind]
    plugins = bt.build(bt.defaults())
    assert len(plugins) == len(bt.plugins)
    x = (np.random.default_rng(0).standard_normal((1, 4800)) * 0.05).astype(np.float32)
    y = Pedalboard(plugins)(x, 48000)
    assert y.shape == x.shape
    assert np.isfinite(y).all()
    bt.update(plugins, {p.name: 1.0 for p in bt.params})
    bt.update(plugins, {p.name: 0.0 for p in bt.params})


def test_param_norm_denorm_round_trip() -> None:
    for spec in (p for bt in CATALOG.values() for p in bt.params):
        for v in (0.0, 0.25, 0.5, 0.9, 1.0):
            assert spec.norm(spec.denorm(v)) == pytest.approx(v, abs=1e-6), spec


def test_log_curve_is_geometric() -> None:
    spec = ParamSpec("f", "F", 100, 10000, 1000, "Hz", "log")
    assert spec.denorm(0.5) == pytest.approx(1000)
    assert spec.format(0.5) == "1kHz"
    assert spec.format(0.0) == "100Hz"


def test_defaults_land_on_declared_default() -> None:
    for bt in CATALOG.values():
        for p in bt.params:
            assert p.denorm(bt.defaults()[p.name]) == pytest.approx(p.default, rel=1e-6)


def test_od_level_tracks_drive_to_stay_sane() -> None:
    bt = CATALOG["od"]
    quiet = bt.settings(bt.real_values({"drive": 0.0, "tone": 0.5, "level": 0.5}))[-1]["gain_db"]
    loud = bt.settings(bt.real_values({"drive": 1.0, "tone": 0.5, "level": 0.5}))[-1]["gain_db"]
    assert loud < quiet
