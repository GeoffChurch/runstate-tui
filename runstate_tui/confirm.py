from __future__ import annotations

from textual.app import ComposeResult
from textual.screen import ModalScreen
from textual.widgets import Label


class ConfirmStopScreen(ModalScreen[bool]):
    """The confirm-before-stop gate (spec §6.2): the stop is effectful and
    irreversible, so it passes this modal first. Dismisses True on `y`,
    False on `n`/`escape`."""

    BINDINGS = [
        ("y", "confirm", "Yes"),
        ("n", "cancel", "No"),
        ("escape", "cancel", "No"),
    ]

    def __init__(self, prompt: str) -> None:
        super().__init__()
        self._prompt = prompt

    def compose(self) -> ComposeResult:
        yield Label(self._prompt, id="confirm-prompt")

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)
