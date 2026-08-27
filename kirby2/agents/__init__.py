"""Deterministic synthetic participant ecology for execution-training drills."""

from .base import MarketAgent
from .ecology import AgentEcology, EcologyRunResult, EcologySummary, run_agent_ecology
from .models import (
    AGENT_ECOLOGY_SCHEMA_VERSION,
    SYNTHETIC_VENUE_ID,
    AgentBounds,
    AgentFamily,
    AgentInformationSet,
    AgentSafetyClass,
    AgentSpec,
    PopulationDefinition,
)
from .populations import (
    ADVERSARIAL_DRILL_IDS,
    POPULATION_IDS,
    compose_population,
    get_adversarial_drill,
    get_population,
)
from .replay import (
    ECOLOGY_RECORDING_SCHEMA_VERSION,
    LEGACY_ECOLOGY_RECORDING_SCHEMA_VERSION,
    EcologyRecording,
    EcologyReplayReport,
    replay_agent_ecology,
)

__all__ = [
    "ADVERSARIAL_DRILL_IDS",
    "AGENT_ECOLOGY_SCHEMA_VERSION",
    "ECOLOGY_RECORDING_SCHEMA_VERSION",
    "LEGACY_ECOLOGY_RECORDING_SCHEMA_VERSION",
    "POPULATION_IDS",
    "SYNTHETIC_VENUE_ID",
    "AgentBounds",
    "AgentEcology",
    "AgentFamily",
    "AgentInformationSet",
    "AgentSafetyClass",
    "AgentSpec",
    "EcologyRunResult",
    "EcologyRecording",
    "EcologyReplayReport",
    "EcologySummary",
    "MarketAgent",
    "PopulationDefinition",
    "compose_population",
    "get_adversarial_drill",
    "get_population",
    "run_agent_ecology",
    "replay_agent_ecology",
]
