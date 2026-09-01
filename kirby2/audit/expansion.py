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
RECORDED_DEVIATIONS = (
    ("DEV-0001", "K2X-02"),
    ("DEV-0002", "WO31-B"),
    ("DEV-0003", "WO31-B"),
    ("DEV-0004", "WO31-E6"),
    ("DEV-0005", "WO36-C"),
    ("DEV-0006", "WO36-C"),
    ("DEV-0007", "WO37-A"),
    ("DEV-0008", "WO40-E"),
    ("DEV-0009", "WO40-E"),
    ("DEV-0010", "WO40-E"),
    ("DEV-0011", "WO40-F"),
    ("DEV-0012", "WO40-F"),
    ("DEV-0013", "WO40-F"),
    ("DEV-0014", "WO40-G"),
)
REGISTERABLE_GATE_IDS = (
    "DEV-0001",
    "K2X-02",
    "WO31-A",
    "DEV-0002",
    "DEV-0003",
    "WO31-B",
    "WO31-C",
    "WO31-D",
    "WO31-E1",
    "WO31-E2",
    "WO31-E3",
    "WO31-E4",
    "WO31-E5",
    "DEV-0004",
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
    "DEV-0005",
    "DEV-0006",
    "WO36-C",
    "WO36-D",
    "WO36-E",
    "DEV-0007",
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
    "DEV-0008",
    "DEV-0009",
    "DEV-0010",
    "WO40-E",
    "DEV-0011",
    "DEV-0012",
    "DEV-0013",
    "WO40-F",
    "DEV-0014",
    "WO40-G",
    "WO40-H",
    "WO40-I",
    "WO40-J",
)
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

        report_rows: list[ExpansionGateReport] = []
        closeout_registered = "WO40-J" in self._gates
        for gate_id in self.registered_ids:
            if gate_id == "WO40-J":
                continue
            report_rows.append(self._invoke(gate_id))

        if closeout_registered:
            prior_statuses = tuple(report.status for report in report_rows)
            prior_passed = bool(report_rows) and all(
                status
                in {
                    ExpansionGateStatus.PASS,
                    ExpansionGateStatus.PASS_WITH_WARNINGS,
                }
                for status in prior_statuses
            )
            if prior_passed and self.gate_set_complete:
                try:
                    from kirby2.audit.release import (
                        RELEASE_REQUIRED_DEVIATION_GATE_IDS_V1,
                        publish_release_closeout_prerequisites,
                    )
                    from kirby2.packs.formats import canonical_json_bytes
                    from kirby2.release.qualification import (
                        WO40_J_REQUIRED_PRIOR_GATES_V1,
                    )

                    by_id = {report.card_id: report for report in report_rows}
                    publication_ids = (
                        *WO40_J_REQUIRED_PRIOR_GATES_V1,
                        *RELEASE_REQUIRED_DEVIATION_GATE_IDS_V1,
                    )
                    publish_release_closeout_prerequisites(
                        Path(__file__).resolve().parents[2],
                        tuple(
                            (
                                gate_id,
                                canonical_json_bytes(by_id[gate_id].as_dict()),
                                by_id[gate_id].status.value,
                            )
                            for gate_id in publication_ids
                        ),
                    )
                except Exception as error:
                    detail = (
                        "passing aggregate could not publish immutable closeout "
                        f"prerequisites: {type(error).__name__}"
                    )
                    report_rows.append(
                        ExpansionGateReport(
                            card_id="WO40-J",
                            status=ExpansionGateStatus.FAIL,
                            checks=(
                                ExpansionGateCheck(
                                    code="PREREQUISITE_PUBLICATION_FAILED",
                                    status=ExpansionGateStatus.FAIL,
                                    detail=detail,
                                ),
                            ),
                            failures=(detail,),
                            reason_code="PREREQUISITE_PUBLICATION_FAILED",
                        )
                    )
                else:
                    report_rows.append(self._invoke("WO40-J"))
            else:
                report_rows.append(self._invoke("WO40-J"))

        reports = tuple(report_rows)
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

        if closeout_registered:
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


def _audit_wo31a() -> ExpansionGateReport:
    """Run the contract-only full-day audit without claiming a runtime."""

    from kirby2.audit.full_day import audit_wo31a_contracts

    cases = audit_wo31a_contracts()
    checks: list[ExpansionGateCheck] = []
    failures: list[str] = []
    for case in cases:
        if case.status_override is not None:
            status = ExpansionGateStatus(case.status_override)
        else:
            status = (
                ExpansionGateStatus.FAIL
                if case.failures
                else ExpansionGateStatus.PASS
            )
        checks.append(
            ExpansionGateCheck(
                code=case.name,
                status=status,
                detail=(
                    case.detail
                    if case.reason_code is None
                    else f"{case.detail}; reason_code={case.reason_code}"
                ),
                required=case.required,
            )
        )
        failures.extend(f"{case.name}: {failure}" for failure in case.failures)
    runtime_cases = tuple(
        case
        for case in cases
        if case.status_override == ExpansionGateStatus.NOT_EXERCISED.value
    )
    if (
        len(runtime_cases) != 1
        or runtime_cases[0].required
        or runtime_cases[0].reason_code != "RESTORE_NOT_IMPLEMENTED"
    ):
        failures.append(
            "WO31-A requires exactly one optional NOT_EXERCISED runtime case with "
            "reason RESTORE_NOT_IMPLEMENTED"
        )
    runtime_reason = (
        runtime_cases[0].reason_code if len(runtime_cases) == 1 else "INVALID_RUNTIME_CASE"
    )
    return ExpansionGateReport(
        card_id="WO31-A",
        status=(ExpansionGateStatus.FAIL if failures else ExpansionGateStatus.PASS),
        checks=tuple(checks),
        failures=tuple(failures),
        metadata=(
            ("contract_case_count", str(len(cases))),
            ("runtime_capability", ExpansionGateStatus.NOT_EXERCISED.value),
            ("runtime_reason_code", runtime_reason or "MISSING_RUNTIME_REASON"),
        ),
    )


def _audit_wo31b() -> ExpansionGateReport:
    """Run the duration-aware state runtime audit without composing Hawkes."""

    from kirby2.audit.full_day import audit_wo31b_transitions

    cases = audit_wo31b_transitions()
    checks: list[ExpansionGateCheck] = []
    failures: list[str] = []
    for case in cases:
        if case.status_override is not None:
            status = ExpansionGateStatus(case.status_override)
        else:
            status = (
                ExpansionGateStatus.FAIL
                if case.failures
                else ExpansionGateStatus.PASS
            )
        checks.append(
            ExpansionGateCheck(
                code=case.name,
                status=status,
                detail=(
                    case.detail
                    if case.reason_code is None
                    else f"{case.detail}; reason_code={case.reason_code}"
                ),
                required=case.required,
            )
        )
        failures.extend(f"{case.name}: {failure}" for failure in case.failures)
    optional_cases = tuple(
        case
        for case in cases
        if case.status_override == ExpansionGateStatus.NOT_EXERCISED.value
    )
    if (
        len(optional_cases) != 1
        or optional_cases[0].required
        or optional_cases[0].reason_code
        != "HAWKES_COMPOSITION_DEFERRED_TO_WO31_E2"
    ):
        failures.append(
            "WO31-B requires exactly one optional NOT_EXERCISED Hawkes-composition "
            "case deferred to WO31-E2"
        )
    hawkes_reason = (
        optional_cases[0].reason_code
        if len(optional_cases) == 1
        else "INVALID_HAWKES_CAPABILITY_CASE"
    )
    return ExpansionGateReport(
        card_id="WO31-B",
        status=(ExpansionGateStatus.FAIL if failures else ExpansionGateStatus.PASS),
        checks=tuple(checks),
        failures=tuple(failures),
        metadata=(
            ("transition_runtime", "EXECUTED"),
            ("transition_case_count", str(len(cases) - len(optional_cases))),
            ("hawkes_composition", ExpansionGateStatus.NOT_EXERCISED.value),
            ("hawkes_reason_code", hawkes_reason or "MISSING_HAWKES_REASON"),
        ),
    )


def _audit_wo31c() -> ExpansionGateReport:
    """Run the required portable-checkpoint and governed-data-path audit."""

    from kirby2.audit.full_day import audit_wo31c_checkpoints
    from kirby2.full_day.checkpoints import RUNTIME_CHECKPOINT_FORMAT_ID

    cases = audit_wo31c_checkpoints()
    checks: list[ExpansionGateCheck] = []
    failures: list[str] = []
    if not cases:
        failures.append("WO31-C audit must return at least one required case")
    for case in cases:
        wrapper_failures: list[str] = []
        if not case.required:
            wrapper_failures.append("WO31-C cases must all be required")
        if case.status_override is not None:
            wrapper_failures.append(
                "WO31-C cases must report ordinary PASS/FAIL status"
            )
        failed = bool(case.failures or wrapper_failures)
        checks.append(
            ExpansionGateCheck(
                code=case.name,
                status=(
                    ExpansionGateStatus.FAIL
                    if failed
                    else ExpansionGateStatus.PASS
                ),
                detail=case.detail,
                required=True,
            )
        )
        failures.extend(f"{case.name}: {failure}" for failure in case.failures)
        failures.extend(
            f"{case.name}: {failure}" for failure in wrapper_failures
        )
    return ExpansionGateReport(
        card_id="WO31-C",
        status=(ExpansionGateStatus.FAIL if failures else ExpansionGateStatus.PASS),
        checks=tuple(checks),
        failures=tuple(failures),
        metadata=(
            ("checkpoint_case_count", str(len(cases))),
            ("checkpoint_format", RUNTIME_CHECKPOINT_FORMAT_ID),
            ("checkpoint_runtime", "EXECUTED"),
        ),
    )


def _audit_wo31d() -> ExpansionGateReport:
    """Run fresh-process restoration at every fixed core-session boundary."""

    from kirby2.audit.full_day import audit_wo31d_core_restore
    from kirby2.full_day.restore import CORE_SESSION_CHECKPOINT_FORMAT_ID

    cases = audit_wo31d_core_restore()
    checks: list[ExpansionGateCheck] = []
    failures: list[str] = []
    expected_names = (
        "core_restore_post_t0_quiet",
        "core_restore_auction_order_imbalance",
        "core_restore_post_uncross",
        "core_restore_partial_fill",
        "core_restore_working_player_order",
        "core_restore_queued_fifo_depth",
        "core_restore_halt",
        "core_restore_reopen",
        "core_restore_hostile_refusals",
        "core_restore_worker_protocol_scope",
    )
    if tuple(case.name for case in cases) != expected_names:
        failures.append("WO31-D audit cases differ from the fixed boundary inventory")
    for case in cases:
        wrapper_failures: list[str] = []
        if not case.required:
            wrapper_failures.append("WO31-D cases must all be required")
        if case.status_override is not None:
            wrapper_failures.append(
                "WO31-D cases must report ordinary PASS/FAIL status"
            )
        failed = bool(case.failures or wrapper_failures)
        checks.append(
            ExpansionGateCheck(
                code=case.name,
                status=(
                    ExpansionGateStatus.FAIL
                    if failed
                    else ExpansionGateStatus.PASS
                ),
                detail=case.detail,
                required=True,
            )
        )
        failures.extend(f"{case.name}: {failure}" for failure in case.failures)
        failures.extend(
            f"{case.name}: {failure}" for failure in wrapper_failures
        )
    return ExpansionGateReport(
        card_id="WO31-D",
        status=(ExpansionGateStatus.FAIL if failures else ExpansionGateStatus.PASS),
        checks=tuple(checks),
        failures=tuple(failures),
        metadata=(
            ("checkpoint_format", CORE_SESSION_CHECKPOINT_FORMAT_ID),
            ("fixed_boundary_count", "8"),
            ("fresh_process_runtime", "EXECUTED"),
            ("hostile_refusal_count", "14"),
            ("restored_scope", "SINGLE_VENUE_MARKET_MECHANICS"),
        ),
    )


def _audit_wo31e1() -> ExpansionGateReport:
    """Run the executable mechanics/agent spine and composed restore cuts."""

    from kirby2.audit.full_day import audit_wo31e1_runtime_restore
    from kirby2.full_day.composition import (
        INITIAL_PROFILE_ID,
        executable_agent_mechanics_composition_matrix,
    )
    from kirby2.full_day.restore import (
        FULL_DAY_RUNTIME_RESTORE_REQUEST_FORMAT_ID,
    )

    cases = audit_wo31e1_runtime_restore()
    expected_names = (
        "full_day_mechanics_agent_composition",
        "full_day_one_shot_subdivided",
        "full_day_restore_auction_imbalance",
        "full_day_restore_auction_uncross",
        "full_day_restore_halt",
        "full_day_restore_reopen",
        "full_day_restore_participant_activation",
        "full_day_restore_participant_withdrawal",
        "full_day_restore_active_metaorder",
        "full_day_restore_agent_inventories",
        "full_day_restore_next_scheduled_decision",
        "full_day_restore_agent_substream_state",
        "full_day_restore_order_allocator",
        "full_day_restore_exchange_queues",
        "full_day_restore_same_time_microsteps",
        "full_day_inactive_scheduler_absent",
        "full_day_hostile_owner_protocol_refusals",
        "full_day_restore_worker_protocol_scope",
    )
    failures: list[str] = []
    checks: list[ExpansionGateCheck] = []
    if tuple(case.name for case in cases) != expected_names:
        failures.append("WO31-E1 cases differ from the fixed evidence inventory")
    for case in cases:
        wrapper_failures: list[str] = []
        if not case.required:
            wrapper_failures.append("WO31-E1 cases must all be required")
        if case.status_override is not None:
            wrapper_failures.append(
                "WO31-E1 cases must report ordinary PASS/FAIL status"
            )
        failed = bool(case.failures or wrapper_failures)
        checks.append(
            ExpansionGateCheck(
                code=case.name,
                status=(
                    ExpansionGateStatus.FAIL
                    if failed
                    else ExpansionGateStatus.PASS
                ),
                detail=case.detail,
                required=True,
            )
        )
        failures.extend(f"{case.name}: {failure}" for failure in case.failures)
        failures.extend(
            f"{case.name}: {failure}" for failure in wrapper_failures
        )
    matrix = executable_agent_mechanics_composition_matrix()
    profile = matrix.profile(INITIAL_PROFILE_ID, 2)
    return ExpansionGateReport(
        card_id="WO31-E1",
        status=(ExpansionGateStatus.FAIL if failures else ExpansionGateStatus.PASS),
        checks=tuple(checks),
        failures=tuple(failures),
        metadata=(
            ("composition_matrix_sha256", matrix.sha256),
            ("fresh_process_boundary_count", "13"),
            ("hostile_refusal_count", "20"),
            ("profile_id", profile.profile_id),
            ("profile_version", str(profile.profile_version)),
            ("restore_format", FULL_DAY_RUNTIME_RESTORE_REQUEST_FORMAT_ID),
            ("restored_scope", "SINGLE_VENUE_AGENT_MECHANICS"),
        ),
    )


def _audit_wo31e2() -> ExpansionGateReport:
    """Run all three exactly-one full-day flow/restore models."""

    from kirby2.audit.full_day import audit_wo31e2_flow_restore
    from kirby2.full_day.composition import (
        FLOW_PROFILE_ID,
        executable_queue_reactive_flow_composition_matrix,
    )
    from kirby2.full_day.restore import (
        FULL_DAY_RUNTIME_RESTORE_REQUEST_FORMAT_ID,
    )

    cases = audit_wo31e2_flow_restore()
    expected_names = (
        "full_day_simple_flow_composition",
        "full_day_simple_flow_one_shot_subdivided",
        "full_day_simple_flow_fresh_process_restore",
        "full_day_simple_flow_ownership_refusals",
        "full_day_hawkes_flow_composition",
        "full_day_hawkes_flow_one_shot_subdivided",
        "full_day_hawkes_flow_fresh_process_restore",
        "full_day_hawkes_flow_ownership_refusals",
        "full_day_queue_reactive_flow_composition",
        "full_day_queue_reactive_one_shot_subdivided",
        "full_day_queue_reactive_fresh_process_restore",
        "full_day_queue_reactive_ownership_refusals",
    )
    failures: list[str] = []
    checks: list[ExpansionGateCheck] = []
    if tuple(case.name for case in cases) != expected_names:
        failures.append("WO31-E2 cases differ from the fixed evidence inventory")
    for case in cases:
        wrapper_failures: list[str] = []
        if not case.required:
            wrapper_failures.append("WO31-E2 cases must all be required")
        if case.status_override is not None:
            wrapper_failures.append(
                "WO31-E2 cases must report ordinary PASS/FAIL status"
            )
        failed = bool(case.failures or wrapper_failures)
        checks.append(
            ExpansionGateCheck(
                code=case.name,
                status=(
                    ExpansionGateStatus.FAIL
                    if failed
                    else ExpansionGateStatus.PASS
                ),
                detail=case.detail,
                required=True,
            )
        )
        failures.extend(f"{case.name}: {failure}" for failure in case.failures)
        failures.extend(
            f"{case.name}: {failure}" for failure in wrapper_failures
        )
    matrix = executable_queue_reactive_flow_composition_matrix()
    profile = matrix.profile(FLOW_PROFILE_ID, 3)
    return ExpansionGateReport(
        card_id="WO31-E2",
        status=(ExpansionGateStatus.FAIL if failures else ExpansionGateStatus.PASS),
        checks=tuple(checks),
        failures=tuple(failures),
        metadata=(
            ("composition_matrix_sha256", matrix.sha256),
            ("flow_model_count", "3"),
            ("fresh_process_boundary_count", "3"),
            ("hostile_refusal_count", "20"),
            ("profile_id", profile.profile_id),
            ("profile_version", str(profile.profile_version)),
            ("restore_format", FULL_DAY_RUNTIME_RESTORE_REQUEST_FORMAT_ID),
            ("restored_scope", "SIMPLE_HAWKES_QUEUE_REACTIVE_FLOW"),
        ),
    )


def _audit_wo31e4() -> ExpansionGateReport:
    """Run observable research, player routing, timer, and restart evidence."""

    from kirby2.audit.full_day import audit_wo31e4_research_restore
    from kirby2.full_day.composition import (
        RESEARCH_PROFILE_ID,
        executable_research_composition_matrix,
    )
    from kirby2.full_day.restore import FULL_DAY_RUNTIME_RESTORE_REQUEST_FORMAT_ID

    cases = audit_wo31e4_research_restore()
    expected_names = (
        "full_day_research_composition",
        "full_day_research_observable_decisions",
        "full_day_research_fresh_process_restore",
        "full_day_research_ownership_refusals",
    )
    failures: list[str] = []
    checks: list[ExpansionGateCheck] = []
    if tuple(case.name for case in cases) != expected_names:
        failures.append("WO31-E4 cases differ from the fixed evidence inventory")
    for case in cases:
        wrapper_failures: list[str] = []
        if not case.required:
            wrapper_failures.append("WO31-E4 cases must all be required")
        if case.status_override is not None:
            wrapper_failures.append(
                "WO31-E4 cases must report ordinary PASS/FAIL status"
            )
        failed = bool(case.failures or wrapper_failures)
        checks.append(
            ExpansionGateCheck(
                code=case.name,
                status=(
                    ExpansionGateStatus.FAIL
                    if failed
                    else ExpansionGateStatus.PASS
                ),
                detail=case.detail,
                required=True,
            )
        )
        failures.extend(f"{case.name}: {failure}" for failure in case.failures)
        failures.extend(
            f"{case.name}: {failure}" for failure in wrapper_failures
        )
    matrix = executable_research_composition_matrix()
    profile = matrix.profile(RESEARCH_PROFILE_ID, 1)
    return ExpansionGateReport(
        card_id="WO31-E4",
        status=(ExpansionGateStatus.FAIL if failures else ExpansionGateStatus.PASS),
        checks=tuple(checks),
        failures=tuple(failures),
        metadata=(
            ("composition_matrix_sha256", matrix.sha256),
            ("fresh_process_boundary_count", "3"),
            ("hostile_refusal_count", "7"),
            ("profile_id", profile.profile_id),
            ("profile_version", str(profile.profile_version)),
            ("restore_format", FULL_DAY_RUNTIME_RESTORE_REQUEST_FORMAT_ID),
            (
                "restored_scope",
                "CLIENT_FEATURES_STRATEGY_TIMERS_PLAYER_STATE",
            ),
        ),
    )


def _audit_wo31e5() -> ExpansionGateReport:
    """Run standalone multivenue/hidden restore and ownership evidence."""

    from kirby2.audit.full_day import audit_wo31e5_multivenue_restore
    from kirby2.full_day.composition import (
        MULTIVENUE_HIDDEN_PROFILE_ID,
        restorable_multivenue_hidden_composition_matrix,
    )
    from kirby2.full_day.restore import MULTIVENUE_HIDDEN_RESTORE_REQUEST_FORMAT_ID

    cases = audit_wo31e5_multivenue_restore()
    expected_names = (
        "full_day_multivenue_component_composition",
        "full_day_multivenue_checkpoint_privacy_and_conservation",
        "full_day_multivenue_fresh_process_restore",
        "full_day_multivenue_hostile_refusals",
    )
    failures: list[str] = []
    checks: list[ExpansionGateCheck] = []
    if tuple(case.name for case in cases) != expected_names:
        failures.append("WO31-E5 cases differ from the fixed evidence inventory")
    for case in cases:
        wrapper_failures: list[str] = []
        if not case.required:
            wrapper_failures.append("WO31-E5 cases must all be required")
        if case.status_override is not None:
            wrapper_failures.append(
                "WO31-E5 cases must report ordinary PASS/FAIL status"
            )
        failed = bool(case.failures or wrapper_failures)
        checks.append(
            ExpansionGateCheck(
                code=case.name,
                status=(
                    ExpansionGateStatus.FAIL
                    if failed
                    else ExpansionGateStatus.PASS
                ),
                detail=case.detail,
                required=True,
            )
        )
        failures.extend(f"{case.name}: {failure}" for failure in case.failures)
        failures.extend(
            f"{case.name}: {failure}" for failure in wrapper_failures
        )
    matrix = restorable_multivenue_hidden_composition_matrix()
    profile = matrix.profile(MULTIVENUE_HIDDEN_PROFILE_ID, 1)
    return ExpansionGateReport(
        card_id="WO31-E5",
        status=(ExpansionGateStatus.FAIL if failures else ExpansionGateStatus.PASS),
        checks=tuple(checks),
        failures=tuple(failures),
        metadata=(
            ("composition_matrix_sha256", matrix.sha256),
            ("fresh_process_boundary_count", "3"),
            ("hostile_refusal_count", "9"),
            ("profile_id", profile.profile_id),
            ("profile_status", profile.implementation_status),
            ("profile_version", str(profile.profile_version)),
            ("restore_format", MULTIVENUE_HIDDEN_RESTORE_REQUEST_FORMAT_ID),
            ("restored_scope", "STANDALONE_MULTIVENUE_HIDDEN_COMPONENT"),
        ),
    )


def _audit_wo31e6() -> ExpansionGateReport:
    """Run standalone execution-algorithm restore and refusal evidence."""

    from kirby2.audit.full_day import audit_wo31e6_execution_algorithm_restore
    from kirby2.full_day.composition import (
        EXECUTION_ALGORITHM_PROFILE_ID,
        restorable_execution_algorithm_composition_matrix,
    )
    from kirby2.full_day.restore import (
        EXECUTION_ALGORITHM_RESTORE_REQUEST_FORMAT_ID,
    )

    cases = audit_wo31e6_execution_algorithm_restore()
    expected_names = (
        "full_day_algorithm_component_composition",
        "full_day_algorithm_checkpoint_cutoff_and_conservation",
        "full_day_algorithm_fresh_process_restore",
        "full_day_algorithm_hostile_refusals",
    )
    failures: list[str] = []
    checks: list[ExpansionGateCheck] = []
    if tuple(case.name for case in cases) != expected_names:
        failures.append("WO31-E6 cases differ from the fixed evidence inventory")
    for case in cases:
        wrapper_failures: list[str] = []
        if not case.required:
            wrapper_failures.append("WO31-E6 cases must all be required")
        if case.status_override is not None:
            wrapper_failures.append(
                "WO31-E6 cases must report ordinary PASS/FAIL status"
            )
        failed = bool(case.failures or wrapper_failures)
        checks.append(
            ExpansionGateCheck(
                code=case.name,
                status=(
                    ExpansionGateStatus.FAIL
                    if failed
                    else ExpansionGateStatus.PASS
                ),
                detail=case.detail,
                required=True,
            )
        )
        failures.extend(f"{case.name}: {failure}" for failure in case.failures)
        failures.extend(
            f"{case.name}: {failure}" for failure in wrapper_failures
        )
    matrix = restorable_execution_algorithm_composition_matrix()
    profile = matrix.profile(EXECUTION_ALGORITHM_PROFILE_ID, 1)
    return ExpansionGateReport(
        card_id="WO31-E6",
        status=(ExpansionGateStatus.FAIL if failures else ExpansionGateStatus.PASS),
        checks=tuple(checks),
        failures=tuple(failures),
        metadata=(
            ("composition_matrix_sha256", matrix.sha256),
            ("fresh_process_boundary_count", "3"),
            ("hostile_refusal_count", "12"),
            ("profile_id", profile.profile_id),
            ("profile_status", profile.implementation_status),
            ("profile_version", str(profile.profile_version)),
            ("restore_format", EXECUTION_ALGORITHM_RESTORE_REQUEST_FORMAT_ID),
            ("restored_scope", "STANDALONE_EXECUTION_ALGORITHM_COMPONENT"),
        ),
    )


def _audit_wo31f() -> ExpansionGateReport:
    """Run complete executable-profile full-day composition evidence."""

    from kirby2.audit.full_day import audit_wo31f_composition
    from kirby2.full_day.checkpoint_contract import load_pilot_limits
    from kirby2.full_day.composition import (
        RESEARCH_PROFILE_ID,
        executable_research_composition_matrix,
    )

    cases = audit_wo31f_composition()
    expected_names = (
        "full_day_composition_profile_and_pilot",
        "full_day_participant_scheduled_shock_orchestration",
        "full_day_complete_bounded_day_replay_restore",
        "full_day_nonexecutable_and_hostile_refusals",
    )
    failures: list[str] = []
    checks: list[ExpansionGateCheck] = []
    if tuple(case.name for case in cases) != expected_names:
        failures.append("WO31-F cases differ from the fixed evidence inventory")
    for case in cases:
        wrapper_failures: list[str] = []
        if not case.required:
            wrapper_failures.append("WO31-F cases must all be required")
        if case.status_override is not None:
            wrapper_failures.append(
                "WO31-F cases must report ordinary PASS/FAIL status"
            )
        failed = bool(case.failures or wrapper_failures)
        checks.append(
            ExpansionGateCheck(
                code=case.name,
                status=(
                    ExpansionGateStatus.FAIL
                    if failed
                    else ExpansionGateStatus.PASS
                ),
                detail=case.detail,
                required=True,
            )
        )
        failures.extend(f"{case.name}: {failure}" for failure in case.failures)
        failures.extend(
            f"{case.name}: {failure}" for failure in wrapper_failures
        )
    matrix = executable_research_composition_matrix()
    profile = matrix.profile(RESEARCH_PROFILE_ID, 1)
    pilot = load_pilot_limits()
    return ExpansionGateReport(
        card_id="WO31-F",
        status=(ExpansionGateStatus.FAIL if failures else ExpansionGateStatus.PASS),
        checks=tuple(checks),
        failures=tuple(failures),
        metadata=(
            ("complete_day_count", "1"),
            ("composition_matrix_sha256", matrix.sha256),
            ("execution_algorithm", "NOT_EXERCISED"),
            ("historical_replay", "NOT_EXERCISED"),
            ("multivenue_hidden", "NOT_EXERCISED"),
            ("pilot_manifest_sha256", pilot.manifest_sha256),
            ("pilot_semantic_sha256", pilot.semantic_sha256),
            ("profile_id", profile.profile_id),
            ("profile_status", profile.implementation_status),
            ("profile_version", str(profile.profile_version)),
            ("short_executable_profile_count", "5"),
        ),
    )


def _audit_wo31g() -> ExpansionGateReport:
    """Run durable full-day storage, seek, extraction, and refusal evidence."""

    from kirby2.audit.full_day import audit_wo31g_storage

    cases = audit_wo31g_storage()
    expected_names = (
        "full_day_store_close_reopen_identity",
        "full_day_seek_boundaries_and_window_lineage",
        "full_day_summary_privacy_and_typed_catalog",
        "full_day_corrupt_escape_and_partial_refusals",
    )
    failures: list[str] = []
    checks: list[ExpansionGateCheck] = []
    if tuple(case.name for case in cases) != expected_names:
        failures.append("WO31-G cases differ from the fixed evidence inventory")
    for case in cases:
        wrapper_failures: list[str] = []
        if not case.required:
            wrapper_failures.append("WO31-G cases must all be required")
        if case.status_override is not None:
            wrapper_failures.append(
                "WO31-G cases must report ordinary PASS/FAIL status"
            )
        failed = bool(case.failures or wrapper_failures)
        checks.append(
            ExpansionGateCheck(
                code=case.name,
                status=(
                    ExpansionGateStatus.FAIL
                    if failed
                    else ExpansionGateStatus.PASS
                ),
                detail=case.detail,
                required=True,
            )
        )
        failures.extend(f"{case.name}: {failure}" for failure in case.failures)
        failures.extend(
            f"{case.name}: {failure}" for failure in wrapper_failures
        )
    return ExpansionGateReport(
        card_id="WO31-G",
        status=(ExpansionGateStatus.FAIL if failures else ExpansionGateStatus.PASS),
        checks=tuple(checks),
        failures=tuple(failures),
        metadata=(
            ("artifact_ledger", "RUN_MANIFEST_V2_TYPED_ARTIFACTS"),
            ("boundary_relation_count", "15"),
            ("public_latest_pointer", "ABSENT"),
            ("storage_activation", "FSYNC_THEN_ATOMIC_RENAME"),
            ("window_reveal_policy", "OBSERVABLE_CONTEXT_V1"),
        ),
    )


def _audit_wo31h() -> ExpansionGateReport:
    """Validate preregistered full-day profiles without running protected seeds."""

    from kirby2.audit.full_day import audit_wo31h_profiles
    from kirby2.full_day.profiles import load_full_day_profile_bundle

    cases = audit_wo31h_profiles()
    expected_names = (
        "full_day_profile_candidate_manifest_identity",
        "full_day_envelope_formula_and_review_preregistration",
        "full_day_performance_workload_and_platform_preregistration",
        "full_day_profile_manifest_hostile_refusals",
    )
    failures: list[str] = []
    checks: list[ExpansionGateCheck] = []
    if tuple(case.name for case in cases) != expected_names:
        failures.append("WO31-H cases differ from the fixed evidence inventory")
    for case in cases:
        wrapper_failures: list[str] = []
        if not case.required:
            wrapper_failures.append("WO31-H cases must all be required")
        if case.status_override is not None:
            wrapper_failures.append(
                "WO31-H cases must report ordinary PASS/FAIL status"
            )
        failed = bool(case.failures or wrapper_failures)
        checks.append(
            ExpansionGateCheck(
                code=case.name,
                status=(
                    ExpansionGateStatus.FAIL
                    if failed
                    else ExpansionGateStatus.PASS
                ),
                detail=case.detail,
                required=True,
            )
        )
        failures.extend(f"{case.name}: {failure}" for failure in case.failures)
        failures.extend(f"{case.name}: {failure}" for failure in wrapper_failures)
    bundle = load_full_day_profile_bundle()
    return ExpansionGateReport(
        card_id="WO31-H",
        status=(ExpansionGateStatus.FAIL if failures else ExpansionGateStatus.PASS),
        checks=tuple(checks),
        failures=tuple(failures),
        metadata=(
            ("automated_readiness", "NOT_EXERCISED"),
            ("bundle_sha256", bundle.bundle_sha256),
            ("candidate_count", "4"),
            ("holdout", "NOT_EXERCISED"),
            ("human_acceptance", "PENDING"),
            ("performance", "NOT_EXERCISED"),
            ("qualification", "NOT_EXERCISED"),
            ("review_selection", "NOT_EXERCISED"),
        ),
    )


def _audit_wo31i() -> ExpansionGateReport:
    """Exercise only disjoint development qualification evidence."""

    from kirby2.audit.full_day import audit_wo31i_qualification

    cases = audit_wo31i_qualification()
    expected_names = (
        "full_day_qualification_formula_and_disposition",
        "full_day_review_selection_and_blinding",
        "full_day_performance_platform_and_abort",
        "full_day_qualification_persistence_and_refusals",
    )
    failures: list[str] = []
    checks: list[ExpansionGateCheck] = []
    if tuple(case.name for case in cases) != expected_names:
        failures.append("WO31-I cases differ from the fixed evidence inventory")
    for case in cases:
        wrapper_failures: list[str] = []
        if not case.required:
            wrapper_failures.append("WO31-I cases must all be required")
        if case.status_override is not None:
            wrapper_failures.append("WO31-I cases must report ordinary PASS/FAIL status")
        failed = bool(case.failures or wrapper_failures)
        checks.append(
            ExpansionGateCheck(
                code=case.name,
                status=(
                    ExpansionGateStatus.FAIL if failed else ExpansionGateStatus.PASS
                ),
                detail=case.detail,
                required=True,
            )
        )
        failures.extend(f"{case.name}: {failure}" for failure in case.failures)
        failures.extend(f"{case.name}: {failure}" for failure in wrapper_failures)
    return ExpansionGateReport(
        card_id="WO31-I",
        status=(ExpansionGateStatus.FAIL if failures else ExpansionGateStatus.PASS),
        checks=tuple(checks),
        failures=tuple(failures),
        metadata=(
            ("development_fixture", "DISJOINT_DEVELOPMENT_ONLY"),
            ("human_acceptance", "PENDING"),
            ("protected_seed_access", "ABSENT"),
            ("real_execution_guard", "EXACT_CLEAN_COMMITTED_WO31_I_HEAD"),
            ("reentry", "VERIFY_ONLY_NEVER_RERUN"),
        ),
    )


def _audit_wo31i1() -> ExpansionGateReport:
    """Read the governed evidence root; never invoke qualification generation."""

    from kirby2.full_day.qualification import verify_qualification_evidence_root

    repository = Path(__file__).resolve().parents[2]
    evidence_root = repository / ".kirby2" / "full_day" / "qualification"
    report = verify_qualification_evidence_root(evidence_root)
    if report is None:
        return ExpansionGateReport(
            card_id="WO31-I1",
            status=ExpansionGateStatus.NOT_EXERCISED,
            checks=(
                ExpansionGateCheck(
                    code="full_day_profile_qualification_evidence",
                    status=ExpansionGateStatus.NOT_EXERCISED,
                    detail="immutable qualification evidence is absent; no workload was regenerated",
                    required=True,
                ),
            ),
            reason_code="QUALIFICATION_EVIDENCE_ABSENT",
            metadata=(
                ("evidence_root", ".kirby2/full_day/qualification"),
                ("generation_authority", "ABSENT_FROM_VALIDATOR"),
                ("human_acceptance", "PENDING"),
            ),
        )
    failures = report.failures
    return ExpansionGateReport(
        card_id="WO31-I1",
        status=(ExpansionGateStatus.FAIL if failures else ExpansionGateStatus.PASS),
        checks=(
            ExpansionGateCheck(
                code="full_day_profile_qualification_evidence",
                status=(
                    ExpansionGateStatus.FAIL
                    if failures
                    else ExpansionGateStatus.PASS
                ),
                detail=f"immutable evidence run_id={report.run_id} status={report.as_dict()['status']}",
                required=True,
            ),
        ),
        failures=failures,
        metadata=(
            ("evidence_root", ".kirby2/full_day/qualification"),
            ("generation_authority", "ABSENT_FROM_VALIDATOR"),
            ("human_acceptance", "PENDING"),
        ),
    )


def _audit_wo32a() -> ExpansionGateReport:
    """Run the contract-only scenario source, identity, and envelope audit."""

    from kirby2.audit.scenario_language import audit_wo32a_scenario_language
    from kirby2.scenario_lang.models import (
        SCENARIO_SOURCE_SECTION_NAMES,
        SCENARIO_TARGET_CONTRACTS_V1,
    )

    cases = audit_wo32a_scenario_language()
    expected_names = (
        "scenario_source_section_inventory",
        "scenario_source_canonical_roundtrip",
        "scenario_identity_domain_separation",
        "scenario_source_strict_refusals",
        "scenario_plan_native_envelopes",
        "scenario_source_immutable_ownership",
    )
    failures: list[str] = []
    checks: list[ExpansionGateCheck] = []
    if tuple(case.name for case in cases) != expected_names:
        failures.append("WO32-A cases differ from the fixed evidence inventory")
    for case in cases:
        wrapper_failures: list[str] = []
        if not case.required:
            wrapper_failures.append("WO32-A contract cases must all be required")
        failed = bool(case.failures or wrapper_failures)
        checks.append(
            ExpansionGateCheck(
                code=case.name,
                status=(
                    ExpansionGateStatus.FAIL
                    if failed
                    else ExpansionGateStatus.PASS
                ),
                detail=case.detail,
                required=True,
            )
        )
        failures.extend(f"{case.name}: {failure}" for failure in case.failures)
        failures.extend(
            f"{case.name}: {failure}" for failure in wrapper_failures
        )
    return ExpansionGateReport(
        card_id="WO32-A",
        status=(ExpansionGateStatus.FAIL if failures else ExpansionGateStatus.PASS),
        checks=tuple(checks),
        failures=tuple(failures),
        metadata=(
            ("identity_domain_count", "3"),
            ("runtime_execution", "OUTSIDE_WO32_A"),
            ("source_schema_version", "1"),
            ("source_section_count", str(len(SCENARIO_SOURCE_SECTION_NAMES))),
            ("strict_refusal_count", "13"),
            ("target_kind_count", str(len(SCENARIO_TARGET_CONTRACTS_V1))),
        ),
    )


def _audit_wo32b() -> ExpansionGateReport:
    """Run confined import and reusable-definition resolution evidence."""

    from kirby2.audit.scenario_language import (
        WO32B_DEFINITION_REFUSAL_COUNT,
        WO32B_IMPORT_REFUSAL_COUNT,
        audit_wo32b_scenario_language,
    )
    from kirby2.scenario_lang.models import ScenarioDefinitionTypeV1

    cases = audit_wo32b_scenario_language()
    expected_names = (
        "scenario_import_wo32a_contract_regression",
        "scenario_nested_import_relocation",
        "scenario_definition_inheritance_merge",
        "scenario_hostile_import_graph_refusals",
        "scenario_hostile_definition_refusals",
    )
    failures: list[str] = []
    checks: list[ExpansionGateCheck] = []
    if tuple(case.name for case in cases) != expected_names:
        failures.append("WO32-B cases differ from the fixed evidence inventory")
    for case in cases:
        wrapper_failures: list[str] = []
        if not case.required:
            wrapper_failures.append("WO32-B import cases must all be required")
        failed = bool(case.failures or wrapper_failures)
        checks.append(
            ExpansionGateCheck(
                code=case.name,
                status=(
                    ExpansionGateStatus.FAIL
                    if failed
                    else ExpansionGateStatus.PASS
                ),
                detail=case.detail,
                required=True,
            )
        )
        failures.extend(f"{case.name}: {failure}" for failure in case.failures)
        failures.extend(
            f"{case.name}: {failure}" for failure in wrapper_failures
        )
    return ExpansionGateReport(
        card_id="WO32-B",
        status=(ExpansionGateStatus.FAIL if failures else ExpansionGateStatus.PASS),
        checks=tuple(checks),
        failures=tuple(failures),
        metadata=(
            ("definition_refusal_count", str(WO32B_DEFINITION_REFUSAL_COUNT)),
            ("definition_type_count", str(len(ScenarioDefinitionTypeV1))),
            ("import_refusal_count", str(WO32B_IMPORT_REFUSAL_COUNT)),
            ("runtime_execution", "OUTSIDE_WO32_B"),
        ),
    )


def _audit_wo32c() -> ExpansionGateReport:
    """Run immutable compiler, target-registry, and seed-policy evidence."""

    from kirby2.audit.scenario_language import (
        WO32C_COMPILER_REFUSAL_COUNT,
        audit_wo32c_scenario_language,
    )
    from kirby2.scenario_lang.models import (
        SCENARIO_COMPILATION_PHASES_V1,
        ScenarioTargetKindV1,
    )

    cases = audit_wo32c_scenario_language()
    expected_names = (
        "scenario_compiler_phase_and_artifact_inventory",
        "scenario_compiler_determinism_and_ambient_independence",
        "scenario_compiler_seed_override_and_substreams",
        "scenario_target_registry_and_fail_closed_runtime",
        "scenario_compiler_hostile_refusals",
    )
    failures: list[str] = []
    checks: list[ExpansionGateCheck] = []
    if tuple(case.name for case in cases) != expected_names:
        failures.append("WO32-C cases differ from the fixed evidence inventory")
    for case in cases:
        wrapper_failures: list[str] = []
        if not case.required:
            wrapper_failures.append("WO32-C compiler cases must all be required")
        failed = bool(case.failures or wrapper_failures)
        checks.append(
            ExpansionGateCheck(
                code=case.name,
                status=(
                    ExpansionGateStatus.FAIL
                    if failed
                    else ExpansionGateStatus.PASS
                ),
                detail=case.detail,
                required=True,
            )
        )
        failures.extend(f"{case.name}: {failure}" for failure in case.failures)
        failures.extend(
            f"{case.name}: {failure}" for failure in wrapper_failures
        )
    return ExpansionGateReport(
        card_id="WO32-C",
        status=(ExpansionGateStatus.FAIL if failures else ExpansionGateStatus.PASS),
        checks=tuple(checks),
        failures=tuple(failures),
        metadata=(
            ("compiler_phase_count", str(len(SCENARIO_COMPILATION_PHASES_V1))),
            ("compiler_refusal_count", str(WO32C_COMPILER_REFUSAL_COUNT)),
            ("execution_eligible", "false"),
            ("target_kind_count", str(len(ScenarioTargetKindV1))),
            ("validator_status", "NOT_IMPLEMENTED_UNTIL_WO32_D"),
        ),
    )


def _audit_wo32d() -> ExpansionGateReport:
    """Run static validation, capability, and finalization evidence."""

    from kirby2.audit.scenario_language import (
        WO32D_FINALIZATION_REFUSAL_COUNT,
        WO32D_VALIDATION_FAMILY_COUNT,
        audit_wo32d_scenario_language,
    )
    from kirby2.scenario_lang.models import (
        SCENARIO_VALIDATION_REPORT_SCHEMA_VERSION,
        ScenarioTargetKindV1,
    )

    cases = audit_wo32d_scenario_language()
    expected_names = (
        "scenario_validation_wo32abc_regression",
        "scenario_validation_report_and_finalization",
        "scenario_validation_family_diagnostics",
        "scenario_validation_target_capability_matrix",
        "scenario_validation_required_unknown_and_refusals",
    )
    failures: list[str] = []
    checks: list[ExpansionGateCheck] = []
    if tuple(case.name for case in cases) != expected_names:
        failures.append("WO32-D cases differ from the fixed evidence inventory")
    for case in cases:
        wrapper_failures: list[str] = []
        if not case.required:
            wrapper_failures.append("WO32-D validation cases must all be required")
        failed = bool(case.failures or wrapper_failures)
        checks.append(
            ExpansionGateCheck(
                code=case.name,
                status=(
                    ExpansionGateStatus.FAIL
                    if failed
                    else ExpansionGateStatus.PASS
                ),
                detail=case.detail,
                required=True,
            )
        )
        failures.extend(f"{case.name}: {failure}" for failure in case.failures)
        failures.extend(
            f"{case.name}: {failure}" for failure in wrapper_failures
        )
    return ExpansionGateReport(
        card_id="WO32-D",
        status=(ExpansionGateStatus.FAIL if failures else ExpansionGateStatus.PASS),
        checks=tuple(checks),
        failures=tuple(failures),
        metadata=(
            ("finalization_refusal_count", str(WO32D_FINALIZATION_REFUSAL_COUNT)),
            ("required_unknown", "BLOCKS_EXECUTION"),
            ("target_kind_count", str(len(ScenarioTargetKindV1))),
            ("validation_family_count", str(WO32D_VALIDATION_FAMILY_COUNT)),
            (
                "validation_report_schema_version",
                str(SCENARIO_VALIDATION_REPORT_SCHEMA_VERSION),
            ),
        ),
    )


def _audit_wo32e() -> ExpansionGateReport:
    from kirby2.audit.scenario_language import (
        WO32E_EXPLAIN_SECTION_COUNT,
        WO32E_SOURCE_INVALID_ROOT_COUNT,
        WO32E_VALIDATION_INVALID_COUNT,
        WO32E_VALID_EXAMPLE_COUNT,
        audit_wo32e_scenario_language,
    )

    cases = audit_wo32e_scenario_language()
    expected_names = (
        "scenario_authoring_wo32abcd_regression",
        "scenario_authoring_command_registration",
        "scenario_authoring_six_example_runtime_matrix",
        "scenario_authoring_explain_and_semantic_diff",
        "scenario_authoring_hostile_diagnostics",
    )
    failures: list[str] = []
    checks: list[ExpansionGateCheck] = []
    if tuple(case.name for case in cases) != expected_names:
        failures.append("WO32-E cases differ from the fixed evidence inventory")
    for case in cases:
        wrapper_failures: list[str] = []
        if not case.required:
            wrapper_failures.append("WO32-E authoring cases must all be required")
        failed = bool(case.failures or wrapper_failures)
        checks.append(
            ExpansionGateCheck(
                code=case.name,
                status=(
                    ExpansionGateStatus.FAIL
                    if failed
                    else ExpansionGateStatus.PASS
                ),
                detail=case.detail,
                required=True,
            )
        )
        failures.extend(f"{case.name}: {failure}" for failure in case.failures)
        failures.extend(
            f"{case.name}: {failure}" for failure in wrapper_failures
        )
    return ExpansionGateReport(
        card_id="WO32-E",
        status=(ExpansionGateStatus.FAIL if failures else ExpansionGateStatus.PASS),
        checks=tuple(checks),
        failures=tuple(failures),
        metadata=(
            ("authoring_action_count", "5"),
            ("explain_section_count", str(WO32E_EXPLAIN_SECTION_COUNT)),
            ("source_invalid_root_count", str(WO32E_SOURCE_INVALID_ROOT_COUNT)),
            ("valid_example_count", str(WO32E_VALID_EXAMPLE_COUNT)),
            ("validation_invalid_count", str(WO32E_VALIDATION_INVALID_COUNT)),
        ),
    )


def _audit_wo33a() -> ExpansionGateReport:
    from kirby2.audit.drill_mining import (
        WO33A_DETECTOR_COUNT,
        WO33A_IDENTITY_KEY_COUNT,
        WO33A_REVIEW_DECISION_COUNT,
        WO33A_SKILL_COUNT,
        audit_drill_mining,
    )

    cases = audit_drill_mining()
    expected_names = (
        "lesson_candidate_exact_identity_and_ancestry",
        "candidate_boundaries_and_review_sidecars_are_separate",
        "candidate_evidence_records_are_content_addressed_and_access_controlled",
        "stable_versioned_skills_and_typed_objectives",
        "detector_capabilities_fail_closed_as_not_exercised",
        "assessment_is_blind_and_historical_lessons_regress",
    )
    failures: list[str] = []
    checks: list[ExpansionGateCheck] = []
    if tuple(case.name for case in cases) != expected_names:
        failures.append("WO33-A cases differ from the fixed evidence inventory")
    for case in cases:
        wrapper_failures: list[str] = []
        if not case.required:
            wrapper_failures.append("WO33-A contract cases must all be required")
        failed = bool(case.failures or wrapper_failures)
        checks.append(
            ExpansionGateCheck(
                code=case.name,
                status=(
                    ExpansionGateStatus.FAIL
                    if failed
                    else ExpansionGateStatus.PASS
                ),
                detail=case.detail,
                required=True,
            )
        )
        failures.extend(f"{case.name}: {failure}" for failure in case.failures)
        failures.extend(
            f"{case.name}: {failure}" for failure in wrapper_failures
        )
    return ExpansionGateReport(
        card_id="WO33-A",
        status=(ExpansionGateStatus.FAIL if failures else ExpansionGateStatus.PASS),
        checks=tuple(checks),
        failures=tuple(failures),
        metadata=(
            ("detector_count", str(WO33A_DETECTOR_COUNT)),
            ("identity_key_count", str(WO33A_IDENTITY_KEY_COUNT)),
            ("review_decision_count", str(WO33A_REVIEW_DECISION_COUNT)),
            ("skill_count", str(WO33A_SKILL_COUNT)),
        ),
    )


def _audit_wo33a1() -> ExpansionGateReport:
    from kirby2.audit.drill_mining import (
        WO33A1_MINING_PLAN_MANIFEST_SHA256,
        WO33A1_POLICY_BUNDLE_SHA256,
        WO33A1_REVIEW_TARGET_COUNT,
        WO33A1_SOURCE_COUNT,
        WO33A1_SOURCE_MANIFEST_SHA256,
        WO33A1_THRESHOLD_MANIFEST_SHA256,
        audit_wo33a1_drill_mining,
    )

    cases = audit_wo33a1_drill_mining()
    expected_names = (
        "mining_detector_thresholds_are_complete_operational_and_digest_bound",
        "mining_difficulty_sampling_and_shortfall_are_preregistered",
        "mining_dedup_diversity_and_review_sampling_are_preregistered",
        "mining_five_source_matrix_resolves_exact_bytes_bounds_and_capabilities",
        "mining_source_replay_identities_verify_without_protected_regeneration",
        "mining_preregistration_is_unexercised_and_hostile_mutations_fail_closed",
    )
    failures: list[str] = []
    checks: list[ExpansionGateCheck] = []
    if tuple(case.name for case in cases) != expected_names:
        failures.append("WO33-A1 cases differ from the fixed evidence inventory")
    for case in cases:
        wrapper_failures: list[str] = []
        if not case.required:
            wrapper_failures.append("WO33-A1 preregistration cases must all be required")
        failed = bool(case.failures or wrapper_failures)
        checks.append(
            ExpansionGateCheck(
                code=case.name,
                status=(
                    ExpansionGateStatus.FAIL
                    if failed
                    else ExpansionGateStatus.PASS
                ),
                detail=case.detail,
                required=True,
            )
        )
        failures.extend(f"{case.name}: {failure}" for failure in case.failures)
        failures.extend(
            f"{case.name}: {failure}" for failure in wrapper_failures
        )
    return ExpansionGateReport(
        card_id="WO33-A1",
        status=(ExpansionGateStatus.FAIL if failures else ExpansionGateStatus.PASS),
        checks=tuple(checks),
        failures=tuple(failures),
        metadata=(
            ("detector_count", "22"),
            ("mining_plan_manifest_sha256", WO33A1_MINING_PLAN_MANIFEST_SHA256),
            ("policy_bundle_sha256", WO33A1_POLICY_BUNDLE_SHA256),
            ("qualification_source_count", str(WO33A1_SOURCE_COUNT)),
            ("qualification_sources_manifest_sha256", WO33A1_SOURCE_MANIFEST_SHA256),
            ("review_target_count", str(WO33A1_REVIEW_TARGET_COUNT)),
            ("threshold_manifest_sha256", WO33A1_THRESHOLD_MANIFEST_SHA256),
        ),
    )


def _audit_wo33b1() -> ExpansionGateReport:
    from kirby2.audit.drill_mining import (
        WO33B1_DETECTOR_COUNT,
        WO33B1_SYNTHETIC_REPORT_SHA256,
        audit_wo33b1_drill_mining,
    )
    from kirby2.mining.runtime import WO33A1_THRESHOLD_MANIFEST_SHA256_V1

    cases = audit_wo33b1_drill_mining()
    expected_names = (
        "b1_fifteen_distinct_handlers_consume_the_committed_a1_manifest",
        "b1_synthetic_boundaries_activate_every_detector_with_explicit_denominators",
        "b1_weak_historical_sources_refuse_and_reconstruction_stays_synthetic",
        "b1_canonical_order_is_storage_independent_and_exclusions_are_recorded",
        "b1_manifest_schema_sampling_measurement_and_event_mutations_fail_closed",
    )
    failures: list[str] = []
    checks: list[ExpansionGateCheck] = []
    if tuple(case.name for case in cases) != expected_names:
        failures.append("WO33-B1 cases differ from the fixed evidence inventory")
    for case in cases:
        wrapper_failures: list[str] = []
        if not case.required:
            wrapper_failures.append("WO33-B1 detector cases must all be required")
        failed = bool(case.failures or wrapper_failures)
        checks.append(
            ExpansionGateCheck(
                code=case.name,
                status=(
                    ExpansionGateStatus.FAIL
                    if failed
                    else ExpansionGateStatus.PASS
                ),
                detail=case.detail,
                required=True,
            )
        )
        failures.extend(f"{case.name}: {failure}" for failure in case.failures)
        failures.extend(
            f"{case.name}: {failure}" for failure in wrapper_failures
        )
    return ExpansionGateReport(
        card_id="WO33-B1",
        status=(ExpansionGateStatus.FAIL if failures else ExpansionGateStatus.PASS),
        checks=tuple(checks),
        failures=tuple(failures),
        metadata=(
            ("detector_count", str(WO33B1_DETECTOR_COUNT)),
            ("synthetic_report_sha256", WO33B1_SYNTHETIC_REPORT_SHA256),
            (
                "threshold_manifest_sha256",
                WO33A1_THRESHOLD_MANIFEST_SHA256_V1,
            ),
        ),
    )


def _audit_wo33b2() -> ExpansionGateReport:
    from kirby2.audit.drill_mining import (
        WO33B2_DETECTOR_COUNT,
        WO33B2_SYNTHETIC_REPORT_SHA256,
        audit_wo33b2_drill_mining,
    )
    from kirby2.mining.runtime import WO33A1_THRESHOLD_MANIFEST_SHA256_V1

    cases = audit_wo33b2_drill_mining()
    expected_names = (
        "b2_seven_distinct_handlers_extend_the_closed_runtime_and_bind_a1",
        "b2_exact_synthetic_boundaries_activate_all_detectors_and_or_branches",
        "b2_every_missing_capability_and_weak_historical_source_is_not_exercised",
        "b2_order_ancestry_labels_and_original_decision_timing_are_preserved",
        "b2_witness_schema_timing_key_and_incomplete_evidence_mutations_fail_closed",
    )
    failures: list[str] = []
    checks: list[ExpansionGateCheck] = []
    if tuple(case.name for case in cases) != expected_names:
        failures.append("WO33-B2 cases differ from the fixed evidence inventory")
    for case in cases:
        wrapper_failures: list[str] = []
        if not case.required:
            wrapper_failures.append("WO33-B2 detector cases must all be required")
        failed = bool(case.failures or wrapper_failures)
        checks.append(
            ExpansionGateCheck(
                code=case.name,
                status=(
                    ExpansionGateStatus.FAIL
                    if failed
                    else ExpansionGateStatus.PASS
                ),
                detail=case.detail,
                required=True,
            )
        )
        failures.extend(f"{case.name}: {failure}" for failure in case.failures)
        failures.extend(
            f"{case.name}: {failure}" for failure in wrapper_failures
        )
    return ExpansionGateReport(
        card_id="WO33-B2",
        status=(ExpansionGateStatus.FAIL if failures else ExpansionGateStatus.PASS),
        checks=tuple(checks),
        failures=tuple(failures),
        metadata=(
            ("detector_count", str(WO33B2_DETECTOR_COUNT)),
            ("synthetic_report_sha256", WO33B2_SYNTHETIC_REPORT_SHA256),
            (
                "threshold_manifest_sha256",
                WO33A1_THRESHOLD_MANIFEST_SHA256_V1,
            ),
        ),
    )


def _audit_wo33c() -> ExpansionGateReport:
    from kirby2.audit.drill_mining import (
        WO33C_DIFFICULTY_COMPONENT_COUNT,
        WO33C_DIVERSITY_DIMENSION_COUNT,
        WO33C_REVIEW_TARGET_COUNT,
        audit_wo33c_drill_mining,
    )

    cases = audit_wo33c_drill_mining()
    expected_names = (
        "c_difficulty_is_transparent_fixed_point_and_rarity_requires_reference",
        "c_ranking_is_permutation_stable_visible_and_not_rarity_optimized",
        "c_semantic_deduplication_is_threshold_exact_and_one_pass_greedy",
        "c_preregistered_greedy_selection_is_deterministic_and_more_diverse",
        "c_quota_pressure_reports_shortfall_without_weakening_or_duplicates",
    )
    failures: list[str] = []
    checks: list[ExpansionGateCheck] = []
    if tuple(case.name for case in cases) != expected_names:
        failures.append("WO33-C cases differ from the fixed evidence inventory")
    for case in cases:
        wrapper_failures: list[str] = []
        if not case.required:
            wrapper_failures.append("WO33-C ranking cases must all be required")
        failed = bool(case.failures or wrapper_failures)
        checks.append(
            ExpansionGateCheck(
                code=case.name,
                status=(
                    ExpansionGateStatus.FAIL
                    if failed
                    else ExpansionGateStatus.PASS
                ),
                detail=case.detail,
                required=True,
            )
        )
        failures.extend(f"{case.name}: {failure}" for failure in case.failures)
        failures.extend(
            f"{case.name}: {failure}" for failure in wrapper_failures
        )
    return ExpansionGateReport(
        card_id="WO33-C",
        status=(ExpansionGateStatus.FAIL if failures else ExpansionGateStatus.PASS),
        checks=tuple(checks),
        failures=tuple(failures),
        metadata=(
            (
                "difficulty_component_count",
                str(WO33C_DIFFICULTY_COMPONENT_COUNT),
            ),
            (
                "diversity_dimension_count",
                str(WO33C_DIVERSITY_DIMENSION_COUNT),
            ),
            ("estimate_state", "UNVALIDATED_ESTIMATE"),
            ("review_target_count", str(WO33C_REVIEW_TARGET_COUNT)),
        ),
    )


def _audit_wo33d() -> ExpansionGateReport:
    from kirby2.audit.drill_mining import (
        WO33D_ASSESSMENT_FIELD_COUNT,
        WO33D_SOURCE_LINEAGE_FIELD_COUNT,
        audit_wo33d_drill_mining,
    )

    cases = audit_wo33d_drill_mining()
    expected_names = (
        "d_seven_field_lineage_and_exact_recorded_feed_prefix_parity",
        "d_warmup_snapshot_and_client_delivery_cut_are_information_fair",
        "d_closed_blind_surface_and_completed_assessment_reveal_grant",
        "d_same_source_candidate_and_cuts_replay_byte_identically",
        "d_player_actions_are_deterministic_parent_linked_overlays_not_history",
    )
    failures: list[str] = []
    checks: list[ExpansionGateCheck] = []
    if tuple(case.name for case in cases) != expected_names:
        failures.append("WO33-D cases differ from the fixed evidence inventory")
    for case in cases:
        wrapper_failures: list[str] = []
        if not case.required:
            wrapper_failures.append("WO33-D extraction cases must all be required")
        failed = bool(case.failures or wrapper_failures)
        checks.append(
            ExpansionGateCheck(
                code=case.name,
                status=(
                    ExpansionGateStatus.FAIL
                    if failed
                    else ExpansionGateStatus.PASS
                ),
                detail=case.detail,
                required=True,
            )
        )
        failures.extend(f"{case.name}: {failure}" for failure in case.failures)
        failures.extend(
            f"{case.name}: {failure}" for failure in wrapper_failures
        )
    return ExpansionGateReport(
        card_id="WO33-D",
        status=(ExpansionGateStatus.FAIL if failures else ExpansionGateStatus.PASS),
        checks=tuple(checks),
        failures=tuple(failures),
        metadata=(
            (
                "assessment_field_count",
                str(WO33D_ASSESSMENT_FIELD_COUNT),
            ),
            ("observable_feed_policy", "RECORDED_CLIENT_FEED_EXACT_V1"),
            (
                "source_lineage_field_count",
                str(WO33D_SOURCE_LINEAGE_FIELD_COUNT),
            ),
        ),
    )


def _audit_wo33e() -> ExpansionGateReport:
    from kirby2.audit.drill_mining import (
        WO33E_CANDIDATE_COUNT,
        WO33E_DETECTOR_FINDING_COUNT,
        WO33E_DETECTOR_OPPORTUNITY_COUNT,
        WO33E_DETECTOR_REPORT_COUNT,
        WO33E_EVENT_DISTINCT_COUNT,
        WO33E_MINING_ARTIFACT_COUNT,
        WO33E_REVIEW_PACKET_COUNT,
        WO33E_REVIEW_SHORTFALL_COUNT,
        WO33E_SOURCE_COUNT,
        audit_wo33e_drill_mining,
    )

    cases = audit_wo33e_drill_mining()
    expected_names = (
        "e_exact_five_source_materialization_and_replay_identity",
        "e_runtime_findings_form_truthful_shortfall_packet",
        "e_typed_persistence_reopens_with_byte_identical_selection",
        "e_review_sidecars_are_immutable_chained_and_authority_gated",
        "e_cli_builds_proposals_without_claiming_human_acceptance",
    )
    failures: list[str] = []
    checks: list[ExpansionGateCheck] = []
    if tuple(case.name for case in cases) != expected_names:
        failures.append("WO33-E cases differ from the fixed evidence inventory")
    for case in cases:
        wrapper_failures: list[str] = []
        if not case.required:
            wrapper_failures.append("WO33-E workflow cases must all be required")
        failed = bool(case.failures or wrapper_failures)
        checks.append(
            ExpansionGateCheck(
                code=case.name,
                status=(
                    ExpansionGateStatus.FAIL if failed else ExpansionGateStatus.PASS
                ),
                detail=case.detail,
                required=True,
            )
        )
        failures.extend(f"{case.name}: {failure}" for failure in case.failures)
        failures.extend(
            f"{case.name}: {failure}" for failure in wrapper_failures
        )
    return ExpansionGateReport(
        card_id="WO33-E",
        status=(ExpansionGateStatus.FAIL if failures else ExpansionGateStatus.PASS),
        checks=tuple(checks),
        failures=tuple(failures),
        metadata=(
            ("actual_human_inspection", "PENDING"),
            ("candidate_count", str(WO33E_CANDIDATE_COUNT)),
            ("detector_finding_count", str(WO33E_DETECTOR_FINDING_COUNT)),
            (
                "detector_opportunity_count",
                str(WO33E_DETECTOR_OPPORTUNITY_COUNT),
            ),
            ("detector_report_count", str(WO33E_DETECTOR_REPORT_COUNT)),
            ("event_materially_distinct_count", str(WO33E_EVENT_DISTINCT_COUNT)),
            ("five_accepted_lessons", "PENDING"),
            ("mining_artifact_count", str(WO33E_MINING_ARTIFACT_COUNT)),
            ("review_packet_count", str(WO33E_REVIEW_PACKET_COUNT)),
            ("review_shortfall_count", str(WO33E_REVIEW_SHORTFALL_COUNT)),
            ("source_count", str(WO33E_SOURCE_COUNT)),
        ),
    )


def _audit_wo34a() -> ExpansionGateReport:
    from kirby2.audit.adaptive_curriculum import (
        WO34A_EDGE_COUNT,
        WO34A_ERROR_COUNT,
        WO34A_EVIDENCE_FAMILY_COUNT,
        WO34A_LEGACY_LESSON_COUNT,
        WO34A_ROOT_COUNT,
        WO34A_SKILL_COUNT,
        WO34A_SKILL_GRAPH_SHA256,
        audit_wo34a_adaptive_curriculum,
    )

    cases = audit_wo34a_adaptive_curriculum()
    expected_names = (
        "a_skill_graph_is_exact_acyclic_content_addressed_and_uniform",
        "a_error_vocabulary_mappings_priority_and_caps_are_closed",
        "a_attempt_evidence_is_canonical_append_only_and_policy_bound",
        "a_inaction_requires_proof_and_ambiguity_or_pnl_cannot_create_mastery",
        "a_legacy_lessons_map_one_primary_skill_without_changing_blind_modes",
    )
    failures: list[str] = []
    checks: list[ExpansionGateCheck] = []
    if tuple(case.name for case in cases) != expected_names:
        failures.append("WO34-A cases differ from the fixed evidence inventory")
    for case in cases:
        wrapper_failures: list[str] = []
        if not case.required:
            wrapper_failures.append("WO34-A evidence cases must all be required")
        failed = bool(case.failures or wrapper_failures)
        checks.append(
            ExpansionGateCheck(
                code=case.name,
                status=(
                    ExpansionGateStatus.FAIL if failed else ExpansionGateStatus.PASS
                ),
                detail=case.detail,
                required=True,
            )
        )
        failures.extend(f"{case.name}: {failure}" for failure in case.failures)
        failures.extend(
            f"{case.name}: {failure}" for failure in wrapper_failures
        )
    return ExpansionGateReport(
        card_id="WO34-A",
        status=(ExpansionGateStatus.FAIL if failures else ExpansionGateStatus.PASS),
        checks=tuple(checks),
        failures=tuple(failures),
        metadata=(
            ("edge_count", str(WO34A_EDGE_COUNT)),
            ("error_count", str(WO34A_ERROR_COUNT)),
            ("evidence_family_count", str(WO34A_EVIDENCE_FAMILY_COUNT)),
            ("learner_projection", "IMPLEMENTED_BY_WO34_B"),
            ("legacy_lesson_count", str(WO34A_LEGACY_LESSON_COUNT)),
            ("root_count", str(WO34A_ROOT_COUNT)),
            ("skill_count", str(WO34A_SKILL_COUNT)),
            ("skill_graph_sha256", WO34A_SKILL_GRAPH_SHA256),
        ),
    )


def _audit_wo34b() -> ExpansionGateReport:
    from kirby2.audit.adaptive_curriculum import (
        WO34B_AUDIT_CASE_COUNT,
        WO34B_PROJECTION_POLICY_SHA256,
        WO34B_SKILL_PROJECTION_COUNT,
        audit_wo34b_adaptive_curriculum,
    )
    from kirby2.curriculum.projections import (
        LEARNER_PROJECTION_MODEL_ID_V1,
        LEARNER_PROJECTION_STATUS_V1,
        RECENT_HISTORY_LIMIT_V1,
    )

    cases = audit_wo34b_adaptive_curriculum()
    expected_names = (
        "b_policy_equations_and_empty_prior_are_exact",
        "b_error_caps_modes_and_recency_are_exact",
        "b_confidence_diversity_sufficiency_and_history_are_exact",
        "b_rebuild_is_prefix_order_clock_and_version_deterministic",
        "b_zero_weight_and_pnl_cannot_update_projection",
    )
    failures: list[str] = []
    checks: list[ExpansionGateCheck] = []
    if len(cases) != WO34B_AUDIT_CASE_COUNT or tuple(
        case.name for case in cases
    ) != expected_names:
        failures.append("WO34-B cases differ from the fixed projection inventory")
    for case in cases:
        wrapper_failures: list[str] = []
        if not case.required:
            wrapper_failures.append("WO34-B projection cases must all be required")
        failed = bool(case.failures or wrapper_failures)
        checks.append(
            ExpansionGateCheck(
                code=case.name,
                status=(
                    ExpansionGateStatus.FAIL if failed else ExpansionGateStatus.PASS
                ),
                detail=case.detail,
                required=True,
            )
        )
        failures.extend(f"{case.name}: {failure}" for failure in case.failures)
        failures.extend(
            f"{case.name}: {failure}" for failure in wrapper_failures
        )
    return ExpansionGateReport(
        card_id="WO34-B",
        status=(ExpansionGateStatus.FAIL if failures else ExpansionGateStatus.PASS),
        checks=tuple(checks),
        failures=tuple(failures),
        metadata=(
            ("audit_case_count", str(WO34B_AUDIT_CASE_COUNT)),
            ("learner_projection_model_id", LEARNER_PROJECTION_MODEL_ID_V1),
            ("model_status", LEARNER_PROJECTION_STATUS_V1),
            ("projection_policy_sha256", WO34B_PROJECTION_POLICY_SHA256),
            ("recent_history_limit", str(RECENT_HISTORY_LIMIT_V1)),
            ("skill_projection_count", str(WO34B_SKILL_PROJECTION_COUNT)),
        ),
    )


def _audit_wo34c() -> ExpansionGateReport:
    from kirby2.audit.adaptive_curriculum import (
        WO34C_AUDIT_CASE_COUNT,
        WO34C_SELECTION_POLICY_SHA256,
        audit_wo34c_adaptive_curriculum,
    )
    from kirby2.curriculum.adaptive_modes import (
        ASSESSMENT_BATCH_SIZE_V1,
        ASSESSMENT_PASS_SCORE_PPM_V1,
    )
    from kirby2.curriculum.selection import (
        CURRICULUM_SELECTION_MODEL_STATUS_V1,
        CURRICULUM_SELECTION_POLICY_SHA256_V1,
        SELECTION_COMPONENT_WEIGHTS_PPM_V1,
        SELECTION_COOLDOWN_WINDOWS_V1,
    )

    cases = audit_wo34c_adaptive_curriculum()
    expected_names = (
        "c_modes_policy_catalog_and_legacy_compatibility_are_fixed",
        "c_cold_start_ranking_prerequisites_and_seeded_ties_are_exact",
        "c_prerequisite_readiness_cooldowns_and_missing_metadata_fail_closed",
        "c_manual_plan_precedence_immutability_and_refusals_are_explicit",
        "c_remediation_uses_latest_ten_fixed_error_priority_without_fallback",
        "c_assessment_freeze_scoring_anti_memorization_and_reveal_are_fixed",
    )
    failures: list[str] = []
    checks: list[ExpansionGateCheck] = []
    if (
        len(cases) != WO34C_AUDIT_CASE_COUNT
        or tuple(case.name for case in cases) != expected_names
    ):
        failures.append("WO34-C cases differ from the fixed selection inventory")
    if CURRICULUM_SELECTION_POLICY_SHA256_V1 != WO34C_SELECTION_POLICY_SHA256:
        failures.append("WO34-C selection policy digest differs")
    for case in cases:
        wrapper_failures: list[str] = []
        if not case.required:
            wrapper_failures.append("WO34-C selection cases must all be required")
        failed = bool(case.failures or wrapper_failures)
        checks.append(
            ExpansionGateCheck(
                code=case.name,
                status=(
                    ExpansionGateStatus.FAIL if failed else ExpansionGateStatus.PASS
                ),
                detail=case.detail,
                required=True,
            )
        )
        failures.extend(f"{case.name}: {failure}" for failure in case.failures)
        failures.extend(
            f"{case.name}: {failure}" for failure in wrapper_failures
        )
    return ExpansionGateReport(
        card_id="WO34-C",
        status=(ExpansionGateStatus.FAIL if failures else ExpansionGateStatus.PASS),
        checks=tuple(checks),
        failures=tuple(failures),
        metadata=(
            ("assessment_batch_size", str(ASSESSMENT_BATCH_SIZE_V1)),
            ("assessment_pass_score_ppm", str(ASSESSMENT_PASS_SCORE_PPM_V1)),
            ("audit_case_count", str(WO34C_AUDIT_CASE_COUNT)),
            ("cooldown_dimension_count", str(len(SELECTION_COOLDOWN_WINDOWS_V1))),
            ("model_status", CURRICULUM_SELECTION_MODEL_STATUS_V1),
            ("ranking_component_count", str(len(SELECTION_COMPONENT_WEIGHTS_PPM_V1))),
            ("selection_policy_sha256", WO34C_SELECTION_POLICY_SHA256),
        ),
    )


def _audit_wo34d() -> ExpansionGateReport:
    from kirby2.audit.adaptive_curriculum import (
        WO34D_AUDIT_CASE_COUNT,
        WO34D_DEMO_SHA256,
        WO34D_SYNTHETIC_LEARNER_COUNT,
        audit_wo34d_adaptive_curriculum,
    )
    from kirby2.curriculum.adaptive_commands import (
        ADAPTIVE_CURRICULUM_DEMO_SEQUENCE_LENGTH_V1,
        ADAPTIVE_ROUTING_CLAIM_V1,
        CROSS_LEARNER_COMPARISON_POLICY_V1,
    )
    from kirby2.curriculum.projections import LEARNER_PROJECTION_STATUS_V1

    cases = audit_wo34d_adaptive_curriculum()
    expected_names = (
        "d_six_evidence_only_fixtures_route_to_distinct_sequences",
        "d_update_projection_selection_and_replay_chain_is_exact",
        "d_typed_learner_update_and_projection_artifacts_rebuild_and_fail_closed",
        "d_claims_remain_unvalidated_and_cross_learner_scores_are_not_compared",
    )
    failures: list[str] = []
    checks: list[ExpansionGateCheck] = []
    if (
        len(cases) != WO34D_AUDIT_CASE_COUNT
        or tuple(case.name for case in cases) != expected_names
    ):
        failures.append("WO34-D cases differ from the fixed demonstration inventory")
    for case in cases:
        wrapper_failures: list[str] = []
        if not case.required:
            wrapper_failures.append("WO34-D demonstration cases must all be required")
        failed = bool(case.failures or wrapper_failures)
        checks.append(
            ExpansionGateCheck(
                code=case.name,
                status=(
                    ExpansionGateStatus.FAIL if failed else ExpansionGateStatus.PASS
                ),
                detail=case.detail,
                required=True,
            )
        )
        failures.extend(f"{case.name}: {failure}" for failure in case.failures)
        failures.extend(
            f"{case.name}: {failure}" for failure in wrapper_failures
        )
    return ExpansionGateReport(
        card_id="WO34-D",
        status=(ExpansionGateStatus.FAIL if failures else ExpansionGateStatus.PASS),
        checks=tuple(checks),
        failures=tuple(failures),
        metadata=(
            ("audit_case_count", str(WO34D_AUDIT_CASE_COUNT)),
            ("claim_scope", ADAPTIVE_ROUTING_CLAIM_V1),
            ("comparison_policy", CROSS_LEARNER_COMPARISON_POLICY_V1),
            ("demo_sha256", WO34D_DEMO_SHA256),
            ("model_status", LEARNER_PROJECTION_STATUS_V1),
            (
                "selection_steps",
                str(
                    WO34D_SYNTHETIC_LEARNER_COUNT
                    * ADAPTIVE_CURRICULUM_DEMO_SEQUENCE_LENGTH_V1
                ),
            ),
            ("synthetic_learner_count", str(WO34D_SYNTHETIC_LEARNER_COUNT)),
        ),
    )


def _audit_wo35a() -> ExpansionGateReport:
    from kirby2.audit.strategy_discovery import (
        WO35A_AUDIT_CASE_COUNT,
        WO35A_FIXTURE_SHA256,
        audit_wo35a_strategy_discovery,
    )
    from kirby2.discovery.ast import STRATEGY_AST_SCHEMA_ID_V1
    from kirby2.discovery.identity import (
        STRATEGY_CANONICALIZATION_POLICY_SHA256_V1,
        STRATEGY_IDENTITY_MIGRATION_ID_V1,
    )
    from kirby2.discovery.lineage import STRATEGY_LINEAGE_SCHEMA_ID_V1

    cases = audit_wo35a_strategy_discovery()
    expected_names = (
        "a_parse_render_parse_is_semantically_stable_for_both_grammars",
        "a_supported_equivalences_collapse_but_transition_priority_remains_semantic",
        "a_legacy_source_and_semantic_ast_identities_remain_separate_and_inspectable",
        "a_lineage_binds_parent_operation_parameters_rng_child_validity_and_diff",
        "a_unsupported_grammar_fails_deterministically_before_mutation",
    )
    failures: list[str] = []
    checks: list[ExpansionGateCheck] = []
    if (
        len(cases) != WO35A_AUDIT_CASE_COUNT
        or tuple(case.name for case in cases) != expected_names
    ):
        failures.append("WO35-A cases differ from the fixed strategy identity inventory")
    for case in cases:
        wrapper_failures: list[str] = []
        if not case.required:
            wrapper_failures.append("WO35-A strategy identity cases must all be required")
        failed = bool(case.failures or wrapper_failures)
        checks.append(
            ExpansionGateCheck(
                code=case.name,
                status=(
                    ExpansionGateStatus.FAIL if failed else ExpansionGateStatus.PASS
                ),
                detail=case.detail,
                required=True,
            )
        )
        failures.extend(f"{case.name}: {failure}" for failure in case.failures)
        failures.extend(
            f"{case.name}: {failure}" for failure in wrapper_failures
        )
    return ExpansionGateReport(
        card_id="WO35-A",
        status=(ExpansionGateStatus.FAIL if failures else ExpansionGateStatus.PASS),
        checks=tuple(checks),
        failures=tuple(failures),
        metadata=(
            ("ast_schema_id", STRATEGY_AST_SCHEMA_ID_V1),
            ("audit_case_count", str(WO35A_AUDIT_CASE_COUNT)),
            (
                "canonicalization_policy_sha256",
                STRATEGY_CANONICALIZATION_POLICY_SHA256_V1,
            ),
            ("fixture_sha256", WO35A_FIXTURE_SHA256),
            ("identity_migration_id", STRATEGY_IDENTITY_MIGRATION_ID_V1),
            ("lineage_schema_id", STRATEGY_LINEAGE_SCHEMA_ID_V1),
            ("mutation_execution", "NOT_IMPLEMENTED_BY_WO35_A"),
        ),
    )


def _audit_wo35b() -> ExpansionGateReport:
    from kirby2.audit.strategy_discovery import (
        WO35B_ACCESS_POLICY_SHA256,
        WO35B_AUDIT_CASE_COUNT,
        WO35B_FIXTURE_SHA256,
        audit_wo35b_strategy_partitions,
    )
    from kirby2.discovery.access import PARTITION_ACCESS_SCHEMA_ID_V1
    from kirby2.discovery.experiment import EXPERIMENT_STATE_SCHEMA_ID_V1
    from kirby2.discovery.partitions import (
        PARTITION_MANIFEST_SCHEMA_ID_V1,
        PARTITION_MANIFEST_SCHEMA_VERSION_V1,
    )

    cases = audit_wo35b_strategy_partitions()
    expected_names = (
        "b_partition_manifest_is_canonical_sealed_and_ancestry_disjoint",
        "b_search_access_obeys_predeclared_validation_schedule",
        "b_candidate_freeze_and_one_shot_reveal_are_terminal",
        "b_successor_requires_new_untouched_terminal_partitions",
        "b_research_store_persists_immutable_audit_visible_access",
    )
    failures: list[str] = []
    checks: list[ExpansionGateCheck] = []
    if (
        len(cases) != WO35B_AUDIT_CASE_COUNT
        or tuple(case.name for case in cases) != expected_names
    ):
        failures.append("WO35-B cases differ from the fixed partition evidence inventory")
    for case in cases:
        wrapper_failures: list[str] = []
        if not case.required:
            wrapper_failures.append("WO35-B partition cases must all be required")
        failed = bool(case.failures or wrapper_failures)
        checks.append(
            ExpansionGateCheck(
                code=case.name,
                status=(
                    ExpansionGateStatus.FAIL if failed else ExpansionGateStatus.PASS
                ),
                detail=case.detail,
                required=True,
            )
        )
        failures.extend(f"{case.name}: {failure}" for failure in case.failures)
        failures.extend(
            f"{case.name}: {failure}" for failure in wrapper_failures
        )
    return ExpansionGateReport(
        card_id="WO35-B",
        status=(ExpansionGateStatus.FAIL if failures else ExpansionGateStatus.PASS),
        checks=tuple(checks),
        failures=tuple(failures),
        metadata=(
            ("access_policy_sha256", WO35B_ACCESS_POLICY_SHA256),
            ("access_schema_id", PARTITION_ACCESS_SCHEMA_ID_V1),
            ("audit_case_count", str(WO35B_AUDIT_CASE_COUNT)),
            ("experiment_state_schema_id", EXPERIMENT_STATE_SCHEMA_ID_V1),
            ("fixture_sha256", WO35B_FIXTURE_SHA256),
            ("holdout_reveal", "ONE_SHOT_TERMINAL"),
            ("partition_manifest_schema_id", PARTITION_MANIFEST_SCHEMA_ID_V1),
            (
                "partition_manifest_schema_version",
                str(PARTITION_MANIFEST_SCHEMA_VERSION_V1),
            ),
            ("partition_roles", "TRAIN,VALIDATION,HOLDOUT,ADVERSARIAL,ROBUSTNESS"),
        ),
    )


def _audit_wo35c() -> ExpansionGateReport:
    from kirby2.audit.strategy_discovery import (
        WO35C_ACCOUNTING_SHA256,
        WO35C_AUDIT_CASE_COUNT,
        WO35C_BATCH_SHA256,
        WO35C_FIXTURE_SHA256,
        WO35C_OPERATOR_REGISTRY_SHA256,
        audit_wo35c_strategy_mutations,
    )
    from kirby2.discovery.diffs import (
        STRATEGY_COMPLEXITY_SCHEMA_ID_V1,
        STRATEGY_MUTATION_DIFF_SCHEMA_ID_V1,
    )
    from kirby2.discovery.generation import (
        STRATEGY_MUTATION_BATCH_SCHEMA_ID_V1,
        STRATEGY_MUTATION_GENERATION_ORDER_V1,
        STRATEGY_MUTATION_SUBSTREAM_LABEL_V1,
    )
    from kirby2.discovery.lineage import STRATEGY_LINEAGE_SCHEMA_ID_V1
    from kirby2.discovery.mutations import (
        REQUIRED_MUTATION_OPERATORS_V1,
        STRATEGY_MUTATION_SCHEMA_ID_V1,
    )

    cases = audit_wo35c_strategy_mutations()
    expected_names = (
        "c_required_operator_registry_is_complete_declared_and_bounded",
        "c_every_operator_has_deterministic_valid_and_invalid_fixtures",
        "c_semantic_diff_complexity_and_lineage_agree_exactly",
        "c_generation_order_substreams_and_duplicates_are_deterministic",
        "c_lookahead_observability_permissions_and_resources_fail_closed",
    )
    failures: list[str] = []
    checks: list[ExpansionGateCheck] = []
    if (
        len(cases) != WO35C_AUDIT_CASE_COUNT
        or tuple(case.name for case in cases) != expected_names
    ):
        failures.append("WO35-C cases differ from the fixed mutation evidence inventory")
    for case in cases:
        wrapper_failures: list[str] = []
        if not case.required:
            wrapper_failures.append("WO35-C mutation cases must all be required")
        failed = bool(case.failures or wrapper_failures)
        checks.append(
            ExpansionGateCheck(
                code=case.name,
                status=(
                    ExpansionGateStatus.FAIL if failed else ExpansionGateStatus.PASS
                ),
                detail=case.detail,
                required=True,
            )
        )
        failures.extend(f"{case.name}: {failure}" for failure in case.failures)
        failures.extend(
            f"{case.name}: {failure}" for failure in wrapper_failures
        )
    return ExpansionGateReport(
        card_id="WO35-C",
        status=(ExpansionGateStatus.FAIL if failures else ExpansionGateStatus.PASS),
        checks=tuple(checks),
        failures=tuple(failures),
        metadata=(
            ("accounting_sha256", WO35C_ACCOUNTING_SHA256),
            ("audit_case_count", str(WO35C_AUDIT_CASE_COUNT)),
            ("batch_fixture_sha256", WO35C_BATCH_SHA256),
            ("batch_schema_id", STRATEGY_MUTATION_BATCH_SCHEMA_ID_V1),
            ("complexity_schema_id", STRATEGY_COMPLEXITY_SCHEMA_ID_V1),
            ("diff_schema_id", STRATEGY_MUTATION_DIFF_SCHEMA_ID_V1),
            ("fixture_sha256", WO35C_FIXTURE_SHA256),
            ("generation_order", STRATEGY_MUTATION_GENERATION_ORDER_V1),
            ("lineage_schema_id", STRATEGY_LINEAGE_SCHEMA_ID_V1),
            ("mutation_schema_id", STRATEGY_MUTATION_SCHEMA_ID_V1),
            ("operator_count", str(len(REQUIRED_MUTATION_OPERATORS_V1))),
            ("operator_registry_sha256", WO35C_OPERATOR_REGISTRY_SHA256),
            ("rejected_evaluation_eligibility", "NEVER"),
            ("substream_policy", STRATEGY_MUTATION_SUBSTREAM_LABEL_V1),
        ),
    )


def _audit_wo35d() -> ExpansionGateReport:
    from kirby2.audit.strategy_discovery import (
        WO35D_ACCESS_FIXTURE_SHA256,
        WO35D_AUDIT_CASE_COUNT,
        WO35D_MANIFEST_FIXTURE_SHA256,
        WO35D_NO_WINNER_RUN_SHA256,
        WO35D_OBJECTIVE_FIXTURE_SHA256,
        WO35D_POLICY_FIXTURE_SHA256,
        audit_wo35d_strategy_search,
    )
    from kirby2.discovery.evaluation import (
        SYNTHETIC_ORACLE_DATA_SOURCE_V1,
        SYNTHETIC_ORACLE_SCHEMA_ID_V1,
        VALIDATION_QUALIFICATION_RULE_ID_V1,
    )
    from kirby2.discovery.objectives import (
        ALL_OBJECTIVE_SPECS_V1,
        STRATEGY_OBJECTIVE_PROTOCOL_ID_V1,
        STRATEGY_OBJECTIVE_SCHEMA_ID_V1,
    )
    from kirby2.discovery.search import (
        MAX_SEARCH_BUDGET_V1,
        STRATEGY_SEARCH_MANIFEST_SCHEMA_ID_V1,
        STRATEGY_SEARCH_RUN_SCHEMA_ID_V1,
        SearchPolicyV1,
        load_search_manifest,
    )

    cases = audit_wo35d_strategy_search()
    expected_names = (
        "d_manifests_preregister_the_exact_bounded_protocol",
        "d_all_five_policies_are_repeatable_unique_and_budget_bounded",
        "d_objectives_uncertainty_multiplicity_and_complexity_are_exact",
        "d_budget_validation_and_real_partition_access_fail_closed",
        "d_no_candidate_is_a_terminal_success_without_threshold_relaxation",
    )
    failures: list[str] = []
    checks: list[ExpansionGateCheck] = []
    if (
        len(cases) != WO35D_AUDIT_CASE_COUNT
        or tuple(case.name for case in cases) != expected_names
    ):
        failures.append("WO35-D cases differ from the fixed search evidence inventory")
    for case in cases:
        wrapper_failures: list[str] = []
        if not case.required:
            wrapper_failures.append("WO35-D search cases must all be required")
        failed = bool(case.failures or wrapper_failures)
        checks.append(
            ExpansionGateCheck(
                code=case.name,
                status=(
                    ExpansionGateStatus.FAIL if failed else ExpansionGateStatus.PASS
                ),
                detail=case.detail,
                required=True,
            )
        )
        failures.extend(f"{case.name}: {failure}" for failure in case.failures)
        failures.extend(
            f"{case.name}: {failure}" for failure in wrapper_failures
        )
    examples = Path(__file__).resolve().parents[1] / "discovery" / "examples"
    bounded = load_search_manifest(examples / "bounded_search.toml")
    no_winner = load_search_manifest(examples / "no_winner.toml")
    return ExpansionGateReport(
        card_id="WO35-D",
        status=(ExpansionGateStatus.FAIL if failures else ExpansionGateStatus.PASS),
        checks=tuple(checks),
        failures=tuple(failures),
        metadata=(
            ("access_fixture_sha256", WO35D_ACCESS_FIXTURE_SHA256),
            ("audit_case_count", str(WO35D_AUDIT_CASE_COUNT)),
            ("bounded_manifest_sha256", bounded.manifest_sha256),
            ("manifest_fixture_sha256", WO35D_MANIFEST_FIXTURE_SHA256),
            ("manifest_schema_id", STRATEGY_SEARCH_MANIFEST_SCHEMA_ID_V1),
            ("max_budget", str(MAX_SEARCH_BUDGET_V1)),
            ("no_winner_manifest_sha256", no_winner.manifest_sha256),
            ("no_winner_run_sha256", WO35D_NO_WINNER_RUN_SHA256),
            ("objective_count", str(len(ALL_OBJECTIVE_SPECS_V1))),
            ("objective_fixture_sha256", WO35D_OBJECTIVE_FIXTURE_SHA256),
            ("objective_protocol_id", STRATEGY_OBJECTIVE_PROTOCOL_ID_V1),
            ("objective_schema_id", STRATEGY_OBJECTIVE_SCHEMA_ID_V1),
            ("oracle_data_source", SYNTHETIC_ORACLE_DATA_SOURCE_V1),
            ("oracle_schema_id", SYNTHETIC_ORACLE_SCHEMA_ID_V1),
            ("policies", ",".join(item.value for item in SearchPolicyV1)),
            ("policy_fixture_sha256", WO35D_POLICY_FIXTURE_SHA256),
            ("real_partition_access_count", "0"),
            ("run_schema_id", STRATEGY_SEARCH_RUN_SCHEMA_ID_V1),
            ("validation_rule_id", VALIDATION_QUALIFICATION_RULE_ID_V1),
        ),
    )


def _audit_wo35e() -> ExpansionGateReport:
    from kirby2.audit.strategy_discovery import (
        WO35E_AUDIT_CASE_COUNT,
        WO35E_OBSERVABILITY_FIXTURE_SHA256,
        WO35E_OVERFIT_FIXTURE_SHA256,
        WO35E_PERTURBATION_FIXTURE_SHA256,
        WO35E_REVEAL_FIXTURE_SHA256,
        WO35E_ROBUSTNESS_FIXTURE_SHA256,
        audit_wo35e_strategy_robustness,
    )
    from kirby2.discovery.observability import (
        ENDOGENOUS_DIVERGENCE_CLAIM_SCOPE_V1,
        OBSERVABILITY_SCHEMA_ID_V1,
        TERMINAL_REVEAL_POLICY_ID_V1,
        TERMINAL_REVEAL_SCHEMA_ID_V1,
    )
    from kirby2.discovery.overfit import (
        DEVELOPMENT_OVERFIT_DATA_SOURCE_V1,
        OVERFIT_POLICY_ID_V1,
        OVERFIT_SCHEMA_ID_V1,
        POST_REVEAL_ADDITIONS_V1,
        PRE_REVEAL_APPLICABILITY_V1,
    )
    from kirby2.discovery.robustness import (
        MANDATORY_ROBUSTNESS_FAMILIES_V1,
        ROBUSTNESS_EXPECTED_CELL_COUNT_V1,
        ROBUSTNESS_POLICY_ID_V1,
        ROBUSTNESS_ROOTS_V1,
        ROBUSTNESS_SCHEMA_ID_V1,
        ROBUSTNESS_SETTINGS_V1,
        SINGLE_VENUE_CAPABILITY_ID_V1,
        RobustnessFamilyV1,
    )

    cases = audit_wo35e_strategy_robustness()
    expected_names = (
        "e_one_factor_perturbations_and_single_venue_capability_are_exact",
        "e_robustness_pools_exactly_and_fails_closed_by_failure_class",
        "e_decision_projection_excludes_truth_and_unavailable_inputs_fail_closed",
        "e_all_overfit_predicates_apply_once_and_the_training_star_is_rejected",
        "e_robustness_precedes_one_atomic_reveal_and_terminal_claims_stay_named",
    )
    failures: list[str] = []
    checks: list[ExpansionGateCheck] = []
    if (
        len(cases) != WO35E_AUDIT_CASE_COUNT
        or tuple(case.name for case in cases) != expected_names
    ):
        failures.append("WO35-E cases differ from the fixed robustness evidence inventory")
    for case in cases:
        wrapper_failures: list[str] = []
        if not case.required:
            wrapper_failures.append("WO35-E robustness cases must all be required")
        failed = bool(case.failures or wrapper_failures)
        checks.append(
            ExpansionGateCheck(
                code=case.name,
                status=(
                    ExpansionGateStatus.FAIL if failed else ExpansionGateStatus.PASS
                ),
                detail=case.detail,
                required=True,
            )
        )
        failures.extend(f"{case.name}: {failure}" for failure in case.failures)
        failures.extend(
            f"{case.name}: {failure}" for failure in wrapper_failures
        )
    applicable_settings = tuple(
        item
        for item in ROBUSTNESS_SETTINGS_V1
        if item.family is not RobustnessFamilyV1.VENUE_MIX
    )
    return ExpansionGateReport(
        card_id="WO35-E",
        status=(ExpansionGateStatus.FAIL if failures else ExpansionGateStatus.PASS),
        checks=tuple(checks),
        failures=tuple(failures),
        metadata=(
            ("applicable_family_count", str(len(MANDATORY_ROBUSTNESS_FAMILIES_V1))),
            ("applicable_setting_count", str(len(applicable_settings))),
            ("audit_case_count", str(WO35E_AUDIT_CASE_COUNT)),
            ("development_overfit_data_source", DEVELOPMENT_OVERFIT_DATA_SOURCE_V1),
            ("endogenous_claim_scope", ENDOGENOUS_DIVERGENCE_CLAIM_SCOPE_V1),
            ("expected_robustness_cells", str(ROBUSTNESS_EXPECTED_CELL_COUNT_V1)),
            ("observability_fixture_sha256", WO35E_OBSERVABILITY_FIXTURE_SHA256),
            ("observability_schema_id", OBSERVABILITY_SCHEMA_ID_V1),
            ("overfit_fixture_sha256", WO35E_OVERFIT_FIXTURE_SHA256),
            ("overfit_policy_id", OVERFIT_POLICY_ID_V1),
            ("overfit_post_addition_count", str(len(POST_REVEAL_ADDITIONS_V1))),
            ("overfit_pre_applicable_count", str(len(PRE_REVEAL_APPLICABILITY_V1))),
            ("overfit_schema_id", OVERFIT_SCHEMA_ID_V1),
            ("perturbation_fixture_sha256", WO35E_PERTURBATION_FIXTURE_SHA256),
            ("real_partition_access_count", "0"),
            ("reveal_fixture_sha256", WO35E_REVEAL_FIXTURE_SHA256),
            ("robustness_fixture_sha256", WO35E_ROBUSTNESS_FIXTURE_SHA256),
            ("robustness_policy_id", ROBUSTNESS_POLICY_ID_V1),
            ("robustness_root_count", str(len(ROBUSTNESS_ROOTS_V1))),
            ("robustness_schema_id", ROBUSTNESS_SCHEMA_ID_V1),
            ("terminal_reveal_policy_id", TERMINAL_REVEAL_POLICY_ID_V1),
            ("terminal_reveal_schema_id", TERMINAL_REVEAL_SCHEMA_ID_V1),
            ("venue_mix_capability", SINGLE_VENUE_CAPABILITY_ID_V1),
            ("venue_mix_status", "NOT_APPLICABLE"),
        ),
    )


def _audit_wo35f() -> ExpansionGateReport:
    from kirby2.audit.strategy_discovery import (
        WO35F_ARTIFACT_TYPE_FIXTURE_SHA256,
        WO35F_AUDIT_CASE_COUNT,
        WO35F_COMPARISON_FIXTURE_SHA256,
        WO35F_LINEAGE_FIXTURE_SHA256,
        WO35F_MANIFEST_FIXTURE_SHA256,
        WO35F_REPORT_FIXTURE_SHA256,
        audit_wo35f_strategy_lineage,
    )
    from kirby2.discovery.commands import (
        CONTROLLED_EVIDENCE_REASON_V1,
        LINEAGE_DEVELOPMENT_DATA_SOURCE_V1,
        LINEAGE_DEVELOPMENT_SCHEMA_ID_V1,
    )
    from kirby2.discovery.report import (
        DISCOVERY_COMPARISON_SCHEMA_ID_V1,
        DISCOVERY_REPORT_SCHEMA_ID_V1,
    )
    from kirby2.discovery.store import (
        DISCOVERY_BINDING_SCHEMA_ID_V1,
        DISCOVERY_RECORD_SCHEMA_ID_V1,
        DISCOVERY_STORE_POLICY_ID_V1,
    )

    cases = audit_wo35f_strategy_lineage()
    expected_names = (
        "f_committed_contract_and_development_manifest_are_exact",
        "f_append_only_lineage_reloads_and_conflicts_refuse",
        "f_terminal_fields_are_sealed_and_reveal_is_durably_single_use",
        "f_lineage_and_comparison_reports_cover_every_scientific_path",
        "f_discovery_artifacts_project_and_controlled_gate_stays_unexercised",
    )
    failures: list[str] = []
    checks: list[ExpansionGateCheck] = []
    if (
        len(cases) != WO35F_AUDIT_CASE_COUNT
        or tuple(case.name for case in cases) != expected_names
    ):
        failures.append("WO35-F cases differ from the frozen lineage inventory")
    for case in cases:
        wrapper_failures: list[str] = []
        if not case.required:
            wrapper_failures.append("WO35-F lineage cases must all be required")
        failed = bool(case.failures or wrapper_failures)
        checks.append(
            ExpansionGateCheck(
                code=case.name,
                status=(
                    ExpansionGateStatus.FAIL if failed else ExpansionGateStatus.PASS
                ),
                detail=case.detail,
                required=True,
            )
        )
        failures.extend(f"{case.name}: {failure}" for failure in case.failures)
        failures.extend(
            f"{case.name}: {failure}" for failure in wrapper_failures
        )
    return ExpansionGateReport(
        card_id="WO35-F",
        status=(ExpansionGateStatus.FAIL if failures else ExpansionGateStatus.PASS),
        checks=tuple(checks),
        failures=tuple(failures),
        metadata=(
            ("artifact_fixture_sha256", WO35F_ARTIFACT_TYPE_FIXTURE_SHA256),
            ("audit_case_count", str(WO35F_AUDIT_CASE_COUNT)),
            ("binding_schema_id", DISCOVERY_BINDING_SCHEMA_ID_V1),
            ("comparison_fixture_sha256", WO35F_COMPARISON_FIXTURE_SHA256),
            ("comparison_schema_id", DISCOVERY_COMPARISON_SCHEMA_ID_V1),
            ("controlled_gate_absence_reason", CONTROLLED_EVIDENCE_REASON_V1),
            ("development_data_source", LINEAGE_DEVELOPMENT_DATA_SOURCE_V1),
            ("development_manifest_schema_id", LINEAGE_DEVELOPMENT_SCHEMA_ID_V1),
            ("lineage_fixture_sha256", WO35F_LINEAGE_FIXTURE_SHA256),
            ("manifest_fixture_sha256", WO35F_MANIFEST_FIXTURE_SHA256),
            ("real_partition_access_count", "0"),
            ("record_schema_id", DISCOVERY_RECORD_SCHEMA_ID_V1),
            ("report_fixture_sha256", WO35F_REPORT_FIXTURE_SHA256),
            ("report_schema_id", DISCOVERY_REPORT_SCHEMA_ID_V1),
            ("store_policy_id", DISCOVERY_STORE_POLICY_ID_V1),
        ),
    )


def _audit_wo35f1() -> ExpansionGateReport:
    """Validate immutable controlled evidence without generating or rerunning it."""

    from kirby2.discovery.commands import validate_controlled_evidence

    repository = Path(__file__).resolve().parents[2]
    manifest = repository / "kirby2" / "discovery" / "examples" / "bounded_search.toml"
    evidence_root = repository / ".kirby2" / "discovery" / "controlled"
    result = validate_controlled_evidence(
        manifest_path=manifest,
        evidence_root=evidence_root,
    )
    if result["status"] == ExpansionGateStatus.NOT_EXERCISED.value:
        return ExpansionGateReport(
            card_id="WO35-F1",
            status=ExpansionGateStatus.NOT_EXERCISED,
            checks=(
                ExpansionGateCheck(
                    code="controlled_strategy_discovery_evidence",
                    status=ExpansionGateStatus.NOT_EXERCISED,
                    detail=(
                        "immutable controlled discovery evidence is absent; "
                        "no search or terminal partition was regenerated"
                    ),
                    required=True,
                ),
            ),
            reason_code=str(result["reason_code"]),
            metadata=(
                ("evidence_root", ".kirby2/discovery/controlled"),
                ("generation_authority", "ABSENT_FROM_VALIDATOR"),
                ("manifest_sha256", str(result["manifest_sha256"])),
                ("reentry", "VERIFY_ONLY_NEVER_RERUN"),
            ),
        )
    verification = result.get("verification")
    passed = result["status"] == ExpansionGateStatus.PASS.value and isinstance(
        verification,
        dict,
    ) and verification.get("status") == ExpansionGateStatus.PASS.value
    failure = () if passed else ("controlled strategy-discovery evidence is invalid",)
    return ExpansionGateReport(
        card_id="WO35-F1",
        status=(ExpansionGateStatus.PASS if passed else ExpansionGateStatus.FAIL),
        checks=(
            ExpansionGateCheck(
                code="controlled_strategy_discovery_evidence",
                status=(ExpansionGateStatus.PASS if passed else ExpansionGateStatus.FAIL),
                detail=(
                    f"immutable discovery_id={result.get('discovery_id', 'UNKNOWN')} "
                    f"status={result['status']}"
                ),
                required=True,
            ),
        ),
        failures=failure,
        metadata=(
            ("evidence_root", ".kirby2/discovery/controlled"),
            ("generation_authority", "ABSENT_FROM_VALIDATOR"),
            ("manifest_sha256", str(result["manifest_sha256"])),
            ("reentry", "VERIFY_ONLY_NEVER_RERUN"),
        ),
    )


def _audit_wo36a() -> ExpansionGateReport:
    from kirby2.audit.replay_microscope import (
        WO36A_AUDIT_CASE_COUNT,
        WO36A_COMPLETE_INDEX_SHA256,
        WO36A_COMPLETE_SOURCE_SHA256,
        WO36A_LEGACY_INDEX_SHA256,
        WO36A_LEGACY_SOURCE_SHA256,
        audit_replay_microscope,
    )
    from kirby2.microscope import (
        MECHANISTIC_INTERPRETATION,
        TRACE_INDEX_SCHEMA_ID,
        TRACE_SOURCE_SCHEMA_ID,
    )

    cases = audit_replay_microscope()
    checks = tuple(
        ExpansionGateCheck(
            code=case.name,
            status=(
                ExpansionGateStatus.FAIL
                if case.failures
                else ExpansionGateStatus.PASS
            ),
            detail=case.detail,
            required=True,
        )
        for case in cases
    )
    failures = tuple(
        f"{case.name}: {failure}"
        for case in cases
        for failure in case.failures
    )
    return ExpansionGateReport(
        card_id="WO36-A",
        status=(ExpansionGateStatus.FAIL if failures else ExpansionGateStatus.PASS),
        checks=checks,
        failures=failures,
        metadata=(
            ("audit_case_count", str(WO36A_AUDIT_CASE_COUNT)),
            ("complete_index_sha256", WO36A_COMPLETE_INDEX_SHA256),
            ("complete_source_sha256", WO36A_COMPLETE_SOURCE_SHA256),
            ("interpretation", MECHANISTIC_INTERPRETATION),
            ("legacy_index_sha256", WO36A_LEGACY_INDEX_SHA256),
            ("legacy_source_sha256", WO36A_LEGACY_SOURCE_SHA256),
            ("source_mutation", "ABSENT"),
            ("timestamp_causality_inference", "REFUSED"),
            ("trace_index_schema_id", TRACE_INDEX_SCHEMA_ID),
            ("trace_source_schema_id", TRACE_SOURCE_SCHEMA_ID),
        ),
    )


def _audit_wo36b() -> ExpansionGateReport:
    from kirby2.audit.replay_microscope import (
        WO36B_AUDIT_CASE_COUNT,
        audit_replay_observation_policies,
    )
    from kirby2.microscope.policy import (
        AS_OBSERVED_POLICY_ID,
        OBSERVATION_POLICY_SCHEMA_ID,
        OBSERVATION_POLICY_SCHEMA_VERSION,
        POSTMORTEM_POLICY_ID,
    )
    from kirby2.microscope.query import OBSERVATION_QUERY_SCHEMA_ID

    cases = audit_replay_observation_policies()
    checks = tuple(
        ExpansionGateCheck(
            code=case.name,
            status=(
                ExpansionGateStatus.FAIL
                if case.failures
                else ExpansionGateStatus.PASS
            ),
            detail=case.detail,
            required=True,
        )
        for case in cases
    )
    failures = tuple(
        f"{case.name}: {failure}"
        for case in cases
        for failure in case.failures
    )
    return ExpansionGateReport(
        card_id="WO36-B",
        status=(ExpansionGateStatus.FAIL if failures else ExpansionGateStatus.PASS),
        checks=checks,
        failures=failures,
        metadata=(
            ("as_observed_policy_id", AS_OBSERVED_POLICY_ID),
            ("audit_case_count", str(WO36B_AUDIT_CASE_COUNT)),
            ("ground_truth_filtering", "REFUSED"),
            ("historical_hidden_without_capability", "UNAVAILABLE"),
            ("interpolation", "REFUSED"),
            (
                "observation_policy_schema",
                f"{OBSERVATION_POLICY_SCHEMA_ID}@{OBSERVATION_POLICY_SCHEMA_VERSION}",
            ),
            ("observation_query_schema_id", OBSERVATION_QUERY_SCHEMA_ID),
            (
                "observed_source_policy",
                "CLIENT_DELIVERED_FEED_AND_RECORDED_DECISION_ONLY",
            ),
            ("postmortem_policy_id", POSTMORTEM_POLICY_ID),
        ),
    )


def _audit_wo31e3() -> ExpansionGateReport:
    """Run the passive venue/client delivery and restart evidence."""

    from kirby2.audit.full_day import audit_wo31e3_delivery_restore
    from kirby2.full_day.composition import (
        DELIVERY_PROFILE_ID,
        executable_delivery_composition_matrix,
    )
    from kirby2.full_day.restore import FULL_DAY_RUNTIME_RESTORE_REQUEST_FORMAT_ID

    cases = audit_wo31e3_delivery_restore()
    expected_names = (
        "full_day_delivery_composition",
        "full_day_delivery_timelines_and_races",
        "full_day_delivery_fresh_process_restore",
        "full_day_delivery_ownership_refusals",
    )
    failures: list[str] = []
    checks: list[ExpansionGateCheck] = []
    if tuple(case.name for case in cases) != expected_names:
        failures.append("WO31-E3 cases differ from the fixed evidence inventory")
    for case in cases:
        wrapper_failures: list[str] = []
        if not case.required:
            wrapper_failures.append("WO31-E3 cases must all be required")
        if case.status_override is not None:
            wrapper_failures.append(
                "WO31-E3 cases must report ordinary PASS/FAIL status"
            )
        failed = bool(case.failures or wrapper_failures)
        checks.append(
            ExpansionGateCheck(
                code=case.name,
                status=(
                    ExpansionGateStatus.FAIL
                    if failed
                    else ExpansionGateStatus.PASS
                ),
                detail=case.detail,
                required=True,
            )
        )
        failures.extend(f"{case.name}: {failure}" for failure in case.failures)
        failures.extend(
            f"{case.name}: {failure}" for failure in wrapper_failures
        )
    matrix = executable_delivery_composition_matrix()
    profile = matrix.profile(DELIVERY_PROFILE_ID, 1)
    return ExpansionGateReport(
        card_id="WO31-E3",
        status=(ExpansionGateStatus.FAIL if failures else ExpansionGateStatus.PASS),
        checks=tuple(checks),
        failures=tuple(failures),
        metadata=(
            ("composition_matrix_sha256", matrix.sha256),
            ("fresh_process_boundary_count", "3"),
            ("hostile_refusal_count", "6"),
            ("profile_id", profile.profile_id),
            ("profile_version", str(profile.profile_version)),
            ("restore_format", FULL_DAY_RUNTIME_RESTORE_REQUEST_FORMAT_ID),
            ("restored_scope", "VENUE_TRUTH_AND_CLIENT_DELIVERY"),
        ),
    )


def _audit_dev0002() -> ExpansionGateReport:
    """Prove the sealed macro-anchor ordering repair remains fail closed."""

    from kirby2.audit.full_day import audit_dev0002_anchor_transition_ordering

    cases = audit_dev0002_anchor_transition_ordering()
    checks = tuple(
        ExpansionGateCheck(
            code=case.name,
            status=(
                ExpansionGateStatus.FAIL
                if case.failures
                else ExpansionGateStatus.PASS
            ),
            detail=case.detail,
            required=case.required,
        )
        for case in cases
    )
    failures = tuple(
        f"{case.name}: {failure}"
        for case in cases
        for failure in case.failures
    )
    return ExpansionGateReport(
        card_id="DEV-0002",
        status=(ExpansionGateStatus.FAIL if failures else ExpansionGateStatus.PASS),
        checks=checks,
        failures=failures,
        metadata=(
            ("accepted_trace_count", "4"),
            ("hostile_refusal_count", "5"),
            ("repaired_owner", "FULL_DAY_EVENT_VALIDATOR_V1"),
        ),
    )


def _audit_dev0003() -> ExpansionGateReport:
    """Prove exact state-runtime authority is checkpoint-inventoried."""

    from kirby2.audit.full_day import audit_dev0003_state_checkpoint_inventory

    cases = audit_dev0003_state_checkpoint_inventory()
    checks = tuple(
        ExpansionGateCheck(
            code=case.name,
            status=(
                ExpansionGateStatus.FAIL
                if case.failures
                else ExpansionGateStatus.PASS
            ),
            detail=case.detail,
            required=case.required,
        )
        for case in cases
    )
    failures = tuple(
        f"{case.name}: {failure}"
        for case in cases
        for failure in case.failures
    )
    return ExpansionGateReport(
        card_id="DEV-0003",
        status=(ExpansionGateStatus.FAIL if failures else ExpansionGateStatus.PASS),
        checks=checks,
        failures=failures,
        metadata=(
            ("owned_state_field_count", "27"),
            ("obsolete_aggregate_alias", "ABSENT"),
            ("repaired_owner", "FULL_DAY_CHECKPOINT_INVENTORY_V1"),
        ),
    )


def _audit_dev0004() -> ExpansionGateReport:
    from kirby2.audit.full_day import audit_dev0004_atomic_boundary_replay

    cases = audit_dev0004_atomic_boundary_replay()
    checks = tuple(
        ExpansionGateCheck(
            code=case.name,
            status=(
                ExpansionGateStatus.FAIL
                if case.failures
                else ExpansionGateStatus.PASS
            ),
            detail=case.detail,
            required=case.required,
        )
        for case in cases
    )
    failures = tuple(
        f"{case.name}: {failure}"
        for case in cases
        for failure in case.failures
    )
    return ExpansionGateReport(
        card_id="DEV-0004",
        status=(ExpansionGateStatus.FAIL if failures else ExpansionGateStatus.PASS),
        checks=checks,
        failures=failures,
        metadata=(
            ("deferred_due_class", "EXACT_TIME_GOOD_UNTIL_TIME"),
            ("engine_owned_schedule_deferral", "REFUSED"),
            ("incomplete_durable_cut", "REFUSED"),
            ("repaired_owner", "MARKET_MECHANICS_OUTER_REPLAY_V1"),
        ),
    )


def _audit_dev0005() -> ExpansionGateReport:
    """Classify and retain the post-WO36-B result-root hardening."""

    from kirby2.audit.replay_microscope import audit_replay_observation_policies

    cases = audit_replay_observation_policies()
    target = next(
        case
        for case in cases
        if case.name == "postmortem_requires_capability_and_bound_authorization"
    )
    raw_invariants = target.evidence.get("result_invariants")
    invariants = raw_invariants if type(raw_invariants) is dict else {}
    required = (
        (
            "CLIENT_DELIVERED_RECEIPT_ROOT",
            "client_delivered_receive_rejected",
            "client-delivered results require exact recorded receipt timing",
        ),
        (
            "REVEAL_CLIENT_CLOCK_ROOT",
            "reveal_client_receive_rejected",
            "revealed values cannot claim a client receipt clock",
        ),
        (
            "REVEAL_VISIBILITY_ROOT",
            "reveal_visibility_rejected",
            "revealed values remain visible from their exact source-event time",
        ),
    )
    checks = tuple(
        ExpansionGateCheck(
            code=code,
            status=(
                ExpansionGateStatus.PASS
                if invariants.get(key) is True
                else ExpansionGateStatus.FAIL
            ),
            detail=detail,
            required=True,
        )
        for code, key, detail in required
    ) + (
        ExpansionGateCheck(
            code="WO36B_REGRESSION",
            status=(
                ExpansionGateStatus.PASS
                if all(not case.failures for case in cases)
                else ExpansionGateStatus.FAIL
            ),
            detail="the fixed six-case WO36-B inventory remains green",
            required=True,
        ),
    )
    failures = tuple(
        check.code for check in checks if check.status is ExpansionGateStatus.FAIL
    )
    return ExpansionGateReport(
        card_id="DEV-0005",
        status=(ExpansionGateStatus.FAIL if failures else ExpansionGateStatus.PASS),
        checks=checks,
        failures=failures,
        metadata=(
            ("classified_commit", "87aad32b7cd7c39fce6773dfa9f0059edc311e63"),
            ("interrupted_card", "WO36-C"),
            ("result_root_invariant_count", "3"),
        ),
    )


def _audit_dev0006() -> ExpansionGateReport:
    """Verify observed evidence is admitted only from caller-pinned immutable bytes."""

    from kirby2.audit.replay_microscope import (
        audit_replay_observation_ingestion,
    )
    from kirby2.microscope.ingestion import (
        OBSERVED_INGEST_ADAPTER_ID,
        OBSERVED_INGEST_ADAPTER_VERSION,
        OBSERVED_INGEST_MANIFEST_SCHEMA_ID,
        OBSERVED_INGEST_MANIFEST_SCHEMA_VERSION,
    )

    cases = audit_replay_observation_ingestion()
    checks = tuple(
        ExpansionGateCheck(
            code=case.name,
            status=(
                ExpansionGateStatus.FAIL
                if case.failures
                else ExpansionGateStatus.PASS
            ),
            detail=case.detail,
            required=True,
        )
        for case in cases
    )
    failures = tuple(
        f"{case.name}: {failure}"
        for case in cases
        for failure in case.failures
    )
    return ExpansionGateReport(
        card_id="DEV-0006",
        status=(ExpansionGateStatus.FAIL if failures else ExpansionGateStatus.PASS),
        checks=checks,
        failures=failures,
        metadata=(
            ("adapter_id", OBSERVED_INGEST_ADAPTER_ID),
            ("adapter_version", str(OBSERVED_INGEST_ADAPTER_VERSION)),
            (
                "manifest_schema",
                f"{OBSERVED_INGEST_MANIFEST_SCHEMA_ID}@"
                f"{OBSERVED_INGEST_MANIFEST_SCHEMA_VERSION}",
            ),
            ("pin_origin", "EXTERNAL_TRUST_ROOT_REQUIRED"),
            ("receipt_ui_visibility", "REFUSED"),
            ("source_scope", "OBSERVED_ONLY"),
            ("ui_raw_evidence_input", "REFUSED"),
        ),
    )


def _audit_wo36c() -> ExpansionGateReport:
    """Verify synchronized cursor, pane, queue, and overlay read models."""

    from kirby2.audit.replay_microscope import (
        WO36C_AUDIT_CASE_COUNT,
        audit_synchronized_replay_read_models,
    )
    from kirby2.microscope.overlays import (
        OVERLAY_KIND_ORDER,
        OVERLAY_SET_SCHEMA_ID,
    )
    from kirby2.microscope.panes import PANE_ORDER, PANE_SNAPSHOT_SCHEMA_ID
    from kirby2.microscope.timeline import (
        TIMELINE_RECEIPT_SCHEMA_ID,
        TIMELINE_SCHEMA_ID,
    )

    cases = audit_synchronized_replay_read_models()
    checks = tuple(
        ExpansionGateCheck(
            code=case.name,
            status=(
                ExpansionGateStatus.FAIL
                if case.failures
                else ExpansionGateStatus.PASS
            ),
            detail=case.detail,
            required=True,
        )
        for case in cases
    )
    failures = tuple(
        f"{case.name}: {failure}"
        for case in cases
        for failure in case.failures
    )
    return ExpansionGateReport(
        card_id="WO36-C",
        status=(ExpansionGateStatus.FAIL if failures else ExpansionGateStatus.PASS),
        checks=checks,
        failures=failures,
        metadata=(
            ("audit_case_count", str(WO36C_AUDIT_CASE_COUNT)),
            ("capability_pin_origin", "EXTERNAL_TRUST_ROOT_REQUIRED"),
            ("cursor_policy", "ONE_EXACT_INTEGER_SIMULATION_TIME"),
            ("future_inventory_ui_visibility", "REFUSED"),
            ("overlay_count", str(len(OVERLAY_KIND_ORDER))),
            ("overlay_set_schema_id", OVERLAY_SET_SCHEMA_ID),
            ("pane_count", str(len(PANE_ORDER))),
            ("pane_snapshot_schema_id", PANE_SNAPSHOT_SCHEMA_ID),
            (
                "queue_truth_policy",
                "PINNED_SYNTHETIC_AUTHORIZED_POSTMORTEM_ONLY",
            ),
            ("timeline_receipt_schema_id", TIMELINE_RECEIPT_SCHEMA_ID),
            ("timeline_schema_id", TIMELINE_SCHEMA_ID),
        ),
    )


def _audit_wo36d() -> ExpansionGateReport:
    """Verify deterministic, self-contained portable replay reports."""

    from kirby2.audit.replay_microscope import (
        WO36D_AUDIT_CASE_COUNT,
        audit_portable_replay_reports,
    )
    from kirby2.microscope.report import (
        OFFLINE_RENDERER_ID,
        OFFLINE_RENDERER_VERSION,
        PORTABLE_REPLAY_REPORT_BUNDLE_SCHEMA_ID,
        PORTABLE_REPLAY_REPORT_SCHEMA_ID,
        REPORT_ASSET_SHA256,
    )

    cases = audit_portable_replay_reports()
    checks = tuple(
        ExpansionGateCheck(
            code=case.name,
            status=(
                ExpansionGateStatus.FAIL
                if case.failures
                else ExpansionGateStatus.PASS
            ),
            detail=case.detail,
            required=True,
        )
        for case in cases
    )
    failures = tuple(
        f"{case.name}: {failure}"
        for case in cases
        for failure in case.failures
    )
    return ExpansionGateReport(
        card_id="WO36-D",
        status=(ExpansionGateStatus.FAIL if failures else ExpansionGateStatus.PASS),
        checks=checks,
        failures=failures,
        metadata=(
            ("asset_count", str(len(REPORT_ASSET_SHA256))),
            ("audit_case_count", str(WO36D_AUDIT_CASE_COUNT)),
            ("bundle_schema_id", PORTABLE_REPLAY_REPORT_BUNDLE_SCHEMA_ID),
            ("network_policy", "OFFLINE_ONLY"),
            ("renderer_id", OFFLINE_RENDERER_ID),
            ("renderer_version", str(OFFLINE_RENDERER_VERSION)),
            ("report_schema_id", PORTABLE_REPLAY_REPORT_SCHEMA_ID),
        ),
    )


def _audit_wo36e() -> ExpansionGateReport:
    """Verify branch comparison, immutable sidecars, and timing-lie review."""

    from kirby2.audit.replay_microscope import (
        WO36E_AUDIT_CASE_COUNT,
        audit_counterfactual_replay_comparison,
    )
    from kirby2.microscope.annotations import (
        REPLAY_ANNOTATION_SCHEMA_ID,
        REPLAY_ANNOTATION_SCHEMA_VERSION,
        REPLAY_BOOKMARK_SCHEMA_ID,
        REPLAY_BOOKMARK_SCHEMA_VERSION,
        SOURCE_MUTATION_POLICY,
        TIMING_LIE_REVIEW_PACKET_SCHEMA_ID,
        TIMING_LIE_REVIEW_PACKET_SCHEMA_VERSION,
        TIMING_LIE_RUBRIC_ORDER,
        TIMING_LIE_RUBRIC_VERSION,
        TimingLieHumanResult,
        TimingLieTechnicalStatus,
    )
    from kirby2.microscope.comparison import (
        BRANCH_COMPARISON_SCHEMA_ID,
        BRANCH_COMPARISON_SCHEMA_VERSION,
        COMPARISON_OVERLAY_ORDER,
        COMPARISON_SERIES_ORDER,
    )

    cases = audit_counterfactual_replay_comparison()
    checks = tuple(
        ExpansionGateCheck(
            code=case.name,
            status=(
                ExpansionGateStatus.FAIL
                if case.failures
                else ExpansionGateStatus.PASS
            ),
            detail=case.detail,
            required=True,
        )
        for case in cases
    )
    failures = tuple(
        f"{case.name}: {failure}"
        for case in cases
        for failure in case.failures
    )
    return ExpansionGateReport(
        card_id="WO36-E",
        status=(ExpansionGateStatus.FAIL if failures else ExpansionGateStatus.PASS),
        checks=checks,
        failures=failures,
        metadata=(
            ("annotation_schema_id", REPLAY_ANNOTATION_SCHEMA_ID),
            ("annotation_schema_version", str(REPLAY_ANNOTATION_SCHEMA_VERSION)),
            ("audit_case_count", str(WO36E_AUDIT_CASE_COUNT)),
            ("bookmark_schema_id", REPLAY_BOOKMARK_SCHEMA_ID),
            ("bookmark_schema_version", str(REPLAY_BOOKMARK_SCHEMA_VERSION)),
            ("comparison_overlay_count", str(len(COMPARISON_OVERLAY_ORDER))),
            ("comparison_schema_id", BRANCH_COMPARISON_SCHEMA_ID),
            ("comparison_schema_version", str(BRANCH_COMPARISON_SCHEMA_VERSION)),
            ("comparison_series_count", str(len(COMPARISON_SERIES_ORDER))),
            ("human_timing_lie_status", TimingLieHumanResult.PENDING.value),
            ("sidecar_source_mutation_policy", SOURCE_MUTATION_POLICY),
            ("timing_lie_review_schema_id", TIMING_LIE_REVIEW_PACKET_SCHEMA_ID),
            (
                "timing_lie_review_schema_version",
                str(TIMING_LIE_REVIEW_PACKET_SCHEMA_VERSION),
            ),
            ("timing_lie_rubric_count", str(len(TIMING_LIE_RUBRIC_ORDER))),
            ("timing_lie_rubric_version", TIMING_LIE_RUBRIC_VERSION),
            (
                "timing_lie_technical_status",
                TimingLieTechnicalStatus.READY_FOR_HUMAN_REVIEW.value,
            ),
        ),
    )


def _audit_dev0007() -> ExpansionGateReport:
    """Require opaque learner identities at the immutable run-store boundary."""

    from kirby2.audit.pseudonymous_learner import (
        DEV0007_AUDIT_CASE_COUNT,
        audit_dev0007_pseudonymous_learner_runs,
    )
    from kirby2.pseudonyms import PSEUDONYMOUS_PROFILE_ID_POLICY

    cases = audit_dev0007_pseudonymous_learner_runs()
    checks = tuple(
        ExpansionGateCheck(
            code=case.name,
            status=(
                ExpansionGateStatus.FAIL
                if case.failures
                else ExpansionGateStatus.PASS
            ),
            detail=case.detail,
            required=True,
        )
        for case in cases
    )
    failures = tuple(
        f"{case.name}: {failure}"
        for case in cases
        for failure in case.failures
    )
    return ExpansionGateReport(
        card_id="DEV-0007",
        status=(ExpansionGateStatus.FAIL if failures else ExpansionGateStatus.PASS),
        checks=checks,
        failures=failures,
        metadata=(
            ("audit_case_count", str(DEV0007_AUDIT_CASE_COUNT)),
            ("interrupted_card", "WO37-A"),
            ("learner_run_identity_policy", PSEUDONYMOUS_PROFILE_ID_POLICY),
            ("legacy_human_readable_learner_id_write", "REFUSED"),
        ),
    )


def _audit_wo37a() -> ExpansionGateReport:
    """Verify pseudonymous profiles, consent, and erasable identity mappings."""

    from kirby2.audit.instructor_console import (
        WO37A_AUDIT_CASE_COUNT,
        audit_pseudonymous_profiles_and_consent,
    )
    from kirby2.instructor.identity import (
        IDENTITY_DELETION_RECEIPT_SCHEMA_ID,
        IDENTITY_DELETION_RECEIPT_SCHEMA_VERSION,
        IDENTITY_MAPPING_SCHEMA_ID,
        IDENTITY_MAPPING_SCHEMA_VERSION,
        IDENTITY_MAPPING_AUTHORITY_POLICY,
    )
    from kirby2.instructor.consent import (
        CURRENT_CONSENT_AUTHORITY_POLICY,
        PSEUDONYMIZATION_CLAIM,
    )
    from kirby2.instructor.models import INSTRUCTOR_RECORD_TYPES

    cases = audit_pseudonymous_profiles_and_consent()
    checks = tuple(
        ExpansionGateCheck(
            code=case.name,
            status=(
                ExpansionGateStatus.FAIL
                if case.failures
                else ExpansionGateStatus.PASS
            ),
            detail=case.detail,
            required=True,
        )
        for case in cases
    )
    failures = tuple(
        f"{case.name}: {failure}"
        for case in cases
        for failure in case.failures
    )
    return ExpansionGateReport(
        card_id="WO37-A",
        status=(ExpansionGateStatus.FAIL if failures else ExpansionGateStatus.PASS),
        checks=checks,
        failures=failures,
        metadata=(
            ("audit_case_count", str(WO37A_AUDIT_CASE_COUNT)),
            ("consent_head_authority", CURRENT_CONSENT_AUTHORITY_POLICY),
            ("identity_mapping_default_export", "REFUSED"),
            ("identity_mapping_authority", IDENTITY_MAPPING_AUTHORITY_POLICY),
            ("identity_mapping_schema_id", IDENTITY_MAPPING_SCHEMA_ID),
            (
                "identity_mapping_schema_version",
                str(IDENTITY_MAPPING_SCHEMA_VERSION),
            ),
            ("model_vocabulary_count", str(len(INSTRUCTOR_RECORD_TYPES))),
            ("privacy_claim", PSEUDONYMIZATION_CLAIM),
            (
                "deletion_receipt_schema_id",
                IDENTITY_DELETION_RECEIPT_SCHEMA_ID,
            ),
            (
                "deletion_receipt_schema_version",
                str(IDENTITY_DELETION_RECEIPT_SCHEMA_VERSION),
            ),
        ),
    )


def _audit_wo37b() -> ExpansionGateReport:
    from kirby2.audit.instructor_console import (
        WO37B_AUDIT_CASE_COUNT,
        audit_versioned_assignments_rubrics_reviews,
    )

    cases = audit_versioned_assignments_rubrics_reviews()
    checks = tuple(
        ExpansionGateCheck(
            code=case.name,
            status=(
                ExpansionGateStatus.FAIL
                if case.failures
                else ExpansionGateStatus.PASS
            ),
            detail=case.detail,
            required=True,
        )
        for case in cases
    )
    failures = tuple(
        f"{case.name}: {failure}"
        for case in cases
        for failure in case.failures
    )
    return ExpansionGateReport(
        card_id="WO37-B",
        status=(ExpansionGateStatus.FAIL if failures else ExpansionGateStatus.PASS),
        checks=checks,
        failures=failures,
        metadata=(
            ("audit_case_count", str(WO37B_AUDIT_CASE_COUNT)),
            ("assignment_lock_policy", "ATTEMPT_RUNTIME_EXACT_MATCH_V1"),
            ("review_mutation_policy", "IMMUTABLE_SUCCESSOR_SIDECARS_V1"),
            ("rubric_correction_policy", "NEW_VERSION_AND_DERIVED_SCORE_V1"),
        ),
    )


def _audit_wo37c() -> ExpansionGateReport:
    from kirby2.audit.instructor_console import (
        WO37C_AUDIT_CASE_COUNT,
        audit_reproducible_local_studies,
    )

    cases = audit_reproducible_local_studies()
    checks = tuple(
        ExpansionGateCheck(
            code=case.name,
            status=(
                ExpansionGateStatus.FAIL
                if case.failures
                else ExpansionGateStatus.PASS
            ),
            detail=case.detail,
            required=True,
        )
        for case in cases
    )
    failures = tuple(
        f"{case.name}: {failure}"
        for case in cases
        for failure in case.failures
    )
    return ExpansionGateReport(
        card_id="WO37-C",
        status=(ExpansionGateStatus.FAIL if failures else ExpansionGateStatus.PASS),
        checks=checks,
        failures=failures,
        metadata=(
            ("audit_case_count", str(WO37C_AUDIT_CASE_COUNT)),
            ("protocol_mutation_policy", "LOCKED_OR_EXECUTED_REFUSED_V1"),
            ("compatibility_policy", "REFUSE_OR_EXPLICIT_STRATIFY_V1"),
            ("default_claim_capability", "DESCRIPTIVE"),
        ),
    )


def _audit_wo37d() -> ExpansionGateReport:
    from kirby2.audit.instructor_console import (
        WO37D_AUDIT_CASE_COUNT,
        audit_instructor_research_console_queries,
    )

    cases = audit_instructor_research_console_queries()
    checks = tuple(
        ExpansionGateCheck(
            code=case.name,
            status=(
                ExpansionGateStatus.FAIL
                if case.failures
                else ExpansionGateStatus.PASS
            ),
            detail=case.detail,
            required=True,
        )
        for case in cases
    )
    failures = tuple(
        f"{case.name}: {failure}"
        for case in cases
        for failure in case.failures
    )
    return ExpansionGateReport(
        card_id="WO37-D",
        status=(ExpansionGateStatus.FAIL if failures else ExpansionGateStatus.PASS),
        checks=checks,
        failures=failures,
        metadata=(
            ("audit_case_count", str(WO37D_AUDIT_CASE_COUNT)),
            ("query_time_policy", "EXPLICIT_AS_OF_LEDGER_POINT_V1"),
            ("comparison_shape_count", "6"),
            ("external_service_policy", "LOCAL_ONLY_NO_EXTERNAL_SERVICES_V1"),
        ),
    )


def _audit_wo37e() -> ExpansionGateReport:
    from kirby2.audit.instructor_console import (
        WO37E_AUDIT_CASE_COUNT,
        audit_redacted_export_and_profile_deletion,
    )

    cases = audit_redacted_export_and_profile_deletion()
    checks = tuple(
        ExpansionGateCheck(
            code=case.name,
            status=(
                ExpansionGateStatus.FAIL
                if case.failures
                else ExpansionGateStatus.PASS
            ),
            detail=case.detail,
            required=True,
        )
        for case in cases
    )
    failures = tuple(
        f"{case.name}: {failure}"
        for case in cases
        for failure in case.failures
    )
    return ExpansionGateReport(
        card_id="WO37-E",
        status=(ExpansionGateStatus.FAIL if failures else ExpansionGateStatus.PASS),
        checks=checks,
        failures=failures,
        metadata=(
            ("audit_case_count", str(WO37E_AUDIT_CASE_COUNT)),
            ("export_format", "UNPACKED_CANONICAL_DIRECTORY_V1"),
            ("redaction_policy", "EXPLICIT_ALLOWLIST_AND_FIELD_MANIFEST_V1"),
            ("deletion_policy", "IDENTITY_ONLY_RETAINED_EVIDENCE_IMMUTABLE_V1"),
        ),
    )


def _audit_wo39a() -> ExpansionGateReport:
    from kirby2.audit.packs import (
        WO39A_AUDIT_CASE_COUNT,
        audit_canonical_pack_identity,
    )

    cases = audit_canonical_pack_identity()
    checks = tuple(
        ExpansionGateCheck(
            code=case.name,
            status=(
                ExpansionGateStatus.FAIL
                if case.failures
                else ExpansionGateStatus.PASS
            ),
            detail=case.detail,
            required=True,
        )
        for case in cases
    )
    failures = tuple(
        f"{case.name}: {failure}"
        for case in cases
        for failure in case.failures
    )
    return ExpansionGateReport(
        card_id="WO39-A",
        status=(ExpansionGateStatus.FAIL if failures else ExpansionGateStatus.PASS),
        checks=checks,
        failures=failures,
        metadata=(
            ("audit_case_count", str(WO39A_AUDIT_CASE_COUNT)),
            ("logical_identity", "SHA256_CANONICAL_PACK_IDENTITY_V1"),
            ("transport_identity", "SHA256_EXACT_ARCHIVE_BYTES_V1"),
            ("content_policy", "CLOSED_DATA_ONLY_ALLOWLIST_V1"),
        ),
    )


def _audit_wo39b() -> ExpansionGateReport:
    from kirby2.audit.packs import (
        WO39B_AUDIT_CASE_COUNT,
        audit_hostile_archive_validation_and_staging,
    )

    cases = audit_hostile_archive_validation_and_staging()
    checks = tuple(
        ExpansionGateCheck(
            code=case.name,
            status=(
                ExpansionGateStatus.FAIL
                if case.failures
                else ExpansionGateStatus.PASS
            ),
            detail=case.detail,
            required=True,
        )
        for case in cases
    )
    failures = tuple(
        f"{case.name}: {failure}"
        for case in cases
        for failure in case.failures
    )
    return ExpansionGateReport(
        card_id="WO39-B",
        status=(ExpansionGateStatus.FAIL if failures else ExpansionGateStatus.PASS),
        checks=checks,
        failures=failures,
        metadata=(
            ("audit_case_count", str(WO39B_AUDIT_CASE_COUNT)),
            ("hostile_fixture_count", "19"),
            ("preflight_policy", "WHOLE_ARCHIVE_BEFORE_EXTRACTION_V1"),
            ("staging_policy", "PRIVATE_NOFOLLOW_REVALIDATED_V1"),
        ),
    )


def _audit_wo39c() -> ExpansionGateReport:
    from kirby2.audit.packs import (
        WO39C_AUDIT_CASE_COUNT,
        audit_atomic_pack_installation,
    )

    cases = audit_atomic_pack_installation()
    checks = tuple(
        ExpansionGateCheck(
            code=case.name,
            status=(
                ExpansionGateStatus.FAIL
                if case.failures
                else ExpansionGateStatus.PASS
            ),
            detail=case.detail,
            required=True,
        )
        for case in cases
    )
    failures = tuple(
        f"{case.name}: {failure}"
        for case in cases
        for failure in case.failures
    )
    return ExpansionGateReport(
        card_id="WO39-C",
        status=(ExpansionGateStatus.FAIL if failures else ExpansionGateStatus.PASS),
        checks=checks,
        failures=failures,
        metadata=(
            ("audit_case_count", str(WO39C_AUDIT_CASE_COUNT)),
            ("dependency_policy", "LOCAL_EXACT_VERSION_AND_DIGEST_ONLY_V1"),
            ("activation_policy", "ATOMIC_REGISTRY_LOCKED_CAS_V1"),
            ("removal_policy", "DEACTIVATE_THEN_RECOVERABLE_MOVE_V1"),
        ),
    )


def _audit_wo38a() -> ExpansionGateReport:
    from kirby2.audit.orchestration import (
        WO38A_AUDIT_CASE_COUNT,
        audit_logical_work_and_attempt_identity,
    )

    cases = audit_logical_work_and_attempt_identity()
    checks = tuple(
        ExpansionGateCheck(
            code=case.name,
            status=(
                ExpansionGateStatus.FAIL
                if case.failures
                else ExpansionGateStatus.PASS
            ),
            detail=case.detail,
            required=True,
        )
        for case in cases
    )
    failures = tuple(
        f"{case.name}: {failure}"
        for case in cases
        for failure in case.failures
    )
    return ExpansionGateReport(
        card_id="WO38-A",
        status=(ExpansionGateStatus.FAIL if failures else ExpansionGateStatus.PASS),
        checks=checks,
        failures=failures,
        metadata=(
            ("audit_case_count", str(WO38A_AUDIT_CASE_COUNT)),
            ("logical_identity", "SHA256_CANONICAL_LOGICAL_WORK_UNIT_V1"),
            ("seed_policy", "KIRBY2_ORCHESTRATION_CELL_SEED_V1"),
            (
                "attempt_policy",
                "OPERATIONAL_HISTORY_OUTSIDE_SCIENTIFIC_IDENTITY_V1",
            ),
            ("distribution_boundary", "INDEPENDENT_COMPLETE_UNITS_ONLY_V1"),
        ),
    )


def _audit_wo38b() -> ExpansionGateReport:
    from kirby2.audit.orchestration import (
        WO38B_AUDIT_CASE_COUNT,
        audit_local_orchestration,
    )

    cases = audit_local_orchestration()
    checks = tuple(
        ExpansionGateCheck(
            code=case.name,
            status=(
                ExpansionGateStatus.FAIL
                if case.failures
                else ExpansionGateStatus.PASS
            ),
            detail=case.detail,
            required=True,
        )
        for case in cases
    )
    failures = tuple(
        f"{case.name}: {failure}"
        for case in cases
        for failure in case.failures
    )
    return ExpansionGateReport(
        card_id="WO38-B",
        status=(ExpansionGateStatus.FAIL if failures else ExpansionGateStatus.PASS),
        checks=checks,
        failures=failures,
        metadata=(
            ("audit_case_count", str(WO38B_AUDIT_CASE_COUNT)),
            ("protocol", "CANONICAL_TYPED_DATA_ONLY_V1"),
            (
                "compatibility",
                "EXACT_SOURCE_RUNTIME_DEPENDENCY_SCHEMA_CAPABILITY_V1",
            ),
            ("verification", "INDEPENDENT_REPLAY_BEFORE_REGISTRATION_V1"),
            ("backends", "SINGLE_AND_FIXED_SUBPROCESS_V1"),
        ),
    )


def _audit_wo38c() -> ExpansionGateReport:
    from kirby2.audit.orchestration import (
        WO38C_ORCHESTRATION_AUDIT_CASE_COUNT,
        audit_verified_content_exchange,
    )
    from kirby2.audit.packs import (
        WO38C_PACK_AUDIT_CASE_COUNT,
        audit_clean_root_pack_transfer,
    )

    cases = (
        *audit_verified_content_exchange(),
        *audit_clean_root_pack_transfer(),
    )
    checks = tuple(
        ExpansionGateCheck(
            code=case.name,
            status=(
                ExpansionGateStatus.FAIL
                if case.failures
                else ExpansionGateStatus.PASS
            ),
            detail=case.detail,
            required=True,
        )
        for case in cases
    )
    failures = tuple(
        f"{case.name}: {failure}"
        for case in cases
        for failure in case.failures
    )
    audit_case_count = (
        WO38C_ORCHESTRATION_AUDIT_CASE_COUNT
        + WO38C_PACK_AUDIT_CASE_COUNT
    )
    return ExpansionGateReport(
        card_id="WO38-C",
        status=(ExpansionGateStatus.FAIL if failures else ExpansionGateStatus.PASS),
        checks=checks,
        failures=failures,
        metadata=(
            ("audit_case_count", str(audit_case_count)),
            ("request_policy", "DIGEST_ONLY_PATH_FREE_V1"),
            (
                "receiver_policy",
                "PREFLIGHT_COMPATIBILITY_THEN_ATOMIC_INSTALL_V1",
            ),
            ("result_policy", "COORDINATOR_VERIFIED_IMMUTABLE_CAS_V1"),
            ("license_policy", "REDISTRIBUTION_FAIL_CLOSED_V1"),
        ),
    )


def _audit_wo38d() -> ExpansionGateReport:
    from kirby2.audit.orchestration import (
        WO38D_AUDIT_CASE_COUNT,
        audit_authenticated_lan_orchestration,
    )

    cases = audit_authenticated_lan_orchestration()
    checks = tuple(
        ExpansionGateCheck(
            code=case.name,
            status=(
                ExpansionGateStatus.FAIL
                if case.failures
                else ExpansionGateStatus.PASS
            ),
            detail=case.detail,
            required=True,
        )
        for case in cases
    )
    failures = tuple(
        f"{case.name}: {failure}"
        for case in cases
        for failure in case.failures
    )
    return ExpansionGateReport(
        card_id="WO38-D",
        status=(ExpansionGateStatus.FAIL if failures else ExpansionGateStatus.PASS),
        checks=checks,
        failures=failures,
        metadata=(
            ("audit_case_count", str(WO38D_AUDIT_CASE_COUNT)),
            ("default_bind", "LOOPBACK_EXPLICIT_LAN_OPT_IN_V1"),
            ("transport", "TLS13_MTLS_PINNED_NO_PLAINTEXT_V1"),
            ("fixture_policy", "AUDIT_LOOPBACK_ONLY_NOT_PRODUCTION_TRUST_V1"),
            ("operational_state", "LEASE_RESOURCE_RESTART_OUTSIDE_RESULTS_V1"),
            ("holdout_policy", "SEALED_STAGE_ACCESS_CONTROL_V1"),
        ),
    )


def _audit_wo38e() -> ExpansionGateReport:
    from kirby2.audit.orchestration import (
        WO38E_AUDIT_CASE_COUNT,
        audit_distributed_recovery,
    )

    cases = audit_distributed_recovery()
    checks = tuple(
        ExpansionGateCheck(
            code=case.name,
            status=(
                ExpansionGateStatus.FAIL
                if case.failures
                else ExpansionGateStatus.PASS
            ),
            detail=case.detail,
            required=True,
        )
        for case in cases
    )
    failures = tuple(
        f"{case.name}: {failure}"
        for case in cases
        for failure in case.failures
    )
    return ExpansionGateReport(
        card_id="WO38-E",
        status=(ExpansionGateStatus.FAIL if failures else ExpansionGateStatus.PASS),
        checks=checks,
        failures=failures,
        metadata=(
            ("audit_case_count", str(WO38E_AUDIT_CASE_COUNT)),
            ("reissue_policy", "SAME_LOGICAL_ID_NEW_ATTEMPT_V1"),
            ("late_result_policy", "IDEMPOTENT_OR_QUARANTINED_V1"),
            ("aggregation", "LOGICAL_ID_ORDER_EXACT_REDUCTION_V1"),
            ("cleanup", "UNREGISTERED_ATTEMPT_ONLY_V1"),
            ("demonstration", "KILL_RESTART_MULTI_PROCESS_WHOLE_RUN_V1"),
            ("lan_status", "NOT_EXERCISED_WITHOUT_EXPLICIT_CONFIGURATION"),
        ),
    )


def _audit_wo39d1() -> ExpansionGateReport:
    from kirby2.audit.packs import (
        WO39D1_AUDIT_CASE_COUNT,
        audit_training_domain_packs,
    )

    cases = audit_training_domain_packs()
    checks = tuple(
        ExpansionGateCheck(
            code=case.name,
            status=(
                ExpansionGateStatus.FAIL
                if case.failures
                else ExpansionGateStatus.PASS
            ),
            detail=case.detail,
            required=True,
        )
        for case in cases
    )
    failures = tuple(
        f"{case.name}: {failure}"
        for case in cases
        for failure in case.failures
    )
    return ExpansionGateReport(
        card_id="WO39-D1",
        status=(ExpansionGateStatus.FAIL if failures else ExpansionGateStatus.PASS),
        checks=checks,
        failures=failures,
        metadata=(
            ("audit_case_count", str(WO39D1_AUDIT_CASE_COUNT)),
            (
                "pack_types",
                "SCENARIO_LESSON_CURRICULUM_STRATEGY_MARKET_PROFILE_V1",
            ),
            (
                "identity_policy",
                "ORIGINAL_BYTES_AND_OWNING_LOGICAL_IDENTITY_V1",
            ),
            (
                "training_boundary",
                "SOURCE_DETECTOR_CAPABILITY_OBSERVABLE_REVEAL_SKILLS_SCORING_REVIEW_V1",
            ),
            ("lifecycle", "BUILD_INSPECT_VERIFY_INSTALL_LIST_REMOVE_V1"),
        ),
    )


def _audit_wo39d2() -> ExpansionGateReport:
    from kirby2.audit.evidence_packs import (
        WO39D2_AUDIT_CASE_COUNT,
        audit_evidence_domain_packs,
    )

    cases = audit_evidence_domain_packs()
    checks = tuple(
        ExpansionGateCheck(
            code=case.name,
            status=(
                ExpansionGateStatus.FAIL
                if case.failures
                else ExpansionGateStatus.PASS
            ),
            detail=case.detail,
            required=True,
        )
        for case in cases
    )
    failures = tuple(
        f"{case.name}: {failure}"
        for case in cases
        for failure in case.failures
    )
    return ExpansionGateReport(
        card_id="WO39-D2",
        status=(ExpansionGateStatus.FAIL if failures else ExpansionGateStatus.PASS),
        checks=checks,
        failures=failures,
        metadata=(
            ("audit_case_count", str(WO39D2_AUDIT_CASE_COUNT)),
            ("pack_types", "HISTORICAL_REPLAY_ANALYSIS_RESEARCH_V1"),
            (
                "historical_policy",
                "CAPABILITY_LICENSE_SELF_CONTAINED_OR_REFERENCE_ONLY_V1",
            ),
            (
                "replay_policy",
                "REGISTERED_ARTIFACTS_ONLY_OWNING_RUN_IDENTITY_V1",
            ),
            (
                "privacy_policy",
                "CONSENT_BOUND_FIELD_REDACTED_NO_DIRECT_IDENTITY_V1",
            ),
            ("renderer_policy", "INSTALLED_RENDERER_DATA_ONLY_PACK_V1"),
        ),
    )


def _audit_wo39e() -> ExpansionGateReport:
    from kirby2.audit.packs import WO39E_AUDIT_CASE_COUNT, audit_pack_portability

    cases = audit_pack_portability()
    checks = tuple(
        ExpansionGateCheck(
            code=case.name,
            status=(
                ExpansionGateStatus.FAIL
                if case.failures
                else ExpansionGateStatus.PASS
            ),
            detail=case.detail,
            required=True,
        )
        for case in cases
    )
    failures = tuple(
        f"{case.name}: {failure}"
        for case in cases
        for failure in case.failures
    )
    return ExpansionGateReport(
        card_id="WO39-E",
        status=(ExpansionGateStatus.FAIL if failures else ExpansionGateStatus.PASS),
        checks=checks,
        failures=failures,
        metadata=(
            ("audit_case_count", str(WO39E_AUDIT_CASE_COUNT)),
            ("sample_group_count", "5"),
            ("hostile_source_case_count", "15"),
            (
                "portability_policy",
                "OFFLINE_CLEAN_ROOT_CONTENT_ADDRESSED_REPLAY_EQUIVALENCE_V1",
            ),
            (
                "signature_policy",
                "OPTIONAL_PROVIDER_AUTHENTICITY_NEVER_OVERRIDES_SAFETY_V1",
            ),
            (
                "removal_policy",
                "DEPENDENCY_SAFE_RECOVERABLE_RUN_EVIDENCE_PRESERVING_V1",
            ),
        ),
    )


def _audit_dev0008() -> ExpansionGateReport:
    """Bind the final WO39-E starter identities into the release protocol."""

    from kirby2.release.build import load_release_protocol_bundle
    from kirby2.release.first_run import build_release_starter_set

    repository = Path(__file__).resolve().parents[2]
    expected_starter_set = {
        "entries": [
            {
                "manifest_path": (
                    "kirby2/packs/fixtures/samples/starter_scenario/manifest.toml"
                ),
                "manifest_sha256": (
                    "d461beff0be99750f074154be4eac8c20f292354f5357b4789bad133c727d898"
                ),
                "pack_id": (
                    "303bcf354eea3b952bcc194c380a976ddbd67a6d59950960f2ee81562dbe7405"
                ),
                "role": "SCENARIO",
            },
            {
                "manifest_path": (
                    "kirby2/packs/fixtures/samples/five_lesson_curriculum/manifest.toml"
                ),
                "manifest_sha256": (
                    "66b3f8942bd96267a7ac8dcc9a7b070cce73e5ba7f9d4166740afe0a6499273e"
                ),
                "pack_id": (
                    "ec4df68073f2bd8cd174825f289f22d9430fb7ad731d8dac096db6cb0d806864"
                ),
                "role": "CURRICULUM",
            },
        ],
        "entries_sha256": (
            "637a7c17fa5343eefebf167ed7f0bcb78746fa35d3715bc28624f902d3c83223"
        ),
        "schema_version": 1,
        "set_id": "RELEASE_STARTER_SET_V1",
    }
    starter_set = build_release_starter_set()
    bundle = load_release_protocol_bundle(repository)
    members = {item.member_id: item for item in bundle.artifact_layout.members}
    expected_archive_paths = {
        "starter-scenario-pack": (
            "starter-packs/"
            "303bcf354eea3b952bcc194c380a976ddbd67a6d59950960f2ee81562dbe7405"
            ".k2pack"
        ),
        "starter-curriculum-pack": (
            "starter-packs/"
            "ec4df68073f2bd8cd174825f289f22d9430fb7ad731d8dac096db6cb0d806864"
            ".k2pack"
        ),
    }
    checks = (
        ExpansionGateCheck(
            code="canonical_starter_build_has_final_content_identities",
            status=(
                ExpansionGateStatus.PASS
                if starter_set.layout_dict() == expected_starter_set
                else ExpansionGateStatus.FAIL
            ),
            detail=(
                "scenario=303bcf354eea curriculum=ec4df68073f2 "
                "set=637a7c17fa53"
            ),
        ),
        ExpansionGateCheck(
            code="release_layout_binds_exact_final_starter_set",
            status=(
                ExpansionGateStatus.PASS
                if bundle.artifact_layout.starter_set == expected_starter_set
                else ExpansionGateStatus.FAIL
            ),
            detail="release artifact layout matches both committed starter manifests",
        ),
        ExpansionGateCheck(
            code="release_archive_members_use_content_addressed_pack_names",
            status=(
                ExpansionGateStatus.PASS
                if all(
                    member_id in members
                    and members[member_id].archive_path == archive_path
                    for member_id, archive_path in expected_archive_paths.items()
                )
                else ExpansionGateStatus.FAIL
            ),
            detail="both starter archive paths equal their final content-derived IDs",
        ),
    )
    failures = tuple(
        f"{check.code}: {check.detail}"
        for check in checks
        if check.status is ExpansionGateStatus.FAIL
    )
    return ExpansionGateReport(
        card_id="DEV-0008",
        status=(ExpansionGateStatus.FAIL if failures else ExpansionGateStatus.PASS),
        checks=checks,
        failures=failures,
        metadata=(
            ("interrupted_card", "WO40-E"),
            ("release_starter_set_id", "RELEASE_STARTER_SET_V1"),
            ("starter_member_count", "2"),
        ),
    )


def _release_suite_report(
    card_id: str,
    suite: object,
) -> ExpansionGateReport:
    """Translate the independent release-audit result into the expansion contract."""

    from kirby2.audit.release import ReleaseAuditSuite

    if type(suite) is not ReleaseAuditSuite or suite.gate_id != card_id:
        raise TypeError("release audit suite identity differs from its registered gate")
    checks = tuple(
        ExpansionGateCheck(
            code=case.name,
            status=ExpansionGateStatus(case.status.value),
            detail=case.detail,
            required=True,
        )
        for case in suite.cases
    )
    metadata = (
        ("audit_case_count", str(len(suite.cases))),
        *suite.metadata,
    )
    return ExpansionGateReport(
        card_id=card_id,
        status=ExpansionGateStatus(suite.status.value),
        checks=checks,
        failures=suite.failures,
        warnings=suite.warnings,
        reason_code=suite.reason_code,
        metadata=metadata,
    )


def _audit_wo40a() -> ExpansionGateReport:
    from kirby2.audit.release import audit_release_data_and_migrations

    return _release_suite_report("WO40-A", audit_release_data_and_migrations())


def _audit_wo40b() -> ExpansionGateReport:
    from kirby2.audit.release import audit_release_recovery

    return _release_suite_report("WO40-B", audit_release_recovery())


def _audit_wo40b1() -> ExpansionGateReport:
    from kirby2.audit.release import audit_release_backup_restore

    return _release_suite_report("WO40-B1", audit_release_backup_restore())


def _audit_wo40c() -> ExpansionGateReport:
    from kirby2.audit.release import audit_release_first_run

    return _release_suite_report("WO40-C", audit_release_first_run())


def _audit_wo40d() -> ExpansionGateReport:
    from kirby2.audit.release import audit_release_protocol

    return _release_suite_report("WO40-D", audit_release_protocol())


def _audit_wo40d1() -> ExpansionGateReport:
    from kirby2.audit.release import audit_release_resource_report

    return _release_suite_report("WO40-D1", audit_release_resource_report())


def _audit_dev0009() -> ExpansionGateReport:
    from kirby2.audit.release import audit_release_frontier_registration

    return _release_suite_report(
        "DEV-0009",
        audit_release_frontier_registration(),
    )


def _audit_dev0010() -> ExpansionGateReport:
    from kirby2.audit.release import audit_release_preflight_provenance

    return _release_suite_report(
        "DEV-0010",
        audit_release_preflight_provenance(),
    )


def _audit_wo40e() -> ExpansionGateReport:
    from kirby2.audit.release import audit_release_candidate_source

    return _release_suite_report("WO40-E", audit_release_candidate_source())


def _audit_dev0011() -> ExpansionGateReport:
    from kirby2.audit.release import audit_release_candidate_input_restart

    return _release_suite_report(
        "DEV-0011",
        audit_release_candidate_input_restart(),
    )


def _audit_dev0012() -> ExpansionGateReport:
    from kirby2.audit.release import audit_release_resource_fingerprint_restart

    return _release_suite_report(
        "DEV-0012",
        audit_release_resource_fingerprint_restart(),
    )


def _audit_dev0013() -> ExpansionGateReport:
    from kirby2.audit.release import audit_release_artifact_executor_restart

    return _release_suite_report(
        "DEV-0013",
        audit_release_artifact_executor_restart(),
    )


def _audit_wo40f() -> ExpansionGateReport:
    from kirby2.audit.release import audit_release_build_evidence

    return _release_suite_report("WO40-F", audit_release_build_evidence())


def _audit_dev0014() -> ExpansionGateReport:
    from kirby2.audit.release import audit_release_qualification_executor_restart

    return _release_suite_report(
        "DEV-0014",
        audit_release_qualification_executor_restart(),
    )


def _audit_wo40g() -> ExpansionGateReport:
    from kirby2.audit.release import audit_release_macos_evidence

    return _release_suite_report("WO40-G", audit_release_macos_evidence())


def _audit_wo40h() -> ExpansionGateReport:
    from kirby2.audit.release import audit_release_linux_evidence

    return _release_suite_report("WO40-H", audit_release_linux_evidence())


def _audit_wo40i() -> ExpansionGateReport:
    from kirby2.audit.release import audit_release_performance_evidence

    return _release_suite_report("WO40-I", audit_release_performance_evidence())


def _audit_wo40j() -> ExpansionGateReport:
    from kirby2.audit.release import audit_release_closeout_prerequisites

    return _release_suite_report(
        "WO40-J",
        audit_release_closeout_prerequisites(),
    )


GATE_SPECS: tuple[tuple[str, ExpansionGate], ...] = (
    ("DEV-0001", _audit_dev0001),
    ("K2X-02", _audit_k2x02),
    ("WO31-A", _audit_wo31a),
    ("DEV-0002", _audit_dev0002),
    ("DEV-0003", _audit_dev0003),
    ("WO31-B", _audit_wo31b),
    ("WO31-C", _audit_wo31c),
    ("WO31-D", _audit_wo31d),
    ("WO31-E1", _audit_wo31e1),
    ("WO31-E2", _audit_wo31e2),
    ("WO31-E3", _audit_wo31e3),
    ("WO31-E4", _audit_wo31e4),
    ("WO31-E5", _audit_wo31e5),
    ("DEV-0004", _audit_dev0004),
    ("WO31-E6", _audit_wo31e6),
    ("WO31-F", _audit_wo31f),
    ("WO31-G", _audit_wo31g),
    ("WO31-H", _audit_wo31h),
    ("WO31-I", _audit_wo31i),
    ("WO31-I1", _audit_wo31i1),
    ("WO32-A", _audit_wo32a),
    ("WO32-B", _audit_wo32b),
    ("WO32-C", _audit_wo32c),
    ("WO32-D", _audit_wo32d),
    ("WO32-E", _audit_wo32e),
    ("WO33-A", _audit_wo33a),
    ("WO33-A1", _audit_wo33a1),
    ("WO33-B1", _audit_wo33b1),
    ("WO33-B2", _audit_wo33b2),
    ("WO33-C", _audit_wo33c),
    ("WO33-D", _audit_wo33d),
    ("WO33-E", _audit_wo33e),
    ("WO34-A", _audit_wo34a),
    ("WO34-B", _audit_wo34b),
    ("WO34-C", _audit_wo34c),
    ("WO34-D", _audit_wo34d),
    ("WO35-A", _audit_wo35a),
    ("WO35-B", _audit_wo35b),
    ("WO35-C", _audit_wo35c),
    ("WO35-D", _audit_wo35d),
    ("WO35-E", _audit_wo35e),
    ("WO35-F", _audit_wo35f),
    ("WO35-F1", _audit_wo35f1),
    ("WO36-A", _audit_wo36a),
    ("WO36-B", _audit_wo36b),
    ("DEV-0005", _audit_dev0005),
    ("DEV-0006", _audit_dev0006),
    ("WO36-C", _audit_wo36c),
    ("WO36-D", _audit_wo36d),
    ("WO36-E", _audit_wo36e),
    ("DEV-0007", _audit_dev0007),
    ("WO37-A", _audit_wo37a),
    ("WO37-B", _audit_wo37b),
    ("WO37-C", _audit_wo37c),
    ("WO37-D", _audit_wo37d),
    ("WO37-E", _audit_wo37e),
    ("WO39-A", _audit_wo39a),
    ("WO39-B", _audit_wo39b),
    ("WO39-C", _audit_wo39c),
    ("WO38-A", _audit_wo38a),
    ("WO38-B", _audit_wo38b),
    ("WO38-C", _audit_wo38c),
    ("WO38-D", _audit_wo38d),
    ("WO38-E", _audit_wo38e),
    ("WO39-D1", _audit_wo39d1),
    ("WO39-D2", _audit_wo39d2),
    ("WO39-E", _audit_wo39e),
    ("WO40-A", _audit_wo40a),
    ("WO40-B", _audit_wo40b),
    ("WO40-B1", _audit_wo40b1),
    ("WO40-C", _audit_wo40c),
    ("WO40-D", _audit_wo40d),
    ("WO40-D1", _audit_wo40d1),
    ("DEV-0008", _audit_dev0008),
    ("DEV-0009", _audit_dev0009),
    ("DEV-0010", _audit_dev0010),
    ("WO40-E", _audit_wo40e),
    ("DEV-0011", _audit_dev0011),
    ("DEV-0012", _audit_dev0012),
    ("DEV-0013", _audit_dev0013),
    ("WO40-F", _audit_wo40f),
    ("DEV-0014", _audit_dev0014),
    ("WO40-G", _audit_wo40g),
    ("WO40-H", _audit_wo40h),
    ("WO40-I", _audit_wo40i),
    ("WO40-J", _audit_wo40j),
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
