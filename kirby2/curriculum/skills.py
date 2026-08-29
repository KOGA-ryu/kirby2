"""Immutable WO34-A skill graph and prerequisite contracts."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

SKILL_GRAPH_SCHEMA_VERSION_V1 = 1
SKILL_GRAPH_ID_V1 = "SKILL_GRAPH_V1"
PREREQUISITE_POLICY_ID_V1 = "PREREQUISITE_READY_V1"
PREREQUISITE_MASTERY_MIN_PPM_V1 = 650_000
PREREQUISITE_CONFIDENCE_MIN_PPM_V1 = 500_000
STABLE_SKILL_REGISTRY_SHA256_V1 = (
    "d05c720fe82f66e61047cc503f2bb086ff853495e32df25d985cf563c5f99c68"
)
CURRICULUM_SKILL_IDS_V1 = (
    "ABSORPTION_RECOGNITION",
    "ADVERSE_SELECTION",
    "AGGRESSIVE_ENTRY",
    "AUCTION_EXECUTION",
    "BOOK_READING",
    "CANCEL_TIMING",
    "EXIT_EXECUTION",
    "HALT_REOPENING",
    "HIDDEN_LIQUIDITY",
    "HOTKEY_ACCURACY",
    "LATENCY_AWARENESS",
    "LIQUIDITY_WITHDRAWAL",
    "MULTI_VENUE_ROUTING",
    "PARTIAL_FILL_MANAGEMENT",
    "PASSIVE_ENTRY",
    "POSITION_MANAGEMENT",
    "QUEUE_POSITION",
    "REGIME_RECOGNITION",
    "REPLACE_TIMING",
    "SCRIPT_DISCIPLINE",
    "SPREAD_DECISION",
    "TAPE_READING",
    "VOLUME_CONTEXT",
)
_CURRICULUM_SKILL_ID_SET_V1 = frozenset(CURRICULUM_SKILL_IDS_V1)
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_IDENTIFIER = re.compile(r"[A-Z][A-Z0-9_]{0,95}\Z")


def canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")


def sha256_json(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _canonical_object(raw: bytes, label: str) -> dict[str, object]:
    if type(raw) is not bytes:
        raise TypeError(f"{label} requires exact bytes")
    try:
        payload = json.loads(raw.decode("ascii"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"{label} must be canonical ASCII JSON") from error
    if not isinstance(payload, dict):
        raise TypeError(f"{label} must contain an object")
    try:
        canonical = canonical_json_bytes(payload)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{label} contains a non-canonical value") from error
    if canonical != raw:
        raise ValueError(f"{label} is not canonical JSON")
    return payload


def _exact_text(value: object, label: str) -> str:
    if type(value) is not str:
        raise TypeError(f"{label} must be exact text")
    return value


def _exact_int(value: object, label: str) -> int:
    if type(value) is not int:
        raise TypeError(f"{label} must be an exact integer")
    return value


def require_stable_skill_v1(skill_id: str) -> str:
    """Validate a curriculum skill against the closed WO34-A registry."""

    if type(skill_id) is not str or skill_id not in _CURRICULUM_SKILL_ID_SET_V1:
        raise ValueError(f"UNKNOWN_SKILL_REFERENCE: {skill_id}")
    return skill_id


@dataclass(frozen=True, slots=True)
class PrerequisiteReadinessPolicyV1:
    policy_id: str = PREREQUISITE_POLICY_ID_V1
    sufficient_evidence_required: bool = True
    mastery_min_ppm: int = PREREQUISITE_MASTERY_MIN_PPM_V1
    confidence_min_ppm: int = PREREQUISITE_CONFIDENCE_MIN_PPM_V1

    def __post_init__(self) -> None:
        if (
            self.policy_id != PREREQUISITE_POLICY_ID_V1
            or self.sufficient_evidence_required is not True
            or self.mastery_min_ppm != PREREQUISITE_MASTERY_MIN_PPM_V1
            or self.confidence_min_ppm != PREREQUISITE_CONFIDENCE_MIN_PPM_V1
        ):
            raise ValueError("WO34 prerequisite-readiness policy differs")

    def as_dict(self) -> dict[str, object]:
        return {
            "confidence_min_ppm": self.confidence_min_ppm,
            "mastery_min_ppm": self.mastery_min_ppm,
            "policy_id": self.policy_id,
            "sufficient_evidence_required": self.sufficient_evidence_required,
        }


@dataclass(frozen=True, slots=True)
class SkillPrerequisiteEdgeV1:
    prerequisite_skill_id: str
    dependent_skill_id: str
    rationale_id: str
    policy_id: str = PREREQUISITE_POLICY_ID_V1

    def __post_init__(self) -> None:
        for value, label in (
            (self.prerequisite_skill_id, "prerequisite skill"),
            (self.dependent_skill_id, "dependent skill"),
            (self.rationale_id, "prerequisite rationale"),
            (self.policy_id, "prerequisite policy"),
        ):
            if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
                raise ValueError(f"{label} must be a stable identifier")
        if self.prerequisite_skill_id == self.dependent_skill_id:
            raise ValueError("skill prerequisite edge cannot be self-referential")
        require_stable_skill_v1(self.prerequisite_skill_id)
        require_stable_skill_v1(self.dependent_skill_id)
        if self.policy_id != PREREQUISITE_POLICY_ID_V1:
            raise ValueError("skill edge uses an unregistered readiness policy")

    @property
    def sort_key(self) -> tuple[bytes, bytes]:
        return (
            self.prerequisite_skill_id.encode("utf-8"),
            self.dependent_skill_id.encode("utf-8"),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "dependent_skill_id": self.dependent_skill_id,
            "policy_id": self.policy_id,
            "prerequisite_skill_id": self.prerequisite_skill_id,
            "rationale_id": self.rationale_id,
        }

    @classmethod
    def from_dict(cls, payload: object) -> SkillPrerequisiteEdgeV1:
        if not isinstance(payload, dict) or set(payload) != {
            "dependent_skill_id",
            "policy_id",
            "prerequisite_skill_id",
            "rationale_id",
        }:
            raise ValueError("skill prerequisite edge fields differ")
        return cls(
            prerequisite_skill_id=_exact_text(
                payload["prerequisite_skill_id"],
                "prerequisite skill ID",
            ),
            dependent_skill_id=_exact_text(
                payload["dependent_skill_id"],
                "dependent skill ID",
            ),
            rationale_id=_exact_text(
                payload["rationale_id"],
                "prerequisite rationale ID",
            ),
            policy_id=_exact_text(payload["policy_id"], "prerequisite policy ID"),
        )


_EXACT_EDGES_V1 = (
    SkillPrerequisiteEdgeV1(
        "BOOK_READING",
        "HIDDEN_LIQUIDITY",
        "HIDDEN_LIQUIDITY_INFERENCE_REQUIRES_BOOK_READING",
    ),
    SkillPrerequisiteEdgeV1(
        "LATENCY_AWARENESS",
        "CANCEL_TIMING",
        "CANCEL_RACE_TIMING_REQUIRES_LATENCY_AWARENESS",
    ),
    SkillPrerequisiteEdgeV1(
        "QUEUE_POSITION",
        "MULTI_VENUE_ROUTING",
        "ADVANCED_PASSIVE_ROUTING_REQUIRES_QUEUE_POSITION",
    ),
    SkillPrerequisiteEdgeV1(
        "TAPE_READING",
        "ABSORPTION_RECOGNITION",
        "ABSORPTION_INTERPRETATION_REQUIRES_TAPE_READING",
    ),
)


def _assert_acyclic(
    skills: tuple[str, ...],
    edges: tuple[SkillPrerequisiteEdgeV1, ...],
) -> None:
    remaining = {skill: 0 for skill in skills}
    children = {skill: [] for skill in skills}
    for edge in edges:
        remaining[edge.dependent_skill_id] += 1
        children[edge.prerequisite_skill_id].append(edge.dependent_skill_id)
    ready = sorted(skill for skill, count in remaining.items() if count == 0)
    visited = 0
    while ready:
        skill = ready.pop(0)
        visited += 1
        for child in sorted(children[skill]):
            remaining[child] -= 1
            if remaining[child] == 0:
                ready.append(child)
                ready.sort()
    if visited != len(skills):
        raise ValueError("skill prerequisite graph contains a cycle")


@dataclass(frozen=True, slots=True)
class SkillGraphV1:
    skills: tuple[str, ...]
    edges: tuple[SkillPrerequisiteEdgeV1, ...]
    roots: tuple[str, ...]
    readiness_policy: PrerequisiteReadinessPolicyV1
    graph_id: str = SKILL_GRAPH_ID_V1
    graph_version: int = 1
    skill_registry_sha256: str = STABLE_SKILL_REGISTRY_SHA256_V1
    previous_graph_sha256: str | None = None
    schema_version: int = SKILL_GRAPH_SCHEMA_VERSION_V1

    def __post_init__(self) -> None:
        if (
            self.graph_id != SKILL_GRAPH_ID_V1
            or type(self.graph_version) is not int
            or self.graph_version != 1
            or type(self.schema_version) is not int
            or self.schema_version != SKILL_GRAPH_SCHEMA_VERSION_V1
            or self.previous_graph_sha256 is not None
        ):
            raise ValueError("skill graph version metadata differs")
        if self.skill_registry_sha256 != STABLE_SKILL_REGISTRY_SHA256_V1:
            raise ValueError("skill graph is not bound to the stable skill registry")
        if self.skills != CURRICULUM_SKILL_IDS_V1:
            raise ValueError("skill graph node inventory differs from the 23 stable IDs")
        if any(not isinstance(edge, SkillPrerequisiteEdgeV1) for edge in self.edges):
            raise TypeError("skill graph edge is untyped")
        if self.edges != tuple(sorted(self.edges, key=lambda edge: edge.sort_key)):
            raise ValueError("skill graph edges are not canonically ordered")
        edge_keys = tuple(
            (edge.prerequisite_skill_id, edge.dependent_skill_id)
            for edge in self.edges
        )
        if len(edge_keys) != len(set(edge_keys)):
            raise ValueError("skill graph contains a duplicate edge")
        _assert_acyclic(self.skills, self.edges)
        computed_roots = tuple(
            skill
            for skill in self.skills
            if skill not in {edge.dependent_skill_id for edge in self.edges}
        )
        if self.roots != computed_roots:
            raise ValueError("skill graph roots differ from its incoming edges")
        if self.edges != _EXACT_EDGES_V1:
            raise ValueError("SKILL_GRAPH_V1 edge inventory differs")
        if not isinstance(self.readiness_policy, PrerequisiteReadinessPolicyV1):
            raise TypeError("skill graph readiness policy is invalid")

    def prerequisites(self, skill_id: str) -> tuple[str, ...]:
        require_stable_skill_v1(skill_id)
        return tuple(
            edge.prerequisite_skill_id
            for edge in self.edges
            if edge.dependent_skill_id == skill_id
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "edges": [edge.as_dict() for edge in self.edges],
            "graph_id": self.graph_id,
            "graph_version": self.graph_version,
            "previous_graph_sha256": self.previous_graph_sha256,
            "readiness_policy": self.readiness_policy.as_dict(),
            "record_kind": "IMMUTABLE_SKILL_GRAPH_V1",
            "roots": list(self.roots),
            "schema_version": self.schema_version,
            "skill_registry_sha256": self.skill_registry_sha256,
            "skills": list(self.skills),
        }

    @property
    def graph_sha256(self) -> str:
        return sha256_json(self.as_dict())

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())

    @classmethod
    def from_json_bytes(cls, raw: bytes) -> SkillGraphV1:
        graph = cls.from_dict(_canonical_object(raw, "skill graph"))
        if graph.canonical_bytes() != raw:
            raise ValueError("skill graph changed during restoration")
        return graph

    @classmethod
    def from_dict(cls, payload: object) -> SkillGraphV1:
        if not isinstance(payload, dict) or set(payload) != {
            "edges",
            "graph_id",
            "graph_version",
            "previous_graph_sha256",
            "readiness_policy",
            "record_kind",
            "roots",
            "schema_version",
            "skill_registry_sha256",
            "skills",
        }:
            raise ValueError("skill graph fields differ")
        if payload["record_kind"] != "IMMUTABLE_SKILL_GRAPH_V1":
            raise ValueError("skill graph record kind differs")
        raw_edges = payload["edges"]
        raw_skills = payload["skills"]
        raw_roots = payload["roots"]
        raw_policy = payload["readiness_policy"]
        if (
            not isinstance(raw_edges, list)
            or not isinstance(raw_skills, list)
            or not isinstance(raw_roots, list)
            or not isinstance(raw_policy, dict)
            or set(raw_policy) != {
                "confidence_min_ppm",
                "mastery_min_ppm",
                "policy_id",
                "sufficient_evidence_required",
            }
        ):
            raise TypeError("skill graph nested records are invalid")
        if (
            type(raw_policy["sufficient_evidence_required"]) is not bool
            or any(type(item) is not str for item in raw_skills)
            or any(type(item) is not str for item in raw_roots)
        ):
            raise TypeError("skill graph primitive values are invalid")
        policy = PrerequisiteReadinessPolicyV1(
            policy_id=_exact_text(raw_policy["policy_id"], "readiness policy ID"),
            sufficient_evidence_required=raw_policy[
                "sufficient_evidence_required"
            ],
            mastery_min_ppm=_exact_int(
                raw_policy["mastery_min_ppm"],
                "readiness mastery minimum",
            ),
            confidence_min_ppm=_exact_int(
                raw_policy["confidence_min_ppm"],
                "readiness confidence minimum",
            ),
        )
        previous = payload["previous_graph_sha256"]
        if previous is not None and (
            type(previous) is not str or _SHA256.fullmatch(previous) is None
        ):
            raise ValueError("previous skill graph digest is invalid")
        return cls(
            skills=tuple(raw_skills),
            edges=tuple(SkillPrerequisiteEdgeV1.from_dict(item) for item in raw_edges),
            roots=tuple(raw_roots),
            readiness_policy=policy,
            graph_id=_exact_text(payload["graph_id"], "skill graph ID"),
            graph_version=_exact_int(payload["graph_version"], "skill graph version"),
            skill_registry_sha256=_exact_text(
                payload["skill_registry_sha256"],
                "skill registry digest",
            ),
            previous_graph_sha256=previous,
            schema_version=_exact_int(payload["schema_version"], "skill graph schema"),
        )


_ROOTS_V1 = tuple(
    skill
    for skill in CURRICULUM_SKILL_IDS_V1
    if skill not in {edge.dependent_skill_id for edge in _EXACT_EDGES_V1}
)
SKILL_GRAPH_V1 = SkillGraphV1(
    skills=CURRICULUM_SKILL_IDS_V1,
    edges=_EXACT_EDGES_V1,
    roots=_ROOTS_V1,
    readiness_policy=PrerequisiteReadinessPolicyV1(),
)


__all__ = [
    "CURRICULUM_SKILL_IDS_V1",
    "PREREQUISITE_CONFIDENCE_MIN_PPM_V1",
    "PREREQUISITE_MASTERY_MIN_PPM_V1",
    "PREREQUISITE_POLICY_ID_V1",
    "SKILL_GRAPH_ID_V1",
    "SKILL_GRAPH_SCHEMA_VERSION_V1",
    "SKILL_GRAPH_V1",
    "STABLE_SKILL_REGISTRY_SHA256_V1",
    "PrerequisiteReadinessPolicyV1",
    "SkillGraphV1",
    "SkillPrerequisiteEdgeV1",
    "require_stable_skill_v1",
]
