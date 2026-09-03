"""Strict V1 finalization and immutable simulation Replay artifact records."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from .simulation_contract import (
    ResolvedSimulationConfigurationV1,
    SimulationComponentRefV1,
    SimulationContractIntegrityError,
    SimulationProfileRefV1,
    SimulationProfileSelectionV1,
    _array,
    _digest,
    _enum,
    _exact,
    _freeze,
    _integer,
    _object,
    _plain,
    _positive_integer,
    _snapshot,
    _text,
    canonical_digest,
)
from .simulation_live_contract import (
    SimulationFrameV1,
    SimulationTrainingOptionsV1,
    _validate_cursor,
    _validate_diagnostic,
    _validate_metric,
    _validate_provenance,
)


ARTIFACT_SCHEMA_ID = "KIRBY2_SIMULATION_REPLAY_ARTIFACT_V1"
ARTIFACT_REFERENCE_SCHEMA_ID = "KIRBY2_REPLAY_ARTIFACT_REFERENCE_V1"
RUN_RESULT_SCHEMA_ID = "KIRBY2_SIMULATION_RUN_RESULT_V1"
FINALIZE_RESULT_SCHEMA_ID = "KIRBY2_SIMULATION_FINALIZE_RESULT_V1"
SCHEMA_VERSION = 1

ARTIFACT_KIND = "SIMULATION_REPLAY_ARTIFACT"
RECORDING_MEDIA_TYPE = "application/vnd.kirby2.session-recording+json"
RECORDING_SCHEMA_VERSION = 2
RECORDING_ENCODING = "BASE64_RFC4648"
TIMELINE_KINDS = frozenset(
    {
        "INPUT",
        "COMMAND",
        "REJECTED",
        "PARTIAL_FILL",
        "FILL",
        "POSITION",
        "CANCEL",
        "REPLACE",
        "TRAFFIC",
        "STRATEGY_EVALUATION",
        "OBJECTIVE",
        "CURRICULUM",
        "MID",
        "BOOK",
    }
)
FINALIZE_MODES = frozenset({"COMPLETE_ONLY", "ALLOW_PARTIAL"})
FINALIZE_UNAVAILABLE_REASONS = frozenset(
    {
        "STALE_ORIGIN",
        "SOURCE_RUN_MISMATCH",
        "RUN_NOT_COMPLETE",
        "ALREADY_ABANDONED",
        "RESET_PENDING",
    }
)

_RUN_ID = re.compile(r"simulation-run-[0-9a-f]{32}\Z")
_REPLAY_RUN_ID = re.compile(r"run-[0-9a-f]{24}\Z")
_FRAME_ID = re.compile(r"simulation-frame-[0-9a-f]{24}\Z")
_CURSOR_ID = re.compile(r"simulation-cursor-[0-9a-f]{24}\Z")
_ARTIFACT_ID = re.compile(r"replay-artifact-[0-9a-f]{24}\Z")
_RUN_RESULT_ID = re.compile(r"simulation-run-result-[0-9a-f]{24}\Z")
_FINALIZE_RESULT_ID = re.compile(r"simulation-finalize-result-[0-9a-f]{24}\Z")
_LOCATOR = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}\Z")

_COMPONENT_FIELDS = frozenset({"component_ref", "payload"})
_COMPONENT_PAYLOAD_FIELDS = frozenset(
    {
        "schema_id",
        "schema_version",
        "component_kind",
        "component_id",
        "component_version",
        "payload",
    }
)
_RECORDING_FIELDS = frozenset(
    {
        "media_type",
        "recording_schema_version",
        "encoding",
        "bytes_base64",
        "bytes_sha256",
    }
)
_RECORDING_PAYLOAD_FIELDS = frozenset(
    {
        "auto_start",
        "complete",
        "completed_time_us",
        "curriculum_drill",
        "duration_seconds",
        "expected_state_sha256",
        "expected_timeline_sha256",
        "initial_quantity",
        "inputs",
        "layout",
        "liquidity",
        "market_states",
        "objective",
        "quantity_options",
        "record_type",
        "relative_volume",
        "scenario_definition",
        "schema_version",
        "seed",
        "strategy_source",
    }
)
_RECORDING_SCENARIO_FIELDS = frozenset(
    {
        "accepted_replay_sha256",
        "behavioral_envelope",
        "duration_seconds",
        "initial_depth",
        "initial_mid_ticks",
        "liquidity",
        "name",
        "parameter_overrides",
        "regime",
        "relative_volume",
        "seed",
    }
)
_RECORDING_LAYOUT_FIELDS = frozenset({"bindings", "name", "schema_version"})
_TIMELINE_EVENT_FIELDS = frozenset(
    {"sequence", "simulation_time_us", "kind", "message", "data"}
)
_ARTIFACT_FIELDS = frozenset(
    {
        "schema_id",
        "schema_version",
        "source_run_id",
        "replay_run_id",
        "profile_ref",
        "selection",
        "resolved_configuration_sha256",
        "resolved_configuration",
        "training_options",
        "run_request_sha256",
        "component_payloads",
        "session_recording",
        "event_tape",
        "event_tape_sha256",
        "terminal_status",
        "final_frame",
        "provenance",
    }
)
_ARTIFACT_REFERENCE_FIELDS = frozenset(
    {
        "schema_id",
        "schema_version",
        "artifact_id",
        "artifact_kind",
        "artifact_schema_id",
        "artifact_schema_version",
        "artifact_sha256",
        "source_run_id",
        "replay_run_id",
        "store_id",
        "object_key",
    }
)
_RUN_RESULT_FIELDS = frozenset(
    {
        "schema_id",
        "schema_version",
        "result_id",
        "source_run_id",
        "replay_run_id",
        "profile_ref",
        "selection_sha256",
        "resolved_configuration_sha256",
        "run_request_sha256",
        "terminal_status",
        "final_frame_id",
        "final_cursor",
        "final_book_state_sha256",
        "event_tape_sha256",
        "replay_artifact",
        "metrics",
        "diagnostics",
        "provenance",
    }
)
_FINALIZE_RESULT_FIELDS = frozenset(
    {
        "schema_id",
        "schema_version",
        "result_id",
        "status",
        "mode",
        "source_run_id",
        "origin_frame_id",
        "origin_cursor_id",
        "run_result",
        "unavailable_reason",
    }
)


def replay_run_id_for_source(source_run_id: str) -> str:
    _prefixed_id(source_run_id, _RUN_ID, "simulation source run ID")
    basis = {
        "schema_id": "KIRBY2_LIVE_TO_REPLAY_RUN_ID_V1",
        "schema_version": 1,
        "source_run_id": source_run_id,
    }
    return f"run-{canonical_digest(basis)[:24]}"


def _schema(root: Mapping[str, object], schema_id: str, label: str) -> None:
    if (
        root["schema_id"] != schema_id
        or type(root["schema_version"]) is not int
        or root["schema_version"] != SCHEMA_VERSION
    ):
        raise ValueError(f"{label} schema is unsupported")


def _prefixed_id(value: object, pattern: re.Pattern[str], label: str) -> str:
    result = _text(value, label)
    if pattern.fullmatch(result) is None:
        raise ValueError(f"{label} has an invalid V1 form")
    return result


def _locator(value: object, label: str) -> str:
    result = _text(value, label)
    if _LOCATOR.fullmatch(result) is None:
        raise ValueError(f"{label} is not a safe governed locator")
    return result


def _cursor_from_frame(frame: SimulationFrameV1) -> Mapping[str, object]:
    cursor = frame.record["cursor"]
    if not isinstance(cursor, Mapping):
        raise SimulationContractIntegrityError("validated frame lost its cursor")
    return cursor


def _recording_payload(raw: bytes) -> dict[str, object]:
    def exact_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"session recording repeats JSON key {key!r}")
            result[key] = value
        return result

    def reject_constant(value: str) -> object:
        raise ValueError(f"session recording contains non-finite number {value}")

    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=exact_object,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("embedded session recording is not valid UTF-8 JSON") from error
    if type(value) is not dict:
        raise TypeError("embedded session recording root must be an object")
    _exact(value, _RECORDING_PAYLOAD_FIELDS, "embedded session recording payload")
    if (
        value.get("record_type") != "kirby2_session_recording"
        or type(value.get("schema_version")) is not int
        or value["schema_version"] != RECORDING_SCHEMA_VERSION
    ):
        raise ValueError("embedded session recording schema is unsupported")

    def reject_nonfinite(item: object, path: str) -> None:
        if type(item) is float and not math.isfinite(item):
            raise ValueError(f"embedded session recording has non-finite number at {path}")
        if type(item) is dict:
            for key, child in item.items():
                reject_nonfinite(child, f"{path}.{key}")
        elif type(item) is list:
            for index, child in enumerate(item):
                reject_nonfinite(child, f"{path}[{index}]")

    reject_nonfinite(value, "$recording")
    if type(value["auto_start"]) is not bool or type(value["complete"]) is not bool:
        raise ValueError("embedded session recording lifecycle flags must be Boolean")
    duration_seconds = _positive_integer(
        value["duration_seconds"], "embedded recording.duration_seconds"
    )
    completed_time_us = _integer(
        value["completed_time_us"],
        "embedded recording.completed_time_us",
        minimum=0,
    )
    if completed_time_us > duration_seconds * 1_000_000:
        raise ValueError("embedded session recording exceeds its duration")
    if value["complete"] and completed_time_us != duration_seconds * 1_000_000:
        raise ValueError("complete embedded session recording does not end at its duration")
    _integer(value["seed"], "embedded recording.seed", minimum=0)
    quantities = _array(value["quantity_options"], "embedded recording.quantity_options")
    normalized_quantities = tuple(
        _positive_integer(item, f"embedded recording.quantity_options[{index}]")
        for index, item in enumerate(quantities)
    )
    if (
        not normalized_quantities
        or any(
            left >= right
            for left, right in zip(normalized_quantities, normalized_quantities[1:])
        )
    ):
        raise ValueError("embedded recording quantities must be ascending and unique")
    initial_quantity = _positive_integer(
        value["initial_quantity"], "embedded recording.initial_quantity"
    )
    if initial_quantity not in normalized_quantities:
        raise ValueError("embedded recording initial quantity is not advertised")
    _text(value["relative_volume"], "embedded recording.relative_volume")
    _text(value["liquidity"], "embedded recording.liquidity")
    _digest(value["expected_state_sha256"], "embedded recording state digest")
    _digest(value["expected_timeline_sha256"], "embedded recording timeline digest")
    for field in ("inputs", "market_states"):
        rows = _array(value[field], f"embedded recording.{field}")
        if any(type(item) is not dict for item in rows):
            raise TypeError(f"embedded recording.{field} entries must be objects")
    for field in ("objective", "curriculum_drill"):
        if value[field] is not None and type(value[field]) is not dict:
            raise TypeError(f"embedded recording.{field} must be an object or null")
    if value["strategy_source"] is not None and type(value["strategy_source"]) is not str:
        raise TypeError("embedded recording.strategy_source must be text or null")

    scenario = value["scenario_definition"]
    if type(scenario) is not dict:
        raise TypeError("embedded recording scenario definition must be an object")
    _exact(scenario, _RECORDING_SCENARIO_FIELDS, "embedded recording scenario")
    _digest(scenario["accepted_replay_sha256"], "embedded scenario accepted replay")
    _text(scenario["name"], "embedded scenario.name")
    _text(scenario["regime"], "embedded scenario.regime")
    _text(scenario["relative_volume"], "embedded scenario.relative_volume")
    _text(scenario["liquidity"], "embedded scenario.liquidity")
    for field in ("seed", "duration_seconds", "initial_mid_ticks", "initial_depth"):
        _positive_integer(scenario[field], f"embedded scenario.{field}")
    if type(scenario["parameter_overrides"]) is not dict:
        raise TypeError("embedded scenario.parameter_overrides must be an object")
    if type(scenario["behavioral_envelope"]) is not dict:
        raise TypeError("embedded scenario.behavioral_envelope must be an object")

    layout = value["layout"]
    if type(layout) is not dict:
        raise TypeError("embedded recording layout must be an object")
    _exact(layout, _RECORDING_LAYOUT_FIELDS, "embedded recording layout")
    if type(layout["schema_version"]) is not int or layout["schema_version"] != 1:
        raise ValueError("embedded recording layout schema is unsupported")
    _text(layout["name"], "embedded recording layout.name")
    bindings = layout["bindings"]
    if type(bindings) is not dict or any(
        type(key) is not str or not key or type(command) is not str or not command
        for key, command in bindings.items()
    ):
        raise ValueError("embedded recording layout bindings are invalid")
    return value


def _component_inner_payload(component: EmbeddedComponentV1) -> Mapping[str, object]:
    inner = component.payload.get("payload")
    if not isinstance(inner, Mapping):
        raise SimulationContractIntegrityError("embedded component lost its inner payload")
    return inner


def _legacy_intensity_ppm(value: object) -> int:
    if type(value) not in {int, float}:
        raise ValueError("embedded scenario event intensity must be numeric")
    try:
        scaled = Decimal(str(value)) * Decimal(1_000_000)
    except InvalidOperation as error:
        raise ValueError("embedded scenario event intensity is invalid") from error
    if scaled != scaled.to_integral_value():
        raise ValueError("embedded scenario event intensity is not ppm-exact")
    return int(scaled)


def _validate_recording_correlation(
    recording: EmbeddedSessionRecordingV1,
    configuration: ResolvedSimulationConfigurationV1,
    training: SimulationTrainingOptionsV1,
    components: tuple[EmbeddedComponentV1, ...],
    terminal_status: str,
    cursor: Mapping[str, object],
) -> None:
    payload = recording.recording_payload
    scenario = payload["scenario_definition"]
    layout = payload["layout"]
    if not isinstance(scenario, Mapping) or not isinstance(layout, Mapping):
        raise SimulationContractIntegrityError(
            "embedded recording lost its scenario or layout"
        )
    components_by_kind = {
        item.component_ref.component_kind: _component_inner_payload(item)
        for item in components
    }
    scenario_component = components_by_kind["SCENARIO_DEFINITION"]
    layout_component = components_by_kind["HOTKEY_LAYOUT"]
    overrides = scenario["parameter_overrides"]
    if not isinstance(overrides, Mapping):
        raise SimulationContractIntegrityError(
            "embedded recording scenario overrides are not an object"
        )
    if set(overrides) - {"event_intensity"}:
        raise SimulationContractIntegrityError(
            "embedded recording scenario has unrepresented overrides"
        )
    event_intensity_ppm = _legacy_intensity_ppm(
        overrides.get("event_intensity", 1)
    )
    expected_objective = (
        None if training.objective is None else training.objective.as_dict()
    )
    if (
        payload["seed"] != configuration.seed
        or payload["duration_seconds"] * 1_000_000 != configuration.duration_us
        or payload["relative_volume"] != configuration.relative_volume
        or payload["liquidity"] != configuration.liquidity
        or payload["initial_quantity"] != training.initial_quantity
        or tuple(payload["quantity_options"]) != training.quantity_options
        or payload["auto_start"] != (training.initial_run_state == "RUNNING")
        or payload["completed_time_us"] != cursor["simulation_time_us"]
        or payload["complete"] != (terminal_status == "COMPLETE")
        or payload["objective"] != expected_objective
        or (training.strategy_ref is None) != (payload["strategy_source"] is None)
        or (training.curriculum_drill_ref is None)
        != (payload["curriculum_drill"] is None)
    ):
        raise SimulationContractIntegrityError(
            "embedded session recording differs from the resolved run"
        )
    scenario_pairs = (
        (scenario["accepted_replay_sha256"], scenario_component.get("accepted_replay_sha256")),
        (scenario["name"], scenario_component.get("scenario_name")),
        (scenario["seed"], scenario_component.get("seed")),
        (scenario["duration_seconds"] * 1_000_000, scenario_component.get("duration_us")),
        (scenario["initial_mid_ticks"], scenario_component.get("initial_mid_ticks")),
        (scenario["initial_depth"], scenario_component.get("initial_depth")),
        (scenario["liquidity"], scenario_component.get("liquidity")),
        (scenario["regime"], scenario_component.get("regime")),
        (scenario["relative_volume"], scenario_component.get("relative_volume")),
        (event_intensity_ppm, scenario_component.get("event_intensity_ppm")),
    )
    if any(actual != expected for actual, expected in scenario_pairs):
        raise SimulationContractIntegrityError(
            "embedded recording scenario differs from its pinned component"
        )
    if layout["name"] != layout_component.get("layout_name"):
        raise SimulationContractIntegrityError(
            "embedded recording layout differs from its pinned component"
        )


@dataclass(frozen=True, slots=True)
class EmbeddedComponentV1:
    component_ref: SimulationComponentRefV1
    payload: Mapping[str, object]
    record: Mapping[str, object]

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> EmbeddedComponentV1:
        root = _object(_snapshot(payload), "embedded simulation component")
        _exact(root, _COMPONENT_FIELDS, "embedded simulation component")
        reference = SimulationComponentRefV1.from_dict(
            _object(root["component_ref"], "embedded component reference")
        )
        component_payload = _object(root["payload"], "embedded component payload")
        if canonical_digest(component_payload) != reference.content_sha256:
            raise SimulationContractIntegrityError(
                "embedded component digest does not match its reference"
            )
        _exact(component_payload, _COMPONENT_PAYLOAD_FIELDS, "embedded component payload")
        if (
            component_payload["schema_id"]
            != "KIRBY2_SIMULATION_COMPONENT_PAYLOAD_V1"
            or type(component_payload["schema_version"]) is not int
            or component_payload["schema_version"] != 1
            or component_payload["component_kind"] != reference.component_kind
            or component_payload["component_id"] != reference.component_id
            or type(component_payload["component_version"]) is not int
            or component_payload["component_version"] != reference.component_version
            or type(component_payload["payload"]) is not dict
        ):
            raise SimulationContractIntegrityError(
                "embedded component payload identity differs from its reference"
            )
        normalized = {
            "component_ref": reference.as_dict(),
            "payload": component_payload,
        }
        return cls(reference, _freeze(component_payload), _freeze(normalized))

    def as_dict(self) -> dict[str, object]:
        return _plain(self.record)


@dataclass(frozen=True, slots=True)
class EmbeddedSessionRecordingV1:
    bytes_sha256: str
    recording_bytes: bytes
    recording_payload: Mapping[str, object]
    record: Mapping[str, object]

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> EmbeddedSessionRecordingV1:
        root = _object(_snapshot(payload), "embedded session recording")
        _exact(root, _RECORDING_FIELDS, "embedded session recording")
        if (
            root["media_type"] != RECORDING_MEDIA_TYPE
            or type(root["recording_schema_version"]) is not int
            or root["recording_schema_version"] != RECORDING_SCHEMA_VERSION
            or root["encoding"] != RECORDING_ENCODING
        ):
            raise ValueError("embedded session recording encoding is unsupported")
        encoded = _text(root["bytes_base64"], "embedded recording.bytes_base64")
        if encoded.strip() != encoded or any(character.isspace() for character in encoded):
            raise ValueError("embedded recording base64 contains whitespace")
        try:
            raw = base64.b64decode(encoded.encode("ascii"), validate=True)
        except (UnicodeEncodeError, binascii.Error) as error:
            raise ValueError("embedded recording is not canonical RFC 4648 base64") from error
        if not raw or base64.b64encode(raw).decode("ascii") != encoded:
            raise ValueError("embedded recording base64 form is not canonical")
        digest = _digest(root["bytes_sha256"], "embedded recording.bytes_sha256")
        if hashlib.sha256(raw).hexdigest() != digest:
            raise SimulationContractIntegrityError(
                "embedded recording digest does not match its decoded bytes"
            )
        recording_payload = _recording_payload(raw)
        normalized = {
            "media_type": RECORDING_MEDIA_TYPE,
            "recording_schema_version": RECORDING_SCHEMA_VERSION,
            "encoding": RECORDING_ENCODING,
            "bytes_base64": encoded,
            "bytes_sha256": digest,
        }
        return cls(digest, raw, _freeze(recording_payload), _freeze(normalized))

    def as_dict(self) -> dict[str, object]:
        return _plain(self.record)


@dataclass(frozen=True, slots=True)
class SimulationTimelineEventV1:
    sequence: int
    simulation_time_us: int
    kind: str
    message: str
    data: Mapping[str, object]
    record: Mapping[str, object]

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> SimulationTimelineEventV1:
        root = _object(_snapshot(payload), "simulation timeline event")
        _exact(root, _TIMELINE_EVENT_FIELDS, "simulation timeline event")
        normalized = {
            "sequence": _positive_integer(
                root["sequence"], "simulation timeline event.sequence"
            ),
            "simulation_time_us": _integer(
                root["simulation_time_us"],
                "simulation timeline event.simulation_time_us",
                minimum=0,
            ),
            "kind": _enum(
                root["kind"], TIMELINE_KINDS, "simulation timeline event.kind"
            ),
            "message": _text(root["message"], "simulation timeline event.message"),
            "data": _object(root["data"], "simulation timeline event.data"),
        }
        return cls(
            int(normalized["sequence"]),
            int(normalized["simulation_time_us"]),
            str(normalized["kind"]),
            str(normalized["message"]),
            _freeze(normalized["data"]),
            _freeze(normalized),
        )

    def as_dict(self) -> dict[str, object]:
        return _plain(self.record)


def _component_identity(reference: SimulationComponentRefV1) -> tuple[object, ...]:
    return (
        reference.component_kind,
        reference.component_id,
        reference.component_version,
        reference.content_sha256,
    )


def _reachable_component_refs(
    configuration: ResolvedSimulationConfigurationV1,
    training: SimulationTrainingOptionsV1,
) -> tuple[SimulationComponentRefV1, ...]:
    references = (
        configuration.scenario_definition_ref,
        configuration.regime_profile_ref,
        configuration.distribution_bundle_ref,
        configuration.queue_reactive_ref,
        configuration.hawkes_ref,
        configuration.intraday_ref,
        training.layout_ref,
        training.strategy_ref,
        training.curriculum_drill_ref,
        training.observation_policy_ref,
    )
    distinct = {reference for reference in references if reference is not None}
    return tuple(sorted(distinct, key=_component_identity))


@dataclass(frozen=True, slots=True)
class SimulationReplayArtifactV1:
    source_run_id: str
    replay_run_id: str
    event_tape_sha256: str
    terminal_status: str
    final_frame: SimulationFrameV1
    session_recording: EmbeddedSessionRecordingV1
    record: Mapping[str, object]

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> SimulationReplayArtifactV1:
        root = _object(_snapshot(payload), "simulation Replay artifact")
        _exact(root, _ARTIFACT_FIELDS, "simulation Replay artifact")
        _schema(root, ARTIFACT_SCHEMA_ID, "simulation Replay artifact")
        source_run_id = _prefixed_id(
            root["source_run_id"], _RUN_ID, "simulation Replay artifact.source_run_id"
        )
        replay_run_id = _prefixed_id(
            root["replay_run_id"], _REPLAY_RUN_ID, "simulation Replay artifact.replay_run_id"
        )
        if replay_run_id != replay_run_id_for_source(source_run_id):
            raise SimulationContractIntegrityError(
                "simulation Replay run ID does not map from its live source"
            )
        profile_ref = SimulationProfileRefV1.from_dict(
            _object(root["profile_ref"], "artifact profile reference")
        )
        selection = SimulationProfileSelectionV1.from_dict(
            _object(root["selection"], "artifact selection")
        )
        configuration_record = _object(
            root["resolved_configuration"], "artifact resolved configuration"
        )
        configuration = ResolvedSimulationConfigurationV1.from_dict(configuration_record)
        configuration_sha256 = _digest(
            root["resolved_configuration_sha256"],
            "artifact resolved configuration digest",
        )
        training = SimulationTrainingOptionsV1.from_dict(
            _object(root["training_options"], "artifact training options")
        )
        run_request_sha256 = _digest(
            root["run_request_sha256"], "artifact run request digest"
        )
        expected_request = canonical_digest(
            {
                "schema_id": "KIRBY2_SIMULATION_RUN_REQUEST_V1",
                "schema_version": 1,
                "resolved_configuration_sha256": configuration_sha256,
                "training_options": training.as_dict(),
            }
        )
        if (
            selection.profile_ref != profile_ref
            or selection.selection_sha256 != configuration.selection_sha256
            or configuration.profile_ref != profile_ref
            or canonical_digest(configuration_record) != configuration_sha256
            or expected_request != run_request_sha256
        ):
            raise SimulationContractIntegrityError(
                "simulation Replay artifact configuration identities disagree"
            )
        components = tuple(
            EmbeddedComponentV1.from_dict(
                _object(item, f"artifact component_payloads[{index}]")
            )
            for index, item in enumerate(
                _array(root["component_payloads"], "artifact component_payloads")
            )
        )
        component_refs = tuple(item.component_ref for item in components)
        if (
            component_refs != tuple(sorted(component_refs, key=_component_identity))
            or len(component_refs) != len(set(component_refs))
            or component_refs != _reachable_component_refs(configuration, training)
        ):
            raise SimulationContractIntegrityError(
                "simulation Replay artifact component inventory is not exact"
            )
        recording = EmbeddedSessionRecordingV1.from_dict(
            _object(root["session_recording"], "artifact session recording")
        )
        events = tuple(
            SimulationTimelineEventV1.from_dict(
                _object(item, f"artifact event_tape[{index}]")
            )
            for index, item in enumerate(_array(root["event_tape"], "artifact event tape"))
        )
        if tuple(item.sequence for item in events) != tuple(range(1, len(events) + 1)):
            raise ValueError("simulation Replay event sequences are not contiguous")
        event_times = tuple(item.simulation_time_us for item in events)
        if event_times != tuple(sorted(event_times)):
            raise ValueError("simulation Replay event times are not nondecreasing")
        event_records = [item.as_dict() for item in events]
        event_tape_sha256 = _digest(
            root["event_tape_sha256"], "artifact event tape digest"
        )
        if canonical_digest(event_records) != event_tape_sha256:
            raise SimulationContractIntegrityError(
                "simulation Replay event tape digest does not match"
            )
        terminal_status = _enum(
            root["terminal_status"],
            frozenset({"COMPLETE", "SAVED_PARTIAL"}),
            "artifact terminal status",
        )
        final_frame = SimulationFrameV1.from_dict(
            _object(root["final_frame"], "artifact final frame")
        )
        cursor = _cursor_from_frame(final_frame)
        provenance = _validate_provenance(root["provenance"])
        recording_payload = recording.recording_payload
        if (
            final_frame.source_run_id != source_run_id
            or final_frame.profile_ref != profile_ref
            or final_frame.resolved_configuration_sha256 != configuration_sha256
            or final_frame.run_request_sha256 != run_request_sha256
            or provenance != final_frame.as_dict()["provenance"]
            or (terminal_status == "COMPLETE") != (cursor["run_state"] == "COMPLETE")
            or (event_times and event_times[-1] > cursor["simulation_time_us"])
            or recording_payload.get("completed_time_us") != cursor["simulation_time_us"]
            or recording_payload.get("complete") != (terminal_status == "COMPLETE")
        ):
            raise SimulationContractIntegrityError(
                "simulation Replay artifact terminal identities disagree"
            )
        _validate_recording_correlation(
            recording,
            configuration,
            training,
            components,
            terminal_status,
            cursor,
        )
        normalized = {
            "schema_id": ARTIFACT_SCHEMA_ID,
            "schema_version": SCHEMA_VERSION,
            "source_run_id": source_run_id,
            "replay_run_id": replay_run_id,
            "profile_ref": profile_ref.as_dict(),
            "selection": selection.as_dict(),
            "resolved_configuration_sha256": configuration_sha256,
            "resolved_configuration": configuration.as_dict(),
            "training_options": training.as_dict(),
            "run_request_sha256": run_request_sha256,
            "component_payloads": [item.as_dict() for item in components],
            "session_recording": recording.as_dict(),
            "event_tape": event_records,
            "event_tape_sha256": event_tape_sha256,
            "terminal_status": terminal_status,
            "final_frame": final_frame.as_dict(),
            "provenance": provenance,
        }
        return cls(
            source_run_id,
            replay_run_id,
            event_tape_sha256,
            terminal_status,
            final_frame,
            recording,
            _freeze(normalized),
        )

    def as_dict(self) -> dict[str, object]:
        return _plain(self.record)


@dataclass(frozen=True, slots=True)
class ReplayArtifactRefV1:
    artifact_id: str
    artifact_sha256: str
    source_run_id: str
    replay_run_id: str
    store_id: str
    object_key: str
    record: Mapping[str, object]

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> ReplayArtifactRefV1:
        root = _object(_snapshot(payload), "Replay artifact reference")
        _exact(root, _ARTIFACT_REFERENCE_FIELDS, "Replay artifact reference")
        _schema(root, ARTIFACT_REFERENCE_SCHEMA_ID, "Replay artifact reference")
        if (
            root["artifact_kind"] != ARTIFACT_KIND
            or root["artifact_schema_id"] != ARTIFACT_SCHEMA_ID
            or type(root["artifact_schema_version"]) is not int
            or root["artifact_schema_version"] != SCHEMA_VERSION
        ):
            raise ValueError("Replay artifact reference target is unsupported")
        artifact_sha256 = _digest(
            root["artifact_sha256"], "Replay artifact reference.artifact_sha256"
        )
        artifact_id = _prefixed_id(
            root["artifact_id"], _ARTIFACT_ID, "Replay artifact reference.artifact_id"
        )
        if artifact_id != f"replay-artifact-{artifact_sha256[:24]}":
            raise SimulationContractIntegrityError(
                "Replay artifact ID does not match its byte digest"
            )
        source_run_id = _prefixed_id(
            root["source_run_id"], _RUN_ID, "Replay artifact reference.source_run_id"
        )
        replay_run_id = _prefixed_id(
            root["replay_run_id"], _REPLAY_RUN_ID, "Replay artifact reference.replay_run_id"
        )
        if replay_run_id != replay_run_id_for_source(source_run_id):
            raise SimulationContractIntegrityError(
                "Replay artifact reference run mapping is invalid"
            )
        store_id = _locator(root["store_id"], "Replay artifact reference.store_id")
        object_key = _locator(root["object_key"], "Replay artifact reference.object_key")
        normalized = {
            "schema_id": ARTIFACT_REFERENCE_SCHEMA_ID,
            "schema_version": SCHEMA_VERSION,
            "artifact_id": artifact_id,
            "artifact_kind": ARTIFACT_KIND,
            "artifact_schema_id": ARTIFACT_SCHEMA_ID,
            "artifact_schema_version": SCHEMA_VERSION,
            "artifact_sha256": artifact_sha256,
            "source_run_id": source_run_id,
            "replay_run_id": replay_run_id,
            "store_id": store_id,
            "object_key": object_key,
        }
        return cls(
            artifact_id,
            artifact_sha256,
            source_run_id,
            replay_run_id,
            store_id,
            object_key,
            _freeze(normalized),
        )

    def as_dict(self) -> dict[str, object]:
        return _plain(self.record)


@dataclass(frozen=True, slots=True)
class SimulationRunResultV1:
    result_id: str
    source_run_id: str
    replay_run_id: str
    terminal_status: str
    final_frame_id: str
    event_tape_sha256: str
    replay_artifact: ReplayArtifactRefV1
    record: Mapping[str, object]

    @classmethod
    def from_dict(
        cls,
        payload: Mapping[str, object],
        *,
        final_frame: SimulationFrameV1 | None = None,
    ) -> SimulationRunResultV1:
        root = _object(_snapshot(payload), "simulation run result")
        _exact(root, _RUN_RESULT_FIELDS, "simulation run result")
        _schema(root, RUN_RESULT_SCHEMA_ID, "simulation run result")
        source_run_id = _prefixed_id(
            root["source_run_id"], _RUN_ID, "simulation run result.source_run_id"
        )
        replay_run_id = _prefixed_id(
            root["replay_run_id"], _REPLAY_RUN_ID, "simulation run result.replay_run_id"
        )
        profile_ref = SimulationProfileRefV1.from_dict(
            _object(root["profile_ref"], "simulation run result.profile_ref")
        )
        selection_sha256 = _digest(
            root["selection_sha256"], "simulation run result.selection_sha256"
        )
        configuration_sha256 = _digest(
            root["resolved_configuration_sha256"],
            "simulation run result.resolved_configuration_sha256",
        )
        run_request_sha256 = _digest(
            root["run_request_sha256"], "simulation run result.run_request_sha256"
        )
        terminal_status = _enum(
            root["terminal_status"],
            frozenset({"COMPLETE", "SAVED_PARTIAL"}),
            "simulation run result.terminal_status",
        )
        final_frame_id = _prefixed_id(
            root["final_frame_id"], _FRAME_ID, "simulation run result.final_frame_id"
        )
        cursor = _validate_cursor(root["final_cursor"], source_run_id)
        book_sha256 = _digest(
            root["final_book_state_sha256"],
            "simulation run result.final_book_state_sha256",
        )
        event_tape_sha256 = _digest(
            root["event_tape_sha256"], "simulation run result.event_tape_sha256"
        )
        reference = ReplayArtifactRefV1.from_dict(
            _object(root["replay_artifact"], "simulation run result.replay_artifact")
        )
        exchange_sequence = int(cursor["exchange_event_sequence"])
        metrics = [
            _validate_metric(item, index, exchange_sequence)
            for index, item in enumerate(_array(root["metrics"], "simulation run result.metrics"))
        ]
        diagnostics = [
            _validate_diagnostic(item, index, exchange_sequence)
            for index, item in enumerate(
                _array(root["diagnostics"], "simulation run result.diagnostics")
            )
        ]
        provenance = _validate_provenance(root["provenance"])
        if (
            replay_run_id != replay_run_id_for_source(source_run_id)
            or reference.source_run_id != source_run_id
            or reference.replay_run_id != replay_run_id
            or (terminal_status == "COMPLETE") != (cursor["run_state"] == "COMPLETE")
        ):
            raise SimulationContractIntegrityError(
                "simulation run result root identities disagree"
            )
        if final_frame is not None:
            frame = final_frame.as_dict()
            market = frame["market_state"]
            if (
                final_frame.frame_id != final_frame_id
                or final_frame.source_run_id != source_run_id
                or final_frame.profile_ref != profile_ref
                or final_frame.resolved_configuration_sha256 != configuration_sha256
                or final_frame.run_request_sha256 != run_request_sha256
                or frame["cursor"] != cursor
                or market["book_state_sha256"] != book_sha256
                or frame["metrics"] != metrics
                or frame["diagnostics"] != diagnostics
                or frame["provenance"] != provenance
            ):
                raise SimulationContractIntegrityError(
                    "simulation run result differs from its final frame"
                )
        normalized = {
            "schema_id": RUN_RESULT_SCHEMA_ID,
            "schema_version": SCHEMA_VERSION,
            "source_run_id": source_run_id,
            "replay_run_id": replay_run_id,
            "profile_ref": profile_ref.as_dict(),
            "selection_sha256": selection_sha256,
            "resolved_configuration_sha256": configuration_sha256,
            "run_request_sha256": run_request_sha256,
            "terminal_status": terminal_status,
            "final_frame_id": final_frame_id,
            "final_cursor": cursor,
            "final_book_state_sha256": book_sha256,
            "event_tape_sha256": event_tape_sha256,
            "replay_artifact": reference.as_dict(),
            "metrics": metrics,
            "diagnostics": diagnostics,
            "provenance": provenance,
        }
        result_id = _prefixed_id(
            root["result_id"], _RUN_RESULT_ID, "simulation run result.result_id"
        )
        if result_id != f"simulation-run-result-{canonical_digest(normalized)[:24]}":
            raise SimulationContractIntegrityError(
                "simulation run result ID does not match its content"
            )
        record = {**normalized, "result_id": result_id}
        return cls(
            result_id,
            source_run_id,
            replay_run_id,
            terminal_status,
            final_frame_id,
            event_tape_sha256,
            reference,
            _freeze(record),
        )

    def as_dict(self) -> dict[str, object]:
        return _plain(self.record)


@dataclass(frozen=True, slots=True)
class SimulationFinalizeResultV1:
    result_id: str
    status: str
    mode: str
    source_run_id: str
    origin_frame_id: str
    origin_cursor_id: str
    run_result: SimulationRunResultV1 | None
    unavailable_reason: str | None
    record: Mapping[str, object]

    @classmethod
    def from_dict(
        cls,
        payload: Mapping[str, object],
        *,
        origin_frame: SimulationFrameV1 | None = None,
        final_frame: SimulationFrameV1 | None = None,
    ) -> SimulationFinalizeResultV1:
        root = _object(_snapshot(payload), "simulation finalize result")
        _exact(root, _FINALIZE_RESULT_FIELDS, "simulation finalize result")
        _schema(root, FINALIZE_RESULT_SCHEMA_ID, "simulation finalize result")
        status = _enum(
            root["status"],
            frozenset({"AVAILABLE", "UNAVAILABLE"}),
            "simulation finalize result.status",
        )
        mode = _enum(root["mode"], FINALIZE_MODES, "simulation finalize result.mode")
        source_run_id = _prefixed_id(
            root["source_run_id"], _RUN_ID, "simulation finalize result.source_run_id"
        )
        origin_frame_id = _prefixed_id(
            root["origin_frame_id"], _FRAME_ID, "simulation finalize result.origin_frame_id"
        )
        origin_cursor_id = _prefixed_id(
            root["origin_cursor_id"],
            _CURSOR_ID,
            "simulation finalize result.origin_cursor_id",
        )
        if origin_frame is not None:
            origin_cursor = _cursor_from_frame(origin_frame)
            if (
                origin_frame.source_run_id != source_run_id
                or origin_frame.frame_id != origin_frame_id
                or origin_cursor["cursor_id"] != origin_cursor_id
            ):
                raise SimulationContractIntegrityError(
                    "simulation finalize result does not echo its origin"
                )
        if status == "AVAILABLE":
            if root["run_result"] is None or root["unavailable_reason"] is not None:
                raise ValueError("available simulation finalize result nullability is invalid")
            run_result = SimulationRunResultV1.from_dict(
                _object(root["run_result"], "simulation finalize result.run_result"),
                final_frame=final_frame,
            )
            if run_result.source_run_id != source_run_id:
                raise SimulationContractIntegrityError(
                    "simulation finalize result changed the source run"
                )
            if mode == "COMPLETE_ONLY" and run_result.terminal_status != "COMPLETE":
                raise SimulationContractIntegrityError(
                    "complete-only finalization produced a partial result"
                )
            unavailable_reason = None
        else:
            if root["run_result"] is not None:
                raise ValueError("unavailable simulation finalize result carries a run result")
            run_result = None
            unavailable_reason = _enum(
                root["unavailable_reason"],
                FINALIZE_UNAVAILABLE_REASONS,
                "simulation finalize result.unavailable_reason",
            )
        normalized = {
            "schema_id": FINALIZE_RESULT_SCHEMA_ID,
            "schema_version": SCHEMA_VERSION,
            "status": status,
            "mode": mode,
            "source_run_id": source_run_id,
            "origin_frame_id": origin_frame_id,
            "origin_cursor_id": origin_cursor_id,
            "run_result": None if run_result is None else run_result.as_dict(),
            "unavailable_reason": unavailable_reason,
        }
        result_id = _prefixed_id(
            root["result_id"], _FINALIZE_RESULT_ID, "simulation finalize result.result_id"
        )
        if result_id != f"simulation-finalize-result-{canonical_digest(normalized)[:24]}":
            raise SimulationContractIntegrityError(
                "simulation finalize result ID does not match its content"
            )
        record = {**normalized, "result_id": result_id}
        return cls(
            result_id,
            status,
            mode,
            source_run_id,
            origin_frame_id,
            origin_cursor_id,
            run_result,
            unavailable_reason,
            _freeze(record),
        )

    def as_dict(self) -> dict[str, object]:
        return _plain(self.record)


__all__ = [
    "ARTIFACT_KIND",
    "ARTIFACT_REFERENCE_SCHEMA_ID",
    "ARTIFACT_SCHEMA_ID",
    "FINALIZE_MODES",
    "FINALIZE_RESULT_SCHEMA_ID",
    "RUN_RESULT_SCHEMA_ID",
    "EmbeddedComponentV1",
    "EmbeddedSessionRecordingV1",
    "ReplayArtifactRefV1",
    "SimulationFinalizeResultV1",
    "SimulationReplayArtifactV1",
    "SimulationRunResultV1",
    "SimulationTimelineEventV1",
    "replay_run_id_for_source",
]
