"""Closed, explicit default materialization for scenario compiler V1."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType

from .models import (
    SCENARIO_SEED_DERIVATION_POLICY_VERSION,
    ScenarioFieldV1,
    ScenarioRecordV1,
    ScenarioSourceV1,
    ScenarioTargetKindV1,
    ScenarioValueKindV1,
)


SCENARIO_SEED_POLICY_LOGICAL_NAME_V1 = "scenario_seed_policy"
SCENARIO_SEED_POLICY_RECORD_TYPE_V1 = "SCENARIO_SEED_POLICY_V1"

DEFAULT_SCENARIO_SUBSTREAMS_V1 = MappingProxyType(
    {
        ScenarioTargetKindV1.FULL_DAY_PLAN_V1: (
            "scenario/full_day/runtime",
        ),
        ScenarioTargetKindV1.MARKET_SCENARIO_V1: (
            "scenario/market/runtime",
        ),
        ScenarioTargetKindV1.HIDDEN_LIQUIDITY_RECORDING_V1: (
            "scenario/hidden_liquidity/runtime",
        ),
        ScenarioTargetKindV1.MULTIVENUE_RECORDING_V1: (
            "scenario/multivenue/runtime",
        ),
        ScenarioTargetKindV1.HISTORICAL_LESSON_V1: (
            "scenario/historical/runtime",
        ),
    }
)

_SEED_POLICY_FIELD_KINDS = MappingProxyType(
    {
        "allow_cli_override": ScenarioValueKindV1.FLAG,
        "policy_version": ScenarioValueKindV1.VERSION,
        "root_seed": ScenarioValueKindV1.SEED,
        "substreams": ScenarioValueKindV1.IDENTIFIERS,
    }
)


@dataclass(frozen=True, slots=True)
class ScenarioAppliedDefaultV1:
    path: str
    field: ScenarioFieldV1

    def __post_init__(self) -> None:
        if type(self.path) is not str or not self.path:
            raise ValueError("scenario applied default requires a stable path")
        if type(self.field) is not ScenarioFieldV1:
            raise TypeError("scenario applied default requires a typed field")

    def as_dict(self) -> dict[str, object]:
        return {
            "materialized_field": self.field.as_dict(),
            "path": self.path,
        }


@dataclass(frozen=True, slots=True)
class ScenarioDefaultMaterializationV1:
    seed_policy_record: ScenarioRecordV1
    applied_defaults: tuple[ScenarioAppliedDefaultV1, ...]

    def __post_init__(self) -> None:
        if type(self.seed_policy_record) is not ScenarioRecordV1:
            raise TypeError("default materialization requires a seed-policy record")
        if type(self.applied_defaults) is not tuple or any(
            type(item) is not ScenarioAppliedDefaultV1
            for item in self.applied_defaults
        ):
            raise TypeError("applied defaults must be an immutable typed tuple")
        paths = tuple(item.path for item in self.applied_defaults)
        if paths != tuple(sorted(set(paths))):
            raise ValueError("applied defaults must have unique sorted paths")


def materialize_scenario_defaults(
    source: ScenarioSourceV1,
) -> ScenarioDefaultMaterializationV1:
    """Materialize only the closed V1 defaults; no ambient values are consulted."""

    if type(source) is not ScenarioSourceV1:
        raise TypeError("scenario defaults require ScenarioSourceV1")
    records = source.seed_policy.records
    if len(records) > 1:
        raise ValueError("scenario source may declare at most one seed policy")
    if records:
        record = records[0]
        if (
            record.logical_name != SCENARIO_SEED_POLICY_LOGICAL_NAME_V1
            or record.record_type != SCENARIO_SEED_POLICY_RECORD_TYPE_V1
            or record.version != 1
            or record.reference is not None
            or record.extends is not None
        ):
            raise ValueError("scenario seed policy uses the wrong closed V1 contract")
        fields = {field.name: field for field in record.fields}
    else:
        fields = {}

    unknown = set(fields).difference(_SEED_POLICY_FIELD_KINDS)
    if unknown:
        raise ValueError(f"scenario seed policy has unknown fields: {sorted(unknown)}")
    for name, field in fields.items():
        if field.value_kind is not _SEED_POLICY_FIELD_KINDS[name]:
            raise ValueError(f"scenario seed policy field {name!r} uses the wrong tag")

    target_kind = source.metadata.target_kind
    defaults = {
        "allow_cli_override": ScenarioFieldV1(
            "allow_cli_override",
            ScenarioValueKindV1.FLAG,
            False,
        ),
        "policy_version": ScenarioFieldV1(
            "policy_version",
            ScenarioValueKindV1.VERSION,
            SCENARIO_SEED_DERIVATION_POLICY_VERSION,
        ),
        "root_seed": ScenarioFieldV1(
            "root_seed",
            ScenarioValueKindV1.SEED,
            0,
        ),
        "substreams": ScenarioFieldV1(
            "substreams",
            ScenarioValueKindV1.IDENTIFIERS,
            DEFAULT_SCENARIO_SUBSTREAMS_V1[target_kind],
        ),
    }
    applied: list[ScenarioAppliedDefaultV1] = []
    for name, field in defaults.items():
        if name in fields:
            continue
        fields[name] = field
        applied.append(
            ScenarioAppliedDefaultV1(
                (
                    f"seed_policy.{SCENARIO_SEED_POLICY_LOGICAL_NAME_V1}."
                    f"{name}"
                ),
                field,
            )
        )
    materialized = ScenarioRecordV1(
        logical_name=SCENARIO_SEED_POLICY_LOGICAL_NAME_V1,
        record_type=SCENARIO_SEED_POLICY_RECORD_TYPE_V1,
        version=1,
        fields=tuple(fields.values()),
    )
    return ScenarioDefaultMaterializationV1(
        materialized,
        tuple(sorted(applied, key=lambda item: item.path)),
    )


__all__ = [
    "DEFAULT_SCENARIO_SUBSTREAMS_V1",
    "SCENARIO_SEED_POLICY_LOGICAL_NAME_V1",
    "SCENARIO_SEED_POLICY_RECORD_TYPE_V1",
    "ScenarioAppliedDefaultV1",
    "ScenarioDefaultMaterializationV1",
    "materialize_scenario_defaults",
]
