"""Causal counterfactual execution branching and immutable evidence."""

from pathlib import Path

from .models import (
    CAUTIOUS_INTERPRETATION,
    ActionMutation,
    BranchSnapshot,
    CounterfactualMode,
    CounterfactualReport,
    MutationManifest,
    TimingSweepReport,
)
from .session import (
    parse_counterfactual_command,
    run_counterfactual as _run_session_counterfactual,
    run_timing_sweep as _run_session_timing_sweep,
)
from .store import CounterfactualStore

__all__ = [
    "CAUTIOUS_INTERPRETATION",
    "ActionMutation",
    "BranchSnapshot",
    "CounterfactualMode",
    "CounterfactualReport",
    "CounterfactualStore",
    "MutationManifest",
    "TimingSweepReport",
    "parse_counterfactual_command",
    "run_counterfactual",
    "run_timing_sweep",
]


def run_counterfactual(
    parent_run_id,
    mutation_manifest,
    mode,
    *,
    parent_store_root,
):
    """Dispatch by verified immutable parent artifact kind."""

    root = Path(parent_store_root)
    session_manifest = root / "runs" / parent_run_id / "manifest.toml"
    algorithm_manifest = root / "runs" / parent_run_id / "manifest.json"
    if session_manifest.is_file():
        return _run_session_counterfactual(
            parent_run_id,
            mutation_manifest,
            mode,
            parent_store_root=root,
        )
    if algorithm_manifest.is_file():
        from .multivenue import run_multivenue_counterfactual

        return run_multivenue_counterfactual(
            parent_run_id,
            mutation_manifest,
            mode,
            parent_store_root=root,
        )
    raise ValueError(
        "parent run was not found as an immutable session or algorithm record"
    )


def run_timing_sweep(
    parent_run_id,
    action_sequence,
    mode,
    *,
    parent_store_root,
):
    root = Path(parent_store_root)
    session_manifest = root / "runs" / parent_run_id / "manifest.toml"
    algorithm_manifest = root / "runs" / parent_run_id / "manifest.json"
    if session_manifest.is_file():
        return _run_session_timing_sweep(
            parent_run_id,
            action_sequence,
            mode,
            parent_store_root=root,
        )
    if algorithm_manifest.is_file():
        from .multivenue import run_multivenue_timing_sweep

        return run_multivenue_timing_sweep(
            parent_run_id,
            action_sequence,
            mode,
            parent_store_root=root,
        )
    raise ValueError(
        "parent run was not found as an immutable session or algorithm record"
    )
