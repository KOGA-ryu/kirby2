"""Capability-gated feature replay over ordered historical activity."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import TYPE_CHECKING

from kirby2.exchange import Order, OrderBook, Side
from kirby2.features.engine import MicrostructureFeatureEngine
from kirby2.features.models import (
    FEATURE_CATALOG,
    FeatureFrame,
    FeatureKey,
    feature_field_name,
)

from .models import HistoricalCommandRecord, HistoricalDataMode, HistoricalRun

if TYPE_CHECKING:
    from kirby2.strategy import StateMachineDefinition, StrategyDefinition


class FeatureAvailability(str, Enum):
    AVAILABLE = "AVAILABLE"
    UNDEFINED = "UNDEFINED"
    UNAVAILABLE = "UNAVAILABLE"


class HistoricalFeatureProvenance(str, Enum):
    OBSERVED = "OBSERVED"
    DERIVED_FROM_SOURCE = "DERIVED_FROM_SOURCE"
    SYNTHETIC_RECONSTRUCTION = "SYNTHETIC_RECONSTRUCTION"
    COUNTERFACTUAL = "COUNTERFACTUAL"
    UNAVAILABLE = "UNAVAILABLE"


class HistoricalEvidenceScope(str, Enum):
    SOURCE_ONLY = "SOURCE_ONLY"
    INCLUDE_RECONSTRUCTION = "INCLUDE_RECONSTRUCTION"


@dataclass(frozen=True, slots=True)
class HistoricalFeatureValue:
    key: FeatureKey
    window_us: int | None
    value: Decimal | None
    availability: FeatureAvailability
    provenance: HistoricalFeatureProvenance
    required_capabilities: tuple[str, ...]
    source_fields: tuple[str, ...]
    reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.key, FeatureKey):
            raise TypeError("historical feature key is invalid")
        definition = FEATURE_CATALOG[self.key]
        if definition.windowed != (self.window_us is not None):
            raise ValueError("historical feature window does not match its definition")
        if self.window_us is not None and self.window_us <= 0:
            raise ValueError("historical feature window must be positive")
        if not self.required_capabilities or not self.reason:
            raise ValueError("historical feature evidence metadata must be explicit")
        if self.availability is FeatureAvailability.UNAVAILABLE:
            if self.value is not None:
                raise ValueError("unavailable historical feature cannot carry a value")
            if self.provenance is not HistoricalFeatureProvenance.UNAVAILABLE:
                raise ValueError("unavailable historical feature needs unavailable provenance")
        elif self.provenance is HistoricalFeatureProvenance.UNAVAILABLE:
            raise ValueError("available historical feature cannot use unavailable provenance")
        if self.availability is FeatureAvailability.AVAILABLE and self.value is None:
            raise ValueError("available historical feature requires a numeric value")
        if self.availability is FeatureAvailability.UNDEFINED and self.value is not None:
            raise ValueError("undefined historical feature cannot carry a numeric value")

    @property
    def field_name(self) -> str:
        return feature_field_name(self.key, self.window_us)

    def as_dict(self) -> dict[str, object]:
        return {
            "availability": self.availability.value,
            "feature": self.key.value,
            "provenance": self.provenance.value,
            "reason": self.reason,
            "required_capabilities": list(self.required_capabilities),
            "source_fields": list(self.source_fields),
            "unit": FEATURE_CATALOG[self.key].units,
            "value": None if self.value is None else str(self.value),
            "window_us": self.window_us,
        }


@dataclass(frozen=True, slots=True)
class HistoricalFeatureFrame:
    fixture_id: str
    historical_mode: HistoricalDataMode
    evidence_scope: HistoricalEvidenceScope
    simulation_time_us: int
    windows_us: tuple[int, ...]
    values: dict[tuple[FeatureKey, int | None], HistoricalFeatureValue]

    def __post_init__(self) -> None:
        if not self.fixture_id or self.simulation_time_us < 0:
            raise ValueError("historical feature frame identity or time is invalid")
        expected = {
            (definition.key, window if definition.windowed else None)
            for definition in FEATURE_CATALOG.values()
            for window in (self.windows_us if definition.windowed else (None,))
        }
        if set(self.values) != expected:
            raise ValueError("historical feature frame does not cover the catalog")
        for identity, value in self.values.items():
            if identity != (value.key, value.window_us):
                raise ValueError("historical feature identity does not match its value")

    def value(
        self,
        key: FeatureKey,
        window_us: int | None = None,
    ) -> HistoricalFeatureValue:
        definition = FEATURE_CATALOG[key]
        actual_window = window_us if definition.windowed else None
        if definition.windowed and actual_window not in self.windows_us:
            raise ValueError(f"feature {key.value} requires a configured window")
        return self.values[(key, actual_window)]

    def as_dict(self) -> dict[str, object]:
        return {
            "evidence_scope": self.evidence_scope.value,
            "fixture_id": self.fixture_id,
            "historical_mode": self.historical_mode.value,
            "simulation_time_us": self.simulation_time_us,
            "values": {
                value.field_name: value.as_dict()
                for _, value in sorted(
                    self.values.items(),
                    key=lambda item: item[1].field_name,
                )
            },
            "windows_us": list(self.windows_us),
        }

    def sha256(self) -> str:
        canonical = json.dumps(
            self.as_dict(),
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class HistoricalFeatureReplay:
    fixture_id: str
    run_replay_sha256: str
    evidence_scope: HistoricalEvidenceScope
    windows_us: tuple[int, ...]
    frames: tuple[HistoricalFeatureFrame, ...]

    def __post_init__(self) -> None:
        if not self.frames:
            raise ValueError("historical feature replay requires at least one frame")
        times = tuple(frame.simulation_time_us for frame in self.frames)
        if times != tuple(sorted(set(times))):
            raise ValueError("historical feature frame times must be sorted and unique")
        if any(
            frame.fixture_id != self.fixture_id
            or frame.evidence_scope is not self.evidence_scope
            or frame.windows_us != self.windows_us
            for frame in self.frames
        ):
            raise ValueError("historical feature replay frame metadata diverged")

    @property
    def terminal_frame(self) -> HistoricalFeatureFrame:
        return self.frames[-1]

    def replay_json_lines(self) -> str:
        header = {
            "evidence_scope": self.evidence_scope.value,
            "fixture_id": self.fixture_id,
            "record_type": "historical_feature_replay",
            "run_replay_sha256": self.run_replay_sha256,
            "windows_us": list(self.windows_us),
        }
        lines = [json.dumps(header, sort_keys=True, separators=(",", ":"))]
        lines.extend(
            json.dumps(
                {"record_type": "historical_feature_frame", **frame.as_dict()},
                sort_keys=True,
                separators=(",", ":"),
            )
            for frame in self.frames
        )
        return "\n".join(lines)

    def replay_sha256(self) -> str:
        return hashlib.sha256(self.replay_json_lines().encode("utf-8")).hexdigest()


class HistoricalStrategyEvidenceError(RuntimeError):
    def __init__(self, unavailable_fields: tuple[str, ...]) -> None:
        self.unavailable_fields = unavailable_fields
        super().__init__(
            "historical strategy requires unavailable evidence: "
            + ", ".join(unavailable_fields)
        )


@dataclass(frozen=True, slots=True)
class HistoricalConditionEvaluation:
    line_number: int
    expression: str
    actual: Decimal | None
    matched: bool
    evidence: HistoricalFeatureValue
    unavailable_policy_applied: str | None

    def as_dict(self) -> dict[str, object]:
        return {
            "actual": None if self.actual is None else str(self.actual),
            "evidence": self.evidence.as_dict(),
            "expression": self.expression,
            "line": self.line_number,
            "matched": self.matched,
            "unavailable_policy_applied": self.unavailable_policy_applied,
        }


@dataclass(frozen=True, slots=True)
class HistoricalStrategyEvaluation:
    setup_name: str
    simulation_time_us: int
    state: str
    reason: str
    unavailable_policy: str
    green_conditions: tuple[HistoricalConditionEvaluation, ...]
    wait_conditions: tuple[HistoricalConditionEvaluation, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "green_conditions": [item.as_dict() for item in self.green_conditions],
            "reason": self.reason,
            "setup_name": self.setup_name,
            "simulation_time_us": self.simulation_time_us,
            "state": self.state,
            "unavailable_policy": self.unavailable_policy,
            "wait_conditions": [item.as_dict() for item in self.wait_conditions],
        }


_TRADE_COUNT_KEYS = {FeatureKey.TRADE_VELOCITY}
_AGGRESSOR_KEYS = {
    FeatureKey.AGGRESSIVE_BUY_VOLUME,
    FeatureKey.AGGRESSIVE_SELL_VOLUME,
    FeatureKey.TRADE_IMBALANCE,
    FeatureKey.BUY_SELL_RATIO,
    FeatureKey.QUEUE_DEPLETION_BID,
    FeatureKey.QUEUE_DEPLETION_ASK,
}
_ORDER_ACTIVITY_KEYS = {
    FeatureKey.CANCEL_VELOCITY_BID,
    FeatureKey.CANCEL_VELOCITY_ASK,
    FeatureKey.QUEUE_REPLENISHMENT_BID,
    FeatureKey.QUEUE_REPLENISHMENT_ASK,
}
_BOOK_KEYS = {
    FeatureKey.MID_PRICE,
    FeatureKey.MICROPRICE,
    FeatureKey.SPREAD_TICKS,
    FeatureKey.TOP_LEVEL_IMBALANCE,
    FeatureKey.MULTI_LEVEL_IMBALANCE,
    FeatureKey.WEIGHTED_DEPTH_BID,
    FeatureKey.WEIGHTED_DEPTH_ASK,
    FeatureKey.SHORT_TERM_RETURN,
    FeatureKey.SHORT_TERM_VOLATILITY,
    FeatureKey.PRICE_VELOCITY,
    FeatureKey.PRICE_ACCELERATION,
    FeatureKey.BEST_BID_SIZE,
    FeatureKey.BEST_ASK_SIZE,
    FeatureKey.SHORT_TERM_PRICE_CHANGE_TICKS,
}


def replay_historical_features(
    run: HistoricalRun,
    frame_times_us: tuple[int, ...] | None = None,
    windows_us: tuple[int, ...] = (250_000, 1_000_000, 5_000_000),
    evidence_scope: HistoricalEvidenceScope = HistoricalEvidenceScope.SOURCE_ONLY,
) -> HistoricalFeatureReplay:
    """Reset at historical start and causally replay ordered commands to frames."""

    times = (run.duration_us,) if frame_times_us is None else frame_times_us
    if (
        not times
        or times != tuple(sorted(set(times)))
        or any(type(value) is not int or value < 0 or value > run.duration_us for value in times)
    ):
        raise ValueError("historical feature frame times must be unique, sorted, and in range")
    book = OrderBook()
    command_index = 0
    if run.mode is HistoricalDataMode.RECONSTRUCTION:
        while (
            command_index < len(run.commands)
            and run.commands[command_index].action == "initial_limit"
            and run.commands[command_index].simulation_time_us == 0
        ):
            _apply_command(run.commands[command_index], book, run)
            command_index += 1
    engine = MicrostructureFeatureEngine(windows_us=windows_us)
    engine.reset(0, book)
    frames: list[HistoricalFeatureFrame] = []
    for frame_time_us in times:
        while (
            command_index < len(run.commands)
            and run.commands[command_index].simulation_time_us <= frame_time_us
        ):
            command = run.commands[command_index]
            engine.advance_to(command.simulation_time_us, book)
            events = _apply_command(command, book, run)
            if events:
                engine.observe(command.simulation_time_us, events, book)
            command_index += 1
        canonical = engine.advance_to(frame_time_us, book)
        frames.append(_evidence_frame(run, canonical, evidence_scope))
    if times[-1] == run.duration_us:
        replayed = book.journal.canonical_json_lines()
        expected = run.book.journal.canonical_json_lines()
        if replayed != expected:
            raise RuntimeError("historical feature command replay diverged from source run")
    book.assert_invariants()
    return HistoricalFeatureReplay(
        fixture_id=run.fixture_id,
        run_replay_sha256=run.replay_sha256(),
        evidence_scope=evidence_scope,
        windows_us=engine.windows_us,
        frames=tuple(frames),
    )


def historical_feature_provenance_summary(
    frame: HistoricalFeatureFrame,
) -> dict[str, object]:
    return {
        "availability": {
            item.value: sum(value.availability is item for value in frame.values.values())
            for item in FeatureAvailability
        },
        "evidence_scope": frame.evidence_scope.value,
        "frame_sha256": frame.sha256(),
        "provenance": {
            item.value: sum(value.provenance is item for value in frame.values.values())
            for item in HistoricalFeatureProvenance
        },
        "simulation_time_us": frame.simulation_time_us,
    }


def strategy_required_feature_values(
    definition: StrategyDefinition | StateMachineDefinition,
    frame: HistoricalFeatureFrame,
) -> dict[str, HistoricalFeatureValue | None]:
    """Resolve all market evidence referenced by either strategy format."""

    from kirby2.strategy import FeatureName, StateMachineDefinition, StrategyDefinition

    names: set[FeatureName] = set()
    unavailable_position_fields: set[str] = set()
    if isinstance(definition, StrategyDefinition):
        names.update(
            condition.feature
            for condition in (*definition.green_conditions, *definition.wait_conditions)
        )
    elif isinstance(definition, StateMachineDefinition):
        for transition in definition.transitions:
            for condition in transition.conditions:
                try:
                    names.add(FeatureName(condition.feature))
                except ValueError:
                    unavailable_position_fields.add(condition.feature)
    else:
        raise TypeError("unsupported strategy definition")
    result: dict[str, HistoricalFeatureValue | None] = {
        name.value: frame.value(
            *_strategy_feature_identity(name, definition.window_us)
        )
        for name in sorted(names, key=lambda item: item.value)
    }
    result.update(
        (name, None) for name in sorted(unavailable_position_fields)
    )
    return result


def require_historical_strategy_evidence(
    definition: StrategyDefinition | StateMachineDefinition,
    frame: HistoricalFeatureFrame,
) -> tuple[str, ...]:
    from kirby2.strategy import UnavailableValuePolicy

    required = strategy_required_feature_values(definition, frame)
    unavailable = tuple(
        name
        for name, evidence in required.items()
        if evidence is None
        or evidence.availability is not FeatureAvailability.AVAILABLE
    )
    if unavailable and definition.unavailable_policy is UnavailableValuePolicy.REFUSE:
        raise HistoricalStrategyEvidenceError(unavailable)
    return unavailable


def evaluate_historical_strategy(
    definition: StrategyDefinition,
    frame: HistoricalFeatureFrame,
) -> HistoricalStrategyEvaluation:
    from kirby2.strategy import StrategyDefinition, TrafficState, UnavailableValuePolicy

    if not isinstance(definition, StrategyDefinition):
        raise TypeError("historical traffic-light evaluation requires a setup strategy")
    require_historical_strategy_evidence(definition, frame)

    def evaluate_conditions(conditions: tuple[object, ...]):
        results: list[HistoricalConditionEvaluation] = []
        for raw_condition in conditions:
            condition = raw_condition
            key, window = _strategy_feature_identity(
                condition.feature,  # type: ignore[attr-defined]
                definition.window_us,
            )
            evidence = frame.value(key, window)
            policy_applied: str | None = None
            if evidence.availability is FeatureAvailability.AVAILABLE:
                actual = evidence.value
                matched = condition.operator.compare(  # type: ignore[attr-defined]
                    actual,
                    condition.threshold,  # type: ignore[attr-defined]
                )
            elif definition.unavailable_policy is UnavailableValuePolicy.AS_ZERO:
                actual = Decimal(0)
                policy_applied = definition.unavailable_policy.value
                matched = condition.operator.compare(  # type: ignore[attr-defined]
                    actual,
                    condition.threshold,  # type: ignore[attr-defined]
                )
            else:
                actual = None
                policy_applied = definition.unavailable_policy.value
                matched = False
            results.append(
                HistoricalConditionEvaluation(
                    line_number=condition.line_number,  # type: ignore[attr-defined]
                    expression=condition.render(),  # type: ignore[attr-defined]
                    actual=actual,
                    matched=matched,
                    evidence=evidence,
                    unavailable_policy_applied=policy_applied,
                )
            )
        return tuple(results)

    green = evaluate_conditions(definition.green_conditions)
    wait = evaluate_conditions(definition.wait_conditions)
    if all(item.matched for item in green):
        state = TrafficState.GREEN
        reason = f"GREEN: {len(green)}/{len(green)} GREEN conditions matched"
    elif all(item.matched for item in wait):
        state = TrafficState.WAIT
        reason = f"WAIT: {len(wait)}/{len(wait)} WAIT conditions matched"
    else:
        state = TrafficState.RED
        reason = "RED: neither GREEN nor WAIT conditions matched"
    return HistoricalStrategyEvaluation(
        setup_name=definition.name,
        simulation_time_us=frame.simulation_time_us,
        state=state.value,
        reason=reason,
        unavailable_policy=definition.unavailable_policy.value,
        green_conditions=green,
        wait_conditions=wait,
    )


def _apply_command(
    command: HistoricalCommandRecord,
    book: OrderBook,
    source_run: HistoricalRun,
) -> tuple[object, ...]:
    if not command.applied:
        return ()
    payload = command.command
    if payload is None:
        raise RuntimeError("applied historical command lost its payload")
    start = len(book.journal.events)
    action = command.action
    if action in {"limit", "initial_limit", "limit_buy", "limit_sell"}:
        quantity = int(payload.get("quantity", payload.get("original_quantity", 0)))
        book.process(
            Order.limit(
                str(payload["order_id"]),
                Side(str(payload["side"])),
                quantity,
                int(payload["price_ticks"]),
            )
        )
    elif action in {"market", "market_buy", "market_sell"}:
        book.process(
            Order.market(
                str(payload["order_id"]),
                Side(str(payload["side"])),
                int(payload["quantity"]),
            )
        )
    elif action in {"cancel", "cancel_bid", "cancel_ask"}:
        command_id = str(payload.get("command_id", payload.get("order_id", "")))
        book.process(Order.cancel(command_id, str(payload["target_order_id"])))
    else:
        raise ValueError(f"unsupported historical command action {action!r}")
    emitted = book.journal.events[start:]
    expected = source_run.exchange_events[
        command.exchange_event_start - 1 : command.exchange_event_end
    ]
    if tuple(event.as_dict() for event in emitted) != tuple(
        event.as_dict() for event in expected
    ):
        raise RuntimeError(
            f"historical command {command.sequence} replayed different exchange events"
        )
    return emitted


def _evidence_frame(
    run: HistoricalRun,
    canonical: FeatureFrame,
    scope: HistoricalEvidenceScope,
) -> HistoricalFeatureFrame:
    values = {
        identity: _evidence_value(run, canonical, identity, scope)
        for identity in canonical.values
    }
    return HistoricalFeatureFrame(
        fixture_id=run.fixture_id,
        historical_mode=run.mode,
        evidence_scope=scope,
        simulation_time_us=canonical.simulation_time_us,
        windows_us=canonical.windows_us,
        values=values,
    )


def _evidence_value(
    run: HistoricalRun,
    canonical: FeatureFrame,
    identity: tuple[FeatureKey, int | None],
    scope: HistoricalEvidenceScope,
) -> HistoricalFeatureValue:
    key, window_us = identity
    value = canonical.values[identity]
    if run.mode is HistoricalDataMode.RECONSTRUCTION:
        spread = _observed_spread(run, canonical.simulation_time_us)
        if key is FeatureKey.SPREAD_TICKS and spread is not None:
            return _available_value(
                key,
                window_us,
                Decimal(spread),
                HistoricalFeatureProvenance.OBSERVED,
                ("spread_observations",),
                ("timestamp_us", "spread_ticks"),
                "direct source spread observation at the requested timestamp",
            )
        if scope is HistoricalEvidenceScope.SOURCE_ONLY:
            return _unavailable_value(
                key,
                window_us,
                _required_capabilities(key),
                "source lacks the complete fields required by the canonical definition",
            )
        return _available_or_undefined(
            key,
            window_us,
            value,
            HistoricalFeatureProvenance.SYNTHETIC_RECONSTRUCTION,
            _required_capabilities(key),
            ("synthetic_exchange_events", "reconstruction_seed"),
            "computed from deterministic synthetic reconstruction",
        )

    capabilities = run.provenance
    required = _required_capabilities(key)
    if key is FeatureKey.RELATIVE_VOLUME:
        return _unavailable_value(
            key,
            window_us,
            required,
            "historical source defines no relative-volume baseline",
        )
    if key in _TRADE_COUNT_KEYS and not capabilities.provides_trade_events:
        return _unavailable_value(
            key,
            window_us,
            required,
            "source does not provide an ordered complete trade-event stream",
        )
    if key in _AGGRESSOR_KEYS and (
        not capabilities.provides_trade_events
        or not capabilities.provides_trade_aggressor_side
    ):
        return _unavailable_value(
            key,
            window_us,
            required,
            "source trade aggressor side is unavailable",
        )
    if key in _ORDER_ACTIVITY_KEYS and not capabilities.provides_order_events:
        return _unavailable_value(
            key,
            window_us,
            required,
            "source does not provide ordered add/cancel messages",
        )
    if key in _BOOK_KEYS and not (
        capabilities.provides_order_events or capabilities.provides_book_events
    ):
        return _unavailable_value(
            key,
            window_us,
            required,
            "source does not provide order-level or book-state evidence",
        )
    return _available_or_undefined(
        key,
        window_us,
        value,
        HistoricalFeatureProvenance.DERIVED_FROM_SOURCE,
        required,
        ("ordered_source_messages", "validated_exchange_events"),
        "canonical feature derived causally from supported source evidence",
    )


def _available_value(
    key: FeatureKey,
    window_us: int | None,
    value: Decimal,
    provenance: HistoricalFeatureProvenance,
    capabilities: tuple[str, ...],
    fields: tuple[str, ...],
    reason: str,
) -> HistoricalFeatureValue:
    return HistoricalFeatureValue(
        key,
        window_us,
        value,
        FeatureAvailability.AVAILABLE,
        provenance,
        capabilities,
        fields,
        reason,
    )


def _available_or_undefined(
    key: FeatureKey,
    window_us: int | None,
    value: Decimal | None,
    provenance: HistoricalFeatureProvenance,
    capabilities: tuple[str, ...],
    fields: tuple[str, ...],
    reason: str,
) -> HistoricalFeatureValue:
    return HistoricalFeatureValue(
        key,
        window_us,
        value,
        (
            FeatureAvailability.AVAILABLE
            if value is not None
            else FeatureAvailability.UNDEFINED
        ),
        provenance,
        capabilities,
        fields,
        reason if value is not None else f"{reason}; numeric value is undefined",
    )


def _unavailable_value(
    key: FeatureKey,
    window_us: int | None,
    capabilities: tuple[str, ...],
    reason: str,
) -> HistoricalFeatureValue:
    return HistoricalFeatureValue(
        key,
        window_us,
        None,
        FeatureAvailability.UNAVAILABLE,
        HistoricalFeatureProvenance.UNAVAILABLE,
        capabilities,
        (),
        reason,
    )


def _required_capabilities(key: FeatureKey) -> tuple[str, ...]:
    if key is FeatureKey.RELATIVE_VOLUME:
        return ("relative_volume_baseline",)
    if key in _TRADE_COUNT_KEYS:
        return ("ordered_trade_events",)
    if key in _AGGRESSOR_KEYS:
        return ("ordered_trade_events", "trade_aggressor_side")
    if key in _ORDER_ACTIVITY_KEYS:
        return ("ordered_order_events",)
    if key in _BOOK_KEYS:
        return ("ordered_order_events_or_book_states",)
    raise RuntimeError(f"feature capability mapping omitted {key.value}")


def _observed_spread(run: HistoricalRun, time_us: int) -> int | None:
    if run.constraints is None:
        return None
    observed = tuple(
        item
        for item in run.constraints.spread_observations
        if item.timestamp_us == time_us
    )
    return None if not observed else observed[-1].spread_ticks


def _strategy_feature_identity(
    feature: object,
    window_us: int,
) -> tuple[FeatureKey, int | None]:
    from kirby2.strategy import FeatureName

    if not isinstance(feature, FeatureName):
        raise TypeError("strategy feature name is invalid")
    mapping = {
        FeatureName.SPREAD_TICKS: FeatureKey.SPREAD_TICKS,
        FeatureName.BEST_BID_SIZE: FeatureKey.BEST_BID_SIZE,
        FeatureName.BEST_ASK_SIZE: FeatureKey.BEST_ASK_SIZE,
        FeatureName.BOOK_IMBALANCE: FeatureKey.TOP_LEVEL_IMBALANCE,
        FeatureName.AGGRESSIVE_BUY_VOLUME: FeatureKey.AGGRESSIVE_BUY_VOLUME,
        FeatureName.AGGRESSIVE_SELL_VOLUME: FeatureKey.AGGRESSIVE_SELL_VOLUME,
        FeatureName.BUY_SELL_RATIO: FeatureKey.BUY_SELL_RATIO,
        FeatureName.TRADE_VELOCITY: FeatureKey.TRADE_VELOCITY,
        FeatureName.BID_DEPLETION_RATE: FeatureKey.QUEUE_DEPLETION_BID,
        FeatureName.ASK_DEPLETION_RATE: FeatureKey.QUEUE_DEPLETION_ASK,
        FeatureName.BID_REPLENISHMENT_RATE: FeatureKey.QUEUE_REPLENISHMENT_BID,
        FeatureName.ASK_REPLENISHMENT_RATE: FeatureKey.QUEUE_REPLENISHMENT_ASK,
        FeatureName.BID_CANCEL_RATE: FeatureKey.CANCEL_VELOCITY_BID,
        FeatureName.ASK_CANCEL_RATE: FeatureKey.CANCEL_VELOCITY_ASK,
        FeatureName.RELATIVE_VOLUME: FeatureKey.RELATIVE_VOLUME,
        FeatureName.SHORT_TERM_PRICE_CHANGE: FeatureKey.SHORT_TERM_PRICE_CHANGE_TICKS,
        FeatureName.MICROPRICE: FeatureKey.MICROPRICE,
    }
    key = mapping[feature]
    return key, window_us if FEATURE_CATALOG[key].windowed else None
