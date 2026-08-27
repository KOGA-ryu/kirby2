"""Typed wrapper for production-subsystem fault observations."""

from __future__ import annotations

from kirby2.immutable import thaw_json

from ..faults import inject_and_observe
from ..models import (
    CaseRecording,
    CheckResult,
    CheckStatus,
    ExerciseRecord,
    ExerciseStatus,
    ExecutorLane,
    FailureKind,
    FailureObservation,
    FaultObservation,
    GeneratedCaseResult,
    GeneratedConfiguration,
    canonical_sha256,
)


FAULT_RECORDING_TYPE = "EXPLICIT_FAULT_OBSERVATION"
_RECORDING_FIELDS = frozenset({"configuration", "fault_observation"})


class FaultExecutor:
    """Return raw evidence for one explicitly selected production fault."""

    lane = ExecutorLane.FAULT

    def execute(
        self,
        configuration: GeneratedConfiguration,
    ) -> GeneratedCaseResult:
        self._require_configuration(configuration)
        observation = inject_and_observe(configuration)
        if observation is None:
            raise RuntimeError("fault executor produced no fault observation")
        recording = CaseRecording(
            lane=self.lane,
            recording_type=FAULT_RECORDING_TYPE,
            payload={
                "configuration": configuration.as_dict(),
                "fault_observation": observation.as_dict(),
            },
        )
        return _result(configuration, recording, observation, replay_match=True)

    def replay(self, recording: CaseRecording) -> GeneratedCaseResult:
        if not isinstance(recording, CaseRecording):
            raise TypeError("fault replay requires CaseRecording")
        if recording.lane is not self.lane:
            raise ValueError("fault replay received a different lane")
        if recording.recording_type != FAULT_RECORDING_TYPE:
            raise ValueError("unsupported fault recording type")
        payload = thaw_json(recording.payload)
        if not isinstance(payload, dict) or set(payload) != _RECORDING_FIELDS:
            raise ValueError("fault recording fields are not exact")
        raw_configuration = payload["configuration"]
        if not isinstance(raw_configuration, dict):
            raise TypeError("fault recording configuration must be an object")
        configuration = GeneratedConfiguration.from_dict(raw_configuration)
        self._require_configuration(configuration)
        observation = inject_and_observe(configuration)
        if observation is None:
            raise RuntimeError("fault replay produced no fault observation")
        replay_match = observation.as_dict() == payload["fault_observation"]
        return _result(
            configuration,
            recording,
            observation,
            replay_match=replay_match,
        )

    def _require_configuration(
        self,
        configuration: GeneratedConfiguration,
    ) -> None:
        if not isinstance(configuration, GeneratedConfiguration):
            raise TypeError("fault executor requires GeneratedConfiguration")
        if configuration.lane is not self.lane:
            raise ValueError("fault executor received a different lane")
        if configuration.injected_fault is None:
            raise ValueError("fault executor requires one explicit fault")


def _result(
    configuration: GeneratedConfiguration,
    recording: CaseRecording,
    observation: FaultObservation,
    *,
    replay_match: bool,
) -> GeneratedCaseResult:
    repeated = inject_and_observe(configuration)
    configuration_round_trip = (
        GeneratedConfiguration.from_dict(configuration.as_dict())
        == configuration
    )
    adapter_deterministic = (
        repeated is not None and repeated.as_dict() == observation.as_dict()
    )
    raw_evidence_sha256 = canonical_sha256(observation.as_dict())
    checks = (
        _check(
            "fault_injected",
            observation.fault is configuration.injected_fault,
            {
                "configuration_sha256": configuration.sha256,
                "fault": observation.fault.value,
                "injection_event": observation.injection_event,
                "injection_location": observation.injection_location,
            },
        ),
        _check(
            "production_detector_exercised",
            bool(observation.raw_events or observation.raw_issues),
            {
                "detector": observation.detector,
                "observed_code": observation.observed_code,
                "raw_evidence_sha256": raw_evidence_sha256,
                "raw_event_count": len(observation.raw_events),
                "raw_issue_count": len(observation.raw_issues),
                "subsystem": observation.subsystem,
            },
        ),
        _check(
            "unrelated_invariants_survive",
            configuration_round_trip and adapter_deterministic,
            {
                "adapter_repeat_match": adapter_deterministic,
                "configuration_round_trip_match": configuration_round_trip,
                "configuration_sha256": configuration.sha256,
                "raw_evidence_sha256": raw_evidence_sha256,
            },
        ),
    )
    failures = [
        FailureObservation(
            kind=FailureKind.INVARIANT_VIOLATION,
            code=f"FAULT_{check.name.upper()}",
            message=check.detail,
            evidence={
                "check": check.name,
                "check_evidence_sha256": canonical_sha256(
                    check.as_dict()["evidence"]
                ),
            },
        )
        for check in checks
        if check.status is CheckStatus.FAIL
    ]
    if not replay_match:
        failures.append(
            FailureObservation(
                kind=FailureKind.REPLAY_MISMATCH,
                code="FAULT_REPLAY_MISMATCH",
                message="fault observation did not reproduce from its configuration",
                evidence={"recording_sha256": recording.sha256},
            )
        )
    return GeneratedCaseResult(
        configuration=configuration,
        lane=ExecutorLane.FAULT,
        recording=recording,
        event_projection=(
            {
                "data": observation.as_dict(),
                "record_type": "fault_observation",
                "sequence": 1,
            },
        ),
        final_state_projection={
            "configuration_sha256": configuration.sha256,
            "fault_observation": observation.as_dict(),
            "raw_evidence_sha256": raw_evidence_sha256,
        },
        metrics={
            "injected_fault_count": 1,
            "observed_code_count": int(observation.observed_code is not None),
            "raw_observation_count": 1,
        },
        exercises=(
            ExerciseRecord(
                ExecutorLane.FAULT,
                "injected_fault",
                observation.fault.value,
                ExerciseStatus.EXERCISED,
                {
                    "detector": observation.detector,
                    "raw_evidence_sha256": raw_evidence_sha256,
                    "recording_sha256": recording.sha256,
                    "subsystem": observation.subsystem,
                },
            ),
        ),
        checks=checks,
        failures=tuple(failures),
        observable_projection={
            "fault": observation.fault.value,
            "observed_code": observation.observed_code,
            "representation": "PRODUCTION_FAULT_OBSERVATION",
            "subsystem": observation.subsystem,
        },
        fault_observation=observation,
    )


def _check(
    name: str,
    passed: bool,
    evidence: dict[str, object],
) -> CheckResult:
    return CheckResult(
        name=name,
        status=CheckStatus.PASS if passed else CheckStatus.FAIL,
        required=True,
        detail=(
            f"production fault-observation check passed: {name}"
            if passed
            else f"production fault-observation check failed: {name}"
        ),
        evidence={"source": "FaultExecutor", **evidence},
    )
