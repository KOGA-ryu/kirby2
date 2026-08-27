"""Exact replay records for canonical synthetic participant populations."""

from __future__ import annotations

from dataclasses import dataclass

from .ecology import EcologyRunResult, run_agent_ecology
from .models import AGENT_ECOLOGY_SCHEMA_VERSION
from .populations import ADVERSARIAL_DRILL_IDS, POPULATION_IDS, get_population


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
    schema_version: int = AGENT_ECOLOGY_SCHEMA_VERSION
    record_label: str = "CANONICAL_SYNTHETIC_AGENT_ECOLOGY_RECORD"

    def __post_init__(self) -> None:
        if self.population_id not in {*POPULATION_IDS, *ADVERSARIAL_DRILL_IDS}:
            raise ValueError("ecology recording requires a canonical built-in population")
        if self.schema_version != AGENT_ECOLOGY_SCHEMA_VERSION:
            raise ValueError("unsupported agent ecology recording schema")
        if self.seed < 0:
            raise ValueError("ecology recording seed must be nonnegative")
        definition = get_population(self.population_id)
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
        if result.definition.population_id not in {
            *POPULATION_IDS,
            *ADVERSARIAL_DRILL_IDS,
        }:
            raise ValueError("only canonical populations have portable exact replay records")
        return cls(
            result.definition.population_id,
            result.definition.sha256(),
            result.seed,
            result.summary.starting_book_sha256,
            result.summary.state_sha256,
            result.summary.public_event_sha256,
            result.summary.truth_event_sha256,
            result.result_sha256,
            result.summary.as_dict(),
        )

    def as_dict(self) -> dict[str, object]:
        return {
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

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> EcologyRecording:
        expected = {
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
        if set(payload) != expected or not isinstance(payload["expected_summary"], dict):
            raise ValueError("ecology recording field inventory is invalid")
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
            schema_version=int(payload["schema_version"]),
            record_label=str(payload["record_label"]),
        )


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
    definition = get_population(recording.population_id)
    result = run_agent_ecology(definition, seed=recording.seed)
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
