"""Rig data model, patch application and undo history.

Everything the agent can do to a rig goes through :func:`apply_patch`, which
takes a list of small JSON-serialisable operations, validates them against the
block catalog and returns human-readable change lines. The audio engine never
sees the agent directly.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from tonetech.catalog import CATALOG, BlockType, ParamSpec, PatchError

__all__ = ["Block", "Rig", "RigState", "HistoryEntry", "PatchError", "apply_patch"]


@dataclass
class Block:
    id: str
    kind: str
    params: dict[str, float]
    bypass: bool = False

    @property
    def type(self) -> BlockType:
        return CATALOG[self.kind]

    @property
    def label(self) -> str:
        return self.type.label

    def real(self, name: str) -> float:
        """Return a parameter in engineering units (Hz, dB, ms...)."""
        return self.type.param(name).denorm(self.params[name])

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "params": dict(self.params),
            "bypass": self.bypass,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Block":
        return cls(
            id=str(data["id"]),
            kind=str(data["kind"]),
            params={k: float(v) for k, v in data.get("params", {}).items()},
            bypass=bool(data.get("bypass", False)),
        )


@dataclass
class Rig:
    name: str = "Untitled"
    blocks: list[Block] = field(default_factory=list)
    notes: str = ""
    _next_id: int = field(default=1, repr=False, compare=False)

    def new_block(self, kind: str, params: dict[str, float] | None = None, bypass: bool = False) -> Block:
        if kind not in CATALOG:
            raise PatchError(f"unknown block kind {kind!r}")
        btype = CATALOG[kind]
        merged = btype.defaults()
        for name, value in (params or {}).items():
            spec = btype.param(name)  # raises PatchError on unknown param
            merged[name] = spec.clamp(float(value))
        # Make sure ids never collide with blocks loaded from disk.
        while True:
            bid = f"b{self._next_id}"
            self._next_id += 1
            if all(b.id != bid for b in self.blocks):
                break
        return Block(id=bid, kind=kind, params=merged, bypass=bypass)

    def find(self, block_id: str) -> Block:
        for block in self.blocks:
            if block.id == block_id:
                return block
        raise PatchError(f"no block with id {block_id!r}")

    def index_of(self, block_id: str) -> int:
        for i, block in enumerate(self.blocks):
            if block.id == block_id:
                return i
        raise PatchError(f"no block with id {block_id!r}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "notes": self.notes,
            "blocks": [b.to_dict() for b in self.blocks],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=True)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Rig":
        rig = cls(name=str(data.get("name", "Untitled")), notes=str(data.get("notes", "")))
        for raw in data.get("blocks", []):
            block = Block.from_dict(raw)
            if block.kind not in CATALOG:
                raise PatchError(f"unknown block kind {block.kind!r} in saved rig")
            # Fill in any params added to the catalog after the rig was saved.
            defaults = block.type.defaults()
            defaults.update(block.params)
            block.params = defaults
            rig.blocks.append(block)
        return rig

    @classmethod
    def from_json(cls, text: str) -> "Rig":
        return cls.from_dict(json.loads(text))

    def describe(self) -> str:
        """Compact plain-text description used in prompts and the status bar."""
        if not self.blocks:
            return f"{self.name}: (empty chain)"
        parts = []
        for block in self.blocks:
            flag = " [bypassed]" if block.bypass else ""
            params = ", ".join(
                f"{spec.label}={spec.format(block.params[spec.name])}" for spec in block.type.params
            )
            parts.append(f"{block.id} {block.label}{flag} ({params})")
        return f"{self.name}: " + " -> ".join(parts)


# --------------------------------------------------------------------------
# Patches
# --------------------------------------------------------------------------

PatchOp = dict[str, Any]


def _fmt_change(spec: ParamSpec, before: float, after: float) -> str:
    return f"{spec.label} {spec.format(before)} -> {spec.format(after)}"


def apply_patch(rig: Rig, ops: Iterable[PatchOp]) -> list[str]:
    """Apply operations to *rig* in place and return one line per change.

    The rig is only mutated if every op validates, so a bad op in the middle
    of a batch leaves the rig untouched.
    """
    ops = list(ops)
    trial = copy.deepcopy(rig)
    lines: list[str] = []
    for op in ops:
        lines.extend(_apply_one(trial, op))
    # Commit.
    rig.name = trial.name
    rig.notes = trial.notes
    rig.blocks = trial.blocks
    rig._next_id = trial._next_id
    return lines


def _apply_one(rig: Rig, op: PatchOp) -> list[str]:
    if not isinstance(op, dict) or "op" not in op:
        raise PatchError(f"patch op must be an object with an 'op' key, got {op!r}")
    name = op["op"]

    if name == "set":
        block = rig.find(str(op.get("block")))
        pname = str(op.get("param"))
        spec = block.type.param(pname)
        if "value" in op:
            after = spec.clamp(float(op["value"]))
        elif "real" in op:
            after = spec.clamp(spec.norm(float(op["real"])))
        elif "delta" in op:
            after = spec.clamp(block.params[pname] + float(op["delta"]))
        else:
            raise PatchError("'set' needs one of value (0..1), real (units) or delta")
        before = block.params[pname]
        block.params[pname] = after
        if abs(after - before) < 1e-9:
            return []
        return [f"{block.id} {block.label}: {_fmt_change(spec, before, after)}"]

    if name == "bypass":
        block = rig.find(str(op.get("block")))
        value = op.get("value")
        after = (not block.bypass) if value is None else bool(value)
        if after == block.bypass:
            return []
        block.bypass = after
        return [f"{block.id} {block.label}: {'bypassed' if after else 'engaged'}"]

    if name == "add":
        kind = str(op.get("kind"))
        block = rig.new_block(kind, op.get("params"), bool(op.get("bypass", False)))
        at = op.get("at")
        index = len(rig.blocks) if at is None else max(0, min(int(at), len(rig.blocks)))
        rig.blocks.insert(index, block)
        return [f"added {block.id} {block.label} at position {index}"]

    if name == "remove":
        index = rig.index_of(str(op.get("block")))
        block = rig.blocks.pop(index)
        return [f"removed {block.id} {block.label}"]

    if name == "move":
        index = rig.index_of(str(op.get("block")))
        to = max(0, min(int(op.get("to", index)), len(rig.blocks) - 1))
        if to == index:
            return []
        block = rig.blocks.pop(index)
        rig.blocks.insert(to, block)
        return [f"moved {block.id} {block.label} to position {to}"]

    if name == "rename":
        before, rig.name = rig.name, str(op.get("name", rig.name)).strip() or rig.name
        return [] if before == rig.name else [f"renamed rig {before!r} -> {rig.name!r}"]

    if name == "notes":
        rig.notes = str(op.get("text", ""))
        return ["updated notes"]

    if name == "clear":
        count = len(rig.blocks)
        rig.blocks = []
        return [f"cleared {count} blocks"]

    raise PatchError(f"unknown patch op {name!r}")


# --------------------------------------------------------------------------
# Undo history
# --------------------------------------------------------------------------


@dataclass
class HistoryEntry:
    source: str  # "you" | "agent" | "load"
    lines: list[str]

    @property
    def summary(self) -> str:
        if not self.lines:
            return "(no change)"
        return self.lines[0] if len(self.lines) == 1 else f"{self.lines[0]} (+{len(self.lines) - 1} more)"


class RigState:
    """Owns the live rig plus undo/redo stacks and change notifications.

    Listeners are called with ``(kind, payload)`` where kind is ``"param"``
    (a single knob moved; payload is ``(block_id, param)``) or ``"structure"``
    (anything else). The audio engine uses this to decide between a cheap
    live parameter update and a full chain rebuild.
    """

    def __init__(self, rig: Rig | None = None, max_undo: int = 200) -> None:
        self.rig = rig or Rig()
        self._undo: list[tuple[Rig, HistoryEntry]] = []
        self._redo: list[tuple[Rig, HistoryEntry]] = []
        self.history: list[HistoryEntry] = []
        self._listeners: list[Callable[[str, Any], None]] = []
        self._max_undo = max_undo

    # -- listeners ---------------------------------------------------------
    def subscribe(self, fn: Callable[[str, Any], None]) -> None:
        self._listeners.append(fn)

    def _emit(self, kind: str, payload: Any = None) -> None:
        for fn in list(self._listeners):
            fn(kind, payload)

    # -- mutation ----------------------------------------------------------
    def patch(self, ops: Iterable[PatchOp], source: str = "you") -> list[str]:
        ops = list(ops)
        snapshot = copy.deepcopy(self.rig)
        lines = apply_patch(self.rig, ops)
        if not lines:
            return lines
        entry = HistoryEntry(source=source, lines=lines)
        self._undo.append((snapshot, entry))
        del self._undo[: -self._max_undo]
        self._redo.clear()
        self.history.append(entry)
        if len(ops) == 1 and ops[0].get("op") == "set":
            self._emit("param", (ops[0]["block"], ops[0]["param"]))
        else:
            self._emit("structure")
        return lines

    def nudge(self, block_id: str, param: str, delta: float) -> list[str]:
        """Keyboard knob turn. Consecutive nudges of the same knob coalesce."""
        block = self.rig.find(block_id)
        spec = block.type.param(param)
        before = block.params[param]
        after = spec.clamp(before + delta)
        if abs(after - before) < 1e-9:
            return []
        coalesce = (
            self._undo
            and self._undo[-1][1].source == "you"
            and self._undo[-1][1].lines
            and self._undo[-1][1].lines[0].startswith(f"{block.id} {block.label}: {spec.label} ")
            and not self._redo
        )
        if not coalesce:
            snapshot = copy.deepcopy(self.rig)
            entry = HistoryEntry(source="you", lines=[])
            self._undo.append((snapshot, entry))
            self.history.append(entry)
        entry = self._undo[-1][1]
        block.params[param] = after
        original = self._undo[-1][0].find(block_id).params[param]
        entry.lines = [f"{block.id} {block.label}: {_fmt_change(spec, original, after)}"]
        self._redo.clear()
        self._emit("param", (block_id, param))
        return entry.lines

    def replace(self, rig: Rig, source: str = "load") -> None:
        snapshot = copy.deepcopy(self.rig)
        entry = HistoryEntry(source=source, lines=[f"loaded rig {rig.name!r}"])
        self._undo.append((snapshot, entry))
        self._redo.clear()
        self.history.append(entry)
        self.rig = rig
        self._emit("structure")

    def undo(self) -> HistoryEntry | None:
        if not self._undo:
            return None
        snapshot, entry = self._undo.pop()
        self._redo.append((copy.deepcopy(self.rig), entry))
        self.rig = snapshot
        self.history.append(HistoryEntry(source="undo", lines=[f"undid: {entry.summary}"]))
        self._emit("structure")
        return entry

    def redo(self) -> HistoryEntry | None:
        if not self._redo:
            return None
        snapshot, entry = self._redo.pop()
        self._undo.append((copy.deepcopy(self.rig), entry))
        self.rig = snapshot
        self.history.append(HistoryEntry(source="redo", lines=[f"redid: {entry.summary}"]))
        self._emit("structure")
        return entry

    @property
    def can_undo(self) -> bool:
        return bool(self._undo)

    @property
    def can_redo(self) -> bool:
        return bool(self._redo)
