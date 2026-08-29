"""Explicit CLI operations for immutable lesson mining and review (WO33-E)."""

from __future__ import annotations

import argparse
import json
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from kirby2.cli.registry import CommandModule, CommandSpec
from kirby2.research.store import DEFAULT_RESEARCH_STORE, LessonMiningStore, RunStore

from .reviews import (
    LESSON_REVIEW_RUBRIC_VERSION_V1,
    LessonReviewDecisionV1,
    ReviewerAuthorityV1,
    compare_review_candidates,
    load_qualification_source_manifest,
    qualify_lesson_candidates,
)


def _repository() -> Path:
    return Path(__file__).resolve().parents[2]


def _default_source_matrix() -> Path:
    return _repository() / "kirby2/mining/fixtures/qualification_sources.toml"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _nonnegative_integer(value: str) -> int:
    try:
        selected = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("value must be an integer") from error
    if selected < 0:
        raise argparse.ArgumentTypeError("value must be nonnegative")
    return selected


def _print(payload: object) -> None:
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))


def _configure_store(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--store-root",
        type=Path,
        default=DEFAULT_RESEARCH_STORE,
        help="research store root (default: .kirby2/research)",
    )


def _configure_source_matrix(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--source-matrix",
        type=Path,
        default=_default_source_matrix(),
        help="exact committed WO33-A1 source matrix",
    )


def _configure_mine(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("run_id", help="one preregistered immutable source run ID")
    parser.add_argument("--seed", type=_nonnegative_integer, default=42)
    _configure_source_matrix(parser)
    _configure_store(parser)


def _handle_mine(args: argparse.Namespace) -> int:
    repository = _repository()
    _raw, manifest = load_qualification_source_manifest(args.source_matrix)
    matching = tuple(
        row
        for row in manifest.rows
        if args.run_id
        in {
            str(row.identity["source_id"]),
            str(row.provenance["full_day_run_id"]),
        }
    )
    if len(matching) != 1:
        raise ValueError(
            "mine-drills RUN_ID must resolve exactly one preregistered source row"
        )
    with tempfile.TemporaryDirectory(prefix="kirby2-mine-drills-") as temporary:
        result = qualify_lesson_candidates(
            repository=repository,
            source_manifest_path=args.source_matrix,
            materialization_root=Path(temporary),
            seed=args.seed,
            active_source_rows=(matching[0].row_id,),
        )
        parent = args.run_id if re.fullmatch(r"run-[0-9a-f]{24}", args.run_id) else None
        store = LessonMiningStore(args.store_root)
        stored = store.record_mining_result(
            result,
            parent_run_id=parent,
            repository=repository,
        )
        _print(
            {
                **result.summary(),
                "mining_run_id": stored.run_id,
                "source_run_id": args.run_id,
                "verification": store.verify_run(stored.run_id).as_dict(),
            }
        )
    return 0


def _configure_run_id(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("run_id")
    _configure_store(parser)


def _handle_list(args: argparse.Namespace) -> int:
    store = LessonMiningStore(args.store_root)
    result = store.load_mining_result(args.run_id)
    _print(
        {
            "candidates": [
                {
                    "candidate_id": item.candidate.candidate_id,
                    "detector_id": item.candidate.detector.detector_id,
                    "human_review_status": "PENDING",
                    "source_row": item.recipe.row_id,
                    "technical_status": item.technical_status.value,
                }
                for item in result.candidates
            ],
            "mining_run_id": args.run_id,
            "summary": result.summary(),
        }
    )
    return 0


def _configure_candidate_id(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("candidate_id")
    _configure_store(parser)


def _handle_inspect(args: argparse.Namespace) -> int:
    store = LessonMiningStore(args.store_root)
    run_id, reviewable = store.find_candidate(args.candidate_id)
    history = store.review_history(args.candidate_id)
    _print(
        {
            "candidate": reviewable.candidate.as_dict(),
            "human_review_status": (
                "PENDING" if not history else history[-1][1].decision.value
            ),
            "mining_run_id": run_id,
            "review_count": len(history),
            "technical_reason_codes": list(reviewable.technical_reason_codes),
            "technical_status": reviewable.technical_status.value,
        }
    )
    return 0


def _configure_accept(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("candidate_id")
    parser.add_argument("--reviewer-id", required=True)
    parser.add_argument(
        "--reviewer-reference",
        required=True,
        help="authenticated reference beginning local: or auth:",
    )
    parser.add_argument(
        "--rubric-version", default=LESSON_REVIEW_RUBRIC_VERSION_V1
    )
    parser.add_argument("--reason", action="append", required=True)
    parser.add_argument("--reason-code", action="append", default=[])
    parser.add_argument("--created-at-utc")
    _configure_store(parser)


def _handle_accept(args: argparse.Namespace) -> int:
    store = LessonMiningStore(args.store_root)
    reason_codes = tuple(
        sorted(set(args.reason_code or ("REVIEWER_ACCEPTED",)))
    )
    manifest = store.record_review(
        args.candidate_id,
        decision=LessonReviewDecisionV1.ACCEPTED,
        reviewer_id=args.reviewer_id,
        reviewer_reference=args.reviewer_reference,
        reviewer_authority=ReviewerAuthorityV1.LOCAL_AUTHENTICATED,
        rubric_version=args.rubric_version,
        reasons=tuple(sorted(set(args.reason), key=lambda item: item.encode("utf-8"))),
        reason_codes=reason_codes,
        created_at_utc=args.created_at_utc or _utc_now(),
        repository=_repository(),
    )
    sidecar = store.load_review_sidecar(manifest.run_id)
    _print(
        {
            "candidate_id": args.candidate_id,
            "decision": sidecar.decision.value,
            "review_id": sidecar.review_id,
            "review_run_id": manifest.run_id,
            "sidecar_sha256": sidecar.sidecar_sha256,
            "status": "STORED_IMMUTABLY",
            "verification": store.verify_run(manifest.run_id).as_dict(),
        }
    )
    return 0


def _configure_build(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("candidate_id")
    parser.add_argument("--created-at-utc")
    _configure_store(parser)


def _handle_build(args: argparse.Namespace) -> int:
    store = LessonMiningStore(args.store_root)
    manifest = store.build_lesson_proposal(
        args.candidate_id,
        created_at_utc=args.created_at_utc or _utc_now(),
        repository=_repository(),
    )
    proposal = store.load_build_proposal(manifest.run_id)
    _print(
        {
            **proposal.as_dict(),
            "build_run_id": manifest.run_id,
            "lesson_id": proposal.lesson_id,
            "verification": store.verify_run(manifest.run_id).as_dict(),
        }
    )
    return 0


def _configure_compare(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("left_candidate_id")
    parser.add_argument("right_candidate_id")
    _configure_store(parser)


def _handle_compare(args: argparse.Namespace) -> int:
    store = LessonMiningStore(args.store_root)
    _left_run, left = store.find_candidate(args.left_candidate_id)
    _right_run, right = store.find_candidate(args.right_candidate_id)
    _print(compare_review_candidates(left, right))
    return 0


def _handle_history(args: argparse.Namespace) -> int:
    history = LessonMiningStore(args.store_root).review_history(args.candidate_id)
    _print(
        {
            "candidate_id": args.candidate_id,
            "history": [
                {"review_run_id": run_id, **sidecar.as_dict()}
                for run_id, sidecar in history
            ],
            "review_count": len(history),
        }
    )
    return 0


def _configure_demo(parser: argparse.ArgumentParser) -> None:
    _configure_source_matrix(parser)
    parser.add_argument("--seed", type=_nonnegative_integer, default=42)


def _handle_demo(args: argparse.Namespace) -> int:
    repository = _repository()
    with tempfile.TemporaryDirectory(prefix="kirby2-lesson-miner-demo-") as temporary:
        root = Path(temporary)
        result = qualify_lesson_candidates(
            repository=repository,
            source_manifest_path=args.source_matrix,
            materialization_root=root / "materialized-sources",
            seed=args.seed,
        )
        store = LessonMiningStore(root / "research")
        manifest = store.record_mining_result(result, repository=repository)
        reopened = store.load_mining_result(manifest.run_id)
        verification = RunStore(root / "research").verify_run(manifest.run_id)
        typed_artifacts = RunStore(root / "research").query_lesson_mining_artifacts(
            manifest.run_id
        )
        summary = reopened.summary()
        _print(
            {
                **summary,
                "actual_human_inspection_gate": "PENDING",
                "five_accepted_lessons_gate": "PENDING",
                "mining_run_id": manifest.run_id,
                "persistence_replay_parity": reopened.artifact_payloads()
                == result.artifact_payloads(),
                "status": "PASS" if verification.passed else "FAIL",
                "typed_artifact_count": len(typed_artifacts),
                "verification": verification.as_dict(),
            }
        )
        return 0 if verification.passed else 1


MINING_COMMAND_MODULE = CommandModule(
    module_id="LESSON_MINING_REVIEW",
    commands=(
        CommandSpec(
            command_id="MINE_DRILLS",
            name="mine-drills",
            help="mine review-ready lesson proposals from one immutable source run",
            handler=_handle_mine,
            configure=_configure_mine,
        ),
        CommandSpec(
            command_id="LIST_MINED_CANDIDATES",
            name="list-candidates",
            help="list immutable candidates produced by one mining run",
            handler=_handle_list,
            configure=_configure_run_id,
        ),
        CommandSpec(
            command_id="INSPECT_MINED_CANDIDATE",
            name="inspect-candidate",
            help="inspect one candidate without altering candidate or review state",
            handler=_handle_inspect,
            configure=_configure_candidate_id,
        ),
        CommandSpec(
            command_id="ACCEPT_MINED_CANDIDATE",
            name="accept-candidate",
            help="record an authenticated human acceptance as an immutable sidecar",
            handler=_handle_accept,
            configure=_configure_accept,
        ),
        CommandSpec(
            command_id="BUILD_MINED_LESSON",
            name="build-lesson",
            help="build an immutable technical lesson proposal from a ready candidate",
            handler=_handle_build,
            configure=_configure_build,
        ),
        CommandSpec(
            command_id="COMPARE_MINED_CANDIDATES",
            name="compare-candidates",
            help="compare two candidates with the committed semantic duplicate policy",
            handler=_handle_compare,
            configure=_configure_compare,
        ),
        CommandSpec(
            command_id="LESSON_REVIEW_HISTORY",
            name="review-history",
            help="show the immutable supersession chain for one lesson candidate",
            handler=_handle_history,
            configure=_configure_candidate_id,
        ),
        CommandSpec(
            command_id="LESSON_MINER_DEMO",
            name="lesson-miner-demo",
            help="execute the exact five-source qualification and persistence workflow",
            handler=_handle_demo,
            configure=_configure_demo,
        ),
    ),
)


__all__ = ["MINING_COMMAND_MODULE"]
