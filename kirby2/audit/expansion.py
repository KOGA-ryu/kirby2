"""Deterministic gate registry for the Work Orders 31-40 expansion."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from pathlib import Path


CANONICAL_IMPLEMENTATION_CARD_IDS = (
    "K2X-02",
    "WO31-A",
    "WO31-B",
    "WO31-C",
    "WO31-D",
    "WO31-E1",
    "WO31-E2",
    "WO31-E3",
    "WO31-E4",
    "WO31-E5",
    "WO31-E6",
    "WO31-F",
    "WO31-G",
    "WO31-H",
    "WO31-I",
    "WO31-I1",
    "WO32-A",
    "WO32-B",
    "WO32-C",
    "WO32-D",
    "WO32-E",
    "WO33-A",
    "WO33-A1",
    "WO33-B1",
    "WO33-B2",
    "WO33-C",
    "WO33-D",
    "WO33-E",
    "WO34-A",
    "WO34-B",
    "WO34-C",
    "WO34-D",
    "WO35-A",
    "WO35-B",
    "WO35-C",
    "WO35-D",
    "WO35-E",
    "WO35-F",
    "WO35-F1",
    "WO36-A",
    "WO36-B",
    "WO36-C",
    "WO36-D",
    "WO36-E",
    "WO37-A",
    "WO37-B",
    "WO37-C",
    "WO37-D",
    "WO37-E",
    "WO39-A",
    "WO39-B",
    "WO39-C",
    "WO38-A",
    "WO38-B",
    "WO38-C",
    "WO38-D",
    "WO38-E",
    "WO39-D1",
    "WO39-D2",
    "WO39-E",
    "WO40-A",
    "WO40-B",
    "WO40-B1",
    "WO40-C",
    "WO40-D",
    "WO40-D1",
    "WO40-E",
    "WO40-F",
    "WO40-G",
    "WO40-H",
    "WO40-I",
    "WO40-J",
)
RECORDED_DEVIATIONS = (("DEV-0001", "K2X-02"),)
REGISTERABLE_GATE_IDS = ("DEV-0001", "K2X-02")
_BASELINE_ARTIFACT_SHA256 = (
    "41b934c01794435e4477143a7894faf2f88bb7d4fd11b49c078cf962a955318d"
)
_LEGACY_PROJECTION_SHA256 = (
    "8b5da0c0ff5d2e769f7252104f8eb907c91c1653d3c82ebb38c2d93a72770a8e"
)
_LEGACY_ROOT_HELP_SHA256 = (
    "44be0c313151592c385da44ddb5940bed4309b3a3d810e278f799050a343031b"
)
_LEGACY_COMMAND_ORDER = (
    "demo",
    "latency-demo",
    "mechanics-demo",
    "agent-ecology",
    "hidden-liquidity-demo",
    "multivenue-demo",
    "benchmark-execution",
    "counterfactual",
    "simulate",
    "compare-flow",
    "inspect-intensity",
    "probe-intensity",
    "features",
    "inspect-distribution",
    "inspect-session",
    "measure-compare",
    "calibrate",
    "scenario",
    "audit-scenarios",
    "audit-hawkes-stability",
    "audit-strategy-time",
    "audit-distribution-truth",
    "audit-historical-features",
    "audit-historical-lessons",
    "audit-run-store",
    "audit-market-data",
    "audit-latency",
    "audit-market-mechanics",
    "audit-hidden-liquidity",
    "audit-multivenue",
    "audit-execution-algorithms",
    "audit-counterfactuals",
    "audit-agent-ecology",
    "audit-model-risk-lab",
    "audit-lab",
    "ingest-market-data",
    "inspect-dataset",
    "validate-dataset",
    "replay-capability",
    "record-run",
    "inspect-run",
    "query-runs",
    "verify-run",
    "matrix",
    "ui",
    "strategy",
    "experiment",
    "layout",
    "replay",
    "report",
    "curriculum",
    "timeline",
    "lesson-list",
    "lesson-run",
    "historical",
)
_DEVIATION_PATTERN = re.compile(r"DEV-[0-9]{4}\Z")


class ExpansionGateStatus(str, Enum):
    PASS = "PASS"
    PASS_WITH_WARNINGS = "PASS_WITH_WARNINGS"
    FAIL = "FAIL"
    NOT_EXERCISED = "NOT_EXERCISED"


_REQUESTED_EXIT_CODES = {
    ExpansionGateStatus.PASS: 0,
    ExpansionGateStatus.PASS_WITH_WARNINGS: 0,
    ExpansionGateStatus.FAIL: 1,
    ExpansionGateStatus.NOT_EXERCISED: 2,
}


class ExpansionGateRegistrationError(ValueError):
    """A deterministic gate-registration refusal."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True, slots=True)
class ExpansionGateCheck:
    code: str
    status: ExpansionGateStatus
    detail: str
    required: bool = True

    def __post_init__(self) -> None:
        if (
            not isinstance(self.code, str)
            or not self.code
            or not isinstance(self.detail, str)
            or not self.detail
        ):
            raise ValueError("gate checks require nonempty code and detail")
        if not isinstance(self.status, ExpansionGateStatus):
            raise TypeError("gate check status must use ExpansionGateStatus")
        if not isinstance(self.required, bool):
            raise TypeError("gate check required must be a bool")

    def as_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "detail": self.detail,
            "required": self.required,
            "status": self.status.value,
        }


@dataclass(frozen=True, slots=True)
class ExpansionGateReport:
    card_id: str
    status: ExpansionGateStatus
    checks: tuple[ExpansionGateCheck, ...]
    failures: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    reason_code: str | None = None
    metadata: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if not self.card_id:
            raise ValueError("gate report card_id must be nonempty")
        if not isinstance(self.status, ExpansionGateStatus):
            raise TypeError("gate report status must use ExpansionGateStatus")
        codes = tuple(check.code for check in self.checks)
        if len(codes) != len(set(codes)):
            raise ValueError("gate check codes must be unique")
        keys = tuple(key for key, _ in self.metadata)
        if len(keys) != len(set(keys)):
            raise ValueError("gate metadata keys must be unique")
        if any(
            not isinstance(key, str)
            or not key
            or not isinstance(value, str)
            or not value
            for key, value in self.metadata
        ):
            raise ValueError("gate metadata requires nonempty string keys and values")
        for label, values in (
            ("failure", self.failures),
            ("warning", self.warnings),
        ):
            if len(values) != len(set(values)) or any(
                not isinstance(value, str) or not value for value in values
            ):
                raise ValueError(f"gate {label}s must be unique nonempty strings")
        if self.reason_code is not None and not re.fullmatch(
            r"[A-Z][A-Z0-9_]*",
            self.reason_code,
        ):
            raise ValueError("gate reason_code must be an uppercase identifier")
        if self.status is ExpansionGateStatus.PASS and (self.failures or self.warnings):
            raise ValueError("PASS reports cannot contain failures or warnings")
        if self.status is ExpansionGateStatus.PASS_WITH_WARNINGS and (
            self.failures or not self.warnings
        ):
            raise ValueError("PASS_WITH_WARNINGS requires warnings and no failures")
        if self.status is ExpansionGateStatus.FAIL and not self.failures:
            raise ValueError("FAIL reports require at least one failure")
        if self.status is ExpansionGateStatus.NOT_EXERCISED and not self.reason_code:
            raise ValueError("NOT_EXERCISED reports require a reason_code")
        if self.status is ExpansionGateStatus.NOT_EXERCISED and self.failures:
            raise ValueError("NOT_EXERCISED reports cannot contain failures")
        if any(
            check.status is ExpansionGateStatus.FAIL for check in self.checks
        ) and self.status is not ExpansionGateStatus.FAIL:
            raise ValueError("a failed check requires overall FAIL")
        if any(
            check.required
            and check.status is ExpansionGateStatus.NOT_EXERCISED
            for check in self.checks
        ) and self.status not in {
            ExpansionGateStatus.FAIL,
            ExpansionGateStatus.NOT_EXERCISED,
        }:
            raise ValueError(
                "a required unexercised check cannot produce a passing gate"
            )
        if any(
            check.status is ExpansionGateStatus.PASS_WITH_WARNINGS
            for check in self.checks
        ) and self.status is ExpansionGateStatus.PASS:
            raise ValueError("a warning check cannot produce overall PASS")

    @property
    def exit_code(self) -> int:
        return _REQUESTED_EXIT_CODES[self.status]

    def as_dict(self) -> dict[str, object]:
        return {
            "card_id": self.card_id,
            "checks": [check.as_dict() for check in self.checks],
            "failures": list(self.failures),
            "metadata": {key: value for key, value in sorted(self.metadata)},
            "reason_code": self.reason_code,
            "status": self.status.value,
            "warnings": list(self.warnings),
        }


ExpansionGate = Callable[[], ExpansionGateReport]


@dataclass(frozen=True, slots=True)
class ExpansionAuditResult:
    selector: str
    reports: tuple[ExpansionGateReport, ...]
    aggregate_status: ExpansionGateStatus | None
    reason_code: str | None
    canonical_complete: bool
    registered_ids: tuple[str, ...]

    @property
    def refused(self) -> bool:
        return self.aggregate_status is None

    @property
    def exit_code(self) -> int:
        if self.refused:
            return 2
        if self.selector == "all":
            return 1 if self.aggregate_status is ExpansionGateStatus.FAIL else 0
        assert self.aggregate_status is not None
        return _REQUESTED_EXIT_CODES[self.aggregate_status]


class ExpansionGateRegistry:
    """Resolve explicit gates in canonical card/deviation order."""

    def __init__(
        self,
        *,
        canonical_card_ids: Iterable[str] = CANONICAL_IMPLEMENTATION_CARD_IDS,
        recorded_deviations: Iterable[tuple[str, str]] = RECORDED_DEVIATIONS,
        registerable_gate_ids: Iterable[str] = REGISTERABLE_GATE_IDS,
    ) -> None:
        self._canonical = tuple(canonical_card_ids)
        self._deviations = tuple(recorded_deviations)
        self._registerable = tuple(registerable_gate_ids)
        self._gates: dict[str, ExpansionGate] = {}
        self._validate_contract()
        self._resolved_order = self._build_resolved_order()
        expected_registerable_order = tuple(
            gate_id
            for gate_id in self._resolved_order
            if gate_id in self._registerable
        )
        if self._registerable != expected_registerable_order:
            raise ExpansionGateRegistrationError(
                "NONCANONICAL_REGISTERABLE_ORDER",
                "registerable gates must follow resolved canonical/deviation order",
            )

    def _validate_contract(self) -> None:
        if not self._canonical or len(self._canonical) != len(set(self._canonical)):
            raise ExpansionGateRegistrationError(
                "INVALID_CANONICAL_GATE_SET",
                "canonical card IDs must be nonempty and unique",
            )
        if "all" in self._canonical:
            raise ExpansionGateRegistrationError(
                "RESERVED_SELECTOR",
                "lowercase all cannot be a card ID",
            )
        deviation_ids = tuple(item[0] for item in self._deviations)
        if len(deviation_ids) != len(set(deviation_ids)):
            raise ExpansionGateRegistrationError(
                "DUPLICATE_DEVIATION_ID",
                "recorded deviation IDs must be unique",
            )
        for deviation_id, interrupted_card in self._deviations:
            if not _DEVIATION_PATTERN.fullmatch(deviation_id):
                raise ExpansionGateRegistrationError(
                    "INVALID_DEVIATION_ID",
                    f"invalid recorded deviation ID {deviation_id!r}",
                )
            if interrupted_card not in self._canonical:
                raise ExpansionGateRegistrationError(
                    "INVALID_DEVIATION_TARGET",
                    f"deviation {deviation_id!r} targets unknown card "
                    f"{interrupted_card!r}",
                )
        deviation_ordinals = tuple(
            int(deviation_id.removeprefix("DEV-")) for deviation_id in deviation_ids
        )
        if deviation_ordinals != tuple(range(1, len(deviation_ordinals) + 1)):
            raise ExpansionGateRegistrationError(
                "NONCONTIGUOUS_DEVIATION_IDS",
                "recorded deviations must be monotonically contiguous from DEV-0001",
            )
        insertion_indices = tuple(
            self._canonical.index(interrupted_card)
            for _, interrupted_card in self._deviations
        )
        if insertion_indices != tuple(sorted(insertion_indices)):
            raise ExpansionGateRegistrationError(
                "NONCANONICAL_DEVIATION_ORDER",
                "deviation insertion points cannot move backward in canonical order",
            )
        if len(self._registerable) != len(set(self._registerable)):
            raise ExpansionGateRegistrationError(
                "DUPLICATE_REGISTERABLE_GATE_ID",
                "registerable gate IDs must be unique",
            )
        allowed = set(self._canonical) | set(deviation_ids)
        unknown = tuple(
            gate_id for gate_id in self._registerable if gate_id not in allowed
        )
        if unknown:
            raise ExpansionGateRegistrationError(
                "NOT_REGISTERED",
                f"registerable gate IDs are not canonical or recorded: {unknown!r}",
            )

    def _build_resolved_order(self) -> tuple[str, ...]:
        deviations_by_target: dict[str, list[str]] = {}
        for deviation_id, interrupted_card in self._deviations:
            deviations_by_target.setdefault(interrupted_card, []).append(deviation_id)
        ordered: list[str] = []
        for card_id in self._canonical:
            ordered.extend(
                sorted(
                    deviations_by_target.get(card_id, ()),
                    key=lambda value: int(value.removeprefix("DEV-")),
                )
            )
            ordered.append(card_id)
        return tuple(ordered)

    @property
    def registered_ids(self) -> tuple[str, ...]:
        return tuple(
            gate_id for gate_id in self._resolved_order if gate_id in self._gates
        )

    @property
    def canonical_complete(self) -> bool:
        return set(self._canonical) == set(self._gates).intersection(self._canonical)

    @property
    def gate_set_complete(self) -> bool:
        required = set(self._canonical) | {item[0] for item in self._deviations}
        return required == set(self._gates)

    def register(self, gate_id: str, gate: ExpansionGate) -> None:
        if gate_id == "all":
            raise ExpansionGateRegistrationError(
                "RESERVED_SELECTOR",
                "lowercase all cannot be registered as a gate",
            )
        if gate_id not in self._registerable:
            raise ExpansionGateRegistrationError(
                "NOT_REGISTERED",
                f"gate {gate_id!r} is not registerable in this source revision",
            )
        if gate_id in self._gates:
            raise ExpansionGateRegistrationError(
                "DUPLICATE_GATE_ID",
                f"gate {gate_id!r} is already registered",
            )
        if not callable(gate):
            raise ExpansionGateRegistrationError(
                "INVALID_GATE",
                f"gate {gate_id!r} is not callable",
            )
        self._gates[gate_id] = gate

    def _invoke(self, gate_id: str) -> ExpansionGateReport:
        gate = self._gates[gate_id]
        try:
            report = gate()
        except Exception as error:
            failure = f"gate raised {type(error).__name__}"
            return ExpansionGateReport(
                card_id=gate_id,
                status=ExpansionGateStatus.FAIL,
                checks=(
                    ExpansionGateCheck(
                        code="GATE_EXCEPTION",
                        status=ExpansionGateStatus.FAIL,
                        detail=failure,
                    ),
                ),
                failures=(failure,),
                reason_code="GATE_EXCEPTION",
            )
        if not isinstance(report, ExpansionGateReport):
            return ExpansionGateReport(
                card_id=gate_id,
                status=ExpansionGateStatus.FAIL,
                checks=(
                    ExpansionGateCheck(
                        code="INVALID_GATE_REPORT",
                        status=ExpansionGateStatus.FAIL,
                        detail="gate did not return ExpansionGateReport",
                    ),
                ),
                failures=("gate did not return ExpansionGateReport",),
                reason_code="INVALID_GATE_REPORT",
            )
        if report.card_id != gate_id:
            detail = f"registered {gate_id!r}, report named {report.card_id!r}"
            return ExpansionGateReport(
                card_id=gate_id,
                status=ExpansionGateStatus.FAIL,
                checks=(
                    ExpansionGateCheck(
                        code="GATE_ID_MISMATCH",
                        status=ExpansionGateStatus.FAIL,
                        detail=detail,
                    ),
                ),
                failures=(detail,),
                reason_code="GATE_ID_MISMATCH",
            )
        return report

    def run(self, selector: str) -> ExpansionAuditResult:
        if selector != "all":
            if selector not in self._gates:
                return ExpansionAuditResult(
                    selector=selector,
                    reports=(),
                    aggregate_status=None,
                    reason_code="NOT_REGISTERED",
                    canonical_complete=self.canonical_complete,
                    registered_ids=self.registered_ids,
                )
            report = self._invoke(selector)
            if selector == "WO40-J" and not self.gate_set_complete:
                report = ExpansionGateReport(
                    card_id=selector,
                    status=ExpansionGateStatus.FAIL,
                    checks=(
                        *report.checks,
                        ExpansionGateCheck(
                            code="INCOMPLETE_GATE_SET",
                            status=ExpansionGateStatus.FAIL,
                            detail=(
                                "final gate requires every canonical and recorded "
                                "deviation gate"
                            ),
                        ),
                    ),
                    failures=(*report.failures, "INCOMPLETE_GATE_SET"),
                    warnings=report.warnings,
                    reason_code="INCOMPLETE_GATE_SET",
                    metadata=report.metadata,
                )
            return ExpansionAuditResult(
                selector=selector,
                reports=(report,),
                aggregate_status=report.status,
                reason_code=report.reason_code,
                canonical_complete=self.canonical_complete,
                registered_ids=self.registered_ids,
            )

        reports = tuple(self._invoke(gate_id) for gate_id in self.registered_ids)
        statuses = tuple(report.status for report in reports)
        reason_code: str | None = None
        if not reports or any(
            status in {ExpansionGateStatus.FAIL, ExpansionGateStatus.NOT_EXERCISED}
            for status in statuses
        ):
            aggregate = ExpansionGateStatus.FAIL
        elif ExpansionGateStatus.PASS_WITH_WARNINGS in statuses:
            aggregate = ExpansionGateStatus.PASS_WITH_WARNINGS
        else:
            aggregate = ExpansionGateStatus.PASS

        if "WO40-J" in self._gates:
            deviation_ids = {item[0] for item in self._deviations}
            if not self.gate_set_complete:
                aggregate = ExpansionGateStatus.FAIL
                reason_code = "INCOMPLETE_GATE_SET"
            else:
                report_by_id = {report.card_id: report for report in reports}
                if any(
                    report_by_id[deviation_id].status
                    not in {
                        ExpansionGateStatus.PASS,
                        ExpansionGateStatus.PASS_WITH_WARNINGS,
                    }
                    for deviation_id in deviation_ids
                ):
                    aggregate = ExpansionGateStatus.FAIL
                    reason_code = "INCOMPLETE_GATE_SET"

        return ExpansionAuditResult(
            selector=selector,
            reports=reports,
            aggregate_status=aggregate,
            reason_code=reason_code,
            canonical_complete=self.canonical_complete,
            registered_ids=self.registered_ids,
        )


def _stable_argparse_value(value: object) -> object:
    if value is argparse.SUPPRESS:
        return {"kind": "argparse.SUPPRESS"}
    if value is None or type(value) in {bool, int, float, str}:
        return value
    if isinstance(value, Decimal):
        return {"kind": "Decimal", "value": str(value)}
    if isinstance(value, Path):
        return {"kind": "Path", "value": value.as_posix()}
    if isinstance(value, Enum):
        return {
            "kind": value.__class__.__module__ + "." + value.__class__.__qualname__,
            "value": _stable_argparse_value(value.value),
        }
    if isinstance(value, (tuple, list)):
        return [_stable_argparse_value(item) for item in value]
    if isinstance(value, Mapping):
        return {
            str(key): _stable_argparse_value(item)
            for key, item in sorted(value.items(), key=lambda item: str(item[0]))
        }
    if callable(value):
        return {
            "kind": "callable",
            "value": getattr(value, "__module__", "")
            + "."
            + getattr(
                value,
                "__qualname__",
                getattr(value, "__name__", type(value).__qualname__),
            ),
        }
    return {
        "kind": type(value).__module__ + "." + type(value).__qualname__,
        "value": str(value),
    }


def _project_parser(
    parser: argparse.ArgumentParser,
    path: list[str],
) -> list[dict[str, object]]:
    actions: list[dict[str, object]] = []
    children: list[dict[str, object]] = []
    for action in parser._actions:
        row: dict[str, object] = {
            "action": action.__class__.__module__
            + "."
            + action.__class__.__qualname__,
            "choices": (
                None
                if isinstance(action, argparse._SubParsersAction)
                else _stable_argparse_value(action.choices)
            ),
            "const": _stable_argparse_value(action.const),
            "default": _stable_argparse_value(action.default),
            "dest": action.dest,
            "help": action.help,
            "metavar": _stable_argparse_value(action.metavar),
            "nargs": _stable_argparse_value(action.nargs),
            "option_strings": list(action.option_strings),
            "required": action.required,
            "type": _stable_argparse_value(action.type),
        }
        if isinstance(action, argparse._SubParsersAction):
            row["subcommands"] = list(action.choices)
            for name, child in action.choices.items():
                children.extend(_project_parser(child, [*path, name]))
        actions.append(row)
    return [
        {
            "actions": actions,
            "description": parser.description,
            "epilog": parser.epilog,
            "help_sha256": hashlib.sha256(
                parser.format_help().encode("utf-8")
            ).hexdigest(),
            "path": path,
            "prog": parser.prog,
        },
        *children,
    ]


def _assert_legacy_cli_projection() -> str:
    from kirby2.__main__ import _parser
    from kirby2.cli.expansion import declared_expansion_command_names
    from kirby2.cli.registry import dispatch_registered_command

    previous_columns = os.environ.get("COLUMNS")
    os.environ["COLUMNS"] = "80"
    try:
        parser = _parser()
        root = next(
            action
            for action in parser._actions
            if isinstance(action, argparse._SubParsersAction)
        )
        missing = tuple(
            name for name in _LEGACY_COMMAND_ORDER if name not in root.choices
        )
        assert not missing, f"legacy commands missing: {missing!r}"
        observed_order = tuple(
            name for name in root.choices if name in _LEGACY_COMMAND_ORDER
        )
        assert observed_order == _LEGACY_COMMAND_ORDER
        rows: list[dict[str, object]] = []
        for name in _LEGACY_COMMAND_ORDER:
            rows.extend(_project_parser(root.choices[name], [name]))
        projection = json.dumps(
            {"commands": list(_LEGACY_COMMAND_ORDER), "parsers": rows},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        digest = hashlib.sha256(projection).hexdigest()
        assert len(_LEGACY_COMMAND_ORDER) == 55 and len(rows) == 62
        assert digest == _LEGACY_PROJECTION_SHA256
        additions = tuple(
            name for name in root.choices if name not in _LEGACY_COMMAND_ORDER
        )
        assert additions == declared_expansion_command_names()
        assert dispatch_registered_command(parser.parse_args(["demo"])) is False
        registered_args = parser.parse_args(
            ["audit-expansion", "--gate", "K2X-02"]
        )
        assert callable(vars(registered_args).get("_kirby2_expansion_handler"))
        for name in additions:
            root.choices.pop(name)
        root._choices_actions[:] = [
            action for action in root._choices_actions if action.dest not in additions
        ]
        legacy_root_help_digest = hashlib.sha256(
            parser.format_help().encode("utf-8")
        ).hexdigest()
        assert legacy_root_help_digest == _LEGACY_ROOT_HELP_SHA256

        baseline_path = Path(__file__).resolve().parents[2] / (
            "KIRBY2_WORK_ORDERS_31_40_BASELINE.json"
        )
        raw = baseline_path.read_bytes()
        assert hashlib.sha256(raw).hexdigest() == _BASELINE_ARTIFACT_SHA256
        baseline = json.loads(raw)
        assert baseline["cli"]["command_order"] == list(_LEGACY_COMMAND_ORDER)
        assert baseline["cli"]["parser_projection"] == rows
        assert baseline["cli"]["projection_sha256"] == digest
        return digest
    finally:
        if previous_columns is None:
            os.environ.pop("COLUMNS", None)
        else:
            os.environ["COLUMNS"] = previous_columns


def _expect_registration_error(code: str, operation: Callable[[], object]) -> None:
    try:
        operation()
    except Exception as error:
        observed = getattr(error, "code", None)
        assert observed == code, f"expected {code}, got {observed}: {error}"
    else:
        raise AssertionError(f"expected registration refusal {code}")


def _assert_command_registry() -> None:
    from kirby2.cli.registry import (
        CommandModule,
        CommandRegistry,
        CommandSpec,
        dispatch_registered_command,
    )

    calls: list[str] = []

    def handler(args: argparse.Namespace) -> int:
        calls.append(args.command)
        return 0

    def spec(command_id: str, name: str) -> CommandSpec:
        return CommandSpec(
            command_id=command_id,
            name=name,
            help=f"help for {name}",
            handler=handler,
        )

    parser = argparse.ArgumentParser(prog="registry-audit")
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("legacy")
    registry = CommandRegistry(subcommands)
    before = tuple(subcommands.choices)
    duplicate_modules = (
        CommandModule("MODULE", (spec("ONE", "one"),)),
        CommandModule("MODULE", (spec("TWO", "two"),)),
    )
    _expect_registration_error(
        "DUPLICATE_MODULE_ID",
        lambda: registry.register_modules(duplicate_modules),
    )
    assert tuple(subcommands.choices) == before
    duplicate_ids = CommandModule(
        "IDS",
        (spec("SAME", "one"), spec("SAME", "two")),
    )
    _expect_registration_error(
        "DUPLICATE_COMMAND_ID",
        lambda: registry.register_module(duplicate_ids),
    )
    assert tuple(subcommands.choices) == before
    duplicate_names = CommandModule(
        "NAMES",
        (spec("ONE", "same"), spec("TWO", "same")),
    )
    _expect_registration_error(
        "DUPLICATE_COMMAND",
        lambda: registry.register_module(duplicate_names),
    )
    assert tuple(subcommands.choices) == before
    shadow = CommandModule("SHADOW", (spec("LEGACY", "legacy"),))
    _expect_registration_error(
        "SHADOWED_COMMAND",
        lambda: registry.register_module(shadow),
    )
    assert tuple(subcommands.choices) == before

    registry.register_module(
        CommandModule(
            "CASE_SENSITIVE",
            (spec("UPPER", "Case"), spec("LOWER", "case")),
        )
    )
    assert registry.module_ids == ("CASE_SENSITIVE",)
    assert registry.command_ids == ("UPPER", "LOWER")
    assert registry.registered_names == ("Case", "case")

    assert dispatch_registered_command(argparse.Namespace(command="legacy")) is False
    private_args = parser.parse_args(["Case"])
    assert dispatch_registered_command(private_args) is True
    assert calls == ["Case"]
    nonzero = argparse.Namespace(_kirby2_expansion_handler=lambda _args: 2)
    try:
        dispatch_registered_command(nonzero)
    except SystemExit as error:
        assert error.code == 2
    else:
        raise AssertionError("nonzero registered handler did not exit")
    with contextlib.redirect_stderr(io.StringIO()):
        try:
            parser.parse_args(["unknown"])
        except SystemExit as error:
            assert error.code == 2
        else:
            raise AssertionError("unknown argparse command did not exit 2")


def _fixture_report(
    card_id: str,
    status: ExpansionGateStatus,
) -> ExpansionGateReport:
    warnings = (
        ("fixture warning",)
        if status is ExpansionGateStatus.PASS_WITH_WARNINGS
        else ()
    )
    failures = ("fixture failure",) if status is ExpansionGateStatus.FAIL else ()
    reason = (
        "FIXTURE_NOT_EXERCISED"
        if status is ExpansionGateStatus.NOT_EXERCISED
        else None
    )
    return ExpansionGateReport(
        card_id=card_id,
        status=status,
        checks=(
            ExpansionGateCheck(
                code="FIXTURE",
                status=status,
                detail="in-memory status mapping fixture",
            ),
        ),
        failures=failures,
        warnings=warnings,
        reason_code=reason,
    )


def _assert_gate_registry() -> None:
    try:
        ExpansionGateReport(
            card_id="K2X-02",
            status=ExpansionGateStatus.PASS,
            checks=(
                ExpansionGateCheck(
                    code="CONTRADICTORY_FAILURE",
                    status=ExpansionGateStatus.FAIL,
                    detail="hostile contradictory report",
                ),
            ),
        )
    except ValueError:
        pass
    else:
        raise AssertionError("PASS report accepted a failed check")
    optional = ExpansionGateReport(
        card_id="K2X-02",
        status=ExpansionGateStatus.PASS,
        checks=(
            ExpansionGateCheck(
                code="OPTIONAL_CAPABILITY",
                status=ExpansionGateStatus.NOT_EXERCISED,
                detail="optional capability remains truthfully unexercised",
                required=False,
            ),
        ),
    )
    assert optional.exit_code == 0

    ids = CANONICAL_IMPLEMENTATION_CARD_IDS[:5]
    registry = ExpansionGateRegistry(
        canonical_card_ids=ids,
        recorded_deviations=(),
        registerable_gate_ids=ids[:4],
    )
    statuses = (
        ExpansionGateStatus.PASS,
        ExpansionGateStatus.PASS_WITH_WARNINGS,
        ExpansionGateStatus.FAIL,
        ExpansionGateStatus.NOT_EXERCISED,
    )
    calls = {gate_id: 0 for gate_id in ids[:4]}
    for gate_id, status in zip(ids[:4], statuses, strict=True):
        def gate(
            gate_id: str = gate_id,
            status: ExpansionGateStatus = status,
        ) -> ExpansionGateReport:
            calls[gate_id] += 1
            return _fixture_report(gate_id, status)

        registry.register(gate_id, gate)

    assert tuple(registry.run(gate_id).exit_code for gate_id in ids[:4]) == (0, 0, 1, 2)
    before_refusals = dict(calls)
    for selector in (ids[4], "k2x-02", "UNKNOWN", "ALL"):
        refused = registry.run(selector)
        assert refused.refused and refused.reason_code == "NOT_REGISTERED"
        assert refused.exit_code == 2 and not refused.reports
    assert calls == before_refusals

    _expect_registration_error(
        "DUPLICATE_GATE_ID",
        lambda: registry.register(ids[0], lambda: _fixture_report(ids[0], statuses[0])),
    )
    _expect_registration_error(
        "NOT_REGISTERED",
        lambda: registry.register(ids[4], lambda: _fixture_report(ids[4], statuses[0])),
    )
    _expect_registration_error(
        "RESERVED_SELECTOR",
        lambda: registry.register("all", lambda: _fixture_report("all", statuses[0])),
    )

    calls_before_all = dict(calls)
    aggregate = registry.run("all")
    assert aggregate.aggregate_status is ExpansionGateStatus.FAIL
    assert aggregate.exit_code == 1
    assert tuple(report.card_id for report in aggregate.reports) == ids[:4]
    assert all(calls[gate_id] == calls_before_all[gate_id] + 1 for gate_id in ids[:4])

    deviation_registry = ExpansionGateRegistry(
        canonical_card_ids=ids[:3],
        recorded_deviations=(("DEV-0001", ids[2]),),
        registerable_gate_ids=(ids[0], ids[1], "DEV-0001", ids[2]),
    )
    for gate_id in (ids[2], "DEV-0001", ids[0], ids[1]):
        deviation_registry.register(
            gate_id,
            lambda gate_id=gate_id: _fixture_report(
                gate_id,
                ExpansionGateStatus.PASS,
            ),
        )
    assert deviation_registry.registered_ids == (
        ids[0],
        ids[1],
        "DEV-0001",
        ids[2],
    )
    _expect_registration_error(
        "INVALID_DEVIATION_ID",
        lambda: ExpansionGateRegistry(
            canonical_card_ids=ids[:2],
            recorded_deviations=(("dev-0001", ids[1]),),
            registerable_gate_ids=ids[:1],
        ),
    )
    _expect_registration_error(
        "NONCONTIGUOUS_DEVIATION_IDS",
        lambda: ExpansionGateRegistry(
            canonical_card_ids=ids[:2],
            recorded_deviations=(("DEV-0002", ids[1]),),
            registerable_gate_ids=ids[:1],
        ),
    )
    _expect_registration_error(
        "NONCANONICAL_REGISTERABLE_ORDER",
        lambda: ExpansionGateRegistry(
            canonical_card_ids=ids[:2],
            recorded_deviations=(),
            registerable_gate_ids=(ids[1], ids[0]),
        ),
    )
    _expect_registration_error(
        "NONCANONICAL_DEVIATION_ORDER",
        lambda: ExpansionGateRegistry(
            canonical_card_ids=ids[:3],
            recorded_deviations=(
                ("DEV-0001", ids[2]),
                ("DEV-0002", ids[1]),
            ),
            registerable_gate_ids=ids[:1],
        ),
    )

    final_ids = ("K2X-02", "WO40-J")
    incomplete = ExpansionGateRegistry(
        canonical_card_ids=final_ids,
        recorded_deviations=(),
        registerable_gate_ids=("WO40-J",),
    )
    incomplete.register(
        "WO40-J",
        lambda: _fixture_report("WO40-J", ExpansionGateStatus.PASS),
    )
    incomplete_result = incomplete.run("WO40-J")
    assert incomplete_result.aggregate_status is ExpansionGateStatus.FAIL
    assert incomplete_result.reason_code == "INCOMPLETE_GATE_SET"
    complete = ExpansionGateRegistry(
        canonical_card_ids=final_ids,
        recorded_deviations=(),
        registerable_gate_ids=final_ids,
    )
    for gate_id in final_ids:
        complete.register(
            gate_id,
            lambda gate_id=gate_id: _fixture_report(
                gate_id,
                ExpansionGateStatus.PASS,
            ),
        )
    assert complete.run("WO40-J").aggregate_status is ExpansionGateStatus.PASS


def _audit_k2x02() -> ExpansionGateReport:
    checks: list[ExpansionGateCheck] = []
    failures: list[str] = []

    def run_check(code: str, operation: Callable[[], object], detail: str) -> None:
        try:
            operation()
        except Exception as error:
            failure = f"{code}: {type(error).__name__}"
            failures.append(failure)
            checks.append(
                ExpansionGateCheck(
                    code=code,
                    status=ExpansionGateStatus.FAIL,
                    detail=failure,
                )
            )
        else:
            checks.append(
                ExpansionGateCheck(
                    code=code,
                    status=ExpansionGateStatus.PASS,
                    detail=detail,
                )
            )

    run_check(
        "CANONICAL_GATE_ORDER",
        lambda: (
            len(CANONICAL_IMPLEMENTATION_CARD_IDS) == 72
            and CANONICAL_IMPLEMENTATION_CARD_IDS[0] == "K2X-02"
            and CANONICAL_IMPLEMENTATION_CARD_IDS[-1] == "WO40-J"
            and CANONICAL_IMPLEMENTATION_CARD_IDS.index("WO39-C")
            < CANONICAL_IMPLEMENTATION_CARD_IDS.index("WO38-A")
        )
        or (_ for _ in ()).throw(AssertionError("canonical gate order mismatch")),
        "72 implementation cards retain canonical slice-index order",
    )

    projection: list[str] = []

    def projection_check() -> None:
        projection.append(_assert_legacy_cli_projection())

    run_check(
        "LEGACY_CLI_PROJECTION",
        projection_check,
        "55 legacy commands and 62 parser nodes retain exact help/default digest",
    )
    run_check(
        "COMMAND_REGISTRY_REFUSALS",
        _assert_command_registry,
        "explicit modules preflight IDs, names, shadows, fallthrough, and dispatch",
    )
    run_check(
        "EXPANSION_GATE_SEMANTICS",
        _assert_gate_registry,
        "status exits, refusals, no-short-circuit all, and deviation order pass",
    )

    def current_registration() -> None:
        registry = build_expansion_gate_registry()
        assert registry.registered_ids == REGISTERABLE_GATE_IDS
        assert {"DEV-0001", "K2X-02"}.issubset(registry.registered_ids)
        assert registry.registered_ids.index("DEV-0001") < (
            registry.registered_ids.index("K2X-02")
        )

    run_check(
        "CURRENT_GATE_REGISTRATION",
        current_registration,
        "registered gates match the declared frontier with DEV-0001 before K2X-02",
    )

    status = ExpansionGateStatus.FAIL if failures else ExpansionGateStatus.PASS
    return ExpansionGateReport(
        card_id="K2X-02",
        status=status,
        checks=tuple(checks),
        failures=tuple(failures),
        metadata=(
            ("canonical_gate_count", "72"),
            ("legacy_command_count", "55"),
            ("legacy_parser_count", "62"),
            (
                "legacy_projection_sha256",
                projection[0] if projection else "UNAVAILABLE",
            ),
            ("registered_gate_count", str(len(REGISTERABLE_GATE_IDS))),
        ),
    )


def _audit_dev0001() -> ExpansionGateReport:
    from kirby2.auditlab.runner import (
        PROVENANCE_PACKAGE_ROOTS,
        _implementation_manifest,
    )

    repository = Path(__file__).resolve().parents[2]
    manifest, links, errors = _implementation_manifest(repository)
    expected = {
        "kirby2/cli/__init__.py",
        "kirby2/cli/expansion.py",
        "kirby2/cli/registry.py",
    }
    checks = (
        ExpansionGateCheck(
            code="CLI_PROVENANCE_ROOT",
            status=(
                ExpansionGateStatus.PASS
                if PROVENANCE_PACKAGE_ROOTS.count("cli") == 1
                else ExpansionGateStatus.FAIL
            ),
            detail="cli occurs exactly once in the explicit provenance package roots",
        ),
        ExpansionGateCheck(
            code="CLI_SOURCE_BYTES_BOUND",
            status=(
                ExpansionGateStatus.PASS
                if expected.issubset(manifest)
                else ExpansionGateStatus.FAIL
            ),
            detail=(
                "all three explicit CLI source files are present in the byte manifest"
            ),
        ),
        ExpansionGateCheck(
            code="PROVENANCE_MANIFEST_ERRORS",
            status=(
                ExpansionGateStatus.PASS
                if not links and not errors
                else ExpansionGateStatus.FAIL
            ),
            detail="generic implementation manifest has no link or traversal errors",
        ),
    )
    failures = tuple(
        check.code for check in checks if check.status is ExpansionGateStatus.FAIL
    )
    return ExpansionGateReport(
        card_id="DEV-0001",
        status=(ExpansionGateStatus.FAIL if failures else ExpansionGateStatus.PASS),
        checks=checks,
        failures=failures,
        metadata=(
            ("bound_cli_file_count", str(len(expected.intersection(manifest)))),
            ("provenance_root_count", str(len(PROVENANCE_PACKAGE_ROOTS))),
        ),
    )


GATE_SPECS: tuple[tuple[str, ExpansionGate], ...] = (
    ("DEV-0001", _audit_dev0001),
    ("K2X-02", _audit_k2x02),
)


def build_expansion_gate_registry() -> ExpansionGateRegistry:
    spec_ids = tuple(gate_id for gate_id, _ in GATE_SPECS)
    if spec_ids != REGISTERABLE_GATE_IDS:
        raise ExpansionGateRegistrationError(
            "GATE_SPEC_MISMATCH",
            "explicit gate specs must exactly match the registerable frontier",
        )
    registry = ExpansionGateRegistry()
    for gate_id, gate in GATE_SPECS:
        registry.register(gate_id, gate)
    return registry


def _render_result(result: ExpansionAuditResult) -> str:
    lines = ["KIRBY2_EXPANSION_AUDIT"]
    if result.refused:
        refusal = {
            "gate": result.selector,
            "reason_code": result.reason_code,
            "selection": "REFUSED",
        }
        lines.append(json.dumps(refusal, sort_keys=True, separators=(",", ":")))
        lines.append(
            f"EXPANSION_AUDIT REFUSED gate={result.selector} "
            f"reason={result.reason_code}"
        )
        return "\n".join(lines)

    for report in result.reports:
        lines.append(
            json.dumps(report.as_dict(), sort_keys=True, separators=(",", ":"))
        )
    assert result.aggregate_status is not None
    if result.selector == "all":
        failures = sum(
            report.status is ExpansionGateStatus.FAIL for report in result.reports
        )
        warnings = sum(
            report.status is ExpansionGateStatus.PASS_WITH_WARNINGS
            for report in result.reports
        )
        not_exercised = sum(
            report.status is ExpansionGateStatus.NOT_EXERCISED
            for report in result.reports
        )
        lines.append(
            f"EXPANSION_AUDIT {result.aggregate_status.value} gate=all "
            f"registered={len(result.reports)} failures={failures} warnings={warnings} "
            f"not_exercised={not_exercised} "
            f"canonical_complete={str(result.canonical_complete).lower()}"
            + (f" reason={result.reason_code}" if result.reason_code else "")
        )
    else:
        report = result.reports[0]
        lines.append(
            f"EXPANSION_AUDIT {report.status.value} gate={report.card_id} "
            f"checks={len(report.checks)} failures={len(report.failures)} "
            f"warnings={len(report.warnings)}"
            + (f" reason={report.reason_code}" if report.reason_code else "")
        )
    return "\n".join(lines)


def run_registered_expansion_audit(selector: str) -> int:
    result = build_expansion_gate_registry().run(selector)
    print(_render_result(result))
    return result.exit_code


__all__ = [
    "CANONICAL_IMPLEMENTATION_CARD_IDS",
    "ExpansionAuditResult",
    "ExpansionGateCheck",
    "ExpansionGateRegistrationError",
    "ExpansionGateRegistry",
    "ExpansionGateReport",
    "ExpansionGateStatus",
    "RECORDED_DEVIATIONS",
    "REGISTERABLE_GATE_IDS",
    "build_expansion_gate_registry",
    "run_registered_expansion_audit",
]
