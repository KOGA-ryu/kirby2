"""Exact replay records for canonical synthetic participant populations."""

from __future__ import annotations

from dataclasses import dataclass

from .ecology import EcologyRunResult, run_agent_ecology
from .models import PopulationDefinition
from .populations import ADVERSARIAL_DRILL_IDS, POPULATION_IDS, get_population


ECOLOGY_RECORDING_SCHEMA_VERSION = 2
LEGACY_ECOLOGY_RECORDING_SCHEMA_VERSION = 1
_LEGACY_RECORD_LABEL = "CANONICAL_SYNTHETIC_AGENT_ECOLOGY_RECORD"
_PORTABLE_RECORD_LABEL = "PORTABLE_SYNTHETIC_AGENT_ECOLOGY_RECORD"
_COMMON_FIELDS = {
    "expected_public_event_sha256",
    "expected_result_sha256",
    "expected_starting_book_sha256",
    "expected_state_sha256",
    "expected_summary",
    "expected_truth_event_sha256",
    "population_definition_sha256",
    "population_id",
    "record_label",
    "schema_version",
    "seed",
}


@dataclass(frozen=True, slots=True)
class EcologyRecording:
    population_id: str
    population_definition_sha256: str
    seed: int
    expected_starting_book_sha256: str
    expected_state_sha256: str
    expected_public_event_sha256: str
    expected_truth_event_sha256: str
    expected_result_sha256: str
    expected_summary: dict[str, object]
    population_definition: PopulationDefinition | None = None
    schema_version: int = ECOLOGY_RECORDING_SCHEMA_VERSION
    record_label: str = _PORTABLE_RECORD_LABEL

    def __post_init__(self) -> None:
        if self.schema_version == LEGACY_ECOLOGY_RECORDING_SCHEMA_VERSION:
            if self.population_definition is not None:
                raise ValueError("legacy ecology recording cannot embed a definition")
            if self.population_id not in {*POPULATION_IDS, *ADVERSARIAL_DRILL_IDS}:
                raise ValueError(
                    "legacy ecology recording requires a canonical population"
                )
            if self.record_label != _LEGACY_RECORD_LABEL:
                raise ValueError("legacy ecology recording label is invalid")
            definition = get_population(self.population_id)
        elif self.schema_version == ECOLOGY_RECORDING_SCHEMA_VERSION:
            if not isinstance(self.population_definition, PopulationDefinition):
                raise TypeError("portable ecology recording requires its definition")
            if self.record_label != _PORTABLE_RECORD_LABEL:
                raise ValueError("portable ecology recording label is invalid")
            definition = self.population_definition
            if definition.population_id != self.population_id:
                raise ValueError("embedded population identity does not match recording")
        else:
            raise ValueError("unsupported agent ecology recording schema")
        if type(self.seed) is not int or self.seed < 0:
            raise ValueError("ecology recording seed must be nonnegative")
        if definition.sha256() != self.population_definition_sha256:
            raise ValueError("ecology recording population digest is stale or forged")
        digests = (
            self.expected_starting_book_sha256,
            self.expected_state_sha256,
            self.expected_public_event_sha256,
            self.expected_truth_event_sha256,
            self.expected_result_sha256,
        )
        if any(len(value) != 64 for value in digests):
            raise ValueError("ecology recording digest inventory is invalid")

    @classmethod
    def capture(cls, result: EcologyRunResult) -> EcologyRecording:
        return cls(
            population_id=result.definition.population_id,
            population_definition_sha256=result.definition.sha256(),
            seed=result.seed,
            expected_starting_book_sha256=(
                result.summary.starting_book_sha256
            ),
            expected_state_sha256=result.summary.state_sha256,
            expected_public_event_sha256=result.summary.public_event_sha256,
            expected_truth_event_sha256=result.summary.truth_event_sha256,
            expected_result_sha256=result.result_sha256,
            expected_summary=result.summary.as_dict(),
            population_definition=result.definition,
        )

    def as_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "expected_public_event_sha256": self.expected_public_event_sha256,
            "expected_result_sha256": self.expected_result_sha256,
            "expected_starting_book_sha256": self.expected_starting_book_sha256,
            "expected_state_sha256": self.expected_state_sha256,
            "expected_summary": self.expected_summary,
            "expected_truth_event_sha256": self.expected_truth_event_sha256,
            "population_definition_sha256": self.population_definition_sha256,
            "population_id": self.population_id,
            "record_label": self.record_label,
            "schema_version": self.schema_version,
            "seed": self.seed,
        }
        if self.schema_version == ECOLOGY_RECORDING_SCHEMA_VERSION:
            if self.population_definition is None:  # pragma: no cover - validated
                raise RuntimeError("portable ecology definition is absent")
            payload["population_definition"] = (
                self.population_definition.identity_dict()
            )
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> EcologyRecording:
        raw_schema = payload.get("schema_version")
        if type(raw_schema) is not int:
            raise TypeError("ecology recording schema must be an integer")
        expected = (
            _COMMON_FIELDS
            if raw_schema == LEGACY_ECOLOGY_RECORDING_SCHEMA_VERSION
            else _COMMON_FIELDS | {"population_definition"}
            if raw_schema == ECOLOGY_RECORDING_SCHEMA_VERSION
            else set()
        )
        if set(payload) != expected or not isinstance(
            payload.get("expected_summary"),
            dict,
        ):
            raise ValueError("ecology recording field inventory is invalid")
        raw_definition = payload.get("population_definition")
        if raw_definition is not None and not isinstance(raw_definition, dict):
            raise TypeError("embedded population definition must be an object")
        return cls(
            population_id=str(payload["population_id"]),
            population_definition_sha256=str(payload["population_definition_sha256"]),
            seed=int(payload["seed"]),
            expected_starting_book_sha256=str(
                payload["expected_starting_book_sha256"]
            ),
            expected_state_sha256=str(payload["expected_state_sha256"]),
            expected_public_event_sha256=str(
                payload["expected_public_event_sha256"]
            ),
            expected_truth_event_sha256=str(
                payload["expected_truth_event_sha256"]
            ),
            expected_result_sha256=str(payload["expected_result_sha256"]),
            expected_summary=dict(payload["expected_summary"]),
            population_definition=(
                None
                if raw_definition is None
                else PopulationDefinition.from_dict(raw_definition)
            ),
            schema_version=raw_schema,
            record_label=str(payload["record_label"]),
        )

    @property
    def definition(self) -> PopulationDefinition:
        if self.population_definition is not None:
            return self.population_definition
        return get_population(self.population_id)


@dataclass(frozen=True, slots=True)
class EcologyReplayReport:
    result: EcologyRunResult
    starting_book_match: bool
    state_match: bool
    public_event_match: bool
    truth_event_match: bool
    summary_match: bool
    result_match: bool

    @property
    def passed(self) -> bool:
        return all(
            (
                self.starting_book_match,
                self.state_match,
                self.public_event_match,
                self.truth_event_match,
                self.summary_match,
                self.result_match,
            )
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "public_event_match": self.public_event_match,
            "result_match": self.result_match,
            "starting_book_match": self.starting_book_match,
            "state_match": self.state_match,
            "status": "PASS" if self.passed else "FAIL",
            "summary_match": self.summary_match,
            "truth_event_match": self.truth_event_match,
        }


def replay_agent_ecology(recording: EcologyRecording) -> EcologyReplayReport:
    result = run_agent_ecology(recording.definition, seed=recording.seed)
    return EcologyReplayReport(
        result,
        result.summary.starting_book_sha256
        == recording.expected_starting_book_sha256,
        result.summary.state_sha256 == recording.expected_state_sha256,
        result.summary.public_event_sha256 == recording.expected_public_event_sha256,
        result.summary.truth_event_sha256 == recording.expected_truth_event_sha256,
        result.summary.as_dict() == recording.expected_summary,
        result.result_sha256 == recording.expected_result_sha256,
    )
