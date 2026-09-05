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
from .simulation_episode_contract import (
    SimulationEpisodeIdentityV1,
    SimulationEpisodePreparationRequestV1,
    SimulationEpisodePreparedResultV1,
    SimulationEpisodeRefusalV1,
    SimulationEpisodeVerificationV1,
    build_simulation_episode_preparation_request,
    episode_prefix_projection,
    episode_prefix_projection_sha256,
)
from .simulation_episode_facade import (
    prepare_simulation_episode,
    release_simulation_episode,
    verify_prepared_simulation_episode,
)
from .simulation_practice_contract import (
    PracticeActionRequestV1,
    PracticeAttemptRequestV1,
    PracticeCatalogV1,
    PracticeResultV1,
    build_practice_action_request,
    build_practice_attempt_request,
)
from .simulation_practice_facade import (
    begin_simulation_practice_attempt,
    list_simulation_practice_episodes,
    submit_simulation_practice_action,
)
from .simulation_practice_passage_contract import (
    PracticeObservationPassageRequestV1,
    PracticeObservationPassageResultV1,
    PracticePassageCapabilityCatalogV1,
    PracticePassageObservationV1,
    build_simulation_practice_observation_passage_request,
)
from .simulation_practice_passage_facade import (
    acquire_simulation_practice_observation_passage,
    list_simulation_practice_passage_capabilities,
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
from .simulation_artifact_contract import (
    EmbeddedComponentV1,
    EmbeddedSessionRecordingV1,
    ReplayArtifactRefV1,
    SimulationFinalizeResultV1,
    SimulationReplayArtifactV1,
    SimulationRunResultV1,
    SimulationTimelineEventV1,
)
from .simulation_finalize_facade import finalize_simulation_run
from .simulation_replay_contract import ReplayArtifactVerificationReceiptV1
from .simulation_replay_facade import resolve_replay_artifact
from .simulation_replay_provider import build_replay_provider
from .terminal import TerminalUiConfig, render_terminal_frame, run_terminal_ui

__all__ = [
    "EmbeddedComponentV1",
    "EmbeddedSessionRecordingV1",
    "ReplayArtifactRefV1",
    "ReplayArtifactVerificationReceiptV1",
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
    "SimulationEpisodeIdentityV1",
    "SimulationEpisodePreparationRequestV1",
    "SimulationEpisodePreparedResultV1",
    "SimulationEpisodeRefusalV1",
    "SimulationEpisodeVerificationV1",
    "PracticeActionRequestV1",
    "PracticeAttemptRequestV1",
    "PracticeCatalogV1",
    "PracticeResultV1",
    "PracticeObservationPassageRequestV1",
    "PracticeObservationPassageResultV1",
    "PracticePassageCapabilityCatalogV1",
    "PracticePassageObservationV1",
    "SimulationFinalizeResultV1",
    "SimulationReplayArtifactV1",
    "SimulationResolutionRefusalV1",
    "SimulationResetCommitResultV1",
    "SimulationResetResultV1",
    "SimulationRunResultV1",
    "SimulationStartResultV1",
    "SimulationStartRefusalV1",
    "SimulationTrainingOptionsV1",
    "SimulationTrainingResourceCatalogV1",
    "SimulationTimelineEventV1",
    "TerminalUiConfig",
    "advance_simulation_run",
    "acquire_simulation_practice_observation_passage",
    "build_simulation_episode_preparation_request",
    "build_practice_action_request",
    "build_practice_attempt_request",
    "build_simulation_practice_observation_passage_request",
    "begin_simulation_practice_attempt",
    "build_replay_provider",
    "close_simulation_run",
    "commit_simulation_reset",
    "discard_simulation_reset",
    "dispatch_simulation_command",
    "finalize_simulation_run",
    "list_simulation_profiles",
    "list_simulation_practice_episodes",
    "list_simulation_practice_passage_capabilities",
    "list_simulation_training_resources",
    "prepare_simulation_reset",
    "prepare_simulation_episode",
    "release_simulation_episode",
    "render_terminal_frame",
    "read_current_simulation_frame",
    "resolve_simulation_profile",
    "resolve_replay_artifact",
    "verify_prepared_simulation_episode",
    "episode_prefix_projection",
    "episode_prefix_projection_sha256",
    "run_terminal_ui",
    "start_simulation_run",
    "submit_simulation_practice_action",
]
