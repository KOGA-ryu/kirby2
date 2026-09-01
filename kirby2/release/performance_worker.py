"""Closed production execution for one preregistered release-performance row.

This module deliberately stops at an immutable, canonical row-attempt payload.  A
coordinator owns process supervision, resource-limit enforcement, filesystem staging,
and CAS publication; none of those operational concerns enter the six semantic
members assembled here.
"""

from __future__ import annotations

import hashlib
import resource
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass, replace

from kirby2.auditlab.kernel import run_generated_case
from kirby2.auditlab.models import CaseRecording, GeneratedCaseResult, GeneratedConfiguration
from kirby2.packs.formats import (
    canonical_json_bytes,
    load_canonical_json_bytes,
    require_sha256,
)

from .performance import (
    RELEASE_FLOAT_FREE_SEMANTIC_POLICY_ID_V1,
    RELEASE_LEGACY_DIGEST_EXTRACTION_POLICY_ID_V1,
    RELEASE_PER_ATTEMPT_RSS_LIMIT_BYTES_V1,
    RELEASE_PER_ATTEMPT_TEMP_LIMIT_BYTES_V1,
    RELEASE_PER_ATTEMPT_WALL_LIMIT_NS_V1,
    RELEASE_PERFORMANCE_RESULT_SCHEMA_ID_V1,
    ReleasePerformanceCapabilityRecordV1,
    ReleasePerformanceCellResultV1,
    ReleasePerformanceCheckRecordV1,
    ReleasePerformanceOperationalV1,
    ReleasePerformanceRowTemplateV1,
    RunnerSourceTreeV1,
    bind_performance_row_template,
    build_performance_row_template,
    extract_legacy_digest_bindings,
    performance_semantic_artifact_set,
    release_float_free_semantic,
    validate_performance_attempt_sequence,
    verify_performance_cell_artifacts,
)


_SEMANTIC_MEMBER_ORDER_V1 = (
    "run_manifest.json",
    "native_recording.json",
    "semantic_result.json",
    "capabilities.json",
    "checks.json",
    "audit_result.json",
)
_STANDARD_RUNNER_LANES_V1 = {
    "CORE_FLOW": "CORE_FLOW",
    "MECHANICS": "MECHANICS",
    "LATENCY": "LATENCY",
    "FRAGMENTED": "FRAGMENTED",
    "ECOLOGY": "ECOLOGY",
    "ALGORITHM": "ALGORITHM",
    "FAULT": "FAULT",
}
_BOUND_ROW_FIELDS_V1 = {
    "artifact_form",
    "audit_argv",
    "cell",
    "expected_capabilities",
    "generated_configuration",
    "generated_configuration_sha256",
    "initial_attempt",
    "native_fixture",
    "native_fixture_sha256",
    "required_checks",
    "result_schema_id",
    "root_seed",
    "runner_id",
    "runner_source_sha256",
    "work_unit_id",
}
_TEMPLATE_BOUND_IDENTITY_FIELDS_V1 = (
    "artifact_form",
    "audit_argv",
    "cell",
    "expected_capabilities",
    "generated_configuration",
    "generated_configuration_sha256",
    "initial_attempt",
    "required_checks",
    "result_schema_id",
    "root_seed",
    "runner_id",
    "work_unit_id",
)
_OPERATIONAL_RESULT_KEYS_V1 = frozenset(
    {
        "operational",
        "start_monotonic_ns",
        "end_monotonic_ns",
        "peak_rss_bytes",
        "max_temporary_bytes",
        "retry_reason",
    }
)
_WORKER_FAILURE_CODES_V1 = frozenset(
    {
        "PROCESS_FAILURE",
        "RESOURCE_LIMIT",
        "SEMANTIC_FAILURE",
        "INVARIANT_FAILURE",
        "REPLAY_FAILURE",
        "SCHEMA_FAILURE",
        "DIGEST_FAILURE",
    }
)


class ReleasePerformanceRowExecutionError(RuntimeError):
    """A stable row-execution refusal for the supervising coordinator."""

    def __init__(self, failure_code: str, message: str) -> None:
        if failure_code not in _WORKER_FAILURE_CODES_V1:
            raise ValueError("performance worker failure code is invalid")
        super().__init__(message)
        self.failure_code = failure_code


@dataclass(frozen=True, slots=True)
class ReleasePerformanceRowAttemptV1:
    """Immutable canonical bytes returned to the coordinator for one attempt."""

    result: ReleasePerformanceCellResultV1
    semantic_members: tuple[tuple[str, bytes], ...]
    compatibility_sidecars: tuple[tuple[str, bytes], ...]
    operational_sidecars: tuple[tuple[str, bytes], ...]
    result_bytes: bytes

    def __post_init__(self) -> None:
        if type(self.result) is not ReleasePerformanceCellResultV1:
            raise TypeError("performance row attempt requires a typed result")
        _require_file_tuple(self.semantic_members, "semantic members")
        _require_file_tuple(self.compatibility_sidecars, "compatibility sidecars")
        _require_file_tuple(self.operational_sidecars, "operational sidecars")
        if tuple(name for name, _raw in self.semantic_members) != _SEMANTIC_MEMBER_ORDER_V1:
            raise ValueError("performance row semantic-member order differs")
        if tuple(name for name, _raw in self.compatibility_sidecars) != (
            "legacy_digest_bindings.json",
        ):
            raise ValueError("performance row compatibility sidecars differ")
        if tuple(name for name, _raw in self.operational_sidecars) != (
            f"operational_attempt_{self.result.attempt}.json",
        ):
            raise ValueError("performance row operational sidecars differ")
        if (
            type(self.result_bytes) is not bytes
            or self.result_bytes != self.result.canonical_bytes()
        ):
            raise ValueError("performance row result bytes differ from the typed result")
        members = self.semantic_member_map()
        verify_performance_cell_artifacts(self.result, members)
        operational = load_canonical_json_bytes(
            self.operational_sidecars[0][1],
            "performance operational sidecar",
        )
        if operational != self.result.operational.as_dict():
            raise ValueError("performance operational sidecar differs from the result")
        _validate_legacy_binding_sidecar(
            self.compatibility_sidecars[0][1],
            work_unit_id=self.result.work_unit_id,
        )
        loaded_result = ReleasePerformanceCellResultV1.from_bytes(self.result_bytes)
        if loaded_result != self.result:
            raise ValueError("performance row result read-back differs")

    def semantic_member_map(self) -> dict[str, bytes]:
        """Return the exact dict shape consumed by the frozen artifact verifier."""

        return {name: raw for name, raw in self.semantic_members}

    def files(self) -> tuple[tuple[str, bytes], ...]:
        """Return every canonical work-unit file in deterministic transport order."""

        return (
            *self.semantic_members,
            *self.compatibility_sidecars,
            *self.operational_sidecars,
            ("cell-result.json", self.result_bytes),
        )


@dataclass(frozen=True, slots=True)
class _NativeRowOutputV1:
    raw_recording: dict[str, object]
    raw_result: dict[str, object]
    recording_schema_id: str
    payload_kind: str
    replay_raw_recording: dict[str, object] | None
    replay_raw_result: dict[str, object] | None
    replay_failed: bool
    queue_capability_records: tuple[dict[str, object], ...] = ()
    queue_check_records: tuple[dict[str, object], ...] = ()


def execute_performance_row(
    template: ReleasePerformanceRowTemplateV1,
    source_tree: RunnerSourceTreeV1,
    *,
    attempt: int,
    prior_attempt: ReleasePerformanceCellResultV1 | None = None,
    retry_reason: str | None = None,
) -> ReleasePerformanceRowAttemptV1:
    """Bind and execute one typed preregistered row against one source tree."""

    if type(template) is not ReleasePerformanceRowTemplateV1:
        raise TypeError("performance execution requires an exact row template")
    if type(source_tree) is not RunnerSourceTreeV1:
        raise TypeError("performance execution requires an exact runner source tree")
    bound_row = bind_performance_row_template(template, source_tree)
    return execute_bound_performance_row(
        bound_row,
        attempt=attempt,
        prior_attempt=prior_attempt,
        retry_reason=retry_reason,
    )


def execute_bound_performance_row(
    bound_row: dict[str, object],
    *,
    attempt: int,
    prior_attempt: ReleasePerformanceCellResultV1 | None = None,
    retry_reason: str | None = None,
) -> ReleasePerformanceRowAttemptV1:
    """Execute one exact bound row and return immutable in-memory publication bytes.

    Attempt two is accepted only with its retry-authorizing attempt-one result.  The
    coordinator remains responsible for running this call in a supervised process and
    for publishing :meth:`ReleasePerformanceRowAttemptV1.files` to owned storage/CAS.
    """

    configuration = _validate_bound_row(bound_row)
    retry_reason = _validate_attempt_inputs(
        bound_row,
        attempt=attempt,
        prior_attempt=prior_attempt,
        retry_reason=retry_reason,
    )

    start_monotonic_ns = time.monotonic_ns()
    try:
        native = _dispatch_native_runner(bound_row, configuration)
    except ReleasePerformanceRowExecutionError:
        raise
    except Exception as error:
        raise ReleasePerformanceRowExecutionError(
            "PROCESS_FAILURE",
            "production performance runner raised before returning native output",
        ) from error

    try:
        (
            projected_recording,
            projected_result,
            native_bindings,
            result_bindings,
            replay_failed,
        ) = _project_native_output(native)
        capability_records, check_records = _build_projection_records(
            bound_row,
            native,
            projected_result,
        )
        semantic_members, semantic_failures = _build_semantic_members(
            bound_row,
            native,
            projected_recording,
            projected_result,
            capability_records,
            check_records,
            replay_failed=replay_failed,
        )
    except ReleasePerformanceRowExecutionError:
        raise
    except Exception as error:
        raise ReleasePerformanceRowExecutionError(
            "SEMANTIC_FAILURE",
            "native output could not be projected into the frozen row contract",
        ) from error

    _projection, artifact_set_sha256 = performance_semantic_artifact_set(
        semantic_members
    )
    member_digests = {
        name: hashlib.sha256(raw).hexdigest()
        for name, raw in semantic_members.items()
    }
    end_monotonic_ns = time.monotonic_ns()
    operational = ReleasePerformanceOperationalV1(
        start_monotonic_ns=start_monotonic_ns,
        end_monotonic_ns=end_monotonic_ns,
        peak_rss_bytes=_peak_rss_bytes(),
        max_temporary_bytes=0,
        retry_reason=retry_reason,
    )
    _enforce_operational_limits(operational)
    if semantic_failures:
        raise ReleasePerformanceRowExecutionError(
            _result_failure_code(semantic_failures),
            "production row did not satisfy its semantic, invariant, and replay gates",
        )
    result = ReleasePerformanceCellResultV1(
        work_unit_id=_bound_text(bound_row, "work_unit_id"),
        attempt=attempt,
        status="COMPLETE",
        generated_configuration_sha256=_bound_text(
            bound_row, "generated_configuration_sha256"
        ),
        native_fixture_sha256=_bound_text(bound_row, "native_fixture_sha256"),
        runner_source_sha256=_bound_text(bound_row, "runner_source_sha256"),
        capability_records=capability_records,
        check_records=check_records,
        run_manifest_sha256=member_digests["run_manifest.json"],
        native_recording_sha256=member_digests["native_recording.json"],
        semantic_result_sha256=member_digests["semantic_result.json"],
        artifact_set_sha256=artifact_set_sha256,
        audit_result_sha256=member_digests["audit_result.json"],
        operational=operational,
        failure_code=None,
    )
    result.validate_bound_row(bound_row)
    verify_performance_cell_artifacts(result, semantic_members)
    if prior_attempt is None:
        validate_performance_attempt_sequence((result,))
    else:
        validate_performance_attempt_sequence((prior_attempt, result))

    legacy_sidecar = canonical_json_bytes(
        {
            "native_recording_bindings": list(native_bindings),
            "schema_version": 1,
            "semantic_result_bindings": list(result_bindings),
            "work_unit_id": result.work_unit_id,
        }
    )
    output = ReleasePerformanceRowAttemptV1(
        result=result,
        semantic_members=tuple(semantic_members.items()),
        compatibility_sidecars=(("legacy_digest_bindings.json", legacy_sidecar),),
        operational_sidecars=(
            (
                f"operational_attempt_{attempt}.json",
                canonical_json_bytes(operational.as_dict()),
            ),
        ),
        result_bytes=result.canonical_bytes(),
    )
    verify_performance_row_attempt(output, bound_row)
    return output


def verify_performance_row_attempt(
    output: ReleasePerformanceRowAttemptV1,
    bound_row: Mapping[str, object],
) -> tuple[dict[str, object], ...]:
    """Read back an immutable worker result against its exact bound-row identity."""

    if type(output) is not ReleasePerformanceRowAttemptV1:
        raise TypeError("performance row verification requires exact worker output")
    if type(bound_row) is not dict:
        raise TypeError("performance row verification requires an exact bound row")
    _validate_bound_row(bound_row)
    output.result.validate_bound_row(bound_row)
    retry_reason = output.result.operational.retry_reason
    if output.result.attempt == 1:
        if retry_reason is not None:
            raise ValueError("performance attempt one carries retry state")
    elif retry_reason not in {"PROCESS_FAILURE", "RESOURCE_LIMIT"}:
        raise ValueError("performance attempt two lacks a retryable reason")
    _enforce_operational_limits(output.result.operational)
    projection = verify_performance_cell_artifacts(
        output.result,
        output.semantic_member_map(),
    )
    # Row admission and final publication verification must apply one exact,
    # provider-free semantic grammar.  Import lazily so the execution producer
    # remains independent of publication records until verification is requested.
    from .performance_records import _validate_semantic_members

    _validate_semantic_members(
        output.result,
        bound_row,
        output.semantic_member_map(),
        output.compatibility_sidecars[0][1],
    )
    if ReleasePerformanceCellResultV1.from_bytes(output.result_bytes) != output.result:
        raise ValueError("performance row result verification differs")
    return projection


def verify_performance_row_execution(
    output: ReleasePerformanceRowAttemptV1,
    bound_row: Mapping[str, object],
) -> tuple[dict[str, object], ...]:
    """Public DEV-0016 name for provider-free verification of one row attempt."""

    return verify_performance_row_attempt(output, bound_row)


def replace_performance_row_operational(
    output: ReleasePerformanceRowAttemptV1,
    operational: ReleasePerformanceOperationalV1,
) -> ReleasePerformanceRowAttemptV1:
    """Replace only coordinator-owned operational bytes after supervision/publish.

    The six semantic members and every semantic digest remain untouched.  This is the
    intended seam for the coordinator's authoritative wall/RSS/temporary accounting.
    """

    if type(output) is not ReleasePerformanceRowAttemptV1:
        raise TypeError("operational replacement requires exact worker output")
    if type(operational) is not ReleasePerformanceOperationalV1:
        raise TypeError("operational replacement requires a typed record")
    _enforce_operational_limits(operational)
    result = replace(output.result, operational=operational)
    return ReleasePerformanceRowAttemptV1(
        result=result,
        semantic_members=output.semantic_members,
        compatibility_sidecars=output.compatibility_sidecars,
        operational_sidecars=(
            (
                f"operational_attempt_{result.attempt}.json",
                canonical_json_bytes(operational.as_dict()),
            ),
        ),
        result_bytes=result.canonical_bytes(),
    )


def _require_file_tuple(value: object, label: str) -> None:
    if type(value) is not tuple or not value:
        raise TypeError(f"performance row {label} must be a nonempty tuple")
    for item in value:
        if (
            type(item) is not tuple
            or len(item) != 2
            or type(item[0]) is not str
            or type(item[1]) is not bytes
            or not item[1]
        ):
            raise TypeError(f"performance row {label} entries are invalid")
        load_canonical_json_bytes(item[1], f"performance row file {item[0]}")


def _validate_legacy_binding_sidecar(raw: bytes, *, work_unit_id: str) -> None:
    value = load_canonical_json_bytes(raw, "performance legacy-binding sidecar")
    if type(value) is not dict or set(value) != {
        "schema_version",
        "work_unit_id",
        "native_recording_bindings",
        "semantic_result_bindings",
    }:
        raise ValueError("performance legacy-binding sidecar fields differ")
    if value["schema_version"] != 1 or value["work_unit_id"] != work_unit_id:
        raise ValueError("performance legacy-binding sidecar identity differs")
    for key in ("native_recording_bindings", "semantic_result_bindings"):
        bindings = value[key]
        if type(bindings) is not list:
            raise TypeError("performance legacy bindings must be arrays")
        pointers: list[str] = []
        for binding in bindings:
            if type(binding) is not dict or set(binding) != {
                "json_pointer",
                "legacy_sha256",
            }:
                raise ValueError("performance legacy-binding row fields differ")
            pointer = binding["json_pointer"]
            if type(pointer) is not str:
                raise TypeError("performance legacy-binding pointer must be text")
            require_sha256(
                binding["legacy_sha256"],
                "performance legacy-binding digest",
            )
            pointers.append(pointer)
        if pointers != sorted(pointers, key=lambda item: item.encode("utf-8")):
            raise ValueError("performance legacy-binding pointer order differs")
        if len(pointers) != len(set(pointers)):
            raise ValueError("performance legacy-binding pointers are duplicated")


def _validate_bound_row(bound_row: object) -> GeneratedConfiguration:
    if type(bound_row) is not dict or set(bound_row) != _BOUND_ROW_FIELDS_V1:
        raise ValueError("bound performance row fields differ from the worker schema")
    work_unit_id = _bound_text(bound_row, "work_unit_id")
    parts = work_unit_id.split("/")
    if len(parts) != 3 or parts[0] != "release-perf":
        raise ValueError("bound performance work-unit ID is invalid")
    try:
        root_seed = int(parts[2])
        template = build_performance_row_template(parts[1], root_seed)
    except (TypeError, ValueError) as error:
        raise ValueError("bound performance work-unit ID is invalid") from error
    expected = template.as_dict()
    for field in _TEMPLATE_BOUND_IDENTITY_FIELDS_V1:
        if bound_row[field] != expected[field]:
            raise ValueError(f"bound performance row {field} differs")

    configuration_payload = bound_row["generated_configuration"]
    if type(configuration_payload) is not dict:
        raise TypeError("bound generated configuration must be an exact object")
    configuration = GeneratedConfiguration.from_dict(configuration_payload)
    if configuration.as_dict() != configuration_payload:
        raise ValueError("generated configuration round-trip differs")
    configuration_sha256 = hashlib.sha256(
        canonical_json_bytes(configuration_payload)
    ).hexdigest()
    if (
        configuration.sha256 != configuration_sha256
        or configuration_sha256 != bound_row["generated_configuration_sha256"]
    ):
        raise ValueError("bound generated-configuration digest differs")

    fixture = bound_row["native_fixture"]
    if type(fixture) is not dict or set(fixture) != {
        "constructor_id",
        "fixture_id",
        "generated_configuration_sha256",
        "parameters",
        "schema_version",
    }:
        raise ValueError("bound native-fixture fields differ")
    expected_fixture = template.native_fixture_template
    for field in (
        "constructor_id",
        "fixture_id",
        "generated_configuration_sha256",
        "schema_version",
    ):
        if fixture[field] != expected_fixture[field]:
            raise ValueError(f"bound native-fixture {field} differs")
    fixture_sha256 = hashlib.sha256(canonical_json_bytes(fixture)).hexdigest()
    if fixture_sha256 != bound_row["native_fixture_sha256"]:
        raise ValueError("bound native-fixture digest differs")
    require_sha256(bound_row["runner_source_sha256"], "runner source digest")
    if bound_row["result_schema_id"] != RELEASE_PERFORMANCE_RESULT_SCHEMA_ID_V1:
        raise ValueError("bound performance result schema differs")

    runner_id = _bound_text(bound_row, "runner_id")
    expected_lane = _STANDARD_RUNNER_LANES_V1.get(runner_id)
    if runner_id == "RELEASE_QUEUE_REACTIVE_V1":
        expected_lane = "CORE_FLOW"
    if expected_lane is None or configuration.lane.value != expected_lane:
        raise ValueError("bound performance runner/lane identity differs")
    return configuration


def _validate_attempt_inputs(
    bound_row: dict[str, object],
    *,
    attempt: int,
    prior_attempt: ReleasePerformanceCellResultV1 | None,
    retry_reason: str | None,
) -> str | None:
    if type(attempt) is not int or attempt not in {1, 2}:
        raise ValueError("performance worker attempt must be one or two")
    if retry_reason is not None and (type(retry_reason) is not str or not retry_reason):
        raise TypeError("performance retry reason must be nonempty text or null")
    if attempt == 1:
        if prior_attempt is not None or retry_reason is not None:
            raise ValueError("performance attempt one cannot carry retry state")
        return None
    if type(prior_attempt) is not ReleasePerformanceCellResultV1:
        raise ValueError("performance attempt two requires its attempt-one result")
    prior_attempt.validate_bound_row(bound_row)
    if (
        prior_attempt.attempt != 1
        or prior_attempt.status != "FAILED"
        or prior_attempt.failure_code not in {"PROCESS_FAILURE", "RESOURCE_LIMIT"}
    ):
        raise ValueError("performance attempt one does not authorize a retry")
    if retry_reason is not None and retry_reason != prior_attempt.failure_code:
        raise ValueError("performance retry reason differs from attempt-one failure")
    return prior_attempt.failure_code


def _dispatch_native_runner(
    bound_row: dict[str, object],
    configuration: GeneratedConfiguration,
) -> _NativeRowOutputV1:
    runner_id = _bound_text(bound_row, "runner_id")
    artifact_form = _bound_text(bound_row, "artifact_form")
    if runner_id == "RELEASE_QUEUE_REACTIVE_V1":
        from .probes import ReleaseQueueReactiveResultV1, run_release_queue_reactive_probe

        result = run_release_queue_reactive_probe(configuration)
        if type(result) is not ReleaseQueueReactiveResultV1:
            raise ReleasePerformanceRowExecutionError(
                "SCHEMA_FAILURE",
                "queue-reactive runner returned a different result schema",
            )
        if result.recording.schema_id != artifact_form:
            raise ReleasePerformanceRowExecutionError(
                "SCHEMA_FAILURE",
                "queue-reactive recording schema differs from the bound row",
            )
        return _NativeRowOutputV1(
            raw_recording=result.recording.as_dict(),
            raw_result=result.as_dict(),
            recording_schema_id=result.recording.schema_id,
            payload_kind="QUEUE_REACTIVE",
            replay_raw_recording=None,
            replay_raw_result=None,
            replay_failed=False,
            queue_capability_records=result.capability_records,
            queue_check_records=result.check_records,
        )

    if runner_id not in _STANDARD_RUNNER_LANES_V1:
        raise ReleasePerformanceRowExecutionError(
            "SCHEMA_FAILURE",
            "bound performance runner ID is not registered",
        )
    result = run_generated_case(configuration)
    if type(result) is not GeneratedCaseResult:
        raise ReleasePerformanceRowExecutionError(
            "SCHEMA_FAILURE",
            "registered runner returned a different result schema",
        )
    if result.recording.recording_type != artifact_form:
        raise ReleasePerformanceRowExecutionError(
            "SCHEMA_FAILURE",
            "native recording schema differs from the bound row",
        )

    replay_raw_recording: dict[str, object] | None = None
    replay_raw_result: dict[str, object] | None = None
    replay_failed = False
    try:
        loaded_recording = CaseRecording.from_dict(result.recording.as_dict())
        from kirby2.auditlab.executors import EXECUTOR_REGISTRY

        replay = EXECUTOR_REGISTRY.replay(loaded_recording)
        replay_raw_recording = replay.recording.as_dict()
        replay_raw_result = replay.as_dict()
    except Exception:
        replay_failed = True
    return _NativeRowOutputV1(
        raw_recording=result.recording.as_dict(),
        raw_result=result.as_dict(),
        recording_schema_id=result.recording.recording_type,
        payload_kind="GENERATED_CASE",
        replay_raw_recording=replay_raw_recording,
        replay_raw_result=replay_raw_result,
        replay_failed=replay_failed,
    )


def _project_native_output(
    native: _NativeRowOutputV1,
) -> tuple[
    dict[str, object],
    dict[str, object],
    tuple[dict[str, str], ...],
    tuple[dict[str, str], ...],
    bool,
]:
    raw_result = {
        key: value
        for key, value in native.raw_result.items()
        if key not in _OPERATIONAL_RESULT_KEYS_V1
    }
    projected_recording, native_bindings = _project_one_native(
        native.raw_recording,
        "native recording",
    )
    projected_result, result_bindings = _project_one_native(
        raw_result,
        "native semantic result",
    )
    replay_failed = native.replay_failed
    if native.payload_kind == "GENERATED_CASE" and not replay_failed:
        if native.replay_raw_recording is None or native.replay_raw_result is None:
            replay_failed = True
        else:
            replay_result = {
                key: value
                for key, value in native.replay_raw_result.items()
                if key not in _OPERATIONAL_RESULT_KEYS_V1
            }
            replay_recording, replay_native_bindings = _project_one_native(
                native.replay_raw_recording,
                "replayed native recording",
            )
            replay_semantic, replay_result_bindings = _project_one_native(
                replay_result,
                "replayed native semantic result",
            )
            if (
                replay_recording != projected_recording
                or replay_semantic != projected_result
                or replay_native_bindings != native_bindings
                or replay_result_bindings != result_bindings
            ):
                replay_failed = True
    return (
        projected_recording,
        projected_result,
        native_bindings,
        result_bindings,
        replay_failed,
    )


def _project_one_native(
    raw: dict[str, object],
    label: str,
) -> tuple[dict[str, object], tuple[dict[str, str], ...]]:
    extracted, bindings = extract_legacy_digest_bindings(raw)
    projected = release_float_free_semantic(extracted)
    if type(projected) is not dict:
        raise TypeError(f"projected {label} must be an exact object")
    canonical_json_bytes(projected)
    return projected, bindings


def _build_projection_records(
    bound_row: dict[str, object],
    native: _NativeRowOutputV1,
    projected_result: dict[str, object],
) -> tuple[
    tuple[ReleasePerformanceCapabilityRecordV1, ...],
    tuple[ReleasePerformanceCheckRecordV1, ...],
]:
    expected_capabilities = bound_row["expected_capabilities"]
    expected_checks = bound_row["required_checks"]
    if type(expected_capabilities) is not list or any(
        type(item) is not str for item in expected_capabilities
    ):
        raise TypeError("bound expected capabilities must be a string array")
    if type(expected_checks) is not list or any(type(item) is not str for item in expected_checks):
        raise TypeError("bound required checks must be a string array")
    if native.payload_kind == "QUEUE_REACTIVE":
        return _build_queue_projection_records(
            expected_capabilities,
            expected_checks,
            native,
            projected_result,
        )

    declared = projected_result.get("declared_outputs")
    if type(declared) is not dict:
        raise TypeError("projected generated result lacks declared outputs")
    exercises = declared.get("exercises")
    checks = declared.get("check_results")
    if type(exercises) is not list or any(type(item) is not dict for item in exercises):
        raise TypeError("projected generated exercises must be object rows")
    if type(checks) is not list or any(type(item) is not dict for item in checks):
        raise TypeError("projected generated checks must be object rows")
    exercise_by_name = _unique_rows(exercises, "capability", "exercise")
    check_by_name = _unique_rows(checks, "name", "check")

    capability_records = tuple(
        _capability_from_projected_exercise(name, exercise_by_name[name])
        for name in expected_capabilities
        if name in exercise_by_name
    )
    check_records = tuple(
        _check_from_projected_check(name, check_by_name[name])
        for name in expected_checks
        if name in check_by_name
    )
    if len(capability_records) != len(expected_capabilities):
        raise ValueError("projected generated result lacks an expected capability")
    if len(check_records) != len(expected_checks):
        raise ValueError("projected generated result lacks a required check")
    return capability_records, check_records


def _build_queue_projection_records(
    expected_capabilities: list[object],
    expected_checks: list[object],
    native: _NativeRowOutputV1,
    projected_result: dict[str, object],
) -> tuple[
    tuple[ReleasePerformanceCapabilityRecordV1, ...],
    tuple[ReleasePerformanceCheckRecordV1, ...],
]:
    projected_capabilities = projected_result.get("capability_records")
    projected_checks = projected_result.get("check_records")
    if type(projected_capabilities) is not list or any(
        type(item) is not dict for item in projected_capabilities
    ):
        raise TypeError("projected queue capability records must be object rows")
    if type(projected_checks) is not list or any(
        type(item) is not dict for item in projected_checks
    ):
        raise TypeError("projected queue check records must be object rows")
    raw_capabilities = list(native.queue_capability_records)
    raw_checks = list(native.queue_check_records)
    projected_capability_by_name = _unique_rows(
        projected_capabilities,
        "capability",
        "queue capability",
    )
    raw_capability_by_name = _unique_rows(
        raw_capabilities,
        "capability",
        "raw queue capability",
    )
    raw_check_by_name = _unique_rows(raw_checks, "check_id", "raw queue check")

    capabilities: list[ReleasePerformanceCapabilityRecordV1] = []
    for name in expected_capabilities:
        if name not in projected_capability_by_name or name not in raw_capability_by_name:
            raise ValueError("queue result lacks an expected capability")
        projected = projected_capability_by_name[name]
        raw = raw_capability_by_name[name]
        capabilities.append(
            ReleasePerformanceCapabilityRecordV1(
                capability=name,
                configured_value=projected["configured_value"],
                status=str(raw["status"]),
                evidence_sha256=str(raw["evidence_sha256"]),
            )
        )
    checks: list[ReleasePerformanceCheckRecordV1] = []
    for name in expected_checks:
        if name not in raw_check_by_name:
            raise ValueError("queue result lacks a required check")
        raw = raw_check_by_name[name]
        checks.append(
            ReleasePerformanceCheckRecordV1(
                check_id=name,
                status=str(raw["status"]),
                evidence_sha256=str(raw["evidence_sha256"]),
            )
        )
    return tuple(capabilities), tuple(checks)


def _unique_rows(
    rows: list[dict[str, object]],
    key: str,
    label: str,
) -> dict[str, dict[str, object]]:
    result: dict[str, dict[str, object]] = {}
    for row in rows:
        name = row.get(key)
        if type(name) is not str or not name:
            raise TypeError(f"projected {label} name is invalid")
        if name in result:
            raise ValueError(f"projected {label} mapping is duplicated")
        result[name] = row
    return result


def _capability_from_projected_exercise(
    name: str,
    row: dict[str, object],
) -> ReleasePerformanceCapabilityRecordV1:
    evidence = row.get("evidence")
    if type(evidence) is not dict:
        raise TypeError("projected exercise evidence must be an object")
    return ReleasePerformanceCapabilityRecordV1(
        capability=name,
        configured_value=row.get("configured_value"),
        status=str(row.get("status")),
        evidence_sha256=hashlib.sha256(canonical_json_bytes(evidence)).hexdigest(),
    )


def _check_from_projected_check(
    name: str,
    row: dict[str, object],
) -> ReleasePerformanceCheckRecordV1:
    evidence = row.get("evidence")
    if type(evidence) is not dict:
        raise TypeError("projected check evidence must be an object")
    return ReleasePerformanceCheckRecordV1(
        check_id=name,
        status=str(row.get("status")),
        evidence_sha256=hashlib.sha256(canonical_json_bytes(evidence)).hexdigest(),
    )


def _build_semantic_members(
    bound_row: dict[str, object],
    native: _NativeRowOutputV1,
    projected_recording: dict[str, object],
    projected_result: dict[str, object],
    capability_records: tuple[ReleasePerformanceCapabilityRecordV1, ...],
    check_records: tuple[ReleasePerformanceCheckRecordV1, ...],
    *,
    replay_failed: bool,
) -> tuple[dict[str, bytes], tuple[str, ...]]:
    run_manifest = {
        "schema_version": 1,
        "work_unit_id": bound_row["work_unit_id"],
        "cell": bound_row["cell"],
        "root_seed": bound_row["root_seed"],
        "generated_configuration_sha256": bound_row[
            "generated_configuration_sha256"
        ],
        "native_fixture_sha256": bound_row["native_fixture_sha256"],
        "runner_id": bound_row["runner_id"],
        "runner_source_sha256": bound_row["runner_source_sha256"],
        "artifact_form": bound_row["artifact_form"],
        "expected_capabilities": bound_row["expected_capabilities"],
        "required_checks": bound_row["required_checks"],
    }
    native_envelope = {
        "schema_version": 1,
        "projection_policy": RELEASE_FLOAT_FREE_SEMANTIC_POLICY_ID_V1,
        "legacy_digest_policy": RELEASE_LEGACY_DIGEST_EXTRACTION_POLICY_ID_V1,
        "native_schema_id": native.recording_schema_id,
        "payload": projected_recording,
    }
    semantic_envelope = {
        "schema_version": 1,
        "projection_policy": RELEASE_FLOAT_FREE_SEMANTIC_POLICY_ID_V1,
        "legacy_digest_policy": RELEASE_LEGACY_DIGEST_EXTRACTION_POLICY_ID_V1,
        "native_schema_id": "GENERATED_CASE_RESULT_AS_DICT_V2",
        "payload": projected_result,
    }
    capability_projection = [item.as_dict() for item in capability_records]
    check_projection = [item.as_dict() for item in check_records]
    prefix_members = {
        "run_manifest.json": canonical_json_bytes(
            release_float_free_semantic(run_manifest)
        ),
        # These payloads and wrapper values were already projected exactly once.
        # Re-projecting would correctly reject the reserved scalar/reference keys.
        "native_recording.json": canonical_json_bytes(native_envelope),
        "semantic_result.json": canonical_json_bytes(semantic_envelope),
        "capabilities.json": canonical_json_bytes(capability_projection),
        "checks.json": canonical_json_bytes(check_projection),
    }
    failures = _semantic_failures(
        native,
        projected_result,
        capability_records,
        check_records,
        replay_failed=replay_failed,
    )
    audit_result = {
        "schema_version": 1,
        "work_unit_id": bound_row["work_unit_id"],
        "status": "PASS" if not failures else "FAIL",
        "capability_projection_sha256": hashlib.sha256(
            prefix_members["capabilities.json"]
        ).hexdigest(),
        "check_projection_sha256": hashlib.sha256(
            prefix_members["checks.json"]
        ).hexdigest(),
        "native_recording_sha256": hashlib.sha256(
            prefix_members["native_recording.json"]
        ).hexdigest(),
        "semantic_result_sha256": hashlib.sha256(
            prefix_members["semantic_result.json"]
        ).hexdigest(),
        "failures": list(failures),
    }
    members = {
        **prefix_members,
        "audit_result.json": canonical_json_bytes(audit_result),
    }
    if tuple(members) != _SEMANTIC_MEMBER_ORDER_V1:
        raise RuntimeError("performance semantic-member assembly order differs")
    return members, failures


def _semantic_failures(
    native: _NativeRowOutputV1,
    projected_result: dict[str, object],
    capability_records: tuple[ReleasePerformanceCapabilityRecordV1, ...],
    check_records: tuple[ReleasePerformanceCheckRecordV1, ...],
    *,
    replay_failed: bool,
) -> tuple[str, ...]:
    failures: list[str] = []
    if replay_failed:
        failures.append("REPLAY_FAILURE")
    failures.extend(
        f"CAPABILITY_NOT_EXERCISED:{item.capability}"
        for item in capability_records
        if item.status != "EXERCISED"
    )
    failures.extend(
        f"CHECK_{item.status}:{item.check_id}"
        for item in check_records
        if item.status != "PASS"
    )
    if native.payload_kind == "GENERATED_CASE":
        declared = projected_result.get("declared_outputs")
        raw_failures = declared.get("failures") if type(declared) is dict else None
        if type(raw_failures) is not list:
            raise TypeError("projected generated failures must be an array")
        for failure in raw_failures:
            if type(failure) is not dict:
                raise TypeError("projected generated failure rows must be objects")
            kind = failure.get("kind")
            code = failure.get("code")
            if type(kind) is not str or type(code) is not str:
                raise TypeError("projected generated failure identity is invalid")
            failures.append(f"NATIVE_{kind}:{code}")
    return tuple(sorted(set(failures), key=lambda item: item.encode("utf-8")))


def _result_failure_code(failures: tuple[str, ...]) -> str:
    if "REPLAY_FAILURE" in failures or any(
        item.startswith("NATIVE_REPLAY_MISMATCH:")
        or item.startswith("NATIVE_DETERMINISM_MISMATCH:")
        for item in failures
    ):
        return "REPLAY_FAILURE"
    if any(item.startswith("NATIVE_SCHEMA_VIOLATION:") for item in failures):
        return "SCHEMA_FAILURE"
    if any(
        item.startswith("CAPABILITY_")
        or item.startswith("CHECK_")
        or item.startswith("NATIVE_INVARIANT_VIOLATION:")
        or item.startswith("NATIVE_OBSERVABILITY_LEAK:")
        or item.startswith("NATIVE_DATA_INTEGRITY:")
        for item in failures
    ):
        return "INVARIANT_FAILURE"
    return "SEMANTIC_FAILURE"


def _peak_rss_bytes() -> int:
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if type(value) not in {int, float} or value < 0:
        raise RuntimeError("performance worker peak RSS measurement is invalid")
    peak = int(value)
    return peak if sys.platform == "darwin" else peak * 1024


def _enforce_operational_limits(
    operational: ReleasePerformanceOperationalV1,
) -> None:
    if (
        operational.end_monotonic_ns - operational.start_monotonic_ns
        > RELEASE_PER_ATTEMPT_WALL_LIMIT_NS_V1
        or operational.peak_rss_bytes > RELEASE_PER_ATTEMPT_RSS_LIMIT_BYTES_V1
        or operational.max_temporary_bytes > RELEASE_PER_ATTEMPT_TEMP_LIMIT_BYTES_V1
    ):
        raise ReleasePerformanceRowExecutionError(
            "RESOURCE_LIMIT",
            "performance attempt exceeded an inclusive operational resource limit",
        )


def _bound_text(bound_row: Mapping[str, object], key: str) -> str:
    value = bound_row.get(key)
    if type(value) is not str or not value:
        raise TypeError(f"bound performance row {key} must be nonempty text")
    return value


__all__ = [
    "ReleasePerformanceRowAttemptV1",
    "ReleasePerformanceRowExecutionError",
    "execute_bound_performance_row",
    "execute_performance_row",
    "replace_performance_row_operational",
    "verify_performance_row_execution",
    "verify_performance_row_attempt",
]
