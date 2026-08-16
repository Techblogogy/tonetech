"""The tone tech: a Claude agent that edits the rig through structured tools.

The agent never touches audio. It reads the rig, proposes a patch, and the
patch is validated and applied by :mod:`tonetech.rig`. Every change it makes
is recorded in the undo history with ``source="agent"`` so the player can
roll it back with one key.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from typing import Any, Callable

import anthropic

from tonetech.analysis import analyze
from tonetech.catalog import CATALOG, PatchError, catalog_summary
from tonetech.engine import BaseEngine
from tonetech.library import PRESETS, list_rigs, load_rig, save_rig
from tonetech.rig import HistoryEntry, RigState

MODEL = "claude-opus-5"

SYSTEM_PROMPT = """You are ToneTech, a guitar tech living inside a terminal pedalboard app.

The player talks to you in guitarist language ("more chime", "less fizz", "Gilmour solo tone",
"why is this muddy"). You translate that into concrete changes to their signal chain using the
tools provided. You can also just answer questions about tone, signal order and gear.

How to work:
- Always call get_rig first if you don't already know the current chain in this conversation.
- Make changes with apply_patch. Prefer a few decisive moves over many tiny ones. Use the
  "real" field when you think in units (Hz, dB, ms) and "value" (0-1) when you think in knob
  position. Batch related edits into one apply_patch call so they undo together.
- When the player describes a feel rather than a knob, use your ears: call analyze_input to hear
  what is actually coming out before guessing, especially for words like muddy, fizzy, boxy,
  thin, harsh. Ground your explanation in the measurements.
- Signal-chain conventions unless the player says otherwise: gate/comp first, drives next, then
  amp, then cab, then EQ, modulation, delay, reverb last. A cab block should be last in the
  amp section; without a cab, amps sound fizzy.
- Keep replies short and spoken, like a tech leaning over the amp: what you changed, why, and one
  thing to try playing. No bullet lists, no headers. Mention that 'u' undoes if you made a big move.
- Never invent block kinds or parameters. If asked for something the catalog can't do (tremolo,
  pitch shift, a specific IR), say so and offer the nearest thing.
- If the chain is empty, build a complete one (amp + cab at minimum) before tweaking.

Available block kinds:
""" + catalog_summary()

TOOLS: list[dict[str, Any]] = [
    {
        "name": "get_rig",
        "description": "Return the current rig: name, ordered blocks with ids, kinds, bypass state and every "
        "parameter both as a 0-1 knob position and in real units.",
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
        "strict": True,
    },
    {
        "name": "apply_patch",
        "description": "Apply a list of edits to the rig atomically. Ops: "
        "{op:'set', block, param, value(0-1)|real(units)|delta(+/-0-1)} ; "
        "{op:'bypass', block, value?:bool} ; "
        "{op:'add', kind, at?:int, params?:{name: 0-1 value}} ; "
        "{op:'remove', block} ; {op:'move', block, to:int} ; {op:'rename', name} ; "
        "{op:'notes', text} ; {op:'clear'}. Returns the human-readable change lines and the new rig.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ops": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "Ordered list of patch operations.",
                },
                "why": {
                    "type": "string",
                    "description": "One short sentence for the history log explaining the intent.",
                },
            },
            "required": ["ops", "why"],
            "additionalProperties": False,
        },
    },
    {
        "name": "analyze_input",
        "description": "Listen to the last few seconds of processed output and return level, dynamics, "
        "spectral centroid, per-band balance and descriptive words (muddy, fizzy, boxy...).",
        "input_schema": {
            "type": "object",
            "properties": {
                "seconds": {"type": "number", "description": "How many seconds to analyse (1-8).", "default": 3}
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "list_rigs",
        "description": "List the player's saved rigs and the factory presets.",
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
        "strict": True,
    },
    {
        "name": "save_rig",
        "description": "Save the current rig to the library under a name.",
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "name": "load_rig",
        "description": "Replace the current rig with a saved rig or factory preset by name.",
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
            "additionalProperties": False,
        },
        "strict": True,
    },
]


@dataclass
class ToolEvent:
    name: str
    input: dict[str, Any]
    result: str
    is_error: bool = False


@dataclass
class AgentTurn:
    text: str
    events: list[ToolEvent] = field(default_factory=list)
    changed: bool = False
    error: str | None = None


class ToneTech:
    """Multi-turn conversation with Claude, holding the tool implementations."""

    def __init__(
        self,
        state: RigState,
        engine: BaseEngine,
        client: anthropic.Anthropic | None = None,
        model: str = MODEL,
        on_event: Callable[[ToolEvent], None] | None = None,
    ) -> None:
        self.state = state
        self.engine = engine
        self.model = model
        self.on_event = on_event
        self._client = client
        self.messages: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    @property
    def client(self) -> anthropic.Anthropic:
        if self._client is None:
            self._client = anthropic.Anthropic()
        return self._client

    # -- tool implementations ---------------------------------------------
    def _rig_payload(self) -> dict[str, Any]:
        rig = self.state.rig
        blocks = []
        for block in rig.blocks:
            blocks.append(
                {
                    "id": block.id,
                    "kind": block.kind,
                    "label": block.label,
                    "bypass": block.bypass,
                    "params": {
                        p.name: {"value": round(block.params[p.name], 3), "real": p.format(block.params[p.name])}
                        for p in block.type.params
                    },
                }
            )
        return {"name": rig.name, "notes": rig.notes, "blocks": blocks, "engine": self.engine.describe()}

    def _run_tool(self, name: str, args: dict[str, Any]) -> tuple[str, bool]:
        try:
            if name == "get_rig":
                return json.dumps(self._rig_payload()), False
            if name == "apply_patch":
                ops = args.get("ops")
                if not isinstance(ops, list) or not ops:
                    raise PatchError("ops must be a non-empty list")
                lines = list(self.state.patch(ops, source="agent"))
                why = str(args.get("why", "")).strip()
                if why and lines:
                    self.state.history[-1].lines.append(f"why: {why}")
                return json.dumps({"changes": lines or ["(no effective change)"], "rig": self._rig_payload()}), False
            if name == "analyze_input":
                seconds = float(args.get("seconds", 3) or 3)
                seconds = max(1.0, min(8.0, seconds))
                audio = self.engine.capture(seconds)
                result = analyze(audio, self.engine.sample_rate)
                return json.dumps({"summary": result.summary(), **result.to_dict()}), False
            if name == "list_rigs":
                return json.dumps({"saved": list_rigs(), "presets": list(PRESETS)}), False
            if name == "save_rig":
                path = save_rig(self.state.rig, str(args["name"]))
                self.state.history.append(HistoryEntry("agent", [f"saved {path.name}"]))
                return json.dumps({"saved": str(path)}), False
            if name == "load_rig":
                rig = load_rig(str(args["name"]))
                self.state.replace(rig, source="agent")
                return json.dumps({"loaded": rig.name, "rig": self._rig_payload()}), False
            return json.dumps({"error": f"unknown tool {name}"}), True
        except (PatchError, FileNotFoundError, KeyError, ValueError, TypeError) as exc:
            return json.dumps({"error": f"{type(exc).__name__}: {exc}"}), True

    # -- conversation ------------------------------------------------------
    def ask(self, text: str, max_rounds: int = 8) -> AgentTurn:
        """Send one player message and run the tool loop to completion. Blocking."""
        with self._lock:
            return self._ask(text, max_rounds)

    def _ask(self, text: str, max_rounds: int) -> AgentTurn:
        turn = AgentTurn(text="")
        self.messages.append({"role": "user", "content": text})
        try:
            for _ in range(max_rounds):
                response = self.client.beta.messages.create(
                    model=self.model,
                    max_tokens=4096,
                    system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
                    tools=TOOLS,
                    messages=self.messages,
                    thinking={"type": "adaptive"},
                    output_config={"effort": "medium"},
                    betas=["server-side-fallback-2026-07-01"],
                    fallbacks="default",
                )
                self.messages.append({"role": "assistant", "content": response.content})
                if response.stop_reason == "refusal":
                    details = getattr(response, "stop_details", None)
                    why = getattr(details, "explanation", None) or "the request was declined"
                    turn.error = f"Declined: {why}"
                    break
                texts = [b.text for b in response.content if b.type == "text"]
                if texts:
                    turn.text = "\n".join(texts).strip()
                tool_uses = [b for b in response.content if b.type == "tool_use"]
                if response.stop_reason != "tool_use" or not tool_uses:
                    break
                results = []
                for block in tool_uses:
                    args = block.input if isinstance(block.input, dict) else json.loads(block.input or "{}")
                    result, is_error = self._run_tool(block.name, args)
                    event = ToolEvent(block.name, args, result, is_error)
                    turn.events.append(event)
                    if block.name in ("apply_patch", "load_rig") and not is_error:
                        turn.changed = True
                    if self.on_event:
                        self.on_event(event)
                    results.append(
                        {"type": "tool_result", "tool_use_id": block.id, "content": result, "is_error": is_error}
                    )
                self.messages.append({"role": "user", "content": results})
            else:
                turn.error = "Stopped after too many tool rounds."
        except anthropic.AuthenticationError:
            turn.error = "No API credentials. Set ANTHROPIC_API_KEY (or run `ant auth login`) and restart."
            self._pop_failed_turn()
        except anthropic.RateLimitError:
            turn.error = "Rate limited by the API. Give it a few seconds and ask again."
            self._pop_failed_turn()
        except anthropic.APIConnectionError:
            turn.error = "Can't reach the API. Check your connection."
            self._pop_failed_turn()
        except anthropic.APIStatusError as exc:
            turn.error = f"API error {exc.status_code}: {exc.message}"
            self._pop_failed_turn()
        if not turn.text and not turn.error:
            turn.text = "(done)"
        return turn

    def _pop_failed_turn(self) -> None:
        """Drop the user message (and any partial assistant turn) that errored."""
        while self.messages and self.messages[-1]["role"] == "assistant":
            self.messages.pop()
        if self.messages and self.messages[-1]["role"] == "user":
            content = self.messages[-1]["content"]
            if isinstance(content, str):
                self.messages.pop()

    def reset(self) -> None:
        self.messages.clear()


def kinds_help() -> str:
    """Short list of block kinds for the add-block palette."""
    return ", ".join(f"{k} ({bt.label})" for k, bt in CATALOG.items())
