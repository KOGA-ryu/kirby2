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
from .simulation_interaction_contract import (
    SimulationAdvanceResultV1,
    SimulationCommandOutcomeV1,
    SimulationCommandRequestV1,
    SimulationCommandResultV1,
    SimulationCurrentFrameResultV1,
)
from .simulation_live_contract import (
    ObjectiveDefinitionV1,
    SimulationFrameV1,
    SimulationStartResultV1,
    SimulationStartRefusalV1,
    SimulationTrainingOptionsV1,
)
from .simulation_lifecycle_contract import (
    SimulationCloseResultV1,
    SimulationResetCommitResultV1,
    SimulationResetResultV1,
)
from .simulation_run_facade import (
    advance_simulation_run,
    close_simulation_run,
    commit_simulation_reset,
    discard_simulation_reset,
    dispatch_simulation_command,
    prepare_simulation_reset,
    read_current_simulation_frame,
    start_simulation_run,
)
from .terminal import TerminalUiConfig, render_terminal_frame, run_terminal_ui

__all__ = [
    "ResolvedSimulationConfigurationV1",
    "ObjectiveDefinitionV1",
    "SimulationComponentRefV1",
    "SimulationAdvanceResultV1",
    "SimulationCommandOutcomeV1",
    "SimulationCommandRequestV1",
    "SimulationCommandResultV1",
    "SimulationCloseResultV1",
    "SimulationContractDecodeError",
    "SimulationContractIntegrityError",
    "SimulationCurrentFrameResultV1",
    "SimulationProfileCatalogV1",
    "SimulationProfileRefV1",
    "SimulationProfileResolutionV1",
    "SimulationProfileSelectionV1",
    "SimulationFrameV1",
    "SimulationResolutionRefusalV1",
    "SimulationResetCommitResultV1",
    "SimulationResetResultV1",
    "SimulationStartResultV1",
    "SimulationStartRefusalV1",
    "SimulationTrainingOptionsV1",
    "SimulationTrainingResourceCatalogV1",
    "TerminalUiConfig",
    "advance_simulation_run",
    "close_simulation_run",
    "commit_simulation_reset",
    "discard_simulation_reset",
    "dispatch_simulation_command",
    "list_simulation_profiles",
    "list_simulation_training_resources",
    "prepare_simulation_reset",
    "render_terminal_frame",
    "read_current_simulation_frame",
    "resolve_simulation_profile",
    "run_terminal_ui",
    "start_simulation_run",
]
