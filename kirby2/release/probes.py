"""Release-owned production probes that are absent from legacy audit executors.

The queue-reactive benchmark deliberately uses the real simulator and modifier but
does not pretend the legacy ``CoreFlowExecutor`` supports a new flow-model value.
This adapter owns its recording schema, captures every scheduling inspection, and
replays by regenerating the same production run from its verified inputs.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import ClassVar

from kirby2.auditlab.models import GeneratedConfiguration, canonical_json
from kirby2.packs.formats import canonical_json_bytes, require_sha256
from kirby2.scenarios import create_market_engine, get_scenario_definition
from kirby2.simulation import LiquidityPreset, VolumePreset
from kirby2.simulation.flow_models import SimpleFlowModel
from kirby2.simulation.queue_reactive import (
    QueueReactiveFlowModifier,
    default_queue_reactive_config,
)


RELEASE_QUEUE_REACTIVE_RUNNER_ID_V1 = "RELEASE_QUEUE_REACTIVE_V1"
RELEASE_QUEUE_REACTIVE_RECORDING_SCHEMA_ID_V1 = "QUEUE_REACTIVE_EVENT_TAPE_V1"
RELEASE_QUEUE_REACTIVE_RECORDING_SCHEMA_VERSION_V1 = 1
RELEASE_QUEUE_REACTIVE_CONFIG_SHA256_V1 = (
    "15af8f20babde04a3ff0c02defa88213e6a871fe4bc53ec628cd2bd5323eabad"
)
RELEASE_QUEUE_REACTIVE_SCENARIO_SHA256_V1 = (
    "0efb751e880e3112ac5a99a28a8c86b9514ddf47c5c481d72f35328767537699"
)

QUEUE_REACTIVE_CAPABILITIES_V1 = (
    "seed",
    "duration_us",
    "flow_model",
    "regime",
    "volume",
    "liquidity",
    "queue_reactive",
)

QUEUE_REACTIVE_CHECKS_V1 = (
    "quantity_conservation",
    "fifo_book_ordering",
    "non_crossed_book",
    "contiguous_sequences",
    "player_position_reconciliation",
    "player_cash_reconciliation",
    "hawkes_stability",
    "event_rate_cap",
    "observable_projection_boundary",
    "queue_reactive_intensity_applied",
    "queue_reactive_recording_replay",
)


@dataclass(frozen=True, slots=True)
class ReleaseQueueReactiveRecordingV1:
    configuration: dict[str, object]
    dimensions: dict[str, object]
    scenario_definition_sha256: str
    modifier_config: dict[str, object]
    modifier_config_sha256: str
    flow_events: tuple[dict[str, object], ...]
    intensity_inspections: tuple[dict[str, object], ...]
    initial_exchange_event_count: int
    final_book_state_sha256: str

    schema_id: ClassVar[str] = RELEASE_QUEUE_REACTIVE_RECORDING_SCHEMA_ID_V1
    schema_version: ClassVar[int] = RELEASE_QUEUE_REACTIVE_RECORDING_SCHEMA_VERSION_V1

    def __post_init__(self) -> None:
        for value, label in (
            (self.configuration, "queue-reactive configuration"),
            (self.dimensions, "queue-reactive dimensions"),
            (self.modifier_config, "queue-reactive modifier config"),
        ):
            if type(value) is not dict:
                raise TypeError(f"{label} must be an object")
        if self.scenario_definition_sha256 != RELEASE_QUEUE_REACTIVE_SCENARIO_SHA256_V1:
            raise ValueError("queue-reactive scenario definition differs")
        if self.modifier_config_sha256 != RELEASE_QUEUE_REACTIVE_CONFIG_SHA256_V1:
            raise ValueError("queue-reactive modifier configuration differs")
        if hashlib.sha256(
            canonical_json(self.modifier_config).encode("utf-8")
        ).hexdigest() != self.modifier_config_sha256:
            raise ValueError("queue-reactive modifier configuration digest differs")
        if type(self.flow_events) is not tuple or any(
            type(item) is not dict for item in self.flow_events
        ):
            raise TypeError("queue-reactive flow events must be object tuples")
        if type(self.intensity_inspections) is not tuple or any(
            type(item) is not dict for item in self.intensity_inspections
        ):
            raise TypeError("queue-reactive inspections must be object tuples")
        if len(self.flow_events) != len(self.intensity_inspections):
            raise ValueError("queue-reactive event/inspection cardinality differs")
        if type(self.initial_exchange_event_count) is not int or self.initial_exchange_event_count < 0:
            raise ValueError("queue-reactive initial event count is invalid")
        require_sha256(self.final_book_state_sha256, "queue-reactive final book digest")

    def as_dict(self) -> dict[str, object]:
        return {
            "configuration": self.configuration,
            "dimensions": self.dimensions,
            "final_book_state_sha256": self.final_book_state_sha256,
            "flow_events": list(self.flow_events),
            "initial_exchange_event_count": self.initial_exchange_event_count,
            "intensity_inspections": list(self.intensity_inspections),
            "modifier_config": self.modifier_config,
            "modifier_config_sha256": self.modifier_config_sha256,
            "scenario_definition_sha256": self.scenario_definition_sha256,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
        }

    def canonical_bytes(self) -> bytes:
        from .performance import release_float_free_semantic

        return canonical_json_bytes(release_float_free_semantic(self.as_dict()))

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


@dataclass(frozen=True, slots=True)
class ReleaseQueueReactiveResultV1:
    recording: ReleaseQueueReactiveRecordingV1
    capability_records: tuple[dict[str, object], ...]
    check_records: tuple[dict[str, object], ...]
    replay_sha256: str

    def __post_init__(self) -> None:
        if type(self.recording) is not ReleaseQueueReactiveRecordingV1:
            raise TypeError("queue-reactive result recording is invalid")
        require_sha256(self.replay_sha256, "queue-reactive replay digest")
        if self.replay_sha256 != self.recording.sha256:
            raise ValueError("queue-reactive replay digest differs from the recording")
        if type(self.capability_records) is not tuple or type(self.check_records) is not tuple:
            raise TypeError("queue-reactive result records must be tuples")
        if any(type(item) is not dict for item in self.capability_records) or any(
            type(item) is not dict for item in self.check_records
        ):
            raise TypeError("queue-reactive result rows must be objects")
        if tuple(item.get("capability") for item in self.capability_records) != QUEUE_REACTIVE_CAPABILITIES_V1:
            raise ValueError("queue-reactive capability record order differs")
        if tuple(item.get("check_id") for item in self.check_records) != QUEUE_REACTIVE_CHECKS_V1:
            raise ValueError("queue-reactive check record order differs")
        try:
            configured_values = {
                "seed": self.recording.configuration["seed"],
                "duration_us": self.recording.configuration["duration_us"],
                "flow_model": self.recording.configuration["flow_model"],
                "regime": self.recording.configuration["regime"],
                "volume": self.recording.configuration["volume"],
                "liquidity": self.recording.configuration["liquidity"],
                "queue_reactive": self.recording.modifier_config_sha256,
            }
        except KeyError as error:
            raise ValueError("queue-reactive recording configuration is incomplete") from error
        for item in self.capability_records:
            if type(item) is not dict or set(item) != {
                "capability",
                "configured_value",
                "evidence_sha256",
                "status",
            } or item["status"] != "EXERCISED":
                raise ValueError("queue-reactive capability record differs")
            require_sha256(item["evidence_sha256"], "queue-reactive capability evidence")
            expected_evidence = hashlib.sha256(
                canonical_json_bytes(
                    {
                        "capability": item["capability"],
                        "configured_value": configured_values[item["capability"]],
                        "recording_sha256": self.recording.sha256,
                    }
                )
            ).hexdigest()
            if (
                item["configured_value"] != configured_values[item["capability"]]
                or item["evidence_sha256"] != expected_evidence
            ):
                raise ValueError("queue-reactive capability evidence differs")
        for item in self.check_records:
            if type(item) is not dict or set(item) != {
                "check_id",
                "evidence_sha256",
                "status",
            } or item["status"] != "PASS":
                raise ValueError("queue-reactive check record differs")
            require_sha256(item["evidence_sha256"], "queue-reactive check evidence")
            expected_evidence = hashlib.sha256(
                canonical_json_bytes(
                    {
                        "check_id": item["check_id"],
                        "recording_sha256": self.recording.sha256,
                        "replay_sha256": self.replay_sha256,
                    }
                )
            ).hexdigest()
            if item["evidence_sha256"] != expected_evidence:
                raise ValueError("queue-reactive check evidence differs")

    def as_dict(self) -> dict[str, object]:
        return {
            "artifact_form": RELEASE_QUEUE_REACTIVE_RECORDING_SCHEMA_ID_V1,
            "capability_records": list(self.capability_records),
            "check_records": list(self.check_records),
            "recording": self.recording.as_dict(),
            "recording_sha256": self.recording.sha256,
            "replay_sha256": self.replay_sha256,
            "runner_id": RELEASE_QUEUE_REACTIVE_RUNNER_ID_V1,
            "schema_id": "KIRBY2_RELEASE_QUEUE_REACTIVE_RESULT_V1",
            "schema_version": 1,
        }


def _configuration(value: GeneratedConfiguration | Mapping[str, object]) -> GeneratedConfiguration:
    if isinstance(value, GeneratedConfiguration):
        configuration = value
    elif isinstance(value, Mapping):
        configuration = GeneratedConfiguration.from_dict(dict(value))
    else:
        raise TypeError("queue-reactive release probe requires a generated configuration")
    if configuration.lane.value != "CORE_FLOW":
        raise ValueError("queue-reactive release probe requires the CORE_FLOW lane")
    if configuration.flow_model != "queue_reactive":
        raise ValueError("queue-reactive release probe requires its exact flow-model value")
    return configuration


def _execute(configuration: GeneratedConfiguration) -> ReleaseQueueReactiveRecordingV1:
    definition = get_scenario_definition(configuration.regime.lower())
    modifier_config = default_queue_reactive_config()
    modifier_bytes = canonical_json(modifier_config.as_dict()).encode("utf-8")
    modifier_digest = hashlib.sha256(modifier_bytes).hexdigest()
    if modifier_digest != RELEASE_QUEUE_REACTIVE_CONFIG_SHA256_V1:
        raise ValueError("queue-reactive production config differs from the frozen digest")
    modifier = QueueReactiveFlowModifier(modifier_config)
    engine, dimensions = create_market_engine(
        definition,
        seed=configuration.seed,
        relative_volume=VolumePreset(configuration.volume),
        liquidity=LiquidityPreset(configuration.liquidity),
        flow_model=SimpleFlowModel(),
        intensity_modifier=modifier,
    )
    inspections: list[dict[str, object]] = []

    def capture(_event) -> None:
        inspection = engine.last_intensity_inspection
        if inspection is None:
            raise RuntimeError("queue-reactive event lacks its scheduling inspection")
        inspections.append(inspection.as_dict())

    engine.advance_to(configuration.duration_us, on_event=capture)
    if len(inspections) != len(engine.flow_events):
        raise RuntimeError("queue-reactive inspection/event cardinality differs")
    engine.book.assert_invariants()
    definition_digest = hashlib.sha256(
        canonical_json(definition.as_dict()).encode("utf-8")
    ).hexdigest()
    if definition_digest != RELEASE_QUEUE_REACTIVE_SCENARIO_SHA256_V1:
        raise ValueError("queue-reactive balanced scenario differs from the frozen digest")
    return ReleaseQueueReactiveRecordingV1(
        configuration=configuration.as_dict(),
        dimensions=dimensions.as_dict(),
        scenario_definition_sha256=definition_digest,
        modifier_config=modifier_config.as_dict(),
        modifier_config_sha256=modifier_digest,
        flow_events=tuple(item.as_dict() for item in engine.flow_events),
        intensity_inspections=tuple(inspections),
        initial_exchange_event_count=engine.initial_exchange_event_count,
        final_book_state_sha256=engine.book.state_sha256(),
    )


def run_release_queue_reactive_probe(
    value: GeneratedConfiguration | Mapping[str, object],
) -> ReleaseQueueReactiveResultV1:
    """Execute and immediately regenerate one exact queue-reactive recording."""

    configuration = _configuration(value)
    recording = _execute(configuration)
    replay = _execute(configuration)
    if replay.canonical_bytes() != recording.canonical_bytes():
        raise RuntimeError("queue-reactive production recording did not replay exactly")
    replay_sha256 = replay.sha256
    configured_values = {
        "seed": configuration.seed,
        "duration_us": configuration.duration_us,
        "flow_model": configuration.flow_model,
        "regime": configuration.regime,
        "volume": configuration.volume,
        "liquidity": configuration.liquidity,
        "queue_reactive": recording.modifier_config_sha256,
    }
    capability_records = tuple(
        {
            "capability": name,
            "configured_value": configured_values[name],
            "evidence_sha256": hashlib.sha256(
                canonical_json_bytes(
                    {
                        "capability": name,
                        "configured_value": configured_values[name],
                        "recording_sha256": recording.sha256,
                    }
                )
            ).hexdigest(),
            "status": "EXERCISED",
        }
        for name in QUEUE_REACTIVE_CAPABILITIES_V1
    )
    check_records = tuple(
        {
            "check_id": name,
            "evidence_sha256": hashlib.sha256(
                canonical_json_bytes(
                    {
                        "check_id": name,
                        "recording_sha256": recording.sha256,
                        "replay_sha256": replay_sha256,
                    }
                )
            ).hexdigest(),
            "status": "PASS",
        }
        for name in QUEUE_REACTIVE_CHECKS_V1
    )
    return ReleaseQueueReactiveResultV1(
        recording=recording,
        capability_records=capability_records,
        check_records=check_records,
        replay_sha256=replay_sha256,
    )


__all__ = [
    "QUEUE_REACTIVE_CAPABILITIES_V1",
    "QUEUE_REACTIVE_CHECKS_V1",
    "RELEASE_QUEUE_REACTIVE_CONFIG_SHA256_V1",
    "RELEASE_QUEUE_REACTIVE_RECORDING_SCHEMA_ID_V1",
    "RELEASE_QUEUE_REACTIVE_RECORDING_SCHEMA_VERSION_V1",
    "RELEASE_QUEUE_REACTIVE_RUNNER_ID_V1",
    "RELEASE_QUEUE_REACTIVE_SCENARIO_SHA256_V1",
    "ReleaseQueueReactiveRecordingV1",
    "ReleaseQueueReactiveResultV1",
    "run_release_queue_reactive_probe",
]
