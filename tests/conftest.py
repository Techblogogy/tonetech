"""Shared fixtures: an offline rig state and a scripted fake Anthropic client."""

from __future__ import annotations

import json
import os
import tempfile
from types import SimpleNamespace
from typing import Any

import pytest

os.environ.setdefault("TONETECH_HOME", tempfile.mkdtemp(prefix="tonetech-test-"))

from tonetech.engine import NullEngine  # noqa: E402
from tonetech.library import preset  # noqa: E402
from tonetech.rig import RigState  # noqa: E402


@pytest.fixture
def state() -> RigState:
    return RigState(preset("Bedroom Blues"))


@pytest.fixture
def engine(state: RigState) -> NullEngine:
    return NullEngine(state)


def text_block(text: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=text)


def tool_block(name: str, input: dict[str, Any], id: str = "toolu_1") -> SimpleNamespace:
    return SimpleNamespace(type="tool_use", name=name, input=input, id=id)


def response(content: list[SimpleNamespace], stop_reason: str = "end_turn") -> SimpleNamespace:
    return SimpleNamespace(content=content, stop_reason=stop_reason, stop_details=None)


class FakeMessages:
    def __init__(self, script: list[SimpleNamespace]) -> None:
        self.script = list(script)
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(kwargs)
        if not self.script:
            raise AssertionError("fake client ran out of scripted responses")
        return self.script.pop(0)


class FakeClient:
    """Quacks like ``anthropic.Anthropic`` for ``client.beta.messages.create``."""

    def __init__(self, script: list[SimpleNamespace]) -> None:
        self.messages = FakeMessages(script)
        self.beta = SimpleNamespace(messages=self.messages)

    @property
    def calls(self) -> list[dict[str, Any]]:
        return self.messages.calls

    def last_tool_results(self) -> list[dict[str, Any]]:
        msgs = self.calls[-1]["messages"]
        for msg in reversed(msgs):
            if msg["role"] == "user" and isinstance(msg["content"], list):
                return [json.loads(r["content"]) for r in msg["content"]]
        return []
