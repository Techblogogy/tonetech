"""Textual UI: the board, the knobs, and the tech's chat pane."""

from __future__ import annotations

import os
from typing import Any

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, HorizontalScroll, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Footer, Input, Label, OptionList, RichLog, Static
from textual.widgets.option_list import Option

from tonetech import __version__
from tonetech.agent import AgentTurn, ToneTech, ToolEvent
from tonetech.catalog import CATALOG, CATEGORY_ORDER, PatchError
from tonetech.engine import BaseEngine
from tonetech.library import PRESETS, list_rigs, load_rig, save_rig
from tonetech.rig import Block, RigState

KNOB_WIDTH = 14


def bar(value: float, width: int = KNOB_WIDTH) -> str:
    filled = int(round(max(0.0, min(1.0, value)) * width))
    return "█" * filled + "▒" * (width - filled)


def meter(db: float, width: int = 8, floor: float = -48.0) -> Text:
    frac = max(0.0, min(1.0, (db - floor) / -floor))
    filled = int(round(frac * width))
    text = Text()
    for i in range(width):
        on_ = i < filled
        pos = i / width
        colour = "red" if pos > 0.85 else "yellow" if pos > 0.65 else "green"
        text.append("▮" if on_ else "▯", style=colour if on_ else "dim")
    return text


# --------------------------------------------------------------------------
# Widgets
# --------------------------------------------------------------------------


class RigHeader(Static):
    """Top bar: rig name, engine status, in/out meters."""

    DEFAULT_CSS = """
    RigHeader { height: 1; background: $primary-background; color: $text; padding: 0 1; }
    """

    def __init__(self, app_: "ToneTechApp") -> None:
        super().__init__()
        self._app = app_

    def on_mount(self) -> None:
        self.set_interval(0.1, self.refresh_bar)

    def refresh_bar(self) -> None:
        state, engine = self._app.state, self._app.engine
        text = Text()
        text.append(" RIG ", style="bold reverse")
        text.append(f" {state.rig.name} ", style="bold")
        text.append(f"  {engine.describe()}", style="dim")
        if engine.error:
            text.append(f"  ! {engine.error}", style="bold red")
        width = self.size.width or 80
        right = Text()
        right.append("in ", style="dim")
        right.append(meter(engine.levels.input_db))
        right.append("   ")
        right.append("out ", style="dim")
        right.append(meter(engine.levels.output_db))
        right.append(" ")
        pad = max(1, width - text.cell_len - right.cell_len - 2)
        text.append(" " * pad)
        text.append(right)
        self.update(text)


class BlockCard(Static):
    """One block on the board."""

    DEFAULT_CSS = """
    BlockCard {
        width: auto; height: 5; min-width: 12; padding: 0 1; margin: 0 0;
        border: round $secondary; content-align: center middle;
    }
    BlockCard.-selected { border: heavy $accent; background: $boost; }
    BlockCard.-bypassed { color: $text-muted; border: round $panel; }
    """

    def __init__(self, block: Block, selected: bool) -> None:
        super().__init__()
        self.block = block
        self.set_class(selected, "-selected")
        self.set_class(block.bypass, "-bypassed")

    def render(self) -> Text:
        b = self.block
        title = Text(b.label, style="bold" if not b.bypass else "dim strike")
        sub = Text(b.id + ("  off" if b.bypass else ""), style="dim")
        first = b.type.params[0]
        knob = Text(f"{first.label[:5]:<5} {bar(b.params[first.name], 6)}", style="dim" if b.bypass else "")
        width = max(title.cell_len, sub.cell_len, knob.cell_len)
        out = Text()
        for i, line in enumerate((title, sub, knob)):
            line.pad_right(width - line.cell_len)
            out.append(line)
            if i < 2:
                out.append("\n")
        return out


class Arrow(Static):
    DEFAULT_CSS = "Arrow { width: 2; height: 5; content-align: center middle; color: $text-muted; }"

    def render(self) -> str:
        return "►"


class Endpoint(Static):
    DEFAULT_CSS = """
    Endpoint { width: 5; height: 5; content-align: center middle; color: $text-muted; border: round $panel; }
    """


class ChainView(HorizontalScroll):
    """The horizontal row of blocks."""

    DEFAULT_CSS = """
    ChainView { height: 7; padding: 0 1; border-bottom: solid $panel; scrollbar-size-horizontal: 1; }
    """

    def rebuild(self, state: RigState, selected: int) -> None:
        self.remove_children()
        widgets: list[Any] = [Endpoint("GTR")]
        if not state.rig.blocks:
            widgets.append(Arrow())
            widgets.append(Static("[dim]empty chain: press [b]a[/b] to add a block or ask the tech[/dim]"))
        for i, block in enumerate(state.rig.blocks):
            widgets.append(Arrow())
            widgets.append(BlockCard(block, i == selected))
        widgets.append(Arrow())
        widgets.append(Endpoint("OUT"))
        self.mount_all(widgets)
        self.call_after_refresh(self._scroll_to_selected)

    def _scroll_to_selected(self) -> None:
        for card in self.query(BlockCard):
            if card.has_class("-selected"):
                self.scroll_to_widget(card, animate=False)
                break


class KnobPanel(Static):
    """Knobs of the selected block plus the change history."""

    DEFAULT_CSS = """
    KnobPanel { padding: 0 1; }
    """

    def __init__(self) -> None:
        super().__init__()
        self.block: Block | None = None
        self.knob = 0
        self.history_lines: list[Text] = []

    def show(self, block: Block | None, knob: int, history: list[tuple[str, str]]) -> None:
        self.block = block
        self.knob = knob
        self.history_lines = []
        for source, line in history:
            style = {"agent": "cyan", "you": "green", "undo": "yellow", "redo": "yellow", "load": "magenta"}.get(
                source, ""
            )
            t = Text()
            t.append(f"{source:<6}", style=style)
            t.append(line, style="dim")
            self.history_lines.append(t)
        self.refresh(layout=True)

    def render(self) -> Text:
        out = Text()
        b = self.block
        if b is None:
            out.append("No block selected.\n\n", style="dim")
            out.append("a", style="bold")
            out.append(" add a block, or ask the tech to build a rig.\n", style="dim")
        else:
            out.append(f"{b.label}", style="bold")
            out.append(f"  {b.id}", style="dim")
            out.append("   bypassed\n" if b.bypass else "   engaged\n", style="yellow" if b.bypass else "green")
            out.append(Text(b.type.description, style="dim italic"))
            out.append("\n\n")
            for i, spec in enumerate(b.type.params):
                v = b.params[spec.name]
                sel = i == self.knob
                out.append("▸ " if sel else "  ", style="bold")
                out.append(f"{spec.label:<9}", style="bold" if sel else "")
                out.append(bar(v), style="cyan" if sel else "")
                out.append(f"  {spec.format(v):>8}\n", style="bold" if sel else "dim")
        out.append("\n")
        out.append("history\n", style="bold underline")
        if not self.history_lines:
            out.append("  nothing yet\n", style="dim")
        for line in self.history_lines[-8:]:
            out.append("  ")
            out.append(line)
            out.append("\n")
        return out


class TechLog(RichLog):
    DEFAULT_CSS = "TechLog { height: 1fr; padding: 0 1; border: none; overflow-x: hidden; scrollbar-size-vertical: 1; }"


class TechInput(Input):
    DEFAULT_CSS = "TechInput { dock: bottom; border: tall $accent; }"


class TechPanel(Vertical):
    """Chat with the tech."""

    DEFAULT_CSS = """
    TechPanel { width: 3fr; border-left: solid $panel; }
    TechPanel > Label { padding: 0 1; background: $boost; text-style: bold; height: 1; }
    """

    def compose(self) -> ComposeResult:
        yield Label("TECH")
        yield TechLog(wrap=True, markup=False, highlight=False)
        yield TechInput(placeholder="ask the tech… (e.g. more chime, less mud)")

    @property
    def log_widget(self) -> TechLog:
        return self.query_one(TechLog)

    def say(self, who: str, text: str, style: str = "") -> None:
        t = Text()
        colour = {"you": "green", "tech": "cyan", "sys": "yellow", "tool": "magenta"}.get(who, "")
        t.append(f"{who:<5}", style=f"bold {colour}")
        t.append(text, style=style)
        self.log_widget.write(t)


# --------------------------------------------------------------------------
# Modals
# --------------------------------------------------------------------------


class PickerScreen(ModalScreen[str | None]):
    """Generic option picker used for add-block and load-rig."""

    DEFAULT_CSS = """
    PickerScreen { align: center middle; }
    PickerScreen > Vertical { width: 64; height: auto; max-height: 80%; border: thick $accent; background: $surface; }
    PickerScreen Label { padding: 0 1; text-style: bold; }
    PickerScreen OptionList { height: auto; max-height: 20; }
    """

    BINDINGS = [Binding("escape", "dismiss(None)", "cancel")]

    def __init__(self, title: str, options: list[tuple[str, str]]) -> None:
        super().__init__()
        self.title_text = title
        self.options = options

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(self.title_text)
            yield OptionList(*[Option(label, id=key) for key, label in self.options])

    @on(OptionList.OptionSelected)
    def _picked(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option.id)


class NameScreen(ModalScreen[str | None]):
    DEFAULT_CSS = """
    NameScreen { align: center middle; }
    NameScreen > Vertical { width: 50; height: auto; border: thick $accent; background: $surface; padding: 1; }
    """
    BINDINGS = [Binding("escape", "dismiss(None)", "cancel")]

    def __init__(self, prompt: str, value: str = "") -> None:
        super().__init__()
        self.prompt = prompt
        self.value = value

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(self.prompt)
            yield Input(value=self.value)

    @on(Input.Submitted)
    def _done(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip() or None)


# --------------------------------------------------------------------------
# App
# --------------------------------------------------------------------------


class ToneTechApp(App[None]):
    TITLE = "ToneTech"
    CSS = """
    Screen { layout: vertical; }
    #body { height: 1fr; }
    #left { width: 2fr; }
    """

    BINDINGS = [
        Binding("j,right", "select(1)", "next block", show=False),
        Binding("k,left", "select(-1)", "prev block", show=False),
        Binding("down", "knob(1)", "next knob", show=False),
        Binding("up", "knob(-1)", "prev knob", show=False),
        Binding("l,equals_sign,plus", "turn(0.02)", "knob +", show=False),
        Binding("h,minus", "turn(-0.02)", "knob -", show=False),
        Binding("L", "turn(0.1)", "knob ++", show=False),
        Binding("H", "turn(-0.1)", "knob --", show=False),
        Binding("space", "bypass", "bypass"),
        Binding("a", "add_block", "add"),
        Binding("d", "delete_block", "delete"),
        Binding("bracketleft", "move(-1)", "move ◄", show=False),
        Binding("bracketright", "move(1)", "move ►", show=False),
        Binding("u", "undo", "undo"),
        Binding("r", "redo", "redo"),
        Binding("s", "save", "save"),
        Binding("o", "load", "load"),
        Binding("colon,i", "focus_tech", "ask tech"),
        Binding("escape", "focus_board", "board", show=False),
        Binding("question_mark", "help", "help"),
        Binding("q", "quit", "quit"),
    ]

    class AgentDone(Message):
        def __init__(self, turn: AgentTurn) -> None:
            super().__init__()
            self.turn = turn

    class AgentEvent(Message):
        def __init__(self, event: ToolEvent) -> None:
            super().__init__()
            self.event = event

    def __init__(self, state: RigState, engine: BaseEngine, tech: ToneTech | None = None) -> None:
        super().__init__()
        self.state = state
        self.engine = engine
        self.tech = tech or ToneTech(state, engine, on_event=self._agent_event)
        self.selected = 0
        self.knob = 0
        self.busy = False
        state.subscribe(self._on_state)

    # -- layout -----------------------------------------------------------
    def compose(self) -> ComposeResult:
        yield RigHeader(self)
        yield ChainView()
        with Horizontal(id="body"):
            with VerticalScroll(id="left"):
                yield KnobPanel()
            yield TechPanel()
        yield Footer()

    def on_mount(self) -> None:
        self.engine.start()
        self.refresh_board()
        tech = self.query_one(TechPanel)
        tech.say("sys", f"ToneTech v{__version__}. Engine: {self.engine.describe()}.")
        if self.engine.error:
            tech.say("sys", self.engine.error, style="red")
        tech.say("sys", "Press : to talk to the tech, ? for keys. Try: \"give me an edge-of-breakup blues tone\".")
        self.query_one(TechInput).focus()

    def on_unmount(self) -> None:
        self.engine.stop()

    # -- state sync -------------------------------------------------------
    def _on_state(self, kind: str, payload: Any) -> None:
        # Called from whichever thread patched the rig; marshal to the UI thread.
        try:
            self.call_from_thread(self.refresh_board)
        except RuntimeError:
            self.refresh_board()

    def refresh_board(self) -> None:
        blocks = self.state.rig.blocks
        if blocks:
            self.selected = max(0, min(self.selected, len(blocks) - 1))
        else:
            self.selected = 0
        self.query_one(ChainView).rebuild(self.state, self.selected)
        self.refresh_knobs()

    def refresh_knobs(self) -> None:
        block = self.current_block
        if block is not None:
            self.knob = max(0, min(self.knob, len(block.type.params) - 1))
        history = []
        for entry in self.state.history[-8:]:
            for line in entry.lines[:1]:
                history.append((entry.source, line))
        self.query_one(KnobPanel).show(block, self.knob, history)

    @property
    def current_block(self) -> Block | None:
        blocks = self.state.rig.blocks
        return blocks[self.selected] if blocks else None

    # -- board actions ----------------------------------------------------
    def action_select(self, delta: int) -> None:
        if not self.state.rig.blocks:
            return
        self.selected = (self.selected + delta) % len(self.state.rig.blocks)
        self.refresh_board()

    def action_knob(self, delta: int) -> None:
        block = self.current_block
        if block is None:
            return
        self.knob = (self.knob + delta) % len(block.type.params)
        self.refresh_knobs()

    def action_turn(self, delta: float) -> None:
        block = self.current_block
        if block is None:
            return
        spec = block.type.params[self.knob]
        self.state.nudge(block.id, spec.name, delta)

    def action_bypass(self) -> None:
        block = self.current_block
        if block is not None:
            self.state.patch([{"op": "bypass", "block": block.id}])

    def action_delete_block(self) -> None:
        block = self.current_block
        if block is not None:
            self.state.patch([{"op": "remove", "block": block.id}])

    def action_move(self, delta: int) -> None:
        block = self.current_block
        if block is None:
            return
        target = self.selected + delta
        if 0 <= target < len(self.state.rig.blocks):
            self.state.patch([{"op": "move", "block": block.id, "to": target}])
            self.selected = target
            self.refresh_board()

    def action_undo(self) -> None:
        entry = self.state.undo()
        self.query_one(TechPanel).say("sys", f"undo: {entry.summary}" if entry else "nothing to undo")

    def action_redo(self) -> None:
        entry = self.state.redo()
        self.query_one(TechPanel).say("sys", f"redo: {entry.summary}" if entry else "nothing to redo")

    def action_add_block(self) -> None:
        options = []
        for category in CATEGORY_ORDER:
            for kind, bt in CATALOG.items():
                if bt.category == category:
                    options.append((kind, f"{bt.label:<16} {bt.description[:44]}"))

        def picked(kind: str | None) -> None:
            if kind:
                at = self.selected + 1 if self.state.rig.blocks else 0
                self.state.patch([{"op": "add", "kind": kind, "at": at}])
                self.selected = at
                self.refresh_board()

        self.push_screen(PickerScreen("Add block after selection", options), picked)

    def action_save(self) -> None:
        def named(name: str | None) -> None:
            if name:
                path = save_rig(self.state.rig, name)
                self.query_one(TechPanel).say("sys", f"saved {path}")
                self.refresh_board()

        self.push_screen(NameScreen("Save rig as", self.state.rig.name), named)

    def action_load(self) -> None:
        options = [(n, f"{n:<24} saved") for n in list_rigs()] + [(n, f"{n:<24} preset") for n in PRESETS]

        def picked(name: str | None) -> None:
            if name:
                try:
                    self.state.replace(load_rig(name))
                except (FileNotFoundError, PatchError) as exc:
                    self.query_one(TechPanel).say("sys", str(exc), style="red")

        self.push_screen(PickerScreen("Load rig", options), picked)

    def action_focus_tech(self) -> None:
        self.query_one(TechInput).focus()

    def action_focus_board(self) -> None:
        self.set_focus(None)

    def action_help(self) -> None:
        tech = self.query_one(TechPanel)
        tech.say("sys", "keys: j/k or ◄/► select block · ▲/▼ select knob · h/l turn (H/L coarse) · space bypass")
        tech.say("sys", "      a add · d delete · [ ] move · u undo · r redo · s save · o load · : ask tech · esc board · q quit")

    # -- tech -------------------------------------------------------------
    @on(Input.Submitted, "TechInput")
    def _ask(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        event.input.value = ""
        if not text:
            return
        tech = self.query_one(TechPanel)
        if self.busy:
            tech.say("sys", "the tech is still working on the last one")
            return
        tech.say("you", text)
        self.busy = True
        event.input.placeholder = "thinking…"
        self.run_agent(text)

    @work(thread=True, exclusive=True)
    def run_agent(self, text: str) -> None:
        turn = self.tech.ask(text)
        self.post_message(self.AgentDone(turn))

    def _agent_event(self, event: ToolEvent) -> None:
        self.post_message(self.AgentEvent(event))

    @on(AgentEvent)
    def _show_event(self, message: AgentEvent) -> None:
        ev = message.event
        tech = self.query_one(TechPanel)
        if ev.name == "apply_patch":
            if ev.is_error:
                tech.say("tool", f"patch rejected: {ev.result[:120]}", style="red")
            else:
                for line in self.state.history[-1].lines if self.state.history else []:
                    if not line.startswith("why:"):
                        tech.say("tool", line, style="dim")
        elif ev.name == "analyze_input":
            tech.say("tool", "listened to the last few seconds", style="dim")
        elif ev.name in ("load_rig", "save_rig"):
            tech.say("tool", f"{ev.name.replace('_', ' ')}: {ev.input.get('name', '')}", style="dim")

    @on(AgentDone)
    def _agent_done(self, message: AgentDone) -> None:
        self.busy = False
        tech = self.query_one(TechPanel)
        self.query_one(TechInput).placeholder = "ask the tech… (e.g. more chime, less mud)"
        if message.turn.error:
            tech.say("sys", message.turn.error, style="red")
        if message.turn.text:
            tech.say("tech", message.turn.text)
        if message.turn.changed:
            self.refresh_board()


def run(state: RigState, engine: BaseEngine) -> None:
    ToneTechApp(state, engine).run()
