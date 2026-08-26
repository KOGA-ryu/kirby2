"""Parser and immutable value objects for the traffic-light rule language."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .state_machine import StateMachineDefinition


DEFAULT_WINDOW_US = 5_000_000
_SETUP_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
_WINDOW = re.compile(r"^(\d+)(ms|s)$", re.IGNORECASE)


class TrafficState(str, Enum):
    GREEN = "GREEN"
    WAIT = "WAIT"
    RED = "RED"


class FeatureName(str, Enum):
    SPREAD_TICKS = "spread_ticks"
    BEST_BID_SIZE = "best_bid_size"
    BEST_ASK_SIZE = "best_ask_size"
    BOOK_IMBALANCE = "book_imbalance"
    AGGRESSIVE_BUY_VOLUME = "aggressive_buy_volume"
    AGGRESSIVE_SELL_VOLUME = "aggressive_sell_volume"
    BUY_SELL_RATIO = "buy_sell_ratio"
    TRADE_VELOCITY = "trade_velocity"
    BID_DEPLETION_RATE = "bid_depletion_rate"
    ASK_DEPLETION_RATE = "ask_depletion_rate"
    BID_REPLENISHMENT_RATE = "bid_replenishment_rate"
    ASK_REPLENISHMENT_RATE = "ask_replenishment_rate"
    BID_CANCEL_RATE = "bid_cancel_rate"
    ASK_CANCEL_RATE = "ask_cancel_rate"
    RELATIVE_VOLUME = "relative_volume"
    SHORT_TERM_PRICE_CHANGE = "short_term_price_change"
    MICROPRICE = "microprice"


class ComparisonOperator(str, Enum):
    GREATER = ">"
    GREATER_EQUAL = ">="
    LESS = "<"
    LESS_EQUAL = "<="
    EQUAL = "=="
    NOT_EQUAL = "!="

    def compare(self, actual: Decimal, threshold: Decimal) -> bool:
        if self is ComparisonOperator.GREATER:
            return actual > threshold
        if self is ComparisonOperator.GREATER_EQUAL:
            return actual >= threshold
        if self is ComparisonOperator.LESS:
            return actual < threshold
        if self is ComparisonOperator.LESS_EQUAL:
            return actual <= threshold
        if self is ComparisonOperator.EQUAL:
            return actual == threshold
        return actual != threshold


class RuleSyntaxError(ValueError):
    def __init__(self, line_number: int, reason: str, source_line: str = "") -> None:
        self.line_number = line_number
        self.reason = reason
        self.source_line = source_line
        detail = f"line {line_number}: {reason}"
        if source_line:
            detail += f" [{source_line.strip()}]"
        super().__init__(detail)


@dataclass(frozen=True, slots=True)
class RuleCondition:
    line_number: int
    feature: FeatureName
    operator: ComparisonOperator
    threshold: Decimal

    def __post_init__(self) -> None:
        if self.line_number <= 0 or not self.threshold.is_finite():
            raise ValueError("rule condition line and threshold must be valid")

    def render(self) -> str:
        return f"{self.feature.value} {self.operator.value} {self.threshold}"


@dataclass(frozen=True, slots=True)
class StrategyDefinition:
    name: str
    window_us: int
    green_conditions: tuple[RuleCondition, ...]
    wait_conditions: tuple[RuleCondition, ...]
    source: str

    def __post_init__(self) -> None:
        if not _SETUP_NAME.fullmatch(self.name):
            raise ValueError("invalid strategy setup name")
        if self.window_us <= 0:
            raise ValueError("strategy rolling window must be positive")
        if not self.green_conditions or not self.wait_conditions:
            raise ValueError("GREEN and WAIT rules require conditions")

    @property
    def source_sha256(self) -> str:
        return hashlib.sha256(self.source.encode("utf-8")).hexdigest()

    def as_dict(self) -> dict[str, object]:
        return {
            "green_conditions": [condition.render() for condition in self.green_conditions],
            "name": self.name,
            "source_sha256": self.source_sha256,
            "wait_conditions": [condition.render() for condition in self.wait_conditions],
            "window_us": self.window_us,
        }


def parse_strategy(source: str) -> StrategyDefinition | StateMachineDefinition:
    if not isinstance(source, str):
        raise TypeError("strategy source must be text")
    lines = _meaningful_lines(source)
    if not lines:
        raise RuleSyntaxError(1, "strategy file is empty")

    if lines[0][1].split()[0].lower() == "machine":
        from .state_machine import parse_state_machine

        return parse_state_machine(source)

    line_number, text = lines[0]
    setup_parts = text.split()
    if len(setup_parts) != 2 or setup_parts[0].lower() != "setup":
        raise RuleSyntaxError(line_number, "expected 'setup NAME'", text)
    name = setup_parts[1]
    if not _SETUP_NAME.fullmatch(name):
        raise RuleSyntaxError(
            line_number,
            "setup name must start with a letter and use only letters, digits, '_' or '-'",
            text,
        )

    index = 1
    window_us = DEFAULT_WINDOW_US
    if index < len(lines) and lines[index][1].lower().startswith("window"):
        window_line, window_text = lines[index]
        parts = window_text.split()
        if len(parts) != 2 or parts[0].lower() != "window":
            raise RuleSyntaxError(window_line, "expected 'window NUMBERs'", window_text)
        window_us = _parse_window(parts[1], window_line, window_text)
        index += 1

    index = _expect_header(lines, index, "GREEN when")
    green, index = _parse_conditions_until(lines, index, "WAIT when")
    if not green:
        header_line = lines[index - 1][0] if index else 1
        raise RuleSyntaxError(header_line, "GREEN requires at least one condition")

    index = _expect_header(lines, index, "WAIT when")
    wait, index = _parse_conditions_until(lines, index, "RED otherwise")
    if not wait:
        header_line = lines[index - 1][0] if index else 1
        raise RuleSyntaxError(header_line, "WAIT requires at least one condition")

    index = _expect_header(lines, index, "RED otherwise")
    if index != len(lines):
        extra_line, extra_text = lines[index]
        raise RuleSyntaxError(extra_line, "unexpected content after 'RED otherwise'", extra_text)

    return StrategyDefinition(
        name=name,
        window_us=window_us,
        green_conditions=tuple(green),
        wait_conditions=tuple(wait),
        source=source,
    )


def _meaningful_lines(source: str) -> list[tuple[int, str]]:
    result: list[tuple[int, str]] = []
    for line_number, raw_line in enumerate(source.splitlines(), start=1):
        text = raw_line.split("#", 1)[0].strip()
        if text:
            result.append((line_number, text))
    return result


def _parse_window(value: str, line_number: int, source_line: str) -> int:
    match = _WINDOW.fullmatch(value)
    if match is None:
        raise RuleSyntaxError(line_number, "window must be a positive integer followed by 'ms' or 's'", source_line)
    quantity = int(match.group(1))
    if quantity <= 0:
        raise RuleSyntaxError(line_number, "window must be positive", source_line)
    multiplier = 1_000 if match.group(2).lower() == "ms" else 1_000_000
    return quantity * multiplier


def _expect_header(
    lines: list[tuple[int, str]],
    index: int,
    expected: str,
) -> int:
    if index >= len(lines):
        eof_line = lines[-1][0] + 1 if lines else 1
        raise RuleSyntaxError(eof_line, f"expected '{expected}' before end of file")
    line_number, text = lines[index]
    if text.upper() != expected.upper():
        raise RuleSyntaxError(line_number, f"expected '{expected}'", text)
    return index + 1


def _parse_conditions_until(
    lines: list[tuple[int, str]],
    index: int,
    next_header: str,
) -> tuple[list[RuleCondition], int]:
    conditions: list[RuleCondition] = []
    while index < len(lines) and lines[index][1].upper() != next_header.upper():
        line_number, text = lines[index]
        conditions.append(_parse_condition(line_number, text))
        index += 1
    return conditions, index


def _parse_condition(line_number: int, text: str) -> RuleCondition:
    parts = text.split()
    if len(parts) != 3:
        raise RuleSyntaxError(
            line_number,
            "condition must be 'FEATURE OPERATOR NUMBER'",
            text,
        )
    feature_text, operator_text, threshold_text = parts
    try:
        feature = FeatureName(feature_text)
    except ValueError as error:
        allowed = ", ".join(feature.value for feature in FeatureName)
        raise RuleSyntaxError(
            line_number,
            f"unknown or non-observable feature '{feature_text}'; allowed: {allowed}",
            text,
        ) from error
    try:
        operator = ComparisonOperator(operator_text)
    except ValueError as error:
        allowed = " ".join(operator.value for operator in ComparisonOperator)
        raise RuleSyntaxError(
            line_number,
            f"unsupported operator '{operator_text}'; allowed: {allowed}",
            text,
        ) from error
    try:
        threshold = Decimal(threshold_text)
    except InvalidOperation as error:
        raise RuleSyntaxError(line_number, "threshold must be a decimal number", text) from error
    if not threshold.is_finite():
        raise RuleSyntaxError(line_number, "threshold must be finite", text)
    return RuleCondition(line_number, feature, operator, threshold)
