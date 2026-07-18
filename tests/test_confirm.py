from __future__ import annotations

import asyncio

from textual.app import App, ComposeResult
from textual.widgets import Static

from runstate_tui.confirm import ConfirmStopScreen


class _Harness(App[None]):
    def __init__(self) -> None:
        super().__init__()
        self.result: bool | None = None

    def compose(self) -> ComposeResult:
        yield Static("base")

    def on_mount(self) -> None:
        self.push_screen(ConfirmStopScreen("Stop run x? y/n"), self._record)

    def _record(self, confirmed: bool | None) -> None:
        self.result = confirmed


def _drive(key: str) -> bool | None:
    async def go() -> bool | None:
        app = _Harness()
        async with app.run_test() as pilot:
            await pilot.pause()
            assert isinstance(app.screen, ConfirmStopScreen)
            await pilot.press(key)
            await pilot.pause()
            return app.result

    return asyncio.run(go())


def test_confirm_yes_returns_true():
    assert _drive("y") is True


def test_confirm_n_returns_false():
    assert _drive("n") is False


def test_confirm_escape_returns_false():
    assert _drive("escape") is False
