"""Presentation and detached public contracts for playable Kirby2 sessions."""

from .simulation_contract import (
    ResolvedSimulationConfigurationV1,
    SimulationComponentRefV1,
    SimulationContractDecodeError,
    SimulationContractIntegrityError,
    SimulationProfileCatalogV1,
    SimulationProfileRefV1,
    SimulationProfileResolutionV1,
    SimulationProfileSelectionV1,
    SimulationResolutionRefusalV1,
    SimulationTrainingResourceCatalogV1,
)
from .simulation_facade import (
    list_simulation_profiles,
    list_simulation_training_resources,
    resolve_simulation_profile,
)
from .simulation_live_contract import (
    ObjectiveDefinitionV1,
    SimulationFrameV1,
    SimulationStartResultV1,
    SimulationStartRefusalV1,
    SimulationTrainingOptionsV1,
)
from .simulation_run_facade import start_simulation_run
from .terminal import TerminalUiConfig, render_terminal_frame, run_terminal_ui

__all__ = [
    "ResolvedSimulationConfigurationV1",
    "ObjectiveDefinitionV1",
    "SimulationComponentRefV1",
    "SimulationContractDecodeError",
    "SimulationContractIntegrityError",
    "SimulationProfileCatalogV1",
    "SimulationProfileRefV1",
    "SimulationProfileResolutionV1",
    "SimulationProfileSelectionV1",
    "SimulationFrameV1",
    "SimulationResolutionRefusalV1",
    "SimulationStartResultV1",
    "SimulationStartRefusalV1",
    "SimulationTrainingOptionsV1",
    "SimulationTrainingResourceCatalogV1",
    "TerminalUiConfig",
    "list_simulation_profiles",
    "list_simulation_training_resources",
    "render_terminal_frame",
    "resolve_simulation_profile",
    "run_terminal_ui",
    "start_simulation_run",
]
