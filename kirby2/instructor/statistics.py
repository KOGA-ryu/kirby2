"""Deterministic descriptive statistics for instructor cohort evidence.

The durable identities in this module contain integers, enums, text, and reduced
rational numbers only.  Binary floating point is neither accepted nor emitted.
An observation's exact measured value is ``value / scale`` and its declared
``denominator``/``exposure`` is its positive aggregation weight.  A cohort estimate
is therefore the exact exposure-weighted arithmetic mean.

V1 deliberately uses a small, assumption-free uncertainty description:
``EXACT_OBSERVED_RANGE_CONSERVATIVE_V1``.  Its bounds are the minimum and maximum
included observations.  It describes observed spread only; it is not a confidence
interval for a population parameter.  Exact rational estimates are additionally
rendered at the declared scale with round-half-to-even.  Interval lower bounds use
floor and upper bounds use ceiling, so rendering can never narrow the exact range.

Version compatibility and claim capability are fail-closed.  More than one score,
model, or analysis signature can only be returned as explicit strata.  It can never
be pooled.  Causal wording requires both a causal design and a causal analysis;
descriptive capability is the default throughout.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from enum import Enum


VERSION_SIGNATURE_SCHEMA_ID = "KIRBY2_VERSION_SIGNATURE_V1"
VERSION_SIGNATURE_SCHEMA_VERSION = 1
METRIC_OBSERVATION_SCHEMA_ID = "KIRBY2_METRIC_OBSERVATION_V1"
METRIC_OBSERVATION_SCHEMA_VERSION = 1
RATIONAL_VALUE_SCHEMA_ID = "KIRBY2_RATIONAL_VALUE_V1"
RATIONAL_VALUE_SCHEMA_VERSION = 1
UNCERTAINTY_INTERVAL_SCHEMA_ID = "KIRBY2_UNCERTAINTY_INTERVAL_V1"
UNCERTAINTY_INTERVAL_SCHEMA_VERSION = 1
MISSING_REASON_COUNT_SCHEMA_ID = "KIRBY2_MISSING_REASON_COUNT_V1"
MISSING_REASON_COUNT_SCHEMA_VERSION = 1
COMPATIBILITY_DECISION_SCHEMA_ID = "KIRBY2_COMPATIBILITY_DECISION_V1"
COMPATIBILITY_DECISION_SCHEMA_VERSION = 1
DESCRIPTIVE_ESTIMATE_SCHEMA_ID = "KIRBY2_DESCRIPTIVE_ESTIMATE_V1"
DESCRIPTIVE_ESTIMATE_SCHEMA_VERSION = 1
DESCRIPTIVE_SUMMARY_SCHEMA_ID = "KIRBY2_DESCRIPTIVE_SUMMARY_V1"
DESCRIPTIVE_SUMMARY_SCHEMA_VERSION = 1

ESTIMATE_ROUNDING_RULE_V1 = "ROUND_HALF_TO_EVEN_AT_DECLARED_SCALE"
INTERVAL_ROUNDING_RULE_V1 = "LOWER_FLOOR_UPPER_CEILING_AT_DECLARED_SCALE"

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}\Z")


class AnalysisCapabilityV1(str, Enum):
    """The strongest language supported by an analysis."""

    DESCRIPTIVE = "DESCRIPTIVE"
    CAUSAL = "CAUSAL"


class CompatibilityActionV1(str, Enum):
    """Caller decision for observations carrying different version signatures."""

    POOL = "POOL"
    STRATIFY = "STRATIFY"
    REFUSE = "REFUSE"


class CompatibilityResolutionV1(str, Enum):
    """Recorded outcome of applying a compatibility action."""

    POOLED = "POOLED"
    STRATIFIED = "STRATIFIED"
    REFUSED = "REFUSED"


class UncertaintyMethodV1(str, Enum):
    """Closed V1 uncertainty vocabulary."""

    EXACT_OBSERVED_RANGE_CONSERVATIVE_V1 = (
        "EXACT_OBSERVED_RANGE_CONSERVATIVE_V1"
    )


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")


def _canonical_object(raw: bytes, label: str) -> dict[str, object]:
    if type(raw) is not bytes:
        raise TypeError(f"{label} decoder requires exact bytes")
    try:
        value = json.loads(raw.decode("ascii"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"{label} must be canonical ASCII JSON") from error
    if type(value) is not dict or _canonical_json_bytes(value) != raw:
        raise ValueError(f"{label} must be one canonical JSON object")
    return value


def _fields(value: object, expected: frozenset[str], label: str) -> dict[str, object]:
    if type(value) is not dict:
        raise TypeError(f"{label} must be an exact object")
    actual = frozenset(value)
    if actual != expected:
        raise ValueError(
            f"{label} fields differ: "
            f"missing={sorted(expected - actual)}, extra={sorted(actual - expected)}"
        )
    return value


def _identifier(value: object, label: str) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"{label} must be one canonical identifier")
    return value


def _text(value: object, label: str) -> str:
    if type(value) is not str:
        raise TypeError(f"{label} must be exact text")
    if not value or value != value.strip():
        raise ValueError(f"{label} must be nonempty text without edge whitespace")
    if len(value.encode("utf-8")) > 1024:
        raise ValueError(f"{label} is too long")
    return value


def _digest(value: object, label: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{label} must be one lowercase SHA-256 digest")
    return value


def _positive_int(value: object, label: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _nonnegative_int(value: object, label: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{label} must be a nonnegative integer")
    return value


def _exact_bool(value: object, label: str) -> bool:
    if type(value) is not bool:
        raise TypeError(f"{label} must be an exact boolean")
    return value


def _schema(
    schema_id: object,
    schema_version: object,
    *,
    expected_id: str,
    expected_version: int,
    label: str,
) -> None:
    if type(schema_id) is not str or schema_id != expected_id:
        raise ValueError(f"{label} schema ID changed")
    if type(schema_version) is not int or schema_version != expected_version:
        raise ValueError(f"{label} schema version changed")


def _json_array(value: object, label: str) -> list[object]:
    if type(value) is not list:
        raise TypeError(f"{label} must be a JSON array")
    return value


def _compare_rationals(left: RationalValueV1, right: RationalValueV1) -> int:
    difference = left.numerator * right.denominator - right.numerator * left.denominator
    return (difference > 0) - (difference < 0)


def _round_half_even(numerator: int, denominator: int) -> int:
    """Round one exact rational to an integer, with ties going to an even integer."""

    if denominator <= 0:
        raise ValueError("rounding denominator must be positive")
    sign = -1 if numerator < 0 else 1
    quotient, remainder = divmod(abs(numerator), denominator)
    twice_remainder = 2 * remainder
    if twice_remainder > denominator or (
        twice_remainder == denominator and quotient % 2 == 1
    ):
        quotient += 1
    return sign * quotient


@dataclass(frozen=True, slots=True)
class VersionSignatureV1:
    """Exact score/model/analysis implementation identity for one observation."""

    score_version: int
    score_sha256: str
    model_version: int
    model_sha256: str
    analysis_version: int
    analysis_sha256: str
    schema_id: str = VERSION_SIGNATURE_SCHEMA_ID
    schema_version: int = VERSION_SIGNATURE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _positive_int(self.score_version, "score version")
        _digest(self.score_sha256, "score digest")
        _positive_int(self.model_version, "model version")
        _digest(self.model_sha256, "model digest")
        _positive_int(self.analysis_version, "analysis version")
        _digest(self.analysis_sha256, "analysis digest")
        _schema(
            self.schema_id,
            self.schema_version,
            expected_id=VERSION_SIGNATURE_SCHEMA_ID,
            expected_version=VERSION_SIGNATURE_SCHEMA_VERSION,
            label="version signature",
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "analysis_sha256": self.analysis_sha256,
            "analysis_version": self.analysis_version,
            "model_sha256": self.model_sha256,
            "model_version": self.model_version,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "score_sha256": self.score_sha256,
            "score_version": self.score_version,
        }

    def canonical_bytes(self) -> bytes:
        return _canonical_json_bytes(self.as_dict())

    @property
    def signature_sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    @classmethod
    def from_dict(cls, raw: object) -> VersionSignatureV1:
        value = _fields(
            raw,
            frozenset(
                {
                    "analysis_sha256",
                    "analysis_version",
                    "model_sha256",
                    "model_version",
                    "schema_id",
                    "schema_version",
                    "score_sha256",
                    "score_version",
                }
            ),
            "version signature",
        )
        result = cls(**value)  # type: ignore[arg-type]
        if result.as_dict() != value:
            raise ValueError("version signature did not round-trip exactly")
        return result

    @classmethod
    def from_canonical_bytes(cls, raw: bytes) -> VersionSignatureV1:
        return cls.from_dict(_canonical_object(raw, "version signature"))


@dataclass(frozen=True, slots=True)
class MetricObservationV1:
    """One present scaled-integer measurement or one explicit missing outcome."""

    metric_id: str
    observation_id: str
    version_signature: VersionSignatureV1
    present: bool
    value: int | None
    scale: int
    denominator: int | None = None
    exposure: int | None = None
    missing_reason: str | None = None
    schema_id: str = METRIC_OBSERVATION_SCHEMA_ID
    schema_version: int = METRIC_OBSERVATION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _identifier(self.metric_id, "metric ID")
        _identifier(self.observation_id, "observation ID")
        if type(self.version_signature) is not VersionSignatureV1:
            raise TypeError("observation version signature must be VersionSignatureV1")
        _exact_bool(self.present, "observation presence")
        _positive_int(self.scale, "observation scale")
        if self.present:
            if type(self.value) is not int:
                raise TypeError("present observation value must be an exact integer")
            if self.denominator is None and self.exposure is None:
                raise ValueError("present observation requires a denominator or exposure")
            if self.denominator is not None:
                _positive_int(self.denominator, "observation denominator")
            if self.exposure is not None:
                _positive_int(self.exposure, "observation exposure")
            if (
                self.denominator is not None
                and self.exposure is not None
                and self.denominator != self.exposure
            ):
                raise ValueError("observation denominator and exposure differ")
            if self.missing_reason is not None:
                raise ValueError("present observation cannot have a missing reason")
        else:
            if self.value is not None:
                raise ValueError("missing observation cannot carry a value")
            if self.denominator is not None or self.exposure is not None:
                raise ValueError("missing observation cannot invent denominator/exposure")
            _text(self.missing_reason, "observation missing reason")
        _schema(
            self.schema_id,
            self.schema_version,
            expected_id=METRIC_OBSERVATION_SCHEMA_ID,
            expected_version=METRIC_OBSERVATION_SCHEMA_VERSION,
            label="metric observation",
        )

    @property
    def effective_exposure(self) -> int | None:
        if not self.present:
            return None
        return self.exposure if self.exposure is not None else self.denominator

    def as_dict(self) -> dict[str, object]:
        return {
            "denominator": self.denominator,
            "exposure": self.exposure,
            "metric_id": self.metric_id,
            "missing_reason": self.missing_reason,
            "observation_id": self.observation_id,
            "present": self.present,
            "scale": self.scale,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "value": self.value,
            "version_signature": self.version_signature.as_dict(),
        }

    def canonical_bytes(self) -> bytes:
        return _canonical_json_bytes(self.as_dict())

    @classmethod
    def from_dict(cls, raw: object) -> MetricObservationV1:
        value = _fields(
            raw,
            frozenset(
                {
                    "denominator",
                    "exposure",
                    "metric_id",
                    "missing_reason",
                    "observation_id",
                    "present",
                    "scale",
                    "schema_id",
                    "schema_version",
                    "value",
                    "version_signature",
                }
            ),
            "metric observation",
        )
        result = cls(
            metric_id=value["metric_id"],  # type: ignore[arg-type]
            observation_id=value["observation_id"],  # type: ignore[arg-type]
            version_signature=VersionSignatureV1.from_dict(
                value["version_signature"]
            ),
            present=value["present"],  # type: ignore[arg-type]
            value=value["value"],  # type: ignore[arg-type]
            scale=value["scale"],  # type: ignore[arg-type]
            denominator=value["denominator"],  # type: ignore[arg-type]
            exposure=value["exposure"],  # type: ignore[arg-type]
            missing_reason=value["missing_reason"],  # type: ignore[arg-type]
            schema_id=value["schema_id"],  # type: ignore[arg-type]
            schema_version=value["schema_version"],  # type: ignore[arg-type]
        )
        if result.as_dict() != value:
            raise ValueError("metric observation did not round-trip exactly")
        return result

    @classmethod
    def from_canonical_bytes(cls, raw: bytes) -> MetricObservationV1:
        return cls.from_dict(_canonical_object(raw, "metric observation"))


@dataclass(frozen=True, slots=True)
class RationalValueV1:
    """One canonical reduced rational with a positive denominator."""

    numerator: int
    denominator: int
    schema_id: str = RATIONAL_VALUE_SCHEMA_ID
    schema_version: int = RATIONAL_VALUE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if type(self.numerator) is not int:
            raise TypeError("rational numerator must be an exact integer")
        _positive_int(self.denominator, "rational denominator")
        if math.gcd(abs(self.numerator), self.denominator) != 1:
            raise ValueError("rational value must be reduced")
        _schema(
            self.schema_id,
            self.schema_version,
            expected_id=RATIONAL_VALUE_SCHEMA_ID,
            expected_version=RATIONAL_VALUE_SCHEMA_VERSION,
            label="rational value",
        )

    @classmethod
    def from_fraction(cls, numerator: int, denominator: int) -> RationalValueV1:
        if type(numerator) is not int or type(denominator) is not int:
            raise TypeError("rational fraction requires exact integers")
        if denominator <= 0:
            raise ValueError("rational denominator must be positive")
        divisor = math.gcd(abs(numerator), denominator)
        return cls(numerator=numerator // divisor, denominator=denominator // divisor)

    def as_dict(self) -> dict[str, object]:
        return {
            "denominator": self.denominator,
            "numerator": self.numerator,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
        }

    def canonical_bytes(self) -> bytes:
        return _canonical_json_bytes(self.as_dict())

    def round_half_even(self, scale: int = 1) -> int:
        _positive_int(scale, "rational rendering scale")
        return _round_half_even(self.numerator * scale, self.denominator)

    def floor(self, scale: int = 1) -> int:
        _positive_int(scale, "rational rendering scale")
        return (self.numerator * scale) // self.denominator

    def ceiling(self, scale: int = 1) -> int:
        _positive_int(scale, "rational rendering scale")
        return -((-self.numerator * scale) // self.denominator)

    @classmethod
    def from_dict(cls, raw: object) -> RationalValueV1:
        value = _fields(
            raw,
            frozenset({"denominator", "numerator", "schema_id", "schema_version"}),
            "rational value",
        )
        result = cls(**value)  # type: ignore[arg-type]
        if result.as_dict() != value:
            raise ValueError("rational value did not round-trip exactly")
        return result

    @classmethod
    def from_canonical_bytes(cls, raw: bytes) -> RationalValueV1:
        return cls.from_dict(_canonical_object(raw, "rational value"))


@dataclass(frozen=True, slots=True)
class UncertaintyIntervalV1:
    """Exact descriptive bounds plus their outward-rounded scaled rendering."""

    method: UncertaintyMethodV1
    lower: RationalValueV1
    upper: RationalValueV1
    scale: int
    lower_scaled: int
    upper_scaled: int
    rounding_rule: str = INTERVAL_ROUNDING_RULE_V1
    schema_id: str = UNCERTAINTY_INTERVAL_SCHEMA_ID
    schema_version: int = UNCERTAINTY_INTERVAL_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if type(self.method) is not UncertaintyMethodV1:
            raise TypeError("uncertainty method must be UncertaintyMethodV1")
        if type(self.lower) is not RationalValueV1 or type(self.upper) is not RationalValueV1:
            raise TypeError("uncertainty bounds must be RationalValueV1 values")
        if _compare_rationals(self.lower, self.upper) > 0:
            raise ValueError("uncertainty lower bound exceeds upper bound")
        _positive_int(self.scale, "uncertainty rendering scale")
        if type(self.lower_scaled) is not int or type(self.upper_scaled) is not int:
            raise TypeError("uncertainty scaled bounds must be exact integers")
        if self.lower_scaled != self.lower.floor(self.scale):
            raise ValueError("uncertainty lower bound is not outward floor-rounded")
        if self.upper_scaled != self.upper.ceiling(self.scale):
            raise ValueError("uncertainty upper bound is not outward ceiling-rounded")
        if self.rounding_rule != INTERVAL_ROUNDING_RULE_V1:
            raise ValueError("uncertainty rounding rule changed")
        _schema(
            self.schema_id,
            self.schema_version,
            expected_id=UNCERTAINTY_INTERVAL_SCHEMA_ID,
            expected_version=UNCERTAINTY_INTERVAL_SCHEMA_VERSION,
            label="uncertainty interval",
        )

    @classmethod
    def observed_range(
        cls,
        lower: RationalValueV1,
        upper: RationalValueV1,
        *,
        scale: int,
    ) -> UncertaintyIntervalV1:
        return cls(
            method=UncertaintyMethodV1.EXACT_OBSERVED_RANGE_CONSERVATIVE_V1,
            lower=lower,
            upper=upper,
            scale=scale,
            lower_scaled=lower.floor(scale),
            upper_scaled=upper.ceiling(scale),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "lower": self.lower.as_dict(),
            "lower_scaled": self.lower_scaled,
            "method": self.method.value,
            "rounding_rule": self.rounding_rule,
            "scale": self.scale,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "upper": self.upper.as_dict(),
            "upper_scaled": self.upper_scaled,
        }

    def canonical_bytes(self) -> bytes:
        return _canonical_json_bytes(self.as_dict())

    @classmethod
    def from_dict(cls, raw: object) -> UncertaintyIntervalV1:
        value = _fields(
            raw,
            frozenset(
                {
                    "lower",
                    "lower_scaled",
                    "method",
                    "rounding_rule",
                    "scale",
                    "schema_id",
                    "schema_version",
                    "upper",
                    "upper_scaled",
                }
            ),
            "uncertainty interval",
        )
        result = cls(
            method=UncertaintyMethodV1(value["method"]),
            lower=RationalValueV1.from_dict(value["lower"]),
            upper=RationalValueV1.from_dict(value["upper"]),
            scale=value["scale"],  # type: ignore[arg-type]
            lower_scaled=value["lower_scaled"],  # type: ignore[arg-type]
            upper_scaled=value["upper_scaled"],  # type: ignore[arg-type]
            rounding_rule=value["rounding_rule"],  # type: ignore[arg-type]
            schema_id=value["schema_id"],  # type: ignore[arg-type]
            schema_version=value["schema_version"],  # type: ignore[arg-type]
        )
        if result.as_dict() != value:
            raise ValueError("uncertainty interval did not round-trip exactly")
        return result

    @classmethod
    def from_canonical_bytes(cls, raw: bytes) -> UncertaintyIntervalV1:
        return cls.from_dict(_canonical_object(raw, "uncertainty interval"))


@dataclass(frozen=True, slots=True)
class MissingReasonCountV1:
    """Canonical count for one exact missing-data reason."""

    reason: str
    count: int
    schema_id: str = MISSING_REASON_COUNT_SCHEMA_ID
    schema_version: int = MISSING_REASON_COUNT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _text(self.reason, "missing reason")
        _positive_int(self.count, "missing reason count")
        _schema(
            self.schema_id,
            self.schema_version,
            expected_id=MISSING_REASON_COUNT_SCHEMA_ID,
            expected_version=MISSING_REASON_COUNT_SCHEMA_VERSION,
            label="missing reason count",
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "count": self.count,
            "reason": self.reason,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
        }

    def canonical_bytes(self) -> bytes:
        return _canonical_json_bytes(self.as_dict())

    @classmethod
    def from_dict(cls, raw: object) -> MissingReasonCountV1:
        value = _fields(
            raw,
            frozenset({"count", "reason", "schema_id", "schema_version"}),
            "missing reason count",
        )
        result = cls(**value)  # type: ignore[arg-type]
        if result.as_dict() != value:
            raise ValueError("missing reason count did not round-trip exactly")
        return result


@dataclass(frozen=True, slots=True)
class CompatibilityDecisionV1:
    """Durable proof that signatures were pooled, stratified, or refused."""

    action: CompatibilityActionV1
    resolution: CompatibilityResolutionV1
    reason: str
    signatures: tuple[VersionSignatureV1, ...]
    schema_id: str = COMPATIBILITY_DECISION_SCHEMA_ID
    schema_version: int = COMPATIBILITY_DECISION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if type(self.action) is not CompatibilityActionV1:
            raise TypeError("compatibility action must be CompatibilityActionV1")
        if type(self.resolution) is not CompatibilityResolutionV1:
            raise TypeError("compatibility resolution must be CompatibilityResolutionV1")
        _identifier(self.reason, "compatibility reason")
        if type(self.signatures) is not tuple or not self.signatures:
            raise ValueError("compatibility decision requires version signatures")
        if any(type(item) is not VersionSignatureV1 for item in self.signatures):
            raise TypeError("compatibility signatures must be VersionSignatureV1 values")
        canonical = tuple(sorted(set(self.signatures), key=lambda item: item.canonical_bytes()))
        if canonical != self.signatures:
            raise ValueError("compatibility signatures must be unique and canonical")
        if len(self.signatures) == 1 and self.resolution is not CompatibilityResolutionV1.POOLED:
            raise ValueError("one version signature must resolve as pooled")
        if len(self.signatures) > 1:
            if self.resolution is CompatibilityResolutionV1.POOLED:
                raise ValueError("incompatible version signatures cannot be pooled")
            if (
                self.resolution is CompatibilityResolutionV1.STRATIFIED
                and self.action is not CompatibilityActionV1.STRATIFY
            ):
                raise ValueError("only explicit STRATIFY can resolve as stratified")
            if (
                self.resolution is CompatibilityResolutionV1.REFUSED
                and self.action is CompatibilityActionV1.STRATIFY
            ):
                raise ValueError("STRATIFY cannot produce a compatibility refusal")
        _schema(
            self.schema_id,
            self.schema_version,
            expected_id=COMPATIBILITY_DECISION_SCHEMA_ID,
            expected_version=COMPATIBILITY_DECISION_SCHEMA_VERSION,
            label="compatibility decision",
        )

    @property
    def signature_count(self) -> int:
        return len(self.signatures)

    @property
    def can_pool(self) -> bool:
        return self.signature_count == 1

    def as_dict(self) -> dict[str, object]:
        return {
            "action": self.action.value,
            "reason": self.reason,
            "resolution": self.resolution.value,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "signature_count": self.signature_count,
            "signatures": [item.as_dict() for item in self.signatures],
        }

    def canonical_bytes(self) -> bytes:
        return _canonical_json_bytes(self.as_dict())

    @classmethod
    def from_dict(cls, raw: object) -> CompatibilityDecisionV1:
        value = _fields(
            raw,
            frozenset(
                {
                    "action",
                    "reason",
                    "resolution",
                    "schema_id",
                    "schema_version",
                    "signature_count",
                    "signatures",
                }
            ),
            "compatibility decision",
        )
        signatures = tuple(
            VersionSignatureV1.from_dict(item)
            for item in _json_array(value["signatures"], "compatibility signatures")
        )
        result = cls(
            action=CompatibilityActionV1(value["action"]),
            resolution=CompatibilityResolutionV1(value["resolution"]),
            reason=value["reason"],  # type: ignore[arg-type]
            signatures=signatures,
            schema_id=value["schema_id"],  # type: ignore[arg-type]
            schema_version=value["schema_version"],  # type: ignore[arg-type]
        )
        if type(value["signature_count"]) is not int:
            raise TypeError("compatibility signature count must be an exact integer")
        if result.as_dict() != value:
            raise ValueError("compatibility decision did not round-trip exactly")
        return result

    @classmethod
    def from_canonical_bytes(cls, raw: bytes) -> CompatibilityDecisionV1:
        return cls.from_dict(_canonical_object(raw, "compatibility decision"))


class CompatibilityRefusalV1(ValueError):
    """Raised with a durable refusal decision when pooling is unsafe."""

    def __init__(self, decision: CompatibilityDecisionV1) -> None:
        if type(decision) is not CompatibilityDecisionV1:
            raise TypeError("compatibility refusal requires CompatibilityDecisionV1")
        if decision.resolution is not CompatibilityResolutionV1.REFUSED:
            raise ValueError("compatibility refusal decision must be REFUSED")
        self.decision = decision
        super().__init__(decision.reason)

    def canonical_bytes(self) -> bytes:
        return self.decision.canonical_bytes()


@dataclass(frozen=True, slots=True)
class DescriptiveEstimateV1:
    """One exact estimate for one compatible version signature."""

    metric_id: str
    included_count: int
    denominator: int
    missing_count: int
    missing_reasons: tuple[MissingReasonCountV1, ...]
    version_signature: VersionSignatureV1
    estimate: RationalValueV1 | None
    estimate_scale: int
    estimate_scaled: int | None
    uncertainty: UncertaintyIntervalV1 | None
    analysis_capability: AnalysisCapabilityV1 = AnalysisCapabilityV1.DESCRIPTIVE
    schema_id: str = DESCRIPTIVE_ESTIMATE_SCHEMA_ID
    schema_version: int = DESCRIPTIVE_ESTIMATE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _identifier(self.metric_id, "estimate metric ID")
        _nonnegative_int(self.included_count, "included observation count")
        _nonnegative_int(self.denominator, "estimate denominator")
        _nonnegative_int(self.missing_count, "missing observation count")
        if type(self.missing_reasons) is not tuple or any(
            type(item) is not MissingReasonCountV1 for item in self.missing_reasons
        ):
            raise TypeError("missing reasons must be an immutable typed tuple")
        canonical_reasons = tuple(
            sorted(self.missing_reasons, key=lambda item: item.reason.encode("utf-8"))
        )
        if canonical_reasons != self.missing_reasons:
            raise ValueError("missing reasons must be canonically ordered")
        if len({item.reason for item in self.missing_reasons}) != len(self.missing_reasons):
            raise ValueError("missing reasons must be unique")
        if sum(item.count for item in self.missing_reasons) != self.missing_count:
            raise ValueError("missing reason counts do not equal missing count")
        if type(self.version_signature) is not VersionSignatureV1:
            raise TypeError("estimate signature must be VersionSignatureV1")
        _positive_int(self.estimate_scale, "estimate scale")
        if type(self.analysis_capability) is not AnalysisCapabilityV1:
            raise TypeError("estimate capability must be AnalysisCapabilityV1")
        if self.included_count == 0:
            if self.denominator != 0:
                raise ValueError("empty estimate denominator must be zero")
            if self.estimate is not None or self.estimate_scaled is not None:
                raise ValueError("empty estimate cannot invent a value")
            if self.uncertainty is not None:
                raise ValueError("empty estimate cannot invent uncertainty")
        else:
            _positive_int(self.denominator, "estimate denominator")
            if type(self.estimate) is not RationalValueV1:
                raise TypeError("nonempty estimate must be RationalValueV1")
            if type(self.estimate_scaled) is not int:
                raise TypeError("nonempty scaled estimate must be an exact integer")
            if self.estimate_scaled != self.estimate.round_half_even(self.estimate_scale):
                raise ValueError("estimate is not round-half-to-even at its scale")
            if type(self.uncertainty) is not UncertaintyIntervalV1:
                raise TypeError("nonempty estimate requires UncertaintyIntervalV1")
            if self.uncertainty.scale != self.estimate_scale:
                raise ValueError("estimate and uncertainty scales differ")
            if (
                _compare_rationals(self.estimate, self.uncertainty.lower) < 0
                or _compare_rationals(self.estimate, self.uncertainty.upper) > 0
            ):
                raise ValueError("estimate lies outside its uncertainty bounds")
        _schema(
            self.schema_id,
            self.schema_version,
            expected_id=DESCRIPTIVE_ESTIMATE_SCHEMA_ID,
            expected_version=DESCRIPTIVE_ESTIMATE_SCHEMA_VERSION,
            label="descriptive estimate",
        )

    @property
    def total_count(self) -> int:
        return self.included_count + self.missing_count

    @property
    def estimate_rounding_rule(self) -> str:
        return ESTIMATE_ROUNDING_RULE_V1

    def as_dict(self) -> dict[str, object]:
        return {
            "analysis_capability": self.analysis_capability.value,
            "denominator": self.denominator,
            "estimate": None if self.estimate is None else self.estimate.as_dict(),
            "estimate_rounding_rule": self.estimate_rounding_rule,
            "estimate_scale": self.estimate_scale,
            "estimate_scaled": self.estimate_scaled,
            "included_count": self.included_count,
            "metric_id": self.metric_id,
            "missing_count": self.missing_count,
            "missing_reasons": [item.as_dict() for item in self.missing_reasons],
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "total_count": self.total_count,
            "uncertainty": (
                None if self.uncertainty is None else self.uncertainty.as_dict()
            ),
            "version_signature": self.version_signature.as_dict(),
        }

    def canonical_bytes(self) -> bytes:
        return _canonical_json_bytes(self.as_dict())

    @classmethod
    def from_dict(cls, raw: object) -> DescriptiveEstimateV1:
        value = _fields(
            raw,
            frozenset(
                {
                    "analysis_capability",
                    "denominator",
                    "estimate",
                    "estimate_rounding_rule",
                    "estimate_scale",
                    "estimate_scaled",
                    "included_count",
                    "metric_id",
                    "missing_count",
                    "missing_reasons",
                    "schema_id",
                    "schema_version",
                    "total_count",
                    "uncertainty",
                    "version_signature",
                }
            ),
            "descriptive estimate",
        )
        raw_estimate = value["estimate"]
        raw_uncertainty = value["uncertainty"]
        result = cls(
            metric_id=value["metric_id"],  # type: ignore[arg-type]
            included_count=value["included_count"],  # type: ignore[arg-type]
            denominator=value["denominator"],  # type: ignore[arg-type]
            missing_count=value["missing_count"],  # type: ignore[arg-type]
            missing_reasons=tuple(
                MissingReasonCountV1.from_dict(item)
                for item in _json_array(value["missing_reasons"], "missing reasons")
            ),
            version_signature=VersionSignatureV1.from_dict(
                value["version_signature"]
            ),
            estimate=(
                None
                if raw_estimate is None
                else RationalValueV1.from_dict(raw_estimate)
            ),
            estimate_scale=value["estimate_scale"],  # type: ignore[arg-type]
            estimate_scaled=value["estimate_scaled"],  # type: ignore[arg-type]
            uncertainty=(
                None
                if raw_uncertainty is None
                else UncertaintyIntervalV1.from_dict(raw_uncertainty)
            ),
            analysis_capability=AnalysisCapabilityV1(value["analysis_capability"]),
            schema_id=value["schema_id"],  # type: ignore[arg-type]
            schema_version=value["schema_version"],  # type: ignore[arg-type]
        )
        if value["estimate_rounding_rule"] != ESTIMATE_ROUNDING_RULE_V1:
            raise ValueError("estimate rounding rule changed")
        if type(value["total_count"]) is not int:
            raise TypeError("estimate total count must be an exact integer")
        if result.as_dict() != value:
            raise ValueError("descriptive estimate did not round-trip exactly")
        return result

    @classmethod
    def from_canonical_bytes(cls, raw: bytes) -> DescriptiveEstimateV1:
        return cls.from_dict(_canonical_object(raw, "descriptive estimate"))


@dataclass(frozen=True, slots=True)
class DescriptiveSummaryV1:
    """Compatibility decision and one or more exact per-signature estimates."""

    metric_id: str
    requested_capability: AnalysisCapabilityV1
    analysis_capability: AnalysisCapabilityV1
    compatibility_decision: CompatibilityDecisionV1
    estimates: tuple[DescriptiveEstimateV1, ...]
    schema_id: str = DESCRIPTIVE_SUMMARY_SCHEMA_ID
    schema_version: int = DESCRIPTIVE_SUMMARY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _identifier(self.metric_id, "summary metric ID")
        if type(self.requested_capability) is not AnalysisCapabilityV1:
            raise TypeError("requested capability must be AnalysisCapabilityV1")
        if type(self.analysis_capability) is not AnalysisCapabilityV1:
            raise TypeError("analysis capability must be AnalysisCapabilityV1")
        if (
            self.requested_capability is AnalysisCapabilityV1.CAUSAL
            and self.analysis_capability is not AnalysisCapabilityV1.CAUSAL
        ):
            raise ValueError("causal summary requires causal analysis capability")
        if type(self.compatibility_decision) is not CompatibilityDecisionV1:
            raise TypeError("summary decision must be CompatibilityDecisionV1")
        if self.compatibility_decision.resolution is CompatibilityResolutionV1.REFUSED:
            raise ValueError("a refused compatibility decision cannot become a summary")
        if type(self.estimates) is not tuple or not self.estimates:
            raise ValueError("descriptive summary requires at least one estimate")
        if any(type(item) is not DescriptiveEstimateV1 for item in self.estimates):
            raise TypeError("summary estimates must be DescriptiveEstimateV1 values")
        if any(item.metric_id != self.metric_id for item in self.estimates):
            raise ValueError("summary estimates contain another metric")
        if any(item.analysis_capability is not self.analysis_capability for item in self.estimates):
            raise ValueError("summary estimate analysis capabilities differ")
        estimate_signatures = tuple(item.version_signature for item in self.estimates)
        if estimate_signatures != self.compatibility_decision.signatures:
            raise ValueError("summary estimates differ from compatibility signatures")
        if (
            self.compatibility_decision.resolution is CompatibilityResolutionV1.POOLED
            and len(self.estimates) != 1
        ):
            raise ValueError("pooled summary must contain exactly one estimate")
        _schema(
            self.schema_id,
            self.schema_version,
            expected_id=DESCRIPTIVE_SUMMARY_SCHEMA_ID,
            expected_version=DESCRIPTIVE_SUMMARY_SCHEMA_VERSION,
            label="descriptive summary",
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "analysis_capability": self.analysis_capability.value,
            "compatibility_decision": self.compatibility_decision.as_dict(),
            "estimates": [item.as_dict() for item in self.estimates],
            "metric_id": self.metric_id,
            "requested_capability": self.requested_capability.value,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
        }

    def canonical_bytes(self) -> bytes:
        return _canonical_json_bytes(self.as_dict())

    @classmethod
    def from_dict(cls, raw: object) -> DescriptiveSummaryV1:
        value = _fields(
            raw,
            frozenset(
                {
                    "analysis_capability",
                    "compatibility_decision",
                    "estimates",
                    "metric_id",
                    "requested_capability",
                    "schema_id",
                    "schema_version",
                }
            ),
            "descriptive summary",
        )
        result = cls(
            metric_id=value["metric_id"],  # type: ignore[arg-type]
            requested_capability=AnalysisCapabilityV1(value["requested_capability"]),
            analysis_capability=AnalysisCapabilityV1(value["analysis_capability"]),
            compatibility_decision=CompatibilityDecisionV1.from_dict(
                value["compatibility_decision"]
            ),
            estimates=tuple(
                DescriptiveEstimateV1.from_dict(item)
                for item in _json_array(value["estimates"], "descriptive estimates")
            ),
            schema_id=value["schema_id"],  # type: ignore[arg-type]
            schema_version=value["schema_version"],  # type: ignore[arg-type]
        )
        if result.as_dict() != value:
            raise ValueError("descriptive summary did not round-trip exactly")
        return result

    @classmethod
    def from_canonical_bytes(cls, raw: bytes) -> DescriptiveSummaryV1:
        return cls.from_dict(_canonical_object(raw, "descriptive summary"))


class UnsupportedCausalClaimError(ValueError):
    """Raised when a requested causal claim exceeds design or analysis support."""


def _design_capability_value(design_capability: object) -> str:
    # Accept either a design capability enum, a StudyDesignV1-like object, or a
    # StudyManifestV1-like object.  Attribute-based inspection avoids a circular
    # import while the exact enum text remains closed below.
    value = getattr(design_capability, "design", design_capability)
    value = getattr(value, "capability", value)
    value = getattr(value, "value", value)
    if type(value) is not str or value not in {
        AnalysisCapabilityV1.DESCRIPTIVE.value,
        AnalysisCapabilityV1.CAUSAL.value,
    }:
        raise TypeError("design capability must declare DESCRIPTIVE or CAUSAL")
    return value


def require_claim_capability(
    *,
    requested_capability: AnalysisCapabilityV1 = AnalysisCapabilityV1.DESCRIPTIVE,
    design_capability: object = AnalysisCapabilityV1.DESCRIPTIVE,
    analysis_capability: AnalysisCapabilityV1 = AnalysisCapabilityV1.DESCRIPTIVE,
) -> None:
    """Refuse causal language unless both the design and analysis support it.

    A study-design object may expose ``require_causal_support()`` for stronger design
    validation.  That hook is called before accepting a causal claim.
    """

    if type(requested_capability) is not AnalysisCapabilityV1:
        raise TypeError("requested capability must be AnalysisCapabilityV1")
    if type(analysis_capability) is not AnalysisCapabilityV1:
        raise TypeError("analysis capability must be AnalysisCapabilityV1")
    design_value = _design_capability_value(design_capability)
    if requested_capability is AnalysisCapabilityV1.DESCRIPTIVE:
        return
    if (
        design_value != AnalysisCapabilityV1.CAUSAL.value
        or analysis_capability is not AnalysisCapabilityV1.CAUSAL
    ):
        raise UnsupportedCausalClaimError(
            "causal language requires both CAUSAL design and CAUSAL analysis capability"
        )
    validator = getattr(design_capability, "require_causal_support", None)
    if validator is not None:
        if not callable(validator):
            raise TypeError("design causal support hook must be callable")
        validator()


def _compatibility_decision(
    signatures: tuple[VersionSignatureV1, ...],
    action: CompatibilityActionV1,
) -> CompatibilityDecisionV1:
    if len(signatures) == 1:
        return CompatibilityDecisionV1(
            action=action,
            resolution=CompatibilityResolutionV1.POOLED,
            reason="ONE_VERSION_SIGNATURE",
            signatures=signatures,
        )
    if action is CompatibilityActionV1.STRATIFY:
        return CompatibilityDecisionV1(
            action=action,
            resolution=CompatibilityResolutionV1.STRATIFIED,
            reason="INCOMPATIBLE_SIGNATURES_EXPLICITLY_STRATIFIED",
            signatures=signatures,
        )
    reason = (
        "INCOMPATIBLE_SIGNATURES_CANNOT_POOL"
        if action is CompatibilityActionV1.POOL
        else "INCOMPATIBLE_SIGNATURES_EXPLICITLY_REFUSED"
    )
    raise CompatibilityRefusalV1(
        CompatibilityDecisionV1(
            action=action,
            resolution=CompatibilityResolutionV1.REFUSED,
            reason=reason,
            signatures=signatures,
        )
    )


def _summarize_signature(
    observations: tuple[MetricObservationV1, ...],
    *,
    metric_id: str,
    scale: int,
    signature: VersionSignatureV1,
    analysis_capability: AnalysisCapabilityV1,
) -> DescriptiveEstimateV1:
    included = tuple(item for item in observations if item.present)
    missing = tuple(item for item in observations if not item.present)
    reason_counts: dict[str, int] = {}
    for item in missing:
        assert item.missing_reason is not None
        reason_counts[item.missing_reason] = reason_counts.get(item.missing_reason, 0) + 1
    missing_reasons = tuple(
        MissingReasonCountV1(reason=reason, count=reason_counts[reason])
        for reason in sorted(reason_counts, key=lambda item: item.encode("utf-8"))
    )
    if not included:
        return DescriptiveEstimateV1(
            metric_id=metric_id,
            included_count=0,
            denominator=0,
            missing_count=len(missing),
            missing_reasons=missing_reasons,
            version_signature=signature,
            estimate=None,
            estimate_scale=scale,
            estimate_scaled=None,
            uncertainty=None,
            analysis_capability=analysis_capability,
        )

    denominator = sum(item.effective_exposure or 0 for item in included)
    weighted_value = sum(
        item.value * (item.effective_exposure or 0)  # type: ignore[operator]
        for item in included
    )
    estimate = RationalValueV1.from_fraction(weighted_value, scale * denominator)
    included_values = tuple(item.value for item in included)
    lower = RationalValueV1.from_fraction(min(included_values), scale)  # type: ignore[arg-type]
    upper = RationalValueV1.from_fraction(max(included_values), scale)  # type: ignore[arg-type]
    return DescriptiveEstimateV1(
        metric_id=metric_id,
        included_count=len(included),
        denominator=denominator,
        missing_count=len(missing),
        missing_reasons=missing_reasons,
        version_signature=signature,
        estimate=estimate,
        estimate_scale=scale,
        estimate_scaled=estimate.round_half_even(scale),
        uncertainty=UncertaintyIntervalV1.observed_range(lower, upper, scale=scale),
        analysis_capability=analysis_capability,
    )


def summarize_observations(
    observations: tuple[MetricObservationV1, ...],
    *,
    compatibility_action: CompatibilityActionV1 = CompatibilityActionV1.REFUSE,
    requested_capability: AnalysisCapabilityV1 = AnalysisCapabilityV1.DESCRIPTIVE,
    analysis_capability: AnalysisCapabilityV1 = AnalysisCapabilityV1.DESCRIPTIVE,
    design_capability: object = AnalysisCapabilityV1.DESCRIPTIVE,
) -> DescriptiveSummaryV1:
    """Summarize one metric without floats or implicit version pooling.

    Input order has no effect on output bytes.  Duplicate observation IDs are
    rejected.  Missing observations contribute to missingness, never to a fabricated
    denominator.  With multiple signatures only explicit ``STRATIFY`` succeeds.
    """

    if type(observations) is not tuple or not observations:
        raise ValueError("statistics require a nonempty immutable observation tuple")
    if any(type(item) is not MetricObservationV1 for item in observations):
        raise TypeError("observations must contain exact MetricObservationV1 values")
    if type(compatibility_action) is not CompatibilityActionV1:
        raise TypeError("compatibility action must be CompatibilityActionV1")
    require_claim_capability(
        requested_capability=requested_capability,
        design_capability=design_capability,
        analysis_capability=analysis_capability,
    )
    metric_ids = {item.metric_id for item in observations}
    if len(metric_ids) != 1:
        raise ValueError("one statistics summary cannot mix metric IDs")
    scales = {item.scale for item in observations}
    if len(scales) != 1:
        raise ValueError("one statistics summary cannot mix declared scales")
    identities = tuple(item.observation_id for item in observations)
    if len(identities) != len(set(identities)):
        raise ValueError("statistics observations cannot repeat observation IDs")
    metric_id = next(iter(metric_ids))
    scale = next(iter(scales))
    signatures = tuple(
        sorted(
            {item.version_signature for item in observations},
            key=lambda item: item.canonical_bytes(),
        )
    )
    decision = _compatibility_decision(signatures, compatibility_action)
    estimates = tuple(
        _summarize_signature(
            tuple(
                sorted(
                    (
                        item
                        for item in observations
                        if item.version_signature == signature
                    ),
                    key=lambda item: item.observation_id.encode("ascii"),
                )
            ),
            metric_id=metric_id,
            scale=scale,
            signature=signature,
            analysis_capability=analysis_capability,
        )
        for signature in signatures
    )
    return DescriptiveSummaryV1(
        metric_id=metric_id,
        requested_capability=requested_capability,
        analysis_capability=analysis_capability,
        compatibility_decision=decision,
        estimates=estimates,
    )


def summarize_metric_observations(
    observations: tuple[MetricObservationV1, ...],
    *,
    compatibility_action: CompatibilityActionV1 = CompatibilityActionV1.REFUSE,
    requested_capability: AnalysisCapabilityV1 = AnalysisCapabilityV1.DESCRIPTIVE,
    analysis_capability: AnalysisCapabilityV1 = AnalysisCapabilityV1.DESCRIPTIVE,
    design_capability: object = AnalysisCapabilityV1.DESCRIPTIVE,
) -> DescriptiveSummaryV1:
    """Named compatibility wrapper for :func:`summarize_observations`."""

    return summarize_observations(
        observations,
        compatibility_action=compatibility_action,
        requested_capability=requested_capability,
        analysis_capability=analysis_capability,
        design_capability=design_capability,
    )


def decode_version_signature_v1(raw: bytes) -> VersionSignatureV1:
    return VersionSignatureV1.from_canonical_bytes(raw)


def decode_metric_observation_v1(raw: bytes) -> MetricObservationV1:
    return MetricObservationV1.from_canonical_bytes(raw)


def decode_rational_value_v1(raw: bytes) -> RationalValueV1:
    return RationalValueV1.from_canonical_bytes(raw)


def decode_uncertainty_interval_v1(raw: bytes) -> UncertaintyIntervalV1:
    return UncertaintyIntervalV1.from_canonical_bytes(raw)


def decode_compatibility_decision_v1(raw: bytes) -> CompatibilityDecisionV1:
    return CompatibilityDecisionV1.from_canonical_bytes(raw)


def decode_descriptive_estimate_v1(raw: bytes) -> DescriptiveEstimateV1:
    return DescriptiveEstimateV1.from_canonical_bytes(raw)


def decode_descriptive_summary_v1(raw: bytes) -> DescriptiveSummaryV1:
    return DescriptiveSummaryV1.from_canonical_bytes(raw)


__all__ = [
    "AnalysisCapabilityV1",
    "CompatibilityActionV1",
    "CompatibilityDecisionV1",
    "CompatibilityRefusalV1",
    "CompatibilityResolutionV1",
    "DescriptiveEstimateV1",
    "DescriptiveSummaryV1",
    "ESTIMATE_ROUNDING_RULE_V1",
    "INTERVAL_ROUNDING_RULE_V1",
    "MetricObservationV1",
    "MissingReasonCountV1",
    "RationalValueV1",
    "UncertaintyIntervalV1",
    "UncertaintyMethodV1",
    "UnsupportedCausalClaimError",
    "VersionSignatureV1",
    "decode_compatibility_decision_v1",
    "decode_descriptive_estimate_v1",
    "decode_descriptive_summary_v1",
    "decode_metric_observation_v1",
    "decode_rational_value_v1",
    "decode_uncertainty_interval_v1",
    "decode_version_signature_v1",
    "require_claim_capability",
    "summarize_metric_observations",
    "summarize_observations",
]
