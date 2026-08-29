"""Stable, versioned skill references shared by mined lessons and WO34."""

from __future__ import annotations

import re
from dataclasses import dataclass
from types import MappingProxyType

from .models import MINING_SCHEMA_VERSION_V1, canonical_json_bytes, sha256_json


SKILL_REGISTRY_ID_V1 = "MINING_SKILL_REGISTRY_V1"
SKILL_REGISTRY_VERSION_V1 = 1
_SKILL_ID = re.compile(r"[A-Z][A-Z0-9_]{0,95}\Z")


class UnknownSkillReferenceV1(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class SkillReferenceV1:
    skill_id: str
    version: int = SKILL_REGISTRY_VERSION_V1

    def __post_init__(self) -> None:
        if _SKILL_ID.fullmatch(self.skill_id) is None:
            raise ValueError("skill ID must be an uppercase stable identifier")
        if type(self.version) is not int or self.version <= 0:
            raise ValueError("skill version must be a positive integer")

    def as_dict(self) -> dict[str, object]:
        return {"id": self.skill_id, "version": self.version}


@dataclass(frozen=True, slots=True)
class SkillDefinitionV1:
    skill_id: str
    display_name: str
    version: int = SKILL_REGISTRY_VERSION_V1

    def __post_init__(self) -> None:
        SkillReferenceV1(self.skill_id, self.version)
        if type(self.display_name) is not str or not self.display_name.strip():
            raise ValueError("skill display name must not be empty")

    @property
    def reference(self) -> SkillReferenceV1:
        return SkillReferenceV1(self.skill_id, self.version)

    def as_dict(self) -> dict[str, object]:
        return {
            "display_name": self.display_name,
            "id": self.skill_id,
            "version": self.version,
        }


class SkillRegistryV1:
    """Closed V1 catalog; version changes are append-only in WO34."""

    __slots__ = ("_definitions", "_mapping")

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("skill registry is immutable")

    def __init__(self, definitions: tuple[SkillDefinitionV1, ...]) -> None:
        if type(definitions) is not tuple or not definitions:
            raise ValueError("skill registry requires definitions")
        if any(not isinstance(item, SkillDefinitionV1) for item in definitions):
            raise TypeError("skill registry definition is invalid")
        ordered = tuple(sorted(definitions, key=lambda item: item.skill_id.encode("utf-8")))
        ids = tuple(item.skill_id for item in ordered)
        if len(set(ids)) != len(ids):
            raise ValueError("skill registry IDs must be unique")
        object.__setattr__(self, "_definitions", ordered)
        object.__setattr__(
            self,
            "_mapping",
            MappingProxyType({item.skill_id: item for item in ordered}),
        )

    @property
    def definitions(self) -> tuple[SkillDefinitionV1, ...]:
        return self._definitions

    def require(self, skill_id: str, version: int = 1) -> SkillDefinitionV1:
        if type(skill_id) is not str:
            raise UnknownSkillReferenceV1("UNKNOWN_SKILL_REFERENCE")
        definition = self._mapping.get(skill_id)
        if definition is None or definition.version != version:
            raise UnknownSkillReferenceV1(
                f"UNKNOWN_SKILL_REFERENCE: {skill_id}@{version}"
            )
        return definition

    def as_dict(self) -> dict[str, object]:
        return {
            "registry_id": SKILL_REGISTRY_ID_V1,
            "registry_version": SKILL_REGISTRY_VERSION_V1,
            "schema_version": MINING_SCHEMA_VERSION_V1,
            "skills": [item.as_dict() for item in self._definitions],
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())

    @property
    def sha256(self) -> str:
        return sha256_json(self.as_dict())


_SKILLS = (
    ("BOOK_READING", "Book reading"),
    ("TAPE_READING", "Tape reading"),
    ("QUEUE_POSITION", "Queue position"),
    ("PASSIVE_ENTRY", "Passive entry"),
    ("AGGRESSIVE_ENTRY", "Aggressive entry"),
    ("CANCEL_TIMING", "Cancel timing"),
    ("REPLACE_TIMING", "Replace timing"),
    ("PARTIAL_FILL_MANAGEMENT", "Partial-fill management"),
    ("ADVERSE_SELECTION", "Adverse-selection control"),
    ("SPREAD_DECISION", "Spread decision"),
    ("VOLUME_CONTEXT", "Volume context"),
    ("REGIME_RECOGNITION", "Regime recognition"),
    ("ABSORPTION_RECOGNITION", "Absorption recognition"),
    ("LIQUIDITY_WITHDRAWAL", "Liquidity withdrawal"),
    ("HIDDEN_LIQUIDITY", "Hidden liquidity"),
    ("LATENCY_AWARENESS", "Latency awareness"),
    ("MULTI_VENUE_ROUTING", "Multi-venue routing"),
    ("AUCTION_EXECUTION", "Auction execution"),
    ("HALT_REOPENING", "Halt and reopening"),
    ("SCRIPT_DISCIPLINE", "Script discipline"),
    ("HOTKEY_ACCURACY", "Hotkey accuracy"),
    ("POSITION_MANAGEMENT", "Position management"),
    ("EXIT_EXECUTION", "Exit execution"),
)

SKILL_REGISTRY_V1 = SkillRegistryV1(
    tuple(SkillDefinitionV1(skill_id, display_name) for skill_id, display_name in _SKILLS)
)
STABLE_SKILL_IDS_V1 = tuple(
    definition.skill_id for definition in SKILL_REGISTRY_V1.definitions
)


__all__ = [
    "SKILL_REGISTRY_ID_V1",
    "SKILL_REGISTRY_VERSION_V1",
    "SKILL_REGISTRY_V1",
    "STABLE_SKILL_IDS_V1",
    "SkillDefinitionV1",
    "SkillReferenceV1",
    "SkillRegistryV1",
    "UnknownSkillReferenceV1",
]
