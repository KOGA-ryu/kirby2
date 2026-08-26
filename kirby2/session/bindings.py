"""Configurable keyboard bindings for the minimal execution session."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Mapping


class SessionCommand(str, Enum):
    BUY_BID = "buy_bid"
    BUY_ASK = "buy_ask"
    MARKET_BUY = "market_buy"
    SELL_ASK = "sell_ask"
    SELL_BID = "sell_bid"
    MARKET_SELL = "market_sell"
    CANCEL_NEAREST = "cancel_nearest"
    CANCEL_ALL = "cancel_all"
    REPLACE_NEAREST = "replace_nearest"
    FLATTEN = "flatten"
    INCREASE_QUANTITY = "increase_quantity"
    DECREASE_QUANTITY = "decrease_quantity"
    TOGGLE_RUN = "toggle_run"
    RESET = "reset"
    QUIT = "quit"


COMMAND_LABELS: Mapping[SessionCommand, str] = MappingProxyType(
    {
        SessionCommand.BUY_BID: "BUY BID",
        SessionCommand.BUY_ASK: "BUY ASK",
        SessionCommand.MARKET_BUY: "MKT BUY",
        SessionCommand.SELL_ASK: "SELL ASK",
        SessionCommand.SELL_BID: "SELL BID",
        SessionCommand.MARKET_SELL: "MKT SELL",
        SessionCommand.CANCEL_NEAREST: "CXL NEAR",
        SessionCommand.CANCEL_ALL: "CXL ALL",
        SessionCommand.REPLACE_NEAREST: "REPLACE",
        SessionCommand.FLATTEN: "FLATTEN",
        SessionCommand.INCREASE_QUANTITY: "QTY +",
        SessionCommand.DECREASE_QUANTITY: "QTY -",
        SessionCommand.TOGGLE_RUN: "START/PAUSE",
        SessionCommand.RESET: "RESET",
        SessionCommand.QUIT: "QUIT",
    }
)


DEFAULT_KEY_BINDINGS: Mapping[str, SessionCommand] = MappingProxyType(
    {
        "a": SessionCommand.BUY_BID,
        "s": SessionCommand.BUY_ASK,
        "d": SessionCommand.MARKET_BUY,
        "j": SessionCommand.SELL_ASK,
        "k": SessionCommand.SELL_BID,
        "l": SessionCommand.MARKET_SELL,
        "c": SessionCommand.CANCEL_NEAREST,
        "C": SessionCommand.CANCEL_ALL,
        "v": SessionCommand.REPLACE_NEAREST,
        "f": SessionCommand.FLATTEN,
        "]": SessionCommand.INCREASE_QUANTITY,
        "[": SessionCommand.DECREASE_QUANTITY,
        " ": SessionCommand.TOGGLE_RUN,
        "r": SessionCommand.RESET,
        "q": SessionCommand.QUIT,
    }
)


@dataclass(frozen=True, slots=True)
class Binding:
    key: str
    command: SessionCommand

    @property
    def display_key(self) -> str:
        return "SPACE" if self.key == " " else self.key

    @property
    def label(self) -> str:
        return COMMAND_LABELS[self.command]


class BindingMap:
    def __init__(self, bindings: Mapping[str, SessionCommand]) -> None:
        if not bindings:
            raise ValueError("at least one key binding is required")
        normalized: dict[str, SessionCommand] = {}
        for key, command in bindings.items():
            if not isinstance(key, str) or not key:
                raise ValueError("binding keys must be nonempty strings")
            if not isinstance(command, SessionCommand):
                raise TypeError("binding values must be SessionCommand members")
            if key in normalized:
                raise ValueError(f"duplicate key binding: {key!r}")
            normalized[key] = command
        missing = set(SessionCommand) - set(normalized.values())
        if missing:
            names = ", ".join(sorted(command.value for command in missing))
            raise ValueError(f"hotkey layout is missing required commands: {names}")
        self._bindings = MappingProxyType(normalized)

    @classmethod
    def default(cls) -> BindingMap:
        return cls(DEFAULT_KEY_BINDINGS)

    @property
    def bindings(self) -> tuple[Binding, ...]:
        return tuple(Binding(key, command) for key, command in self._bindings.items())

    def resolve(self, key: str) -> SessionCommand | None:
        return self._bindings.get(key)

    def as_dict(self) -> dict[str, str]:
        return {key: command.value for key, command in self._bindings.items()}

    def edited(
        self,
        assignments: Mapping[str, SessionCommand] | None = None,
        removals: tuple[str, ...] = (),
    ) -> BindingMap:
        values = dict(self._bindings)
        for key in removals:
            values.pop(key, None)
        values.update(assignments or {})
        return BindingMap(values)
