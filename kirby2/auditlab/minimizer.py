"""Deterministic reduction of typed, reproducible audit defects only."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace

from kirby2.immutable import thaw_json

from .fault_oracle import evaluate_fault_observation
from .kernel import run_generated_case
from .models import (
    CaseRecording,
    CheckStatus,
    ExecutorLane,
    FailureIdentity,
    FailurePredicateKind,
    GeneratedCaseResult,
    GeneratedConfiguration,
    MinimizationAttempt,
    MinimizedFailure,
    canonical_json,
    canonical_sha256,
)
from .probes import run_subsystem_probes


@dataclass(frozen=True, slots=True)
class AuditPredicateInjection:
    """Explicit runtime-audit hooks; production minimization passes no hooks."""

    case_transform: Callable[[GeneratedCaseResult], GeneratedCaseResult] | None = None
    recording_transform: Callable[[CaseRecording], CaseRecording] | None = None

    def __post_init__(self) -> None:
        if self.case_transform is None and self.recording_transform is None:
            raise ValueError("audit predicate injection requires a transform")


@dataclass(frozen=True, slots=True)
class _PredicateObservation:
    reproduced: bool
    digest: str
    detail: Mapping[str, object]
    recording: CaseRecording
    command_count: int


def minimize_failure(
    configuration: GeneratedConfiguration,
    identity: FailureIdentity,
    *,
    audit_injection: AuditPredicateInjection | None = None,
) -> MinimizedFailure | None:
    """Reduce only when the typed source predicate reproduces first."""

    if not isinstance(configuration, GeneratedConfiguration):
        raise TypeError("failure minimization requires GeneratedConfiguration")
    if not isinstance(identity, FailureIdentity):
        raise TypeError("failure minimization requires FailureIdentity")
    if configuration.lane is not identity.lane:
        raise ValueError("failure minimization cannot change executor lane")
    if configuration.sha256 != identity.source_configuration_sha256:
        raise ValueError("failure identity differs from its source configuration")
    source = _try_observe(configuration, identity, audit_injection)
    if source is None or not source.reproduced:
        return None

    current = configuration
    current_observation = source
    attempts: list[MinimizationAttempt] = []
    for change in _candidate_changes(configuration):
        try:
            candidate = replace(current, **change)
        except (TypeError, ValueError) as error:
            attempts.append(
                _attempt(
                    len(attempts) + 1,
                    change,
                    accepted=False,
                    reproduced=False,
                    command_count_before=current_observation.command_count,
                    command_count_after=None,
                    detail=f"candidate refused: {type(error).__name__}: {error}",
                )
            )
            continue
        if candidate == current:
            continue
        if candidate.lane is not configuration.lane:
            raise RuntimeError("minimization candidate changed executor lane")
        if candidate.injected_fault is not configuration.injected_fault:
            raise RuntimeError("minimization candidate changed required fault kind")
        observation = _try_observe(candidate, identity, audit_injection)
        accepted = observation is not None and observation.reproduced
        attempts.append(
            MinimizationAttempt(
                sequence=len(attempts) + 1,
                change=change,
                accepted=accepted,
                reproduced=accepted,
                command_count_before=current_observation.command_count,
                command_count_after=(
                    None if observation is None else observation.command_count
                ),
                observation_sha256=(
                    observation.digest
                    if observation is not None
                    else canonical_sha256(
                        {
                            "change": change,
                            "outcome": "EXECUTION_OR_PREDICATE_ERROR",
                        }
                    )
                ),
                detail=(
                    "identical typed predicate reproduced"
                    if accepted
                    else "identical typed predicate did not reproduce"
                ),
            )
        )
        if accepted:
            current = candidate
            if observation is None:  # pragma: no cover - accepted narrows this
                raise RuntimeError("accepted minimization lost its observation")
            current_observation = observation

    verification = tuple(
        _try_observe(current, identity, audit_injection) for _ in range(2)
    )
    if any(item is None for item in verification):
        raise RuntimeError("final minimization verification could not execute")
    first, second = verification
    if first is None or second is None:  # narrowed above
        raise RuntimeError("final minimization verification disappeared")
    return MinimizedFailure(
        identity=identity,
        minimized_configuration=current,
        attempts=tuple(attempts),
        final_recording=second.recording,
        verification_digests=(first.digest, second.digest),
        verification_reproduced=(first.reproduced, second.reproduced),
    )


def _candidate_changes(
    configuration: GeneratedConfiguration,
) -> tuple[dict[str, object], ...]:
    by_lane: dict[ExecutorLane, tuple[dict[str, object], ...]] = {
        ExecutorLane.CORE_FLOW: (
            {"duration_us": 1_000},
            {"flow_model": "simple"},
            {"regime": "BALANCED"},
            {"volume": "0.25x"},
            {"liquidity": "VERY_THIN"},
        ),
        ExecutorLane.MECHANICS: (
            {"session_phase": "CONTINUOUS"},
            {"order_types": "LIMIT_ONLY"},
            {"auction_state": "NONE"},
        ),
        ExecutorLane.LATENCY: (
            {"latency": "ZERO_LATENCY"},
        ),
        ExecutorLane.FRAGMENTED: (
            {"venue_count": 1},
            {"hidden_liquidity": "NONE"},
        ),
        ExecutorLane.ECOLOGY: (
            {"duration_us": 1_000},
            {"agent_count": 1},
            {"agent_population": "liquidity_provision"},
        ),
        ExecutorLane.ALGORITHM: (
            {"strategy": "IMMEDIATE_MARKET"},
            {"objective": "OBSERVE_ONLY"},
        ),
        ExecutorLane.FAULT: (),
    }
    return by_lane[configuration.lane]


def _try_observe(
    configuration: GeneratedConfiguration,
    identity: FailureIdentity,
    injection: AuditPredicateInjection | None,
) -> _PredicateObservation | None:
    try:
        result = run_generated_case(configuration)
    except Exception:
        return None
    try:
        if identity.predicate is FailurePredicateKind.STRUCTURAL_CHECK:
            return _observe_structural(result, identity, injection)
        if identity.predicate is FailurePredicateKind.REPLAY_MISMATCH:
            return _observe_replay(result, identity, injection)
        if identity.predicate is FailurePredicateKind.DETERMINISM_MISMATCH:
            return _observe_determinism(result, identity)
        if identity.predicate is FailurePredicateKind.FAULT_MISS:
            return _observe_fault_miss(result, identity)
        if identity.predicate is FailurePredicateKind.SUBSYSTEM_PROBE:
            return _observe_subsystem_probe(result, identity)
    except Exception as error:
        detail = {
            "code": identity.code,
            "error_message": str(error),
            "error_type": type(error).__name__,
            "field_name": identity.field_name,
            "predicate": identity.predicate.value,
            "reproduced": False,
        }
        return _PredicateObservation(
            False,
            canonical_sha256(detail),
            detail,
            result.recording,
            _recording_command_count(result.recording),
        )
    raise RuntimeError("unsupported minimization predicate")


def _observe_structural(
    result: GeneratedCaseResult,
    identity: FailureIdentity,
    injection: AuditPredicateInjection | None,
) -> _PredicateObservation:
    observed = (
        result
        if injection is None or injection.case_transform is None
        else injection.case_transform(result)
    )
    matching_failures = [
        item
        for item in observed.failures
        if item.kind is identity.kind
        and item.code == identity.code
        and _failure_field(item.evidence, item.code) == identity.field_name
    ]
    matching_checks = [
        item
        for item in observed.checks
        if item.name == identity.field_name and item.status is CheckStatus.FAIL
    ]
    reproduced = bool(matching_failures or matching_checks)
    detail = {
        "code": identity.code,
        "failure_count": len(matching_failures),
        "field_name": identity.field_name,
        "failed_check_count": len(matching_checks),
        "predicate": identity.predicate.value,
        "reproduced": reproduced,
        "result_sha256": observed.result_sha256,
    }
    return _PredicateObservation(
        reproduced,
        canonical_sha256(detail),
        detail,
        observed.recording,
        _recording_command_count(observed.recording),
    )


def _observe_replay(
    result: GeneratedCaseResult,
    identity: FailureIdentity,
    injection: AuditPredicateInjection | None,
) -> _PredicateObservation:
    recording = (
        result.recording
        if injection is None or injection.recording_transform is None
        else injection.recording_transform(result.recording)
    )
    loaded = recording
    expected: Mapping[str, object] = {}
    replay_exception: dict[str, str] | None = None
    actual: Mapping[str, object] = {}
    try:
        loaded = CaseRecording.from_dict(
            json.loads(canonical_json(recording.as_dict()))
        )
        raw_expected = loaded.expected_outputs["digests"]
        if not isinstance(raw_expected, Mapping):
            raise TypeError("replay predicate expected digests are invalid")
        expected = raw_expected
        from .executors import EXECUTOR_REGISTRY

        replay = EXECUTOR_REGISTRY.replay(loaded)
        raw_actual = replay.replay_expectations()["digests"]
        if not isinstance(raw_actual, Mapping):
            raise TypeError("replay predicate actual digests are invalid")
        actual = raw_actual
    except Exception as error:
        replay_exception = {
            "message": str(error),
            "type": type(error).__name__,
        }
    digest_name = _replay_digest_name(identity.field_name)
    mismatch = (
        replay_exception is not None
        if digest_name == "replay_exception"
        else expected.get(digest_name) != actual.get(digest_name)
    )
    detail = {
        "actual_sha256": actual.get(digest_name),
        "code": identity.code,
        "expected_sha256": expected.get(digest_name),
        "field_name": identity.field_name,
        "predicate": identity.predicate.value,
        "replay_exception": replay_exception,
        "reproduced": mismatch,
    }
    return _PredicateObservation(
        mismatch,
        canonical_sha256(detail),
        detail,
        loaded,
        _recording_command_count(loaded),
    )


def _observe_determinism(
    result: GeneratedCaseResult,
    identity: FailureIdentity,
) -> _PredicateObservation:
    environment = dict(os.environ)
    environment["PYTHONHASHSEED"] = "0"
    expected = canonical_json(result.declared_outputs())
    outputs: list[dict[str, object]] = []
    for _ in range(2):
        completed = subprocess.run(
            [sys.executable, "-m", "kirby2.auditlab.worker"],
            input=canonical_json(result.configuration.as_dict()),
            text=True,
            capture_output=True,
            check=False,
            env=environment,
        )
        outputs.append(
            {
                "returncode": completed.returncode,
                "stderr_sha256": canonical_sha256(completed.stderr),
                "stdout": completed.stdout.strip(),
            }
        )
    reproduced = any(item["returncode"] != 0 for item in outputs) or not (
        outputs[0]["stdout"] == outputs[1]["stdout"] == expected
    )
    detail = {
        "code": identity.code,
        "expected_sha256": canonical_sha256(expected),
        "field_name": identity.field_name,
        "predicate": identity.predicate.value,
        "processes": [
            {
                "returncode": item["returncode"],
                "stderr_sha256": item["stderr_sha256"],
                "stdout_sha256": canonical_sha256(item["stdout"]),
            }
            for item in outputs
        ],
        "reproduced": reproduced,
    }
    return _PredicateObservation(
        reproduced,
        canonical_sha256(detail),
        detail,
        result.recording,
        _recording_command_count(result.recording),
    )


def _observe_fault_miss(
    result: GeneratedCaseResult,
    identity: FailureIdentity,
) -> _PredicateObservation:
    observation = result.fault_observation
    evaluation = (
        None if observation is None else evaluate_fault_observation(observation)
    )
    reproduced = evaluation is None or not evaluation.detected
    detail = {
        "code": identity.code,
        "evaluation": None if evaluation is None else evaluation.as_dict(),
        "field_name": identity.field_name,
        "predicate": identity.predicate.value,
        "reproduced": reproduced,
    }
    return _PredicateObservation(
        reproduced,
        canonical_sha256(detail),
        detail,
        result.recording,
        _recording_command_count(result.recording),
    )


def _observe_subsystem_probe(
    result: GeneratedCaseResult,
    identity: FailureIdentity,
) -> _PredicateObservation:
    seed = int(identity.predicate_parameters["probe_seed"])
    probe = next(
        item for item in run_subsystem_probes(seed) if item.name == identity.field_name
    )
    reproduced = probe.required and probe.status is not CheckStatus.PASS
    detail = {
        "code": identity.code,
        "field_name": identity.field_name,
        "predicate": identity.predicate.value,
        "probe": probe.as_dict(),
        "probe_seed": seed,
        "reproduced": reproduced,
    }
    return _PredicateObservation(
        reproduced,
        canonical_sha256(detail),
        detail,
        result.recording,
        _recording_command_count(result.recording),
    )


def _failure_field(evidence: Mapping[str, object], fallback: str) -> str:
    for name in ("check", "field_name", "field", "capability"):
        value = evidence.get(name)
        if type(value) is str and value:
            return value
    return fallback


def _replay_digest_name(field_name: str) -> str:
    return {
        "event": "event_sha256",
        "state": "state_sha256",
        "observable": "observable_sha256",
        "metrics": "metrics_sha256",
        "declared_outputs": "declared_outputs_sha256",
        "replay_exception": "replay_exception",
    }.get(field_name, field_name)


def _recording_command_count(recording: CaseRecording) -> int:
    payload = thaw_json(recording.payload)
    flow_events = payload.get("flow_events")
    if isinstance(flow_events, list):
        return len(flow_events)
    native = payload.get("native_recording")
    if isinstance(native, dict):
        commands = native.get("commands")
        if isinstance(commands, list):
            return len(commands)
        expected_summary = native.get("expected_summary")
        if isinstance(expected_summary, dict):
            return int(expected_summary.get("action_count", 0))
    legs = payload.get("legs")
    if isinstance(legs, list):
        return sum(
            len(item["native_recording"].get("commands", []))
            for item in legs
            if isinstance(item, dict)
            and isinstance(item.get("native_recording"), dict)
        )
    fault = payload.get("fault_observation")
    if isinstance(fault, dict):
        manifest = fault.get("manifest")
        if isinstance(manifest, dict) and isinstance(manifest.get("commands"), list):
            return len(manifest["commands"])
        events = fault.get("raw_events")
        if isinstance(events, list):
            return len(events)
    return 0


def _attempt(
    sequence: int,
    change: Mapping[str, object],
    *,
    accepted: bool,
    reproduced: bool,
    command_count_before: int,
    command_count_after: int | None,
    detail: str,
) -> MinimizationAttempt:
    return MinimizationAttempt(
        sequence=sequence,
        change=change,
        accepted=accepted,
        reproduced=reproduced,
        command_count_before=command_count_before,
        command_count_after=command_count_after,
        observation_sha256=canonical_sha256(
            {
                "accepted": accepted,
                "change": change,
                "detail": detail,
                "reproduced": reproduced,
            }
        ),
        detail=detail,
    )
