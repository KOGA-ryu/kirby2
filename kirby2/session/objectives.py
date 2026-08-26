"""Training-session objective contracts."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ObjectiveType(str, Enum):
    ACQUIRE = "ACQUIRE"
    LIQUIDATE = "LIQUIDATE"
    ROUND_TRIP = "ROUND_TRIP"
    OBSERVE_ONLY = "OBSERVE_ONLY"

    @classmethod
    def parse(cls, value: str) -> ObjectiveType:
        return cls(value.upper().replace("-", "_"))


@dataclass(frozen=True, slots=True)
class SessionObjective:
    objective_type: ObjectiveType
    target_quantity: int
    time_limit_us: int
    preferred_slippage_ticks: int

    def __post_init__(self) -> None:
        if not isinstance(self.objective_type, ObjectiveType):
            raise TypeError("objective type must be an ObjectiveType")
        if type(self.target_quantity) is not int or self.target_quantity < 0:
            raise ValueError("objective target quantity must be a nonnegative integer")
        if type(self.time_limit_us) is not int or self.time_limit_us <= 0:
            raise ValueError("objective time limit must be positive integer microseconds")
        if (
            type(self.preferred_slippage_ticks) is not int
            or self.preferred_slippage_ticks < 0
        ):
            raise ValueError("preferred slippage must be a nonnegative integer")
        if self.objective_type is ObjectiveType.OBSERVE_ONLY:
            if self.target_quantity != 0:
                raise ValueError("OBSERVE_ONLY must have target quantity 0")
        elif self.target_quantity <= 0:
            raise ValueError("trading objectives require a positive target quantity")

    @classmethod
    def observe_only(cls, time_limit_seconds: int) -> SessionObjective:
        return cls(
            ObjectiveType.OBSERVE_ONLY,
            target_quantity=0,
            time_limit_us=time_limit_seconds * 1_000_000,
            preferred_slippage_ticks=0,
        )

    @property
    def time_limit_seconds(self) -> int | None:
        seconds, remainder = divmod(self.time_limit_us, 1_000_000)
        return seconds if remainder == 0 else None

    def as_dict(self) -> dict[str, object]:
        return {
            "objective_type": self.objective_type.value,
            "preferred_slippage_ticks": self.preferred_slippage_ticks,
            "target_quantity": self.target_quantity,
            "time_limit_us": self.time_limit_us,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> SessionObjective:
        return cls(
            objective_type=ObjectiveType.parse(str(payload["objective_type"])),
            target_quantity=int(payload["target_quantity"]),
            time_limit_us=int(payload["time_limit_us"]),
            preferred_slippage_ticks=int(payload["preferred_slippage_ticks"]),
        )

    def describe(self) -> str:
        if self.objective_type is ObjectiveType.OBSERVE_ONLY:
            action = "Observe without trading"
        elif self.objective_type is ObjectiveType.ACQUIRE:
            action = f"Acquire {self.target_quantity} shares"
        elif self.objective_type is ObjectiveType.LIQUIDATE:
            action = f"Liquidate {self.target_quantity} shares"
        else:
            action = f"Round-trip {self.target_quantity} shares"
        seconds, microseconds = divmod(self.time_limit_us, 1_000_000)
        duration = str(seconds)
        if microseconds:
            duration += f".{microseconds:06d}".rstrip("0")
        return (
            f"{action} within {duration}s; "
            f"preferred slippage <= {self.preferred_slippage_ticks} ticks"
        )
