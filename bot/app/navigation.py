"""Short-lived UI controls. Task data and authorization stay in the API."""

from collections import OrderedDict
from dataclasses import dataclass, field
from secrets import token_urlsafe
from time import monotonic
from typing import Literal


@dataclass(frozen=True)
class Page:
    cursor: str | None = None
    previous: "Page | None" = None
    number: int = 1


@dataclass(frozen=True)
class Action:
    kind: Literal["list", "open", "status", "delete", "confirm", "dashboard", "full"]
    page: Page = field(default_factory=Page)
    task_id: str | None = None
    version: int | None = None
    status: str | None = None
    text_page: int = 0


@dataclass
class Control:
    actor: int
    chat: int
    message: int | None
    action: Action
    expires_at: float


class Navigation:
    """Bounded, disposable navigation state; restarting just expires old keyboards."""

    def __init__(self, ttl: float = 900, max_controls: int = 4096, per_actor: int = 256):
        self.ttl = ttl
        self.max_controls = max_controls
        self.per_actor = per_actor
        self.controls: OrderedDict[str, Control] = OrderedDict()

    def _prune(self) -> None:
        now = monotonic()
        for key in list(self.controls):
            if self.controls[key].expires_at <= now:
                del self.controls[key]

    def add(self, actor: int, chat: int, action: Action) -> str:
        self._prune()
        owned = [key for key, value in self.controls.items() if value.actor == actor]
        while len(owned) >= self.per_actor:
            del self.controls[owned.pop(0)]
        while len(self.controls) >= self.max_controls:
            self.controls.popitem(last=False)
        key = "n:" + token_urlsafe(12)
        self.controls[key] = Control(actor, chat, None, action, monotonic() + self.ttl)
        return key

    def bind(self, keys: list[str], actor: int, chat: int, message: int) -> None:
        # A successful Cancel/navigation replaces the confirmation view. A delayed
        # Confirm from that old view must no longer authorize a destructive action.
        for key, control in list(self.controls.items()):
            if (
                key not in keys
                and (control.actor, control.chat, control.message) == (actor, chat, message)
                and control.action.kind == "confirm"
            ):
                del self.controls[key]
        for key in keys:
            control = self.controls.get(key)
            if control and control.actor == actor and control.chat == chat:
                control.message = message

    def resolve(self, key: str | None, actor: int, chat: int, message: int) -> Action | None:
        self._prune()
        control = self.controls.get(key or "")
        if control and (control.actor, control.chat, control.message) == (actor, chat, message):
            return control.action
        return None
