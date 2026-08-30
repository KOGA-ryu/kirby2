"""Modular planning, coordinator, worker, and parity-demo commands for WO38-B."""

from __future__ import annotations

import argparse
import hashlib
import tomllib
from pathlib import Path

from kirby2 import __version__
from kirby2.cli.registry import CommandModule, CommandSpec
from kirby2.packs.formats import canonical_json_bytes
from kirby2.scenarios import get_scenario_definition
from kirby2.simulation import LiquidityPreset, VolumePreset

from .coordinator import CoordinatorRunResultV1, OrchestrationCoordinatorV1
from .local import LocalSubprocessBackendV1, SingleProcessBackendV1
from .models import DigestReferenceV1, LogicalWorkCellV1, WorkKindV1
from .planner import build_experiment_work_plan
from .seeds import build_master_seed_identity
from .worker import (
    complete_run_expected_output_identities,
    main as worker_main,
    measure_local_worker_compatibility,
)


ORCHESTRATION_EXPERIMENT_SCHEMA_ID = "KIRBY2_ORCHESTRATION_EXPERIMENT_V1"
ORCHESTRATION_EXPERIMENT_SCHEMA_VERSION = 1

_MANIFEST_FIELDS = frozenset(
    {
        "cells",
        "duration_seconds",
        "experiment_id",
        "liquidity",
        "master_seed",
        "relative_volume",
        "resource_class",
        "scenario_name",
        "schema_id",
        "schema_version",
    }
)
_CELL_FIELDS = frozenset({"cell_id", "partition_id"})


def load_orchestration_experiment(path: Path):
    """Load one strict scientific TOML manifest into an immutable work plan."""

    if type(path) is not Path:
        raise TypeError("orchestration manifest path must be pathlib.Path")
    raw = path.resolve(strict=True).read_bytes()
    try:
        payload = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise ValueError("orchestration manifest must be valid UTF-8 TOML") from error
    root = _exact_object(payload, _MANIFEST_FIELDS, "orchestration manifest")
    if root["schema_id"] != ORCHESTRATION_EXPERIMENT_SCHEMA_ID:
        raise ValueError("orchestration experiment schema ID is not supported")
    if (
        type(root["schema_version"]) is not int
        or root["schema_version"] != ORCHESTRATION_EXPERIMENT_SCHEMA_VERSION
    ):
        raise ValueError("orchestration experiment schema version is not supported")

    experiment_id = _exact_text(root, "experiment_id")
    master_seed = _exact_integer(root, "master_seed")
    scenario_name = _exact_text(root, "scenario_name")
    duration_seconds = _exact_integer(root, "duration_seconds")
    if not 1 <= duration_seconds <= 86_400:
        raise ValueError("orchestration duration must be in [1, 86400] seconds")
    relative_volume = _exact_text(root, "relative_volume")
    liquidity = _exact_text(root, "liquidity")
    resource_class = _exact_text(root, "resource_class")

    scenario_definition = get_scenario_definition(scenario_name)
    if scenario_definition.name != scenario_name:
        raise ValueError("orchestration scenario name must use its canonical spelling")
    volume = VolumePreset.parse(relative_volume)
    if volume.value != relative_volume:
        raise ValueError("orchestration relative volume must use canonical spelling")
    liquidity_profile = LiquidityPreset.parse(liquidity)
    if liquidity_profile.value != liquidity:
        raise ValueError("orchestration liquidity must use canonical spelling")

    cell_payloads = root["cells"]
    if type(cell_payloads) is not list or not cell_payloads:
        raise ValueError("orchestration experiment requires a nonempty cells array")
    stable_cells: list[tuple[str, str]] = []
    for index, value in enumerate(cell_payloads):
        cell = _exact_object(value, _CELL_FIELDS, f"orchestration cell {index}")
        stable_cells.append(
            (
                _exact_text(cell, "partition_id"),
                _exact_text(cell, "cell_id"),
            )
        )
    ordered_cells = tuple(sorted(stable_cells))

    configuration = {
        "duration_seconds": duration_seconds,
        "liquidity": liquidity,
        "relative_volume": relative_volume,
        "scenario_name": scenario_name,
    }
    logical_cells = tuple(
        LogicalWorkCellV1(
            partition_id=partition_id,
            cell_id=cell_id,
            work_kind=WorkKindV1.COMPLETE_RUN,
            configuration=configuration,
        )
        for partition_id, cell_id in ordered_cells
    )

    compatibility = measure_local_worker_compatibility()
    experiment_projection = {
        "cells": [
            {"cell_id": cell_id, "partition_id": partition_id}
            for partition_id, cell_id in ordered_cells
        ],
        "duration_seconds": duration_seconds,
        "experiment_id": experiment_id,
        "liquidity": liquidity,
        "relative_volume": relative_volume,
        "resource_class": resource_class,
        "scenario_name": scenario_name,
        "schema_id": ORCHESTRATION_EXPERIMENT_SCHEMA_ID,
        "schema_version": ORCHESTRATION_EXPERIMENT_SCHEMA_VERSION,
    }
    experiment_identity = DigestReferenceV1(
        name=experiment_id,
        sha256=hashlib.sha256(canonical_json_bytes(experiment_projection)).hexdigest(),
    )
    scenario_identity = _scenario_identity(scenario_name)
    market_profile_identity = DigestReferenceV1(
        name="market-profile",
        sha256=hashlib.sha256(
            canonical_json_bytes(
                {
                    "duration_seconds": duration_seconds,
                    "liquidity": liquidity,
                    "relative_volume": relative_volume,
                    "schema_id": "KIRBY2_ORCHESTRATION_MARKET_PROFILE_V1",
                    "schema_version": 1,
                }
            )
        ).hexdigest(),
    )
    plan = build_experiment_work_plan(
        master_seed_identity=build_master_seed_identity(master_seed),
        experiment_identity=experiment_identity,
        cells=logical_cells,
        scenario=scenario_identity,
        market_profile=market_profile_identity,
        datasets=(),
        strategies=(),
        packs=(),
        software_version=__version__,
        source_version=compatibility.engine_identity.sha256,
        engine_identity=compatibility.engine_identity,
        runtime_identity=compatibility.runtime_identity,
        dependency_identity=compatibility.dependency_identity,
        compiler_identity=compatibility.compiler_identity,
        schemas=compatibility.schemas,
        capabilities=compatibility.capabilities,
        expected_outputs=complete_run_expected_output_identities(),
        resource_class=resource_class,
    )
    return plan, compatibility


def _scenario_identity(scenario_name: str) -> DigestReferenceV1:
    definitions = Path(__file__).resolve().parents[1] / "scenarios" / "accepted_scenarios.json"
    raw = definitions.read_bytes()
    digest = hashlib.sha256()
    digest.update(b"KIRBY2_ORCHESTRATION_SCENARIO_IDENTITY_V1\x00")
    digest.update(len(scenario_name.encode("ascii")).to_bytes(8, "big"))
    digest.update(scenario_name.encode("ascii"))
    digest.update(len(raw).to_bytes(8, "big"))
    digest.update(raw)
    return DigestReferenceV1(name=f"scenario:{scenario_name}", sha256=digest.hexdigest())


def _build_backend(name: str, workers: int, compatibility):
    if name == "single":
        return SingleProcessBackendV1(compatibility=compatibility)
    if name == "local":
        return LocalSubprocessBackendV1(
            worker_count=workers,
            compatibility=compatibility,
        )
    raise ValueError(f"unsupported orchestration backend {name!r}")


def _run_coordinator(path: Path, backend_name: str, workers: int) -> CoordinatorRunResultV1:
    plan, compatibility = load_orchestration_experiment(path)
    backend = _build_backend(backend_name, workers, compatibility)
    return OrchestrationCoordinatorV1().execute(plan, backend)


def _configure_orchestrate(parser: argparse.ArgumentParser) -> None:
    actions = parser.add_subparsers(dest="orchestration_action", required=True)
    plan = actions.add_parser("plan", help="build one canonical logical-work plan")
    plan.add_argument("--manifest", required=True, type=Path)
    coordinator = actions.add_parser(
        "coordinator",
        help="run one local coordinator backend",
    )
    coordinator.add_argument("--manifest", required=True, type=Path)
    coordinator.add_argument("--backend", choices=("single", "local"), default="single")
    coordinator.add_argument("--workers", type=int, default=1)
    actions.add_parser("worker", help="serve one canonical request on stdin/stdout")


def _handle_orchestrate(args: argparse.Namespace) -> int:
    if args.orchestration_action == "plan":
        plan, _compatibility = load_orchestration_experiment(args.manifest)
        _print_json(plan.as_dict())
        return 0
    if args.orchestration_action == "coordinator":
        result = _run_coordinator(args.manifest, args.backend, args.workers)
        _print_json(result.as_dict())
        return 0
    if args.orchestration_action == "worker":
        worker_main()
        return 0
    raise RuntimeError("orchestration action is not exhaustively handled")


def _configure_demo(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--backends", default="single,local")
    parser.add_argument("--workers", type=int, default=3)


def _handle_demo(args: argparse.Namespace) -> int:
    backend_names = tuple(args.backends.split(","))
    if (
        not backend_names
        or len(backend_names) != len(set(backend_names))
        or any(name not in {"single", "local"} for name in backend_names)
    ):
        raise ValueError("demo backends must be a unique comma list of single,local")
    plan, compatibility = load_orchestration_experiment(args.manifest)
    coordinator = OrchestrationCoordinatorV1()
    results = tuple(
        coordinator.execute(
            plan,
            _build_backend(name, args.workers, compatibility),
        )
        for name in backend_names
    )
    baseline = results[0]
    for candidate in results[1:]:
        if (
            candidate.aggregate_sha256 != baseline.aggregate_sha256
            or candidate.scientific_dict() != baseline.scientific_dict()
        ):
            raise RuntimeError("single/local orchestration scientific results differ")
    _print_json(
        {
            "aggregate_sha256": baseline.aggregate_sha256,
            "backends": [
                {
                    "backend_id": item.backend_id,
                    "logical_work_unit_count": len(item.verified_results),
                }
                for item in results
            ],
            "logical_work_unit_ids": [
                item.logical_work_unit_id for item in baseline.verified_results
            ],
            "plan_id": plan.plan_id,
            "schema_id": "KIRBY2_ORCHESTRATION_DEMO_RESULT_V1",
            "schema_version": 1,
            "status": "PASS",
        }
    )
    return 0


def _print_json(value: object) -> None:
    print(canonical_json_bytes(value).decode("ascii"))


def _exact_object(
    value: object,
    fields: frozenset[str],
    label: str,
) -> dict[str, object]:
    if type(value) is not dict:
        raise TypeError(f"{label} must be an exact object")
    actual = frozenset(value)
    if actual != fields:
        raise ValueError(
            f"{label} fields differ: "
            f"missing={sorted(fields - actual)} unknown={sorted(actual - fields)}"
        )
    return value


def _exact_text(payload: dict[str, object], key: str) -> str:
    value = payload[key]
    if type(value) is not str or not value:
        raise TypeError(f"{key} must be nonempty exact text")
    return value


def _exact_integer(payload: dict[str, object], key: str) -> int:
    value = payload[key]
    if type(value) is not int:
        raise TypeError(f"{key} must be an exact integer")
    return value


ORCHESTRATION_COMMAND_MODULE = CommandModule(
    module_id="DISTRIBUTED_ORCHESTRATION",
    commands=(
        CommandSpec(
            command_id="ORCHESTRATE",
            name="orchestrate",
            help="plan or execute deterministic distributed experiment work",
            handler=_handle_orchestrate,
            configure=_configure_orchestrate,
        ),
        CommandSpec(
            command_id="ORCHESTRATION_DEMO",
            name="orchestration-demo",
            help="compare single-process and local-process orchestration",
            handler=_handle_demo,
            configure=_configure_demo,
        ),
    ),
)


__all__ = [
    "ORCHESTRATION_COMMAND_MODULE",
    "ORCHESTRATION_EXPERIMENT_SCHEMA_ID",
    "ORCHESTRATION_EXPERIMENT_SCHEMA_VERSION",
    "load_orchestration_experiment",
]
