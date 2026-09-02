"""Canonical whole-experiment aggregation for recovered orchestration runs.

The aggregate is intentionally independent of backend, worker count, completion
order, leases, attempts, and wall-clock observations.  Every registered logical
unit contributes one member in logical-ID order.  Numeric columns are reduced with
integer or :class:`~decimal.Decimal` arithmetic so binary floating-point addition
order can never change the result.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import ClassVar

from kirby2.packs.formats import canonical_json_bytes, load_canonical_json_bytes

from .content_store import OrchestrationContentStoreV1
from .models import ExperimentWorkPlanV1
from .protocol import (
    MAX_INLINE_ARTIFACT_BYTES,
    InlineArtifactMediaTypeV1,
    WorkRequestV1,
)
from .worker import complete_run_runtime_audit_identities


ORCHESTRATION_AGGREGATION_SCHEMA_VERSION = 1
EXPERIMENT_AGGREGATE_MEMBER_SCHEMA_ID = "KIRBY2_EXPERIMENT_AGGREGATE_MEMBER_V1"
METRIC_COLUMN_AGGREGATE_SCHEMA_ID = "KIRBY2_METRIC_COLUMN_AGGREGATE_V1"
EXPERIMENT_AGGREGATE_SCHEMA_ID = "KIRBY2_EXPERIMENT_AGGREGATE_V1"

MAX_AGGREGATE_MEMBERS_V1 = 1_000_000
MAX_METRIC_COLUMNS_V1 = 4096
MAX_METRIC_NAME_BYTES_V1 = 128
MAX_DISTINCT_TEXT_VALUES_V1 = 4096

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_METRIC_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_.:-]{0,127}\Z")
_DECIMAL_TEXT = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?\Z")


class MetricValueKindV1(str, Enum):
    """Closed scalar families supported by complete-run metric aggregation."""

    NULL = "NULL"
    INTEGER = "INTEGER"
    DECIMAL = "DECIMAL"
    TEXT = "TEXT"


@dataclass(frozen=True, slots=True)
class ExperimentAggregateMemberV1:
    """One registered logical result retained in the whole-experiment identity."""

    logical_work_unit_id: str
    work_request_id: str
    manifest_sha256: str
    scientific_result_sha256: str
    metrics_sha256: str

    schema_id: ClassVar[str] = EXPERIMENT_AGGREGATE_MEMBER_SCHEMA_ID
    schema_version: ClassVar[int] = ORCHESTRATION_AGGREGATION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _sha256(self.logical_work_unit_id, "aggregate-member logical-work ID")
        _sha256(self.work_request_id, "aggregate-member work-request ID")
        _sha256(self.manifest_sha256, "aggregate-member manifest digest")
        _sha256(
            self.scientific_result_sha256,
            "aggregate-member scientific-result digest",
        )
        _sha256(self.metrics_sha256, "aggregate-member metrics digest")

    @property
    def member_sha256(self) -> str:
        return hashlib.sha256(canonical_json_bytes(self.identity_dict())).hexdigest()

    def identity_dict(self) -> dict[str, object]:
        return {
            "logical_work_unit_id": self.logical_work_unit_id,
            "manifest_sha256": self.manifest_sha256,
            "metrics_sha256": self.metrics_sha256,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "scientific_result_sha256": self.scientific_result_sha256,
            "work_request_id": self.work_request_id,
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_dict(), "member_sha256": self.member_sha256}

    @classmethod
    def from_dict(cls, value: object) -> ExperimentAggregateMemberV1:
        row = _exact(
            value,
            {
                "logical_work_unit_id",
                "manifest_sha256",
                "member_sha256",
                "metrics_sha256",
                "schema_id",
                "schema_version",
                "scientific_result_sha256",
                "work_request_id",
            },
            "experiment aggregate member",
        )
        _schema(row, cls.schema_id, "experiment aggregate member")
        declared = _sha256(row["member_sha256"], "declared aggregate-member digest")
        restored = cls(
            logical_work_unit_id=_text(row, "logical_work_unit_id"),
            work_request_id=_text(row, "work_request_id"),
            manifest_sha256=_text(row, "manifest_sha256"),
            scientific_result_sha256=_text(row, "scientific_result_sha256"),
            metrics_sha256=_text(row, "metrics_sha256"),
        )
        if not hmac.compare_digest(declared, restored.member_sha256):
            raise ValueError("aggregate-member digest differs from canonical content")
        _round_trip(restored, row, "experiment aggregate member")
        return restored


@dataclass(frozen=True, slots=True)
class MetricColumnAggregateV1:
    """Deterministic reduction plus ordered-value commitment for one metric."""

    metric_name: str
    value_kind: MetricValueKindV1
    value_count: int
    null_count: int
    exact_numeric_sum: str | None
    distinct_text_values: tuple[str, ...]
    ordered_values_sha256: str

    schema_id: ClassVar[str] = METRIC_COLUMN_AGGREGATE_SCHEMA_ID
    schema_version: ClassVar[int] = ORCHESTRATION_AGGREGATION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if (
            type(self.metric_name) is not str
            or len(self.metric_name.encode("utf-8")) > MAX_METRIC_NAME_BYTES_V1
            or _METRIC_NAME.fullmatch(self.metric_name) is None
        ):
            raise ValueError("metric-column name must be bounded canonical text")
        if type(self.value_kind) is not MetricValueKindV1:
            raise TypeError("metric-column value kind is invalid")
        _bounded_count(self.value_count, "metric-column value count", maximum=MAX_AGGREGATE_MEMBERS_V1)
        _bounded_count(self.null_count, "metric-column null count", maximum=MAX_AGGREGATE_MEMBERS_V1)
        if self.null_count > self.value_count:
            raise ValueError("metric-column null count exceeds its value count")
        _sha256(self.ordered_values_sha256, "metric-column ordered-value digest")
        if (
            type(self.distinct_text_values) is not tuple
            or len(self.distinct_text_values) > MAX_DISTINCT_TEXT_VALUES_V1
            or any(type(item) is not str for item in self.distinct_text_values)
            or self.distinct_text_values != tuple(sorted(set(self.distinct_text_values)))
        ):
            raise ValueError("metric-column text values must be canonical and unique")
        if self.value_kind in {MetricValueKindV1.INTEGER, MetricValueKindV1.DECIMAL}:
            if (
                type(self.exact_numeric_sum) is not str
                or _DECIMAL_TEXT.fullmatch(self.exact_numeric_sum) is None
            ):
                raise ValueError("numeric metric-column requires one exact decimal sum")
            if self.value_kind is MetricValueKindV1.INTEGER and "." in self.exact_numeric_sum:
                raise ValueError("integer metric-column sum cannot contain a fraction")
            if self.distinct_text_values:
                raise ValueError("numeric metric-column cannot carry text values")
        elif self.value_kind is MetricValueKindV1.TEXT:
            if self.exact_numeric_sum is not None or not self.distinct_text_values:
                raise ValueError("text metric-column has an invalid reduction shape")
        elif self.value_kind is MetricValueKindV1.NULL:
            if (
                self.null_count != self.value_count
                or self.exact_numeric_sum is not None
                or self.distinct_text_values
            ):
                raise ValueError("null metric-column has an invalid reduction shape")
        else:
            raise RuntimeError("metric-column kind is not exhaustively handled")

    def as_dict(self) -> dict[str, object]:
        return {
            "distinct_text_values": list(self.distinct_text_values),
            "exact_numeric_sum": self.exact_numeric_sum,
            "metric_name": self.metric_name,
            "null_count": self.null_count,
            "ordered_values_sha256": self.ordered_values_sha256,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "value_count": self.value_count,
            "value_kind": self.value_kind.value,
        }

    @classmethod
    def from_dict(cls, value: object) -> MetricColumnAggregateV1:
        row = _exact(
            value,
            {
                "distinct_text_values",
                "exact_numeric_sum",
                "metric_name",
                "null_count",
                "ordered_values_sha256",
                "schema_id",
                "schema_version",
                "value_count",
                "value_kind",
            },
            "metric column aggregate",
        )
        _schema(row, cls.schema_id, "metric column aggregate")
        raw_text = row["distinct_text_values"]
        if type(raw_text) is not list:
            raise TypeError("metric-column distinct text values must be an array")
        exact_sum = row["exact_numeric_sum"]
        if exact_sum is not None and type(exact_sum) is not str:
            raise TypeError("metric-column exact sum must be text or null")
        restored = cls(
            metric_name=_text(row, "metric_name"),
            value_kind=MetricValueKindV1(_text(row, "value_kind")),
            value_count=_integer(row, "value_count"),
            null_count=_integer(row, "null_count"),
            exact_numeric_sum=exact_sum,
            distinct_text_values=tuple(raw_text),
            ordered_values_sha256=_text(row, "ordered_values_sha256"),
        )
        _round_trip(restored, row, "metric column aggregate")
        return restored


@dataclass(frozen=True, slots=True)
class ExperimentAggregateV1:
    """Complete backend-neutral result identity for one experiment plan."""

    plan_id: str
    members: tuple[ExperimentAggregateMemberV1, ...]
    metric_columns: tuple[MetricColumnAggregateV1, ...]

    schema_id: ClassVar[str] = EXPERIMENT_AGGREGATE_SCHEMA_ID
    schema_version: ClassVar[int] = ORCHESTRATION_AGGREGATION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _sha256(self.plan_id, "experiment aggregate plan ID")
        if (
            type(self.members) is not tuple
            or not self.members
            or len(self.members) > MAX_AGGREGATE_MEMBERS_V1
            or any(type(item) is not ExperimentAggregateMemberV1 for item in self.members)
        ):
            raise ValueError("experiment aggregate requires bounded typed members")
        logical_ids = tuple(item.logical_work_unit_id for item in self.members)
        if logical_ids != tuple(sorted(logical_ids)) or len(logical_ids) != len(set(logical_ids)):
            raise ValueError("experiment aggregate members must use unique logical-ID order")
        if (
            type(self.metric_columns) is not tuple
            or not self.metric_columns
            or len(self.metric_columns) > MAX_METRIC_COLUMNS_V1
            or any(type(item) is not MetricColumnAggregateV1 for item in self.metric_columns)
        ):
            raise ValueError("experiment aggregate requires bounded metric columns")
        metric_names = tuple(item.metric_name for item in self.metric_columns)
        if metric_names != tuple(sorted(metric_names)) or len(metric_names) != len(set(metric_names)):
            raise ValueError("experiment metric columns must use unique name order")
        if any(item.value_count != len(self.members) for item in self.metric_columns):
            raise ValueError("every metric column must cover the complete experiment")

    def scientific_dict(self) -> dict[str, object]:
        return {
            "members": [item.as_dict() for item in self.members],
            "metric_columns": [item.as_dict() for item in self.metric_columns],
            "plan_id": self.plan_id,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
        }

    @property
    def aggregate_sha256(self) -> str:
        return hashlib.sha256(canonical_json_bytes(self.scientific_dict())).hexdigest()

    def as_dict(self) -> dict[str, object]:
        return {**self.scientific_dict(), "aggregate_sha256": self.aggregate_sha256}

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())

    @classmethod
    def from_dict(cls, value: object) -> ExperimentAggregateV1:
        row = _exact(
            value,
            {
                "aggregate_sha256",
                "members",
                "metric_columns",
                "plan_id",
                "schema_id",
                "schema_version",
            },
            "experiment aggregate",
        )
        _schema(row, cls.schema_id, "experiment aggregate")
        declared = _sha256(row["aggregate_sha256"], "declared experiment aggregate digest")
        raw_members = row["members"]
        raw_columns = row["metric_columns"]
        if type(raw_members) is not list or type(raw_columns) is not list:
            raise TypeError("experiment aggregate members and columns must be arrays")
        restored = cls(
            plan_id=_text(row, "plan_id"),
            members=tuple(ExperimentAggregateMemberV1.from_dict(item) for item in raw_members),
            metric_columns=tuple(MetricColumnAggregateV1.from_dict(item) for item in raw_columns),
        )
        if not hmac.compare_digest(declared, restored.aggregate_sha256):
            raise ValueError("experiment aggregate digest differs from canonical content")
        _round_trip(restored, row, "experiment aggregate")
        return restored

    @classmethod
    def from_canonical_bytes(cls, raw: bytes) -> ExperimentAggregateV1:
        restored = cls.from_dict(load_canonical_json_bytes(raw, "experiment aggregate"))
        if restored.canonical_bytes() != raw:
            raise ValueError("experiment aggregate bytes are not canonical")
        return restored


def aggregate_registered_results(
    plan: ExperimentWorkPlanV1,
    manifest_sha256s: Mapping[str, str],
    content_store: OrchestrationContentStoreV1,
) -> ExperimentAggregateV1:
    """Read and reduce exactly one registered result for every planned unit."""

    if type(plan) is not ExperimentWorkPlanV1:
        raise TypeError("experiment aggregation requires ExperimentWorkPlanV1")
    if not isinstance(manifest_sha256s, Mapping):
        raise TypeError("experiment aggregation requires a manifest mapping")
    if type(content_store) is not OrchestrationContentStoreV1:
        raise TypeError("experiment aggregation requires OrchestrationContentStoreV1")
    expected_ids = tuple(item.logical_work_unit_id for item in plan.logical_units)
    if frozenset(manifest_sha256s) != frozenset(expected_ids):
        raise ValueError("registered manifests do not cover the complete experiment")
    if any(type(key) is not str or type(value) is not str for key, value in manifest_sha256s.items()):
        raise TypeError("registered manifest mapping must contain exact text")

    members: list[ExperimentAggregateMemberV1] = []
    ordinary_rows: list[dict[str, object]] = []
    decimal_rows: list[dict[str, object]] = []
    metric_names: tuple[str, ...] | None = None
    required_audits = complete_run_runtime_audit_identities()

    for logical_unit in plan.logical_units:
        manifest_digest = _sha256(
            manifest_sha256s[logical_unit.logical_work_unit_id],
            "registered manifest mapping digest",
        )
        manifest = content_store.read_result_manifest(manifest_digest)
        expected_request = WorkRequestV1(
            logical_work_unit=logical_unit,
            required_runtime_audits=required_audits,
        )
        if (
            manifest.logical_work_unit_id != logical_unit.logical_work_unit_id
            or manifest.work_request_id != expected_request.work_request_id
        ):
            raise ValueError("registered result belongs to another planned work unit")
        metrics_descriptors = tuple(
            item for item in manifest.artifacts if item.artifact_id == "metrics.json"
        )
        if len(metrics_descriptors) != 1:
            raise ValueError("registered result requires exactly one metrics artifact")
        descriptor = metrics_descriptors[0]
        if descriptor.media_type is not InlineArtifactMediaTypeV1.CANONICAL_JSON:
            raise ValueError("registered metrics artifact must be canonical JSON")
        raw = content_store.read_result_artifact(manifest_digest, descriptor)
        ordinary, decimal = _load_canonical_metrics_object(raw)
        names = tuple(sorted(ordinary))
        if metric_names is None:
            metric_names = names
        elif names != metric_names:
            raise ValueError("registered metric schemas differ across logical work")
        _require_scalar_metric_row(ordinary)
        _require_scalar_metric_row(decimal)
        ordinary_rows.append(ordinary)
        decimal_rows.append(decimal)
        members.append(
            ExperimentAggregateMemberV1(
                logical_work_unit_id=logical_unit.logical_work_unit_id,
                work_request_id=manifest.work_request_id,
                manifest_sha256=manifest_digest,
                scientific_result_sha256=manifest.coordinator_verification_sha256,
                metrics_sha256=descriptor.sha256,
            )
        )

    if metric_names is None:
        raise RuntimeError("nonempty experiment produced no metric schema")
    columns = tuple(
        _reduce_metric_column(
            name,
            tuple(row[name] for row in ordinary_rows),
            tuple(row[name] for row in decimal_rows),
        )
        for name in metric_names
    )
    aggregate = ExperimentAggregateV1(
        plan_id=plan.plan_id,
        members=tuple(members),
        metric_columns=columns,
    )
    if tuple(item.logical_work_unit_id for item in aggregate.members) != expected_ids:
        raise RuntimeError("aggregate lost canonical logical-work coverage")
    return aggregate


def _reduce_metric_column(
    name: str,
    ordinary_values: tuple[object, ...],
    decimal_values: tuple[object, ...],
) -> MetricColumnAggregateV1:
    if len(ordinary_values) != len(decimal_values) or not ordinary_values:
        raise ValueError("metric reduction requires aligned nonempty values")
    null_count = sum(value is None for value in ordinary_values)
    non_null_decimal = tuple(value for value in decimal_values if value is not None)
    ordered_digest = hashlib.sha256(
        _canonical_metrics_json_bytes(list(ordinary_values))
    ).hexdigest()
    if not non_null_decimal:
        kind = MetricValueKindV1.NULL
        exact_sum = None
        distinct_text: tuple[str, ...] = ()
    elif all(type(value) is int for value in non_null_decimal):
        kind = MetricValueKindV1.INTEGER
        exact_sum = str(sum(non_null_decimal))
        distinct_text = ()
    elif all(type(value) in {int, Decimal} for value in non_null_decimal):
        kind = MetricValueKindV1.DECIMAL
        exact_sum = _format_decimal(
            sum(
                (Decimal(value) if type(value) is int else value for value in non_null_decimal),
                Decimal(0),
            )
        )
        distinct_text = ()
    elif all(type(value) is str for value in non_null_decimal):
        kind = MetricValueKindV1.TEXT
        exact_sum = None
        distinct_text = tuple(sorted(set(non_null_decimal)))
    else:
        raise ValueError(f"metric {name!r} mixes unsupported scalar families")
    return MetricColumnAggregateV1(
        metric_name=name,
        value_kind=kind,
        value_count=len(ordinary_values),
        null_count=null_count,
        exact_numeric_sum=exact_sum,
        distinct_text_values=distinct_text,
        ordered_values_sha256=ordered_digest,
    )


def _format_decimal(value: Decimal) -> str:
    if not value.is_finite():
        raise ValueError("metric reduction produced a non-finite decimal")
    if value == 0:
        return "0"
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    if _DECIMAL_TEXT.fullmatch(text) is None:
        raise ValueError("metric reduction did not produce canonical decimal text")
    return text


def _require_scalar_metric_row(row: dict[str, object]) -> None:
    if not row or len(row) > MAX_METRIC_COLUMNS_V1:
        raise ValueError("metric row is empty or exceeds the column limit")
    for key, value in row.items():
        if type(key) is not str or _METRIC_NAME.fullmatch(key) is None:
            raise ValueError("metric row contains a noncanonical column name")
        if value is not None and type(value) not in {int, float, Decimal, str}:
            raise ValueError("metric row values must be scalar numbers, text, or null")
        if type(value) is float and (value != value or value in {float("inf"), float("-inf")}):
            raise ValueError("metric row cannot contain a non-finite float")


def _load_canonical_metrics_object(
    raw: bytes,
) -> tuple[dict[str, object], dict[str, object]]:
    """Parse one bounded metrics object while retaining exact finite decimals.

    Pack identity JSON deliberately forbids every binary float. Runtime metrics use
    a different, data-only contract: finite JSON numbers are accepted, their source
    bytes must still be canonical, and a second Decimal projection drives reduction.
    """

    if (
        type(raw) is not bytes
        or not raw
        or len(raw) > MAX_INLINE_ARTIFACT_BYTES
    ):
        raise ValueError("registered experiment metrics must be bounded exact bytes")
    try:
        text = raw.decode("ascii")
        ordinary = json.loads(
            text,
            object_pairs_hook=_metrics_object_from_pairs,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise ValueError(
            "registered experiment metrics must be one canonical ASCII JSON object"
        ) from error
    if type(ordinary) is not dict:
        raise TypeError("registered experiment metrics must be one exact object")
    _require_scalar_metric_row(ordinary)
    if _canonical_metrics_json_bytes(ordinary) != raw:
        raise ValueError("registered experiment metrics bytes are not canonical JSON")
    try:
        decimal = json.loads(
            text,
            object_pairs_hook=_metrics_object_from_pairs,
            parse_float=Decimal,
            parse_int=int,
            parse_constant=_reject_json_constant,
        )
    except (json.JSONDecodeError, RecursionError) as error:
        raise ValueError(
            "registered experiment metrics decimal projection is invalid"
        ) from error
    if type(decimal) is not dict or tuple(sorted(decimal)) != tuple(sorted(ordinary)):
        raise ValueError("decimal metric projection differs from canonical metrics")
    _require_scalar_metric_row(decimal)
    return ordinary, decimal


def _canonical_metrics_json_bytes(value: object) -> bytes:
    _validate_metrics_json_value(value)
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
    except (TypeError, ValueError) as error:
        raise ValueError("runtime metrics are not finite canonical JSON") from error


def _validate_metrics_json_value(value: object) -> None:
    if value is None or type(value) is int:
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("runtime metrics contain a non-finite number")
        return
    if type(value) is str:
        if unicodedata.normalize("NFC", value) != value:
            raise ValueError("runtime metrics text must be NFC-normalized")
        if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
            raise ValueError("runtime metrics text contains a surrogate code point")
        return
    if type(value) is list:
        for item in value:
            _validate_metrics_json_value(item)
        return
    if type(value) is dict:
        for key, item in value.items():
            if type(key) is not str:
                raise TypeError("runtime metric object keys must be text")
            _validate_metrics_json_value(key)
            _validate_metrics_json_value(item)
        return
    raise TypeError("runtime metrics contain an unsupported JSON value")


def _metrics_object_from_pairs(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"runtime metrics contain duplicate key {key!r}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"non-finite JSON number is forbidden: {value}")


def _exact(value: object, fields: set[str], label: str) -> dict[str, object]:
    if type(value) is not dict:
        raise TypeError(f"{label} must be an exact object")
    if set(value) != fields:
        raise ValueError(f"{label} fields differ from the V1 schema")
    return value


def _schema(row: dict[str, object], schema_id: str, label: str) -> None:
    if (
        type(row["schema_id"]) is not str
        or row["schema_id"] != schema_id
        or type(row["schema_version"]) is not int
        or row["schema_version"] != 1
    ):
        raise ValueError(f"{label} schema is not supported")


def _text(row: dict[str, object], key: str) -> str:
    value = row[key]
    if type(value) is not str or not value:
        raise TypeError(f"{key} must be nonempty exact text")
    return value


def _integer(row: dict[str, object], key: str) -> int:
    value = row[key]
    if type(value) is not int:
        raise TypeError(f"{key} must be an exact integer")
    return value


def _sha256(value: object, label: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{label} must be one lowercase SHA-256 digest")
    return value


def _bounded_count(value: object, label: str, *, maximum: int) -> int:
    if type(value) is not int or not 0 <= value <= maximum:
        raise ValueError(f"{label} is outside its V1 bound")
    return value


def _round_trip(value: object, row: dict[str, object], label: str) -> None:
    if getattr(value, "as_dict")() != row:
        raise ValueError(f"serialized {label} did not round-trip exactly")


__all__ = [
    "EXPERIMENT_AGGREGATE_MEMBER_SCHEMA_ID",
    "EXPERIMENT_AGGREGATE_SCHEMA_ID",
    "METRIC_COLUMN_AGGREGATE_SCHEMA_ID",
    "ORCHESTRATION_AGGREGATION_SCHEMA_VERSION",
    "ExperimentAggregateMemberV1",
    "ExperimentAggregateV1",
    "MetricColumnAggregateV1",
    "MetricValueKindV1",
    "aggregate_registered_results",
]
