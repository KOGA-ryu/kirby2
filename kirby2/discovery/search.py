"""Deterministic bounded search policies for strategy discovery.

One interface implements grid, random, coordinate, beam, and evolutionary
traversal.  WO35-D runs it only with :class:`DevelopmentSyntheticScoreOracleV1`;
real partitions remain behind the later experiment gates.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import re
import struct
import tomllib
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from kirby2.immutable import freeze_json, thaw_json

from .ast import parse_strategy_ast
from .diffs import strategy_complexity
from .evaluation import (
    CandidatePartitionEvidenceV1,
    DevelopmentSyntheticScoreOracleV1,
    QualificationDecisionV1,
    SYNTHETIC_ORACLE_CONTROLLED_ID_V1,
    SYNTHETIC_ORACLE_NO_WINNER_ID_V1,
    SyntheticOracleModeV1,
    require_compatible_evidence,
    validation_qualification,
)
from .identity import canonical_identity_bytes, strategy_semantic_sha256
from .objectives import (
    ALL_OBJECTIVE_SPECS_V1,
    EvidenceCompatibilityKeyV1,
    MULTIPLICITY_METHOD_V1,
    REQUIRED_OBJECTIVE_SPECS_V1,
    ROOT_REDUCTION_ORDER_V1,
    STRATEGY_DISCOVERY_POLICY_VERSION_V1,
    UNCERTAINTY_METHOD_V1,
    common_tie_digest,
    complexity_points,
    objective_protocol_projection,
)
from .partitions import StrategyPartitionV1


STRATEGY_SEARCH_MANIFEST_SCHEMA_ID_V1 = "KIRBY2_STRATEGY_SEARCH_MANIFEST_V1"
STRATEGY_SEARCH_MANIFEST_DIGEST_DOMAIN_V1 = b"KIRBY2_STRATEGY_SEARCH_MANIFEST_V1\x00"
STRATEGY_SEARCH_RUN_SCHEMA_ID_V1 = "KIRBY2_STRATEGY_SEARCH_RUN_V1"
STRATEGY_SEARCH_RUN_DIGEST_DOMAIN_V1 = b"KIRBY2_STRATEGY_SEARCH_RUN_V1\x00"
STRATEGY_VECTOR_SCHEMA_ID_V1 = "KIRBY2_STRATEGY_PARAMETER_VECTOR_V1"
STRATEGY_VECTOR_ORDER_V1 = "NFC_UTF8_PATH_FIRST_SLOWEST_LAST_FASTEST_V1"
CONTROLLED_PROTOCOL_ID_V1 = "BOUNDED_SEARCH_CONTROLLED_V1"
CONTROLLED_BASE_SOURCE_SHA256_V1 = (
    "1f0a39b847093703e58061e396fc80beb02509e3d3b128c3c8c5a22fd51d1df3"
)
MAX_SEARCH_BUDGET_V1 = 64
FINALIST_LIMIT_V1 = 8
COORDINATE_MAX_PASSES_V1 = 4
BEAM_MAX_DEPTH_V1 = 4
BEAM_WIDTH_V1 = 4
EVOLUTION_MAX_GENERATIONS_V1 = 8
EVOLUTION_INITIAL_SIZE_V1 = 8
EVOLUTION_ELITE_LIMIT_V1 = 2
EVOLUTION_CHILD_LIMIT_V1 = 6
MATERIAL_MOVE_V1 = 10_000
RANDOM_ROOT_SEED_V1 = 3_500_001
EVOLUTION_ROOT_SEED_V1 = 3_500_004
_SHA256 = re.compile(r"^[0-9a-f]{64}$")

_CONTROLLED_BASE_SOURCE = b"""setup BOUNDED_BASE_V1
window 5s
unavailable REFUSE
GREEN when
spread_ticks <= 2
book_imbalance >= 0.2
WAIT when
spread_ticks <= 4
RED otherwise
"""

_EXPECTED_ROOTS = {
    "train": list(range(3_501_000, 3_501_012)),
    "validation": list(range(3_502_000, 3_502_008)),
    "holdout": list(range(3_503_000, 3_503_008)),
    "adversarial": list(range(3_504_000, 3_504_008)),
    "robustness": list(range(3_505_000, 3_505_004)),
}


class SearchParameterTypeV1(str, Enum):
    BOOLEAN = "BOOLEAN"
    INTEGER = "INTEGER"
    ENUM = "ENUM"


class SearchPolicyV1(str, Enum):
    GRID = "GRID"
    RANDOM = "RANDOM"
    COORDINATE = "COORDINATE"
    BEAM = "BEAM"
    EVOLUTIONARY = "EVOLUTIONARY"


class SearchOutcomeV1(str, Enum):
    CANDIDATE_SELECTED = "CANDIDATE_SELECTED"
    NO_CANDIDATE_MET_CRITERIA = "NO_CANDIDATE_MET_CRITERIA"


class SearchStopReasonV1(str, Enum):
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    UNIVERSE_EXHAUSTED = "UNIVERSE_EXHAUSTED"
    TWO_STAGNANT_PASSES = "TWO_STAGNANT_PASSES"
    PASS_LIMIT = "PASS_LIMIT"
    EMPTY_EXPANSION = "EMPTY_EXPANSION"
    TWO_STAGNANT_DEPTHS = "TWO_STAGNANT_DEPTHS"
    DEPTH_LIMIT = "DEPTH_LIMIT"
    EMPTY_POPULATION = "EMPTY_POPULATION"
    TWO_STAGNANT_GENERATIONS = "TWO_STAGNANT_GENERATIONS"
    GENERATION_LIMIT = "GENERATION_LIMIT"


@dataclass(frozen=True, slots=True)
class SearchParameterV1:
    path: str
    type_tag: SearchParameterTypeV1
    domain: tuple[bool | int | str, ...]
    base_value: bool | int | str

    def __post_init__(self) -> None:
        _require_nfc(self.path, "parameter path")
        if not self.path.startswith("/"):
            raise ValueError("parameter path must be an absolute JSON pointer")
        if not isinstance(self.type_tag, SearchParameterTypeV1):
            raise TypeError("search parameter type must be typed")
        if type(self.domain) is not tuple or not self.domain:
            raise ValueError("search parameter domain must be nonempty")
        for value in self.domain:
            _validate_parameter_value(self.type_tag, value)
        expected = tuple(sorted(self.domain, key=lambda value: _domain_key(self.type_tag, value)))
        if self.domain != expected or len(set(self.domain)) != len(self.domain):
            raise ValueError("search parameter domain must be unique and canonically ordered")
        _validate_parameter_value(self.type_tag, self.base_value)
        if self.base_value not in self.domain:
            raise ValueError("search parameter base value must belong to its domain")

    def as_dict(self) -> dict[str, object]:
        return {
            "base": self.base_value,
            "domain": list(self.domain),
            "path": self.path,
            "type": self.type_tag.value,
        }


@dataclass(frozen=True, slots=True)
class VectorEntryV1:
    path: str
    type_tag: SearchParameterTypeV1
    value: bool | int | str

    def __post_init__(self) -> None:
        _require_nfc(self.path, "vector path")
        if not isinstance(self.type_tag, SearchParameterTypeV1):
            raise TypeError("vector type must be typed")
        _validate_parameter_value(self.type_tag, self.value)

    def json_row(self) -> list[object]:
        return [self.path, self.type_tag.value, self.value]


@dataclass(frozen=True, slots=True)
class StrategyParameterVectorV1:
    entries: tuple[VectorEntryV1, ...]

    def __post_init__(self) -> None:
        if type(self.entries) is not tuple or not self.entries or any(
            not isinstance(item, VectorEntryV1) for item in self.entries
        ):
            raise TypeError("strategy vector entries must be a nonempty typed tuple")
        paths = tuple(item.path for item in self.entries)
        if paths != tuple(sorted(paths, key=lambda item: item.encode("utf-8"))):
            raise ValueError("strategy vector paths are not in NFC UTF-8 order")
        if len(paths) != len(set(paths)):
            raise ValueError("strategy vector paths must be unique")

    @property
    def canonical_bytes(self) -> bytes:
        return json.dumps(
            [item.json_row() for item in self.entries],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")

    @property
    def vector_sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes).hexdigest()

    def value(self, path: str) -> bool | int | str:
        for item in self.entries:
            if item.path == path:
                return item.value
        raise KeyError(path)

    def as_dict(self) -> dict[str, object]:
        return {
            "entries": [item.json_row() for item in self.entries],
            "schema_id": STRATEGY_VECTOR_SCHEMA_ID_V1,
            "schema_version": 1,
            "vector_sha256": self.vector_sha256,
        }


@dataclass(frozen=True, slots=True)
class SearchCandidateV1:
    candidate_id: str
    semantic_sha256: str
    vector: StrategyParameterVectorV1
    source: bytes
    complexity_points: int
    universe_ordinal: int

    def __post_init__(self) -> None:
        _require_nfc(self.candidate_id, "candidate ID")
        _require_sha256(self.semantic_sha256, "candidate semantic digest")
        if not isinstance(self.vector, StrategyParameterVectorV1):
            raise TypeError("candidate vector must be typed")
        if type(self.source) is not bytes or not self.source.endswith(b"\n"):
            raise ValueError("candidate source must be final-LF UTF-8 bytes")
        self.source.decode("utf-8")
        ast = parse_strategy_ast(self.source.decode("utf-8"))
        if strategy_semantic_sha256(ast) != self.semantic_sha256:
            raise ValueError("candidate source and semantic digest disagree")
        if type(self.complexity_points) is not int or self.complexity_points < 0:
            raise ValueError("candidate complexity points must be nonnegative")
        if type(self.universe_ordinal) is not int or self.universe_ordinal < -1:
            raise ValueError("candidate universe ordinal is invalid")

    @property
    def stable_id_bytes(self) -> bytes:
        return self.candidate_id.encode("utf-8")

    @property
    def oracle_values(self) -> tuple[int, int, int, int]:
        values = (
            self.vector.value("/window_us"),
            self.vector.value("/green/0/threshold_ticks"),
            self.vector.value("/green/1/threshold_ppm"),
            self.vector.value("/wait/0/threshold_ticks"),
        )
        if any(type(item) is not int for item in values):
            raise TypeError("controlled oracle values must all be integers")
        return values  # type: ignore[return-value]

    def as_dict(self) -> dict[str, object]:
        return {
            "candidate_id": self.candidate_id,
            "complexity_points": self.complexity_points,
            "semantic_sha256": self.semantic_sha256,
            "universe_ordinal": self.universe_ordinal,
            "vector": self.vector.as_dict(),
        }


@dataclass(frozen=True, slots=True)
class ControlledSearchSpaceV1:
    parameters: tuple[SearchParameterV1, ...]

    def __post_init__(self) -> None:
        if type(self.parameters) is not tuple or not self.parameters or any(
            not isinstance(item, SearchParameterV1) for item in self.parameters
        ):
            raise TypeError("search space parameters must be a nonempty typed tuple")
        paths = tuple(item.path for item in self.parameters)
        if paths != tuple(sorted(paths, key=lambda item: item.encode("utf-8"))):
            raise ValueError("search parameters must be in NFC UTF-8 order")
        if len(paths) != len(set(paths)):
            raise ValueError("search parameter paths must be unique")
        if self.parameters != controlled_search_parameters():
            raise ValueError("controlled run search space differs from section 5.7.6")

    @property
    def base_vector(self) -> StrategyParameterVectorV1:
        return StrategyParameterVectorV1(
            tuple(
                VectorEntryV1(item.path, item.type_tag, item.base_value)
                for item in self.parameters
            )
        )

    def vector(self, values: tuple[bool | int | str, ...]) -> StrategyParameterVectorV1:
        if type(values) is not tuple or len(values) != len(self.parameters):
            raise ValueError("search vector arity differs from the search space")
        entries = tuple(
            VectorEntryV1(parameter.path, parameter.type_tag, value)
            for parameter, value in zip(self.parameters, values, strict=True)
        )
        for parameter, entry in zip(self.parameters, entries, strict=True):
            if entry.value not in parameter.domain:
                raise ValueError("search vector value is outside its domain")
        return StrategyParameterVectorV1(entries)

    def candidate_for_vector(
        self,
        vector: StrategyParameterVectorV1,
        *,
        universe_ordinal: int,
    ) -> SearchCandidateV1 | None:
        if not isinstance(vector, StrategyParameterVectorV1):
            raise TypeError("candidate construction requires a typed vector")
        green = vector.value("/green/0/threshold_ticks")
        wait = vector.value("/wait/0/threshold_ticks")
        if type(green) is not int or type(wait) is not int:
            raise TypeError("controlled spread thresholds must be integers")
        if wait < green:
            return None
        source = render_controlled_strategy_source(vector)
        ast = parse_strategy_ast(source.decode("utf-8"))
        semantic = strategy_semantic_sha256(ast)
        return SearchCandidateV1(
            candidate_id="CANDIDATE_" + semantic[:24],
            semantic_sha256=semantic,
            vector=vector,
            source=source,
            complexity_points=complexity_points(strategy_complexity(ast)),
            universe_ordinal=universe_ordinal,
        )

    def base_candidate(self) -> SearchCandidateV1:
        candidate = self.candidate_for_vector(self.base_vector, universe_ordinal=-1)
        assert candidate is not None
        return candidate

    def universe(self) -> tuple[SearchCandidateV1, ...]:
        base_semantic = self.base_candidate().semantic_sha256
        retained: list[SearchCandidateV1] = []
        seen: set[str] = {base_semantic}
        for raw_values in itertools.product(*(item.domain for item in self.parameters)):
            vector = self.vector(tuple(raw_values))
            provisional = self.candidate_for_vector(vector, universe_ordinal=len(retained))
            if provisional is None or provisional.semantic_sha256 in seen:
                continue
            seen.add(provisional.semantic_sha256)
            retained.append(provisional)
        return tuple(retained)

    def candidate_by_vector_key(self) -> dict[tuple[object, ...], SearchCandidateV1]:
        return {_vector_key(item.vector): item for item in self.universe()}

    def neighbors(
        self,
        vector: StrategyParameterVectorV1,
        *,
        candidate_by_vector: Mapping[tuple[object, ...], SearchCandidateV1] | None = None,
    ) -> tuple[SearchCandidateV1, ...]:
        current = list(_vector_key(vector))
        candidates = (
            self.candidate_by_vector_key()
            if candidate_by_vector is None
            else candidate_by_vector
        )
        rows: list[SearchCandidateV1] = []
        for index, parameter in enumerate(self.parameters):
            domain_index = parameter.domain.index(current[index])
            for neighbor_index in (domain_index - 1, domain_index + 1):
                if not 0 <= neighbor_index < len(parameter.domain):
                    continue
                changed = list(current)
                changed[index] = parameter.domain[neighbor_index]
                candidate = candidates.get(tuple(changed))
                if candidate is not None:
                    rows.append(candidate)
        return tuple(rows)


@dataclass(frozen=True, slots=True)
class StrategySearchManifestV1:
    experiment_id: str
    oracle_id: str
    expected_outcome: SearchOutcomeV1
    policy: SearchPolicyV1
    budget: int
    hard_budget: int
    finalist_limit: int
    compatibility: EvidenceCompatibilityKeyV1
    partition_manifest_sha256: str
    source_ancestry_sha256: str
    search_space: ControlledSearchSpaceV1
    payload: Mapping[str, object]

    def __post_init__(self) -> None:
        _require_nfc(self.experiment_id, "search experiment ID")
        if self.oracle_id not in {
            SYNTHETIC_ORACLE_CONTROLLED_ID_V1,
            SYNTHETIC_ORACLE_NO_WINNER_ID_V1,
        }:
            raise ValueError("WO35-D permits only its two synthetic score oracles")
        if not isinstance(self.expected_outcome, SearchOutcomeV1):
            raise TypeError("expected search outcome must be typed")
        expected = (
            SearchOutcomeV1.NO_CANDIDATE_MET_CRITERIA
            if self.oracle_id == SYNTHETIC_ORACLE_NO_WINNER_ID_V1
            else SearchOutcomeV1.CANDIDATE_SELECTED
        )
        if self.expected_outcome is not expected:
            raise ValueError("synthetic oracle and expected outcome disagree")
        if self.policy is not SearchPolicyV1.GRID:
            raise ValueError("committed WO35-D manifests select exactly GRID")
        if self.budget != MAX_SEARCH_BUDGET_V1 or self.hard_budget != MAX_SEARCH_BUDGET_V1:
            raise ValueError("committed bounded search budget must be exactly 64")
        if self.finalist_limit != FINALIST_LIMIT_V1:
            raise ValueError("committed finalist limit must be exactly eight")
        if not isinstance(self.compatibility, EvidenceCompatibilityKeyV1):
            raise TypeError("search compatibility key must be typed")
        _require_sha256(self.partition_manifest_sha256, "partition manifest digest")
        _require_sha256(self.source_ancestry_sha256, "source ancestry digest")
        if not isinstance(self.search_space, ControlledSearchSpaceV1):
            raise TypeError("search manifest search space must be typed")
        if not isinstance(self.payload, Mapping):
            raise TypeError("search manifest payload must be an immutable object")
        detached = thaw_json(freeze_json(self.payload))
        canonical_identity_bytes(detached)
        object.__setattr__(self, "payload", freeze_json(detached))

    @property
    def manifest_sha256(self) -> str:
        raw = canonical_identity_bytes(thaw_json(self.payload))
        digest = hashlib.sha256()
        digest.update(STRATEGY_SEARCH_MANIFEST_DIGEST_DOMAIN_V1)
        digest.update(struct.pack(">Q", len(raw)))
        digest.update(raw)
        return digest.hexdigest()

    @classmethod
    def from_toml_bytes(cls, raw: bytes) -> "StrategySearchManifestV1":
        if type(raw) is not bytes or not raw:
            raise ValueError("search manifest bytes must be nonempty")
        try:
            text = raw.decode("utf-8")
            payload = tomllib.loads(text)
        except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
            raise ValueError("search manifest is not canonical UTF-8 TOML") from error
        if text.startswith("\ufeff"):
            raise ValueError("search manifest cannot contain a BOM")
        _validate_manifest_payload(payload)
        parameters = tuple(
            SearchParameterV1(
                path=_text(item, "path"),
                type_tag=SearchParameterTypeV1(_text(item, "type")),
                domain=tuple(item["domain"]),
                base_value=item["base"],
            )
            for item in _object(payload, "search_space")["parameters"]
        )
        return cls(
            experiment_id=_text(payload, "experiment_id"),
            oracle_id=_text(payload, "oracle_id"),
            expected_outcome=SearchOutcomeV1(_text(payload, "expected_outcome")),
            policy=SearchPolicyV1(_text(payload, "policy")),
            budget=_integer(payload, "budget"),
            hard_budget=_integer(payload, "hard_budget"),
            finalist_limit=_integer(payload, "finalist_limit"),
            compatibility=EvidenceCompatibilityKeyV1(
                _text(payload, "scenario_group_id"),
                _text(payload, "objective_group_id"),
                _text(payload, "evidence_group_id"),
            ),
            partition_manifest_sha256=_text(payload, "partition_manifest_sha256"),
            source_ancestry_sha256=_text(payload, "source_ancestry_sha256"),
            search_space=ControlledSearchSpaceV1(parameters),
            payload=payload,
        )


@dataclass(frozen=True, slots=True)
class TrainedCandidateV1:
    candidate: SearchCandidateV1
    evidence: CandidatePartitionEvidenceV1

    def __post_init__(self) -> None:
        if not isinstance(self.candidate, SearchCandidateV1):
            raise TypeError("trained candidate must bind a typed candidate")
        if not isinstance(self.evidence, CandidatePartitionEvidenceV1):
            raise TypeError("trained candidate must bind typed evidence")
        if (
            self.evidence.partition is not StrategyPartitionV1.TRAIN
            or self.evidence.semantic_sha256 != self.candidate.semantic_sha256
        ):
            raise ValueError("trained candidate and evidence disagree")

    @property
    def training_merit(self) -> int:
        return self.evidence.training_merit


@dataclass(frozen=True, slots=True)
class ValidatedCandidateV1:
    trained: TrainedCandidateV1
    evidence: CandidatePartitionEvidenceV1
    qualification: QualificationDecisionV1

    def __post_init__(self) -> None:
        if not isinstance(self.trained, TrainedCandidateV1):
            raise TypeError("validated candidate must bind training evidence")
        if not isinstance(self.evidence, CandidatePartitionEvidenceV1):
            raise TypeError("validated candidate evidence must be typed")
        if self.evidence.partition is not StrategyPartitionV1.VALIDATION:
            raise ValueError("validated candidate requires validation evidence")
        if self.evidence.semantic_sha256 != self.trained.candidate.semantic_sha256:
            raise ValueError("training and validation candidate identities disagree")
        if not isinstance(self.qualification, QualificationDecisionV1):
            raise TypeError("validated candidate qualification must be typed")


@dataclass(frozen=True, slots=True)
class StrategySearchRunV1:
    manifest_sha256: str
    policy: SearchPolicyV1
    effective_budget: int
    stop_reason: SearchStopReasonV1
    evaluated: tuple[TrainedCandidateV1, ...]
    finalists: tuple[ValidatedCandidateV1, ...]
    training_star_semantic_sha256: str
    selected_semantic_sha256: str | None
    outcome: SearchOutcomeV1
    policy_trace: tuple[str, ...]
    real_partition_access_count: int

    def __post_init__(self) -> None:
        _require_sha256(self.manifest_sha256, "search run manifest digest")
        if not isinstance(self.policy, SearchPolicyV1):
            raise TypeError("search run policy must be typed")
        if type(self.effective_budget) is not int or not 1 <= self.effective_budget <= 64:
            raise ValueError("effective search budget must be in 1..64")
        if not isinstance(self.stop_reason, SearchStopReasonV1):
            raise TypeError("search stop reason must be typed")
        if type(self.evaluated) is not tuple or not self.evaluated or any(
            not isinstance(item, TrainedCandidateV1) for item in self.evaluated
        ):
            raise TypeError("search evaluated rows must be a nonempty typed tuple")
        identities = tuple(item.candidate.semantic_sha256 for item in self.evaluated)
        if len(identities) != len(set(identities)) or len(identities) > self.effective_budget:
            raise ValueError("search evaluations violate uniqueness or budget")
        if type(self.finalists) is not tuple or not self.finalists or len(self.finalists) > 8:
            raise ValueError("search finalist evidence must contain one to eight rows")
        finalist_ids = tuple(item.trained.candidate.semantic_sha256 for item in self.finalists)
        if any(item not in identities for item in finalist_ids):
            raise ValueError("search finalist was not trained")
        _require_sha256(self.training_star_semantic_sha256, "training-star digest")
        if self.training_star_semantic_sha256 != finalist_ids[0]:
            raise ValueError("training star must be the first frozen training finalist")
        if self.selected_semantic_sha256 is not None:
            _require_sha256(self.selected_semantic_sha256, "selected semantic digest")
        if not isinstance(self.outcome, SearchOutcomeV1):
            raise TypeError("search outcome must be typed")
        qualified = tuple(
            item.trained.candidate.semantic_sha256
            for item in self.finalists
            if item.qualification.qualified
        )
        if self.outcome is SearchOutcomeV1.CANDIDATE_SELECTED:
            if not qualified or self.selected_semantic_sha256 not in qualified:
                raise ValueError("selected outcome lacks a qualified finalist")
        elif self.selected_semantic_sha256 is not None or qualified:
            raise ValueError("no-candidate outcome cannot retain a qualified selection")
        if type(self.policy_trace) is not tuple or any(
            type(item) is not str or not item for item in self.policy_trace
        ):
            raise TypeError("search policy trace must be a text tuple")
        if self.real_partition_access_count != 0:
            raise ValueError("WO35-D search run exercised a real partition")

    @property
    def run_sha256(self) -> str:
        raw = canonical_identity_bytes(self.as_dict())
        digest = hashlib.sha256()
        digest.update(STRATEGY_SEARCH_RUN_DIGEST_DOMAIN_V1)
        digest.update(struct.pack(">Q", len(raw)))
        digest.update(raw)
        return digest.hexdigest()

    def as_dict(self) -> dict[str, object]:
        candidate_count = len(self.evaluated)
        return {
            "effective_budget": self.effective_budget,
            "evaluated": [
                {
                    "candidate": item.candidate.as_dict(),
                    "evidence": item.evidence.as_dict(candidate_count),
                }
                for item in self.evaluated
            ],
            "finalists": [
                {
                    "qualification": item.qualification.as_dict(),
                    "semantic_sha256": item.trained.candidate.semantic_sha256,
                    "validation_evidence": item.evidence.as_dict(candidate_count),
                }
                for item in self.finalists
            ],
            "manifest_sha256": self.manifest_sha256,
            "outcome": self.outcome.value,
            "policy": self.policy.value,
            "policy_trace": list(self.policy_trace),
            "real_partition_access_count": self.real_partition_access_count,
            "schema_id": STRATEGY_SEARCH_RUN_SCHEMA_ID_V1,
            "schema_version": 1,
            "selected_semantic_sha256": self.selected_semantic_sha256,
            "stop_reason": self.stop_reason.value,
            "training_star_semantic_sha256": self.training_star_semantic_sha256,
        }


class _SearchSession:
    def __init__(
        self,
        space: ControlledSearchSpaceV1,
        oracle: DevelopmentSyntheticScoreOracleV1,
        budget: int,
    ) -> None:
        self.space = space
        self.oracle = oracle
        self.budget = budget
        self.universe = space.universe()
        self.by_vector = space.candidate_by_vector_key()
        self.evaluated: list[TrainedCandidateV1] = []
        self.by_semantic: dict[str, TrainedCandidateV1] = {}
        self.trace: list[str] = []

    @property
    def budget_exhausted(self) -> bool:
        return len(self.evaluated) >= self.budget

    def evaluate(self, candidate: SearchCandidateV1) -> TrainedCandidateV1 | None:
        cached = self.by_semantic.get(candidate.semantic_sha256)
        if cached is not None:
            return cached
        if self.budget_exhausted:
            return None
        evidence = self.oracle.evaluate(
            candidate_id=candidate.candidate_id,
            semantic_sha256=candidate.semantic_sha256,
            vector_values=candidate.oracle_values,
            complexity_points=candidate.complexity_points,
            partition=StrategyPartitionV1.TRAIN,
        )
        row = TrainedCandidateV1(candidate, evidence)
        self.evaluated.append(row)
        self.by_semantic[candidate.semantic_sha256] = row
        return row

    def unseen_neighbors(self, vector: StrategyParameterVectorV1) -> tuple[SearchCandidateV1, ...]:
        return tuple(
            item
            for item in self.space.neighbors(
                vector,
                candidate_by_vector=self.by_vector,
            )
            if item.semantic_sha256 not in self.by_semantic
        )

    def neighbors(
        self,
        vector: StrategyParameterVectorV1,
    ) -> tuple[SearchCandidateV1, ...]:
        return self.space.neighbors(
            vector,
            candidate_by_vector=self.by_vector,
        )


def controlled_search_parameters() -> tuple[SearchParameterV1, ...]:
    return (
        SearchParameterV1(
            "/green/0/threshold_ticks",
            SearchParameterTypeV1.INTEGER,
            (1, 2, 3),
            2,
        ),
        SearchParameterV1(
            "/green/1/threshold_ppm",
            SearchParameterTypeV1.INTEGER,
            (100_000, 200_000, 300_000, 400_000),
            200_000,
        ),
        SearchParameterV1(
            "/wait/0/threshold_ticks",
            SearchParameterTypeV1.INTEGER,
            (2, 4, 6),
            4,
        ),
        SearchParameterV1(
            "/window_us",
            SearchParameterTypeV1.INTEGER,
            (2_000_000, 5_000_000, 10_000_000),
            5_000_000,
        ),
    )


def render_controlled_strategy_source(vector: StrategyParameterVectorV1) -> bytes:
    window = vector.value("/window_us")
    green = vector.value("/green/0/threshold_ticks")
    imbalance = vector.value("/green/1/threshold_ppm")
    wait = vector.value("/wait/0/threshold_ticks")
    if any(type(item) is not int for item in (window, green, imbalance, wait)):
        raise TypeError("controlled strategy values must be integers")
    if window not in {2_000_000, 5_000_000, 10_000_000}:
        raise ValueError("controlled strategy window is outside its domain")
    source = (
        "setup BOUNDED_BASE_V1\n"
        f"window {window // 1_000_000}s\n"
        "unavailable REFUSE\n"
        "GREEN when\n"
        f"spread_ticks <= {green}\n"
        f"book_imbalance >= {_ppm_decimal(imbalance)}\n"
        "WAIT when\n"
        f"spread_ticks <= {wait}\n"
        "RED otherwise\n"
    ).encode("utf-8")
    return source


def load_search_manifest(path: Path) -> StrategySearchManifestV1:
    if not isinstance(path, Path):
        raise TypeError("search manifest path must be pathlib.Path")
    return StrategySearchManifestV1.from_toml_bytes(path.read_bytes())


def run_development_search(
    manifest: StrategySearchManifestV1,
    *,
    policy: SearchPolicyV1 | None = None,
    cli_budget: int | None = None,
) -> StrategySearchRunV1:
    if not isinstance(manifest, StrategySearchManifestV1):
        raise TypeError("search requires a typed manifest")
    chosen_policy = manifest.policy if policy is None else policy
    if not isinstance(chosen_policy, SearchPolicyV1):
        raise TypeError("search policy must be typed")
    if cli_budget is None:
        cli_budget = manifest.budget
    if type(cli_budget) is not int or not 1 <= cli_budget <= MAX_SEARCH_BUDGET_V1:
        raise ValueError("CLI search budget must be in 1..64")
    effective_budget = min(cli_budget, manifest.budget, manifest.hard_budget, 64)
    mode = (
        SyntheticOracleModeV1.NO_WINNER
        if manifest.oracle_id == SYNTHETIC_ORACLE_NO_WINNER_ID_V1
        else SyntheticOracleModeV1.CONTROLLED
    )
    oracle = DevelopmentSyntheticScoreOracleV1(
        mode=mode,
        compatibility=manifest.compatibility,
        train_budget=effective_budget,
    )
    session = _SearchSession(manifest.search_space, oracle, effective_budget)
    stop_reason = _run_policy(session, chosen_policy)
    trained_count = len(session.evaluated)
    ranked = tuple(
        sorted(
            session.evaluated,
            key=lambda item: _final_training_rank_key(item, trained_count),
        )
    )
    frozen_training = ranked[: manifest.finalist_limit]
    oracle.freeze_validation(
        tuple(item.candidate.semantic_sha256 for item in frozen_training)
    )
    validation_rows: list[ValidatedCandidateV1] = []
    for item in frozen_training:
        evidence = oracle.evaluate(
            candidate_id=item.candidate.candidate_id,
            semantic_sha256=item.candidate.semantic_sha256,
            vector_values=item.candidate.oracle_values,
            complexity_points=item.candidate.complexity_points,
            partition=StrategyPartitionV1.VALIDATION,
        )
        validation_rows.append(
            ValidatedCandidateV1(
                item,
                evidence,
                validation_qualification(
                    evidence,
                    trained_candidate_count=trained_count,
                ),
            )
        )
    require_compatible_evidence(tuple(item.evidence for item in validation_rows))
    qualified = tuple(
        sorted(
            (item for item in validation_rows if item.qualification.qualified),
            key=lambda item: _validation_rank_key(item, trained_count),
        )
    )
    selected = qualified[0].trained.candidate.semantic_sha256 if qualified else None
    outcome = (
        SearchOutcomeV1.CANDIDATE_SELECTED
        if selected is not None
        else SearchOutcomeV1.NO_CANDIDATE_MET_CRITERIA
    )
    return StrategySearchRunV1(
        manifest_sha256=manifest.manifest_sha256,
        policy=chosen_policy,
        effective_budget=effective_budget,
        stop_reason=stop_reason,
        evaluated=tuple(session.evaluated),
        finalists=tuple(validation_rows),
        training_star_semantic_sha256=frozen_training[0].candidate.semantic_sha256,
        selected_semantic_sha256=selected,
        outcome=outcome,
        policy_trace=tuple(session.trace),
        real_partition_access_count=oracle.real_partition_access_count,
    )


def _run_policy(session: _SearchSession, policy: SearchPolicyV1) -> SearchStopReasonV1:
    if policy is SearchPolicyV1.GRID:
        return _run_grid(session)
    if policy is SearchPolicyV1.RANDOM:
        return _run_random(session)
    if policy is SearchPolicyV1.COORDINATE:
        return _run_coordinate(session)
    if policy is SearchPolicyV1.BEAM:
        return _run_beam(session)
    if policy is SearchPolicyV1.EVOLUTIONARY:
        return _run_evolutionary(session)
    raise AssertionError(policy)


def _run_grid(session: _SearchSession) -> SearchStopReasonV1:
    for candidate in session.universe:
        if session.budget_exhausted:
            session.trace.append("GRID:BUDGET")
            return SearchStopReasonV1.BUDGET_EXHAUSTED
        session.evaluate(candidate)
    session.trace.append("GRID:EXHAUSTED")
    return SearchStopReasonV1.UNIVERSE_EXHAUSTED


def _run_random(session: _SearchSession) -> SearchStopReasonV1:
    ordered = sorted(session.universe, key=_random_order_key)
    for candidate in ordered:
        if session.budget_exhausted:
            session.trace.append("RANDOM:BUDGET")
            return SearchStopReasonV1.BUDGET_EXHAUSTED
        session.evaluate(candidate)
    session.trace.append("RANDOM:EXHAUSTED")
    return SearchStopReasonV1.UNIVERSE_EXHAUSTED


def _run_coordinate(session: _SearchSession) -> SearchStopReasonV1:
    current_candidate = session.space.base_candidate()
    current_row: TrainedCandidateV1 | None = None
    stagnant = 0
    for pass_index in range(1, COORDINATE_MAX_PASSES_V1 + 1):
        moved_in_pass = False
        for parameter in session.space.parameters:
            candidates = _neighbors_for_parameter(
                session,
                current_candidate.vector,
                parameter.path,
            )
            evaluated: list[TrainedCandidateV1] = []
            for candidate in candidates:
                if candidate.semantic_sha256 in session.by_semantic:
                    evaluated.append(session.by_semantic[candidate.semantic_sha256])
                    continue
                if session.budget_exhausted:
                    session.trace.append(f"COORDINATE:pass={pass_index}:BUDGET")
                    return SearchStopReasonV1.BUDGET_EXHAUSTED
                row = session.evaluate(candidate)
                assert row is not None
                evaluated.append(row)
            if not evaluated:
                continue
            context = f"WO35/COORDINATE/pass={pass_index}/parameter={parameter.path}"
            best = min(evaluated, key=lambda item: _pre_stop_rank_key(item, context))
            current_merit = 0 if current_row is None else current_row.training_merit
            if best.training_merit >= current_merit + MATERIAL_MOVE_V1:
                current_row = best
                current_candidate = best.candidate
                moved_in_pass = True
                session.trace.append(
                    f"COORDINATE:pass={pass_index}:move={parameter.path}:"
                    f"{best.candidate.candidate_id}"
                )
        if moved_in_pass:
            stagnant = 0
        else:
            stagnant += 1
            session.trace.append(f"COORDINATE:pass={pass_index}:NO_MOVE")
        if stagnant >= 2:
            return SearchStopReasonV1.TWO_STAGNANT_PASSES
    return (
        SearchStopReasonV1.BUDGET_EXHAUSTED
        if session.budget_exhausted
        else SearchStopReasonV1.PASS_LIMIT
    )


def _run_beam(session: _SearchSession) -> SearchStopReasonV1:
    beam: tuple[SearchCandidateV1, ...] = (session.space.base_candidate(),)
    all_time_best = 0
    stagnant = 0
    for depth in range(1, BEAM_MAX_DEPTH_V1 + 1):
        context = f"WO35/BEAM/depth={depth}"
        if depth > 1:
            beam = tuple(
                item.candidate
                for item in sorted(
                    (session.by_semantic[item.semantic_sha256] for item in beam),
                    key=lambda row: _pre_stop_rank_key(row, context),
                )
            )
        children: list[TrainedCandidateV1] = []
        child_seen: set[str] = set()
        for parent in beam:
            for child in session.neighbors(parent.vector):
                if child.semantic_sha256 in child_seen:
                    continue
                child_seen.add(child.semantic_sha256)
                if child.semantic_sha256 in session.by_semantic:
                    continue
                if session.budget_exhausted:
                    session.trace.append(f"BEAM:depth={depth}:BUDGET")
                    return SearchStopReasonV1.BUDGET_EXHAUSTED
                row = session.evaluate(child)
                assert row is not None
                children.append(row)
        if not children:
            session.trace.append(f"BEAM:depth={depth}:EMPTY")
            return SearchStopReasonV1.EMPTY_EXPANSION
        ranked = sorted(children, key=lambda item: _pre_stop_rank_key(item, context))
        beam = tuple(item.candidate for item in ranked[:BEAM_WIDTH_V1])
        best_merit = ranked[0].training_merit
        improved = best_merit >= all_time_best + MATERIAL_MOVE_V1
        all_time_best = max(all_time_best, best_merit)
        if improved:
            stagnant = 0
        else:
            stagnant += 1
        session.trace.append(
            f"BEAM:depth={depth}:best={best_merit}:stagnant={stagnant}:width={len(beam)}"
        )
        if stagnant >= 2:
            return SearchStopReasonV1.TWO_STAGNANT_DEPTHS
    return (
        SearchStopReasonV1.BUDGET_EXHAUSTED
        if session.budget_exhausted
        else SearchStopReasonV1.DEPTH_LIMIT
    )


def _run_evolutionary(session: _SearchSession) -> SearchStopReasonV1:
    initial_count = min(EVOLUTION_INITIAL_SIZE_V1, session.budget, len(session.universe))
    initial = sorted(session.universe, key=_evolution_initial_key)[:initial_count]
    population_rows: list[TrainedCandidateV1] = []
    for candidate in initial:
        row = session.evaluate(candidate)
        assert row is not None
        population_rows.append(row)
    if not population_rows:
        return SearchStopReasonV1.EMPTY_POPULATION
    context = "WO35/EVOLUTION/TRAINING/generation=0"
    population_rows.sort(key=lambda item: _pre_stop_rank_key(item, context))
    all_time_best = population_rows[0].training_merit
    stagnant = 0
    session.trace.append(
        f"EVOLUTION:generation=0:population={len(population_rows)}:best={all_time_best}"
    )
    for generation in range(1, EVOLUTION_MAX_GENERATIONS_V1):
        context = f"WO35/EVOLUTION/TRAINING/generation={generation}"
        population_rows.sort(key=lambda item: _pre_stop_rank_key(item, context))
        elites = population_rows[: min(EVOLUTION_ELITE_LIMIT_V1, len(population_rows))]
        children: list[TrainedCandidateV1] = []
        generation_complete = True
        for child_slot in range(EVOLUTION_CHILD_LIMIT_V1):
            if session.budget_exhausted:
                generation_complete = False
                break
            child = _evolution_child(
                session,
                tuple(population_rows),
                generation,
                child_slot,
                context,
            )
            if child is not None:
                children.append(child)
        next_population = elites + children
        if not next_population:
            return SearchStopReasonV1.EMPTY_POPULATION
        population_rows = next_population
        if not generation_complete:
            session.trace.append(f"EVOLUTION:generation={generation}:BUDGET")
            return SearchStopReasonV1.BUDGET_EXHAUSTED
        population_rows.sort(key=lambda item: _pre_stop_rank_key(item, context))
        best_merit = population_rows[0].training_merit
        improved = best_merit >= all_time_best + MATERIAL_MOVE_V1
        all_time_best = max(all_time_best, best_merit)
        if improved:
            stagnant = 0
        else:
            stagnant += 1
        session.trace.append(
            f"EVOLUTION:generation={generation}:population={len(population_rows)}:"
            f"children={len(children)}:best={best_merit}:stagnant={stagnant}"
        )
        if stagnant >= 2:
            return SearchStopReasonV1.TWO_STAGNANT_GENERATIONS
        if not children:
            return SearchStopReasonV1.UNIVERSE_EXHAUSTED
    return SearchStopReasonV1.GENERATION_LIMIT


def _evolution_child(
    session: _SearchSession,
    population: tuple[TrainedCandidateV1, ...],
    generation: int,
    child_slot: int,
    context: str,
) -> TrainedCandidateV1 | None:
    tournament = sorted(
        population,
        key=lambda item: _tournament_hash(item.candidate, generation, child_slot),
    )
    if len(tournament) == 1:
        parents = tournament
    else:
        first_two = tournament[:2]
        winner, runner_up = sorted(
            first_two,
            key=lambda item: _pre_stop_rank_key(item, context),
        )
        remaining = sorted(
            tournament[2:],
            key=lambda item: _pre_stop_rank_key(item, context),
        )
        parents = (winner, runner_up, *remaining)
    for parent in parents:
        neighbors = sorted(
            session.neighbors(parent.candidate.vector),
            key=lambda item: _tournament_hash(item, generation, child_slot),
        )
        for candidate in neighbors:
            if candidate.semantic_sha256 in session.by_semantic:
                continue
            row = session.evaluate(candidate)
            if row is not None:
                return row
            return None
    return None


def _pre_stop_rank_key(row: TrainedCandidateV1, context: str) -> tuple[object, ...]:
    statistic = row.evidence.statistic(0)
    return (
        -row.training_merit,
        -statistic.median_delta,
        row.candidate.complexity_points,
        common_tie_digest(context_id=context, semantic_sha256=row.candidate.semantic_sha256),
        row.candidate.stable_id_bytes,
    )


def _final_training_rank_key(
    row: TrainedCandidateV1,
    trained_count: int,
) -> tuple[object, ...]:
    statistic = row.evidence.statistic(trained_count)
    return (
        -statistic.statistic,
        -statistic.median_delta,
        row.candidate.complexity_points,
        common_tie_digest(
            context_id="WO35/TRAINING_FINALISTS",
            semantic_sha256=row.candidate.semantic_sha256,
        ),
        row.candidate.stable_id_bytes,
    )


def _validation_rank_key(
    row: ValidatedCandidateV1,
    trained_count: int,
) -> tuple[object, ...]:
    validation = row.evidence.statistic(trained_count)
    training = row.trained.evidence.statistic(trained_count)
    candidate = row.trained.candidate
    return (
        -validation.statistic,
        -validation.median_delta,
        -training.statistic,
        candidate.complexity_points,
        common_tie_digest(
            context_id="WO35/VALIDATION_RANKING",
            semantic_sha256=candidate.semantic_sha256,
        ),
        candidate.stable_id_bytes,
    )


def _random_order_key(candidate: SearchCandidateV1) -> tuple[bytes, bytes, bytes]:
    digest = hashlib.sha256()
    digest.update(b"WO35_RANDOM_V1\x00")
    digest.update(struct.pack(">Q", RANDOM_ROOT_SEED_V1))
    digest.update(b"\x00")
    digest.update(bytes.fromhex(candidate.semantic_sha256))
    return digest.digest(), bytes.fromhex(candidate.semantic_sha256), candidate.stable_id_bytes


def _evolution_initial_key(candidate: SearchCandidateV1) -> tuple[bytes, bytes, bytes]:
    vector = candidate.vector.canonical_bytes
    digest = hashlib.sha256()
    digest.update(b"WO35_EVOLUTION_INITIAL_V1\x00")
    digest.update(struct.pack(">Q", EVOLUTION_ROOT_SEED_V1))
    digest.update(b"\x00")
    digest.update(bytes.fromhex(candidate.semantic_sha256))
    digest.update(struct.pack(">I", len(vector)))
    digest.update(vector)
    return digest.digest(), bytes.fromhex(candidate.semantic_sha256), candidate.stable_id_bytes


def _tournament_hash(
    candidate: SearchCandidateV1,
    generation: int,
    child_slot: int,
) -> tuple[bytes, bytes, bytes]:
    vector = candidate.vector.canonical_bytes
    digest = hashlib.sha256()
    digest.update(b"WO35_TOURNAMENT_V1\x00")
    digest.update(struct.pack(">Q", EVOLUTION_ROOT_SEED_V1))
    digest.update(struct.pack(">I", generation))
    digest.update(struct.pack(">I", child_slot))
    digest.update(bytes.fromhex(candidate.semantic_sha256))
    digest.update(struct.pack(">I", len(vector)))
    digest.update(vector)
    return digest.digest(), bytes.fromhex(candidate.semantic_sha256), candidate.stable_id_bytes


def _neighbors_for_parameter(
    session: _SearchSession,
    vector: StrategyParameterVectorV1,
    parameter_path: str,
) -> tuple[SearchCandidateV1, ...]:
    values = list(_vector_key(vector))
    parameter_index = next(
        index
        for index, item in enumerate(session.space.parameters)
        if item.path == parameter_path
    )
    parameter = session.space.parameters[parameter_index]
    value_index = parameter.domain.index(values[parameter_index])
    rows: list[SearchCandidateV1] = []
    for adjacent in (value_index - 1, value_index + 1):
        if not 0 <= adjacent < len(parameter.domain):
            continue
        changed = list(values)
        changed[parameter_index] = parameter.domain[adjacent]
        candidate = session.by_vector.get(tuple(changed))
        if candidate is not None:
            rows.append(candidate)
    return tuple(rows)


def _validate_manifest_payload(payload: object) -> None:
    top = _exact_object(
        payload,
        {
            "base_strategy_source_sha256",
            "budget",
            "complexity",
            "evidence_group_id",
            "expected_outcome",
            "experiment_id",
            "finalist_limit",
            "hard_budget",
            "holdout",
            "multiplicity",
            "objective_group_id",
            "objectives",
            "oracle_id",
            "partition_manifest_sha256",
            "partitions",
            "policy",
            "policy_version",
            "practical_effect",
            "protocol_id",
            "real_partition_execution",
            "robustness",
            "scenario_group_id",
            "schema_id",
            "schema_version",
            "search_space",
            "source_ancestry_sha256",
            "stopping",
            "tie_break",
            "uncertainty",
            "validation",
        },
        "search manifest",
    )
    if _text(top, "schema_id") != STRATEGY_SEARCH_MANIFEST_SCHEMA_ID_V1:
        raise ValueError("unsupported search manifest schema ID")
    if _integer(top, "schema_version") != 1:
        raise ValueError("unsupported search manifest schema version")
    exact_scalars = {
        "protocol_id": CONTROLLED_PROTOCOL_ID_V1,
        "policy": SearchPolicyV1.GRID.value,
        "policy_version": STRATEGY_DISCOVERY_POLICY_VERSION_V1,
        "base_strategy_source_sha256": CONTROLLED_BASE_SOURCE_SHA256_V1,
    }
    for key, expected in exact_scalars.items():
        if _text(top, key) != expected:
            raise ValueError(f"search manifest {key} differs from the fixed protocol")
    if _integer(top, "budget") != 64 or _integer(top, "hard_budget") != 64:
        raise ValueError("search manifest budget must be exactly 64")
    if _integer(top, "finalist_limit") != 8:
        raise ValueError("search manifest finalist limit must be exactly eight")
    if top["real_partition_execution"] is not False:
        raise ValueError("WO35-D manifest must forbid real partition execution")
    _require_sha256(_text(top, "partition_manifest_sha256"), "partition manifest digest")
    _require_sha256(_text(top, "source_ancestry_sha256"), "source ancestry digest")

    partitions = _exact_object(
        top["partitions"],
        {"adversarial", "holdout", "robustness", "train", "validation"},
        "partition roots",
    )
    for key, expected in _EXPECTED_ROOTS.items():
        if partitions[key] != expected:
            raise ValueError(f"search manifest {key} roots differ from section 5.7.6")

    search_space = _exact_object(top["search_space"], {"order", "parameters"}, "search space")
    if _text(search_space, "order") != STRATEGY_VECTOR_ORDER_V1:
        raise ValueError("search-space order differs from the fixed vector order")
    parameter_rows = search_space["parameters"]
    if type(parameter_rows) is not list or any(type(item) is not dict for item in parameter_rows):
        raise TypeError("search-space parameters must be TOML tables")
    expected_parameters = [item.as_dict() for item in controlled_search_parameters()]
    if parameter_rows != expected_parameters:
        raise ValueError("search-space parameters differ from the controlled grid")

    objectives = _exact_object(
        top["objectives"],
        {
            "aggregation",
            "optional",
            "protocol_sha256",
            "required",
            "utility_rules",
            "weights",
        },
        "objectives",
    )
    protocol_digest = hashlib.sha256(
        canonical_identity_bytes(objective_protocol_projection())
    ).hexdigest()
    if _text(objectives, "protocol_sha256") != protocol_digest:
        raise ValueError("objective protocol digest differs from the executable inventory")
    if objectives["required"] != [item.objective_id.value for item in REQUIRED_OBJECTIVE_SPECS_V1]:
        raise ValueError("required objective inventory differs from section 5.7.6")
    if objectives["optional"] != ["PNL"]:
        raise ValueError("P&L must be the sole optional zero-weight objective")
    if objectives["weights"] != [item.weight for item in ALL_OBJECTIVE_SPECS_V1]:
        raise ValueError("objective weights differ from section 5.7.6")
    if objectives["utility_rules"] != [item.utility_rule for item in ALL_OBJECTIVE_SPECS_V1]:
        raise ValueError("objective utility rules differ from section 5.7.6")
    if _text(objectives, "aggregation") != "ROOT_COMPOSITE_THEN_MEDIAN_DELTA_MAD_V1":
        raise ValueError("objective aggregation differs from section 5.7.6")

    _require_exact_section(
        top["validation"],
        {
            "access_schedule_id": "VALIDATION_ONCE_AFTER_FINALISTS_V1",
            "max_access_count": 1,
            "root_count": 8,
            "selection_rule": "QUALIFY_THEN_VALIDATION_STATISTIC_MEDIAN_TRAIN_COMPLEXITY_TIE_V1",
        },
        "validation",
    )
    _require_exact_section(
        top["practical_effect"],
        {
            "candidate_trades_min": 30,
            "component_floor": -50_000,
            "classification_or_opportunity_min": 50_000,
            "positive_roots_min": 6,
            "sensitive_component_floor": -20_000,
            "statistic_min": 30_000,
            "trade_ratio_max_ppm": 1_600_000,
            "trade_ratio_min_ppm": 600_000,
        },
        "practical effect",
    )
    _require_exact_section(
        top["uncertainty"],
        {"method": UNCERTAINTY_METHOD_V1, "reduction_order": ROOT_REDUCTION_ORDER_V1},
        "uncertainty",
    )
    _require_exact_section(
        top["multiplicity"],
        {
            "candidate_count": "UNIQUE_VALID_NON_BASE_TRAINED_V1",
            "method": MULTIPLICITY_METHOD_V1,
            "penalty_step": 5_000,
        },
        "multiplicity",
    )
    _require_exact_section(
        top["complexity"],
        {
            "coefficients": [4, 3, 8, 6, 2, 1],
            "dimensions": [
                "conditions",
                "features",
                "states",
                "transitions",
                "rolling_windows",
                "parameters",
            ],
            "material_equivalence": "PREFER_LOWER_COMPLEXITY_V1",
            "normalization_points": 200,
        },
        "complexity",
    )
    _require_exact_section(
        top["robustness"],
        {
            "applicable_family_count": 7,
            "component_median_floor": -50_000,
            "family_median_floor": -20_000,
            "mandatory_families": [
                "THRESHOLD",
                "ROLLING_WINDOW",
                "LATENCY",
                "FEES",
                "VOLUME",
                "LIQUIDITY",
                "REGIME_MIX",
            ],
            "minimum_cell": -75_000,
            "nonnegative_fraction_denominator": 4,
            "nonnegative_fraction_numerator": 3,
            "sensitive_component_floor": -20_000,
            "trade_ratio_max_ppm": 1_600_000,
            "trade_ratio_min_ppm": 600_000,
            "venue_mix": "NOT_APPLICABLE_SINGLE_VENUE_V1",
        },
        "robustness",
    )
    _require_exact_section(
        top["holdout"],
        {
            "adversarial_rule": "FULL_VALIDATION_RULE_UNCHANGED_V1",
            "mutation_after_result": "FORBIDDEN",
            "reveal_policy": "ONE_ATOMIC_TOKEN_AFTER_ROBUSTNESS_V1",
            "rerun": "FORBIDDEN",
        },
        "holdout",
    )
    _require_exact_section(
        top["stopping"],
        {
            "beam": "DEPTH_4_EMPTY_OR_TWO_NONIMPROVING_10000_V1",
            "coordinate": "PASSES_4_OR_TWO_NO_MOVE_10000_V1",
            "evolutionary": "GENERATIONS_8_OR_TWO_NONIMPROVING_10000_V1",
            "grid": "BUDGET_OR_EXHAUSTION_V1",
            "random": "BUDGET_OR_EXHAUSTION_V1",
        },
        "stopping",
    )
    _require_exact_section(
        top["tie_break"],
        {
            "final_training_context": "WO35/TRAINING_FINALISTS",
            "policy": STRATEGY_DISCOVERY_POLICY_VERSION_V1,
            "root_seed": 3_599_001,
            "validation_context": "WO35/VALIDATION_RANKING",
        },
        "tie break",
    )


def _require_exact_section(payload: object, expected: dict[str, object], label: str) -> None:
    row = _exact_object(payload, set(expected), label)
    if row != expected:
        raise ValueError(f"search manifest {label} differs from the frozen protocol")


def _exact_object(payload: object, keys: set[str], label: str) -> dict[str, object]:
    if type(payload) is not dict:
        raise TypeError(f"{label} must be an object")
    actual = set(payload)
    if actual != keys:
        raise ValueError(
            f"{label} fields differ: missing={sorted(keys - actual)} extra={sorted(actual - keys)}"
        )
    return payload


def _object(payload: dict[str, object], key: str) -> dict[str, object]:
    value = payload[key]
    if type(value) is not dict:
        raise TypeError(f"search manifest {key} must be an object")
    return value


def _text(payload: dict[str, object], key: str) -> str:
    value = payload[key]
    if type(value) is not str or not value:
        raise ValueError(f"search manifest {key} must be nonempty text")
    _require_nfc(value, f"search manifest {key}")
    return value


def _integer(payload: dict[str, object], key: str) -> int:
    value = payload[key]
    if type(value) is not int:
        raise TypeError(f"search manifest {key} must be an integer")
    return value


def _vector_key(vector: StrategyParameterVectorV1) -> tuple[object, ...]:
    return tuple(item.value for item in vector.entries)


def _domain_key(type_tag: SearchParameterTypeV1, value: object) -> object:
    if type_tag is SearchParameterTypeV1.BOOLEAN:
        return int(bool(value))
    if type_tag is SearchParameterTypeV1.INTEGER:
        return value
    assert type(value) is str
    return value.encode("utf-8")


def _validate_parameter_value(type_tag: SearchParameterTypeV1, value: object) -> None:
    if type_tag is SearchParameterTypeV1.BOOLEAN:
        if type(value) is not bool:
            raise TypeError("Boolean search parameter requires JSON Booleans")
    elif type_tag is SearchParameterTypeV1.INTEGER:
        if type(value) is not int:
            raise TypeError("integer search parameter requires integers")
    else:
        if type(value) is not str or not value:
            raise ValueError("enum search parameter requires nonempty text")
        _require_nfc(value, "enum parameter value")


def _ppm_decimal(value: object) -> str:
    if type(value) is not int or not 0 <= value <= 1_000_000:
        raise ValueError("threshold ppm must be an integer in 0..S")
    whole, remainder = divmod(value, 1_000_000)
    if remainder == 0:
        return str(whole)
    return f"{whole}.{remainder:06d}".rstrip("0")


def _require_sha256(value: str, label: str) -> None:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{label} must be lowercase SHA-256")


def _require_nfc(value: str, label: str) -> None:
    if type(value) is not str or not value:
        raise ValueError(f"{label} must be nonempty text")
    if unicodedata.normalize("NFC", value) != value:
        raise ValueError(f"{label} must already be NFC")


if hashlib.sha256(_CONTROLLED_BASE_SOURCE).hexdigest() != CONTROLLED_BASE_SOURCE_SHA256_V1:
    raise RuntimeError("controlled base strategy source differs from section 5.7.6")


__all__ = [
    "BEAM_MAX_DEPTH_V1",
    "BEAM_WIDTH_V1",
    "CONTROLLED_BASE_SOURCE_SHA256_V1",
    "CONTROLLED_PROTOCOL_ID_V1",
    "ControlledSearchSpaceV1",
    "EVOLUTION_MAX_GENERATIONS_V1",
    "FINALIST_LIMIT_V1",
    "MAX_SEARCH_BUDGET_V1",
    "SearchCandidateV1",
    "SearchOutcomeV1",
    "SearchParameterTypeV1",
    "SearchParameterV1",
    "SearchPolicyV1",
    "SearchStopReasonV1",
    "STRATEGY_SEARCH_MANIFEST_SCHEMA_ID_V1",
    "STRATEGY_SEARCH_RUN_SCHEMA_ID_V1",
    "STRATEGY_VECTOR_ORDER_V1",
    "STRATEGY_VECTOR_SCHEMA_ID_V1",
    "StrategyParameterVectorV1",
    "StrategySearchManifestV1",
    "StrategySearchRunV1",
    "TrainedCandidateV1",
    "ValidatedCandidateV1",
    "VectorEntryV1",
    "controlled_search_parameters",
    "load_search_manifest",
    "render_controlled_strategy_source",
    "run_development_search",
]
