"""Command line entry point: ``tonetech`` or ``python -m tonetech``."""

from __future__ import annotations

import argparse
import sys

from tonetech import __version__
from tonetech.library import PRESETS, load_rig, preset
from tonetech.rig import Rig, RigState


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tonetech", description="Agent-driven terminal pedalboard.")
    parser.add_argument("--rig", help="Saved rig or preset to load on start (default: Bedroom Blues).")
    parser.add_argument("--empty", action="store_true", help="Start with an empty chain.")
    parser.add_argument("--no-audio", action="store_true", help="Run without touching audio devices.")
    parser.add_argument("--input", help="Input device name (see --devices).")
    parser.add_argument("--output", help="Output device name (see --devices).")
    parser.add_argument("--buffer", type=int, default=256, help="Buffer size in samples (default 256).")
    parser.add_argument("--rate", type=float, default=48000, help="Sample rate (default 48000).")
    parser.add_argument("--devices", action="store_true", help="List audio devices and exit.")
    parser.add_argument("--presets", action="store_true", help="List factory presets and exit.")
    parser.add_argument("--version", action="version", version=f"tonetech {__version__}")
    args = parser.parse_args(argv)

    if args.presets:
        print("\n".join(PRESETS))
        return 0
    if args.devices:
        from pedalboard.io import AudioStream

        print("inputs:")
        for name in AudioStream.input_device_names:
            print(f"  {'*' if name == AudioStream.default_input_device_name else ' '} {name}")
        print("outputs:")
        for name in AudioStream.output_device_names:
            print(f"  {'*' if name == AudioStream.default_output_device_name else ' '} {name}")
        return 0

    if args.empty:
        rig = Rig(name="Untitled")
    elif args.rig:
        try:
            rig = load_rig(args.rig)
        except FileNotFoundError as exc:
            print(exc, file=sys.stderr)
            return 2
    else:
        rig = preset("Bedroom Blues")

    from tonetech.app import ToneTechApp
    from tonetech.engine import make_engine

    state = RigState(rig)
    engine = make_engine(
        state,
        no_audio=args.no_audio,
        input_device=args.input,
        output_device=args.output,
        buffer_size=args.buffer,
        sample_rate=args.rate,
    )
    ToneTechApp(state, engine).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
