"""Explicit command module for durable full-day generation and inspection."""

from __future__ import annotations

import argparse
import json
import tempfile
from importlib.resources import files
from pathlib import Path

from kirby2.cli.registry import CommandModule, CommandSpec

from .models import FullDayPlanV1
from .runtime import FullDayRuntime
from .store import FullDayStore


def _data_root(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise argparse.ArgumentTypeError("data root must be an explicit absolute path")
    try:
        resolved = path.resolve(strict=False)
    except (OSError, RuntimeError) as error:
        raise argparse.ArgumentTypeError("data root cannot be resolved safely") from error
    if path != resolved:
        raise argparse.ArgumentTypeError("data root must be supplied already resolved")
    return resolved


def _nonnegative_integer(value: str) -> int:
    try:
        selected = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("value must be an integer") from error
    if selected < 0:
        raise argparse.ArgumentTypeError("value must be nonnegative")
    return selected


def _load_plan(path: Path) -> FullDayPlanV1:
    raw = path.read_bytes()
    plan = FullDayPlanV1.from_json_bytes(raw)
    if plan.to_json_bytes() != raw:
        raise ValueError("full-day plan file is not canonical JSON")
    return plan


def _audit_plan_path() -> Path:
    return Path(str(files("kirby2.full_day").joinpath("examples/audit_full_day_plan.json")))


def _runtime_for_plan(plan: FullDayPlanV1) -> FullDayRuntime:
    """Materialize the self-contained mechanics-only V1 command profile.

    Later compiler/source-pack work will resolve external component bytes.  WO31-G
    deliberately refuses a plan whose active composition requires injected owners.
    """

    try:
        return FullDayRuntime.create(plan)
    except (TypeError, ValueError, RuntimeError) as error:
        raise ValueError(
            "plan is not self-contained for the WO31-G command materializer"
        ) from error


def _print(payload: object) -> None:
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))


def _configure_generate(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--data-root", required=True, type=_data_root)
    parser.add_argument("--plan", required=True, type=Path)


def _handle_generate(args: argparse.Namespace) -> int:
    plan = _load_plan(args.plan)
    manifest = FullDayStore(args.data_root).generate_day(
        plan, _runtime_for_plan(plan)
    )
    _print(
        {
            "run_id": manifest.run_id,
            "status": "STORED",
            "verification": FullDayStore(args.data_root)
            .verify_day(manifest.run_id)
            .as_dict(),
        }
    )
    return 0


def _configure_run_id(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("run_id")
    parser.add_argument("--data-root", required=True, type=_data_root)


def _handle_inspect(args: argparse.Namespace) -> int:
    _print(FullDayStore(args.data_root).inspect_day(args.run_id))
    return 0


def _configure_seek(parser: argparse.ArgumentParser) -> None:
    _configure_run_id(parser)
    parser.add_argument("--time-us", required=True, type=_nonnegative_integer)


def _handle_seek(args: argparse.Namespace) -> int:
    _print(FullDayStore(args.data_root).seek(args.run_id, args.time_us).as_dict())
    return 0


def _configure_extract(parser: argparse.ArgumentParser) -> None:
    _configure_run_id(parser)
    parser.add_argument("--start-us", required=True, type=_nonnegative_integer)
    parser.add_argument("--end-us", required=True, type=_nonnegative_integer)
    parser.add_argument(
        "--reveal-policy",
        default="OBSERVABLE_CONTEXT_V1",
        choices=("OBSERVABLE_CONTEXT_V1",),
    )


def _handle_extract(args: argparse.Namespace) -> int:
    store = FullDayStore(args.data_root)
    manifest = store.extract_window(
        args.run_id,
        args.start_us,
        args.end_us,
        reveal_policy=args.reveal_policy,
    )
    _print(
        {
            "child_run_id": manifest.run_id,
            "parent_run_id": manifest.parent_run_id,
            "status": "STORED",
            "verification": store.verify_day(manifest.run_id).as_dict(),
        }
    )
    return 0


def _configure_demo(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--seed", type=int, default=42)


def _handle_demo(args: argparse.Namespace) -> int:
    plan = _load_plan(_audit_plan_path())
    if args.seed != plan.seed_policy.root_seed:
        raise ValueError(
            f"audit plan has frozen seed {plan.seed_policy.root_seed}; supplied {args.seed}"
        )
    with tempfile.TemporaryDirectory(prefix="kirby2-full-day-demo-") as temporary:
        root = Path(temporary).resolve()
        manifest = FullDayStore(root).generate_day(plan, _runtime_for_plan(plan))
        reopened = FullDayStore(root)
        target = plan.calendar.phases[2].start.simulation_time_us
        seek_result = reopened.seek(manifest.run_id, target)
        child = reopened.extract_window(
            manifest.run_id,
            target,
            min(target + 100, plan.calendar.end_time_us),
        )
        _print(
            {
                "child_run_id": child.run_id,
                "event_count": len(seek_result.runtime.events),
                "reopen_status": reopened.verify_day(manifest.run_id).as_dict()["status"],
                "run_id": manifest.run_id,
                "seek_target_us": target,
                "status": "PASS",
            }
        )
    return 0


def _configure_qualification_dev(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--fixture", required=True, type=Path)


def _handle_qualification_dev(args: argparse.Namespace) -> int:
    from .qualification import run_qualification_development_fixture

    report = run_qualification_development_fixture(args.fixture.resolve(strict=True))
    _print(report.as_dict())
    return 0 if report.passed else 1


def _configure_qualify_profiles(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--evidence-root", required=True, type=Path)


def _handle_qualify_profiles(args: argparse.Namespace) -> int:
    from .qualification import qualify_day_profiles_once

    repository = Path(__file__).resolve().parents[2]
    run_id, report = qualify_day_profiles_once(
        repository=repository,
        manifest_path=args.manifest,
        evidence_root=args.evidence_root,
    )
    _print(
        {
            "mode": "VERIFIED_IMMUTABLE_EVIDENCE" if report.passed else "FAILED",
            "run_id": run_id,
            "verification": report.as_dict(),
        }
    )
    return 0 if report.passed else 1


def _configure_audit(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--qualification-evidence", type=Path)


def _handle_audit(args: argparse.Namespace) -> int:
    from kirby2.audit.full_day import audit_full_day

    evidence = (
        None
        if args.qualification_evidence is None
        else args.qualification_evidence
    )
    cases = audit_full_day(evidence)
    failures = tuple(
        f"{case.name}: {failure}" for case in cases for failure in case.failures
    )
    for case in cases:
        _print(case.as_dict())
    print(
        f"AUDIT_FULL_DAY {'FAIL' if failures else 'PASS'} "
        f"cases={len(cases)} failures={len(failures)}"
    )
    if (
        evidence is not None
        and not failures
        and any(
            case.name == "full_day_profile_qualification_evidence"
            and case.status == "NOT_EXERCISED"
            for case in cases
        )
    ):
        return 2
    return 1 if failures else 0


FULL_DAY_COMMAND_MODULE = CommandModule(
    module_id="FULL_DAY_STORAGE",
    commands=(
        CommandSpec(
            command_id="GENERATE_DAY",
            name="generate-day",
            help="generate and atomically store one canonical full trading day",
            handler=_handle_generate,
            configure=_configure_generate,
        ),
        CommandSpec(
            command_id="INSPECT_DAY",
            name="inspect-day",
            help="inspect a verified full day without revealing sealed runtime state",
            handler=_handle_inspect,
            configure=_configure_run_id,
        ),
        CommandSpec(
            command_id="SEEK_FULL_DAY",
            name="seek",
            help="restore and replay a full day to an exact quiescent time cut",
            handler=_handle_seek,
            configure=_configure_seek,
        ),
        CommandSpec(
            command_id="EXTRACT_FULL_DAY_WINDOW",
            name="extract-window",
            help="persist an immutable observable window derived from a parent day",
            handler=_handle_extract,
            configure=_configure_extract,
        ),
        CommandSpec(
            command_id="FULL_DAY_STORAGE_DEMO",
            name="full-day-storage-demo",
            help="exercise generate, reopen, seek, and extraction in a fresh store",
            handler=_handle_demo,
            configure=_configure_demo,
        ),
        CommandSpec(
            command_id="FULL_DAY_QUALIFICATION_DEV_DEMO",
            name="full-day-qualification-dev-demo",
            help="exercise frozen qualification machinery with disjoint toy evidence",
            handler=_handle_qualification_dev,
            configure=_configure_qualification_dev,
        ),
        CommandSpec(
            command_id="QUALIFY_DAY_PROFILES_DEMO",
            name="qualify-day-profiles-demo",
            help="execute or verify the exact one-time preregistered profile qualification",
            handler=_handle_qualify_profiles,
            configure=_configure_qualify_profiles,
        ),
        CommandSpec(
            command_id="AUDIT_FULL_DAY",
            name="audit-full-day",
            help="run the complete non-persisting full-day audit",
            handler=_handle_audit,
            configure=_configure_audit,
        ),
    ),
)


__all__ = ["FULL_DAY_COMMAND_MODULE"]
