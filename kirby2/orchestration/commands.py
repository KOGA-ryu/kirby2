"""Planning plus local and explicit authenticated-LAN orchestration commands."""

from __future__ import annotations

import argparse
import hashlib
import os
import secrets
import subprocess
import tempfile
import tomllib
from dataclasses import dataclass, replace
from pathlib import Path

from kirby2 import __version__
from kirby2.cli.registry import CommandModule, CommandSpec
from kirby2.packs.formats import canonical_json_bytes
from kirby2.research.paths import DataPaths
from kirby2.scenarios import get_scenario_definition
from kirby2.simulation import LiquidityPreset, VolumePreset

from .coordinator import CoordinatorRunResultV1, OrchestrationCoordinatorV1
from .lan import (
    DEFAULT_LAN_PORT_V1,
    LanCoordinatorBackendV1,
    LanWorkerServiceV1,
)
from .leases import LeasePolicyV1
from .local import (
    LocalSubprocessBackendV1,
    LocalWorkerProcessError,
    SingleProcessBackendV1,
    fixed_local_worker_argv,
    fixed_local_worker_environment,
)
from .models import (
    DigestReferenceV1,
    ExperimentWorkPlanV1,
    LogicalWorkCellV1,
    WorkKindV1,
)
from .planner import build_experiment_work_plan
from .protocol import WorkRequestV1, WorkerCompatibilityV1, WorkerResultV1
from .recovery import (
    RecoveryCheckpointV1,
    RecoveryCompletionOrderV1,
    RecoveryCoordinatorV1,
    RecoveryEventKindV1,
    RecoveryExperimentStatusV1,
    RecoveryWorkStateV1,
)
from .resources import (
    MAX_MESSAGE_BYTES_V1,
    MAX_STREAM_BYTES_V1,
    ResourceLimitsV1,
    WorkerResourceAdvertisementV1,
)
from .security import (
    DEFAULT_LAN_BIND_HOST_V1,
    CredentialUseV1,
    LanPeerRoleV1,
    LanTlsConfigurationV1,
)
from .seeds import build_master_seed_identity
from .worker import (
    complete_run_expected_output_identities,
    main as worker_main,
    measure_local_worker_compatibility,
)


ORCHESTRATION_EXPERIMENT_SCHEMA_ID = "KIRBY2_ORCHESTRATION_EXPERIMENT_V1"
ORCHESTRATION_EXPERIMENT_SCHEMA_VERSION = 1

DEFAULT_LAN_MEMORY_BYTES_V1 = 8 * 1024 * 1024 * 1024
DEFAULT_LAN_DISK_BYTES_V1 = 8 * 1024 * 1024 * 1024
DEFAULT_LAN_ELAPSED_SECONDS_V1 = 24 * 60 * 60
DEFAULT_LAN_CLAIM_MEMORY_BYTES_V1 = 4 * 1024 * 1024 * 1024
DEFAULT_LAN_CLAIM_DISK_BYTES_V1 = 1024 * 1024 * 1024
DEFAULT_LAN_LEASE_SECONDS_V1 = 5 * 60
DEFAULT_LAN_HEARTBEAT_SECONDS_V1 = 10
DEFAULT_LAN_MISSED_HEARTBEATS_V1 = 3

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


def _seed(value: str) -> int:
    try:
        selected = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("seed must be an integer") from error
    if not 0 <= selected <= (1 << 63) - 1:
        raise argparse.ArgumentTypeError("seed must be an unsigned 63-bit integer")
    return selected


def load_orchestration_experiment(path: Path):
    """Load one strict scientific TOML manifest into an immutable work plan."""

    if not isinstance(path, Path):
        raise TypeError("orchestration manifest path must be pathlib.Path")
    path = Path(path)
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


def _run_coordinator(
    path: Path,
    backend_name: str,
    workers: int,
    *,
    lan_arguments: argparse.Namespace | None = None,
) -> CoordinatorRunResultV1:
    plan, compatibility = load_orchestration_experiment(path)
    if backend_name == "lan":
        if lan_arguments is None:
            raise ValueError("LAN backend requires explicit LAN command arguments")
        backend = _build_lan_coordinator_backend(
            lan_arguments,
            plan_id=plan.plan_id,
            compatibility=compatibility,
            workers=workers,
        )
    else:
        if lan_arguments is not None and lan_arguments.enable_lan:
            raise ValueError("--enable-lan is valid only with --backend lan")
        backend = _build_backend(backend_name, workers, compatibility)
    return OrchestrationCoordinatorV1().execute(plan, backend)


def _configure_orchestrate(parser: argparse.ArgumentParser) -> None:
    actions = parser.add_subparsers(dest="orchestration_action", required=True)
    plan = actions.add_parser("plan", help="build one canonical logical-work plan")
    plan.add_argument("--manifest", required=True, type=Path)
    coordinator = actions.add_parser(
        "coordinator",
        help="run one explicit coordinator backend",
    )
    coordinator.add_argument("--manifest", required=True, type=Path)
    coordinator.add_argument(
        "--backend",
        choices=("single", "local", "lan"),
        default="single",
    )
    coordinator.add_argument("--workers", type=int, default=1)
    actions.add_parser("worker", help="serve one canonical request on stdin/stdout")
    _configure_lan_coordinator(coordinator)
    submit = actions.add_parser(
        "submit",
        help="persist one canonical experiment for durable execution",
    )
    submit.add_argument("--manifest", required=True, type=Path)
    submit.add_argument("--data-root", required=True, type=_data_root)
    status = actions.add_parser(
        "status",
        help="read the current durable experiment checkpoint",
    )
    _configure_recovery_plan_target(status)
    cancel = actions.add_parser(
        "cancel",
        help="cancel every not-yet-registered logical work unit",
    )
    _configure_recovery_plan_target(cancel)
    cancel.add_argument("--reason-code", default="OPERATOR_CANCELLED")
    resume = actions.add_parser(
        "resume",
        help="recover and execute every outstanding logical work unit",
    )
    _configure_recovery_plan_target(resume)
    resume.add_argument(
        "--backend",
        choices=("single", "local", "lan"),
        default="single",
    )
    resume.add_argument("--workers", type=int, default=1)
    resume.add_argument(
        "--completion-order",
        choices=("canonical", "reverse"),
        default="canonical",
    )
    _configure_lan_coordinator(resume)
    lan_worker = actions.add_parser(
        "lan-worker",
        help="connect one explicit mTLS worker to a LAN coordinator",
    )
    _configure_lan_worker(lan_worker)


def _handle_orchestrate(args: argparse.Namespace) -> int:
    if args.orchestration_action == "plan":
        plan, _compatibility = load_orchestration_experiment(args.manifest)
        _print_json(plan.as_dict())
        return 0
    if args.orchestration_action == "coordinator":
        result = _run_coordinator(
            args.manifest,
            args.backend,
            args.workers,
            lan_arguments=args,
        )
        _print_json(result.as_dict())
        return 0
    if args.orchestration_action == "submit":
        plan, _compatibility = load_orchestration_experiment(args.manifest)
        checkpoint = RecoveryCoordinatorV1(DataPaths(args.data_root)).submit(plan)
        _print_json(_checkpoint_summary(checkpoint))
        return 0
    if args.orchestration_action == "status":
        checkpoint = RecoveryCoordinatorV1(DataPaths(args.data_root)).status(
            args.plan_id
        )
        _print_json(_checkpoint_summary(checkpoint))
        return 0
    if args.orchestration_action == "cancel":
        checkpoint = RecoveryCoordinatorV1(DataPaths(args.data_root)).cancel(
            args.plan_id,
            reason_code=args.reason_code,
        )
        _print_json(_checkpoint_summary(checkpoint))
        return 0
    if args.orchestration_action == "resume":
        recovery = RecoveryCoordinatorV1(DataPaths(args.data_root))
        checkpoint = recovery.status(args.plan_id)
        compatibility = measure_local_worker_compatibility()
        if args.backend == "lan":
            backend = _build_lan_coordinator_backend(
                args,
                plan_id=checkpoint.plan_id,
                compatibility=compatibility,
                workers=args.workers,
            )
        else:
            if args.enable_lan:
                raise ValueError("--enable-lan is valid only with --backend lan")
            backend = _build_backend(args.backend, args.workers, compatibility)
        checkpoint = recovery.resume(
            checkpoint.plan_id,
            backend,
            completion_order=RecoveryCompletionOrderV1(
                args.completion_order.upper()
            ),
        )
        _print_json(_checkpoint_summary(checkpoint))
        return 0
    if args.orchestration_action == "worker":
        worker_main()
        return 0
    if args.orchestration_action == "lan-worker":
        results = _run_lan_worker(args)
        _print_json(
            {
                "backend_id": "authenticated-lan-worker-v1",
                "result_count": len(results),
                "schema_id": "KIRBY2_LAN_WORKER_SESSION_RESULT_V1",
                "schema_version": 1,
                "scientific_result_sha256s": [
                    item.scientific_result_sha256 for item in results
                ],
            }
        )
        return 0
    raise RuntimeError("orchestration action is not exhaustively handled")


def _configure_recovery_plan_target(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--data-root", required=True, type=_data_root)
    parser.add_argument("--plan-id", required=True)


def _configure_lan_coordinator(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--enable-lan", action="store_true")
    parser.add_argument("--lan-host", default=DEFAULT_LAN_BIND_HOST_V1)
    parser.add_argument("--lan-port", type=int, default=DEFAULT_LAN_PORT_V1)
    parser.add_argument("--lan-ca-certificate", type=Path)
    parser.add_argument("--lan-certificate", type=Path)
    parser.add_argument("--lan-private-key", type=Path)
    parser.add_argument("--lan-identity")
    parser.add_argument("--lan-worker-identities")
    parser.add_argument("--lan-audit-fixture", action="store_true")
    parser.add_argument("--lan-timeout-seconds", type=int, default=60)
    parser.add_argument(
        "--lan-maximum-memory-bytes",
        type=int,
        default=DEFAULT_LAN_MEMORY_BYTES_V1,
    )
    parser.add_argument(
        "--lan-maximum-disk-bytes",
        type=int,
        default=DEFAULT_LAN_DISK_BYTES_V1,
    )
    parser.add_argument(
        "--lan-maximum-elapsed-seconds",
        type=int,
        default=DEFAULT_LAN_ELAPSED_SECONDS_V1,
    )
    parser.add_argument(
        "--lan-maximum-message-bytes",
        type=int,
        default=MAX_MESSAGE_BYTES_V1,
    )
    parser.add_argument(
        "--lan-maximum-stream-bytes",
        type=int,
        default=MAX_STREAM_BYTES_V1,
    )
    parser.add_argument(
        "--lan-claim-memory-bytes",
        type=int,
        default=DEFAULT_LAN_CLAIM_MEMORY_BYTES_V1,
    )
    parser.add_argument(
        "--lan-claim-disk-bytes",
        type=int,
        default=DEFAULT_LAN_CLAIM_DISK_BYTES_V1,
    )
    parser.add_argument(
        "--lan-claim-elapsed-seconds",
        type=int,
        default=DEFAULT_LAN_ELAPSED_SECONDS_V1,
    )
    parser.add_argument(
        "--lan-lease-seconds",
        type=int,
        default=DEFAULT_LAN_LEASE_SECONDS_V1,
    )
    parser.add_argument(
        "--lan-heartbeat-seconds",
        type=int,
        default=DEFAULT_LAN_HEARTBEAT_SECONDS_V1,
    )
    parser.add_argument(
        "--lan-maximum-missed-heartbeats",
        type=int,
        default=DEFAULT_LAN_MISSED_HEARTBEATS_V1,
    )


def _configure_lan_worker(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--enable-lan", action="store_true")
    parser.add_argument("--lan-host", default=DEFAULT_LAN_BIND_HOST_V1)
    parser.add_argument("--lan-port", type=int, default=DEFAULT_LAN_PORT_V1)
    parser.add_argument("--lan-ca-certificate", required=True, type=Path)
    parser.add_argument("--lan-certificate", required=True, type=Path)
    parser.add_argument("--lan-private-key", required=True, type=Path)
    parser.add_argument("--lan-identity", required=True)
    parser.add_argument("--lan-coordinator-identity", required=True)
    parser.add_argument("--lan-server-hostname", required=True)
    parser.add_argument(
        "--lan-pinned-coordinator-certificate-sha256",
        required=True,
    )
    parser.add_argument("--lan-resource-classes", required=True)
    parser.add_argument("--lan-audit-fixture", action="store_true")
    parser.add_argument("--lan-timeout-seconds", type=int, default=60)
    parser.add_argument("--lan-maximum-concurrent-runs", type=int, default=1)
    parser.add_argument("--lan-maximum-queue-depth", type=int, default=0)
    parser.add_argument(
        "--lan-maximum-memory-bytes",
        type=int,
        default=DEFAULT_LAN_MEMORY_BYTES_V1,
    )
    parser.add_argument(
        "--lan-maximum-disk-bytes",
        type=int,
        default=DEFAULT_LAN_DISK_BYTES_V1,
    )
    parser.add_argument(
        "--lan-maximum-elapsed-seconds",
        type=int,
        default=DEFAULT_LAN_ELAPSED_SECONDS_V1,
    )
    parser.add_argument(
        "--lan-maximum-message-bytes",
        type=int,
        default=MAX_MESSAGE_BYTES_V1,
    )
    parser.add_argument(
        "--lan-maximum-stream-bytes",
        type=int,
        default=MAX_STREAM_BYTES_V1,
    )


def _build_lan_coordinator_backend(
    args: argparse.Namespace,
    *,
    plan_id: str,
    compatibility,
    workers: int,
) -> LanCoordinatorBackendV1:
    if not args.enable_lan:
        raise ValueError("LAN backend requires the explicit --enable-lan opt-in")
    expected_workers = _canonical_csv(
        args.lan_worker_identities,
        "LAN worker identities",
    )
    configuration = LanTlsConfigurationV1(
        role=LanPeerRoleV1.COORDINATOR,
        enabled=args.enable_lan,
        host=args.lan_host,
        port=args.lan_port,
        ca_certificate=_required_resolved_file(
            args.lan_ca_certificate,
            "LAN CA certificate",
        ),
        certificate=_required_resolved_file(
            args.lan_certificate,
            "LAN coordinator certificate",
        ),
        private_key=_required_resolved_file(
            args.lan_private_key,
            "LAN coordinator private key",
        ),
        local_identity=_required_text(args.lan_identity, "LAN coordinator identity"),
        expected_peer_identities=expected_workers,
        credential_use=_credential_use(args.lan_audit_fixture),
    )
    limits = ResourceLimitsV1(
        maximum_concurrent_runs=1,
        maximum_queue_depth=0,
        maximum_memory_bytes_per_run=args.lan_maximum_memory_bytes,
        maximum_disk_bytes_per_run=args.lan_maximum_disk_bytes,
        maximum_elapsed_seconds_per_run=args.lan_maximum_elapsed_seconds,
        maximum_message_bytes=args.lan_maximum_message_bytes,
        maximum_stream_bytes=args.lan_maximum_stream_bytes,
    )
    return LanCoordinatorBackendV1(
        configuration=configuration,
        compatibility=compatibility,
        plan_id=plan_id,
        worker_count=workers,
        transport_limits=limits,
        lease_policy=LeasePolicyV1(
            lease_seconds=args.lan_lease_seconds,
            heartbeat_interval_seconds=args.lan_heartbeat_seconds,
            maximum_missed_heartbeats=args.lan_maximum_missed_heartbeats,
        ),
        claim_memory_bytes=args.lan_claim_memory_bytes,
        claim_disk_bytes=args.lan_claim_disk_bytes,
        claim_elapsed_seconds=args.lan_claim_elapsed_seconds,
        connection_timeout_seconds=args.lan_timeout_seconds,
        allow_audit_fixture=args.lan_audit_fixture,
    )


def _run_lan_worker(args: argparse.Namespace):
    if not args.enable_lan:
        raise ValueError("LAN worker requires the explicit --enable-lan opt-in")
    compatibility = measure_local_worker_compatibility()
    limits = ResourceLimitsV1(
        maximum_concurrent_runs=args.lan_maximum_concurrent_runs,
        maximum_queue_depth=args.lan_maximum_queue_depth,
        maximum_memory_bytes_per_run=args.lan_maximum_memory_bytes,
        maximum_disk_bytes_per_run=args.lan_maximum_disk_bytes,
        maximum_elapsed_seconds_per_run=args.lan_maximum_elapsed_seconds,
        maximum_message_bytes=args.lan_maximum_message_bytes,
        maximum_stream_bytes=args.lan_maximum_stream_bytes,
    )
    identity = _required_text(args.lan_identity, "LAN worker identity")
    configuration = LanTlsConfigurationV1(
        role=LanPeerRoleV1.WORKER,
        enabled=args.enable_lan,
        host=args.lan_host,
        port=args.lan_port,
        ca_certificate=_required_resolved_file(
            args.lan_ca_certificate,
            "LAN CA certificate",
        ),
        certificate=_required_resolved_file(
            args.lan_certificate,
            "LAN worker certificate",
        ),
        private_key=_required_resolved_file(
            args.lan_private_key,
            "LAN worker private key",
        ),
        local_identity=identity,
        expected_peer_identities=(
            _required_text(
                args.lan_coordinator_identity,
                "LAN coordinator identity",
            ),
        ),
        credential_use=_credential_use(args.lan_audit_fixture),
        server_hostname=_required_text(
            args.lan_server_hostname,
            "LAN coordinator server hostname",
        ),
        pinned_coordinator_certificate_sha256=_sha256_text(
            args.lan_pinned_coordinator_certificate_sha256,
            "LAN coordinator certificate pin",
        ),
    )
    resources = WorkerResourceAdvertisementV1(
        worker_id=identity,
        worker_compatibility_sha256=compatibility.compatibility_sha256,
        resource_classes=_canonical_csv(
            args.lan_resource_classes,
            "LAN resource classes",
        ),
        limits=limits,
        advertisement_nonce=hashlib.sha256(secrets.token_bytes(32)).hexdigest(),
    )
    return LanWorkerServiceV1(
        configuration=configuration,
        compatibility=compatibility,
        resources=resources,
        connection_timeout_seconds=args.lan_timeout_seconds,
        allow_audit_fixture=args.lan_audit_fixture,
    ).run()


def _credential_use(audit_fixture: bool) -> CredentialUseV1:
    if type(audit_fixture) is not bool:
        raise TypeError("LAN audit-fixture flag must be boolean")
    if audit_fixture:
        return CredentialUseV1.AUDIT_LOOPBACK_FIXTURE
    return CredentialUseV1.OPERATOR_PRODUCTION


def _required_resolved_file(value: Path | None, label: str) -> Path:
    if not isinstance(value, Path):
        raise ValueError(f"{label} is required for LAN startup")
    value = Path(value)
    try:
        return value.resolve(strict=True)
    except OSError as error:
        raise ValueError(f"{label} cannot be resolved") from error


def _required_text(value: object, label: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ValueError(f"{label} must be nonempty canonical text")
    return value


def _canonical_csv(value: object, label: str) -> tuple[str, ...]:
    text = _required_text(value, label)
    items = tuple(item.strip() for item in text.split(","))
    if any(not item for item in items) or len(items) != len(set(items)):
        raise ValueError(f"{label} must be a unique nonempty comma list")
    return tuple(sorted(items))


def _sha256_text(value: object, label: str) -> str:
    text = _required_text(value, label)
    if len(text) != 64 or any(
        character not in "0123456789abcdef" for character in text
    ):
        raise ValueError(f"{label} must be one lowercase SHA-256 digest")
    return text


def _checkpoint_summary(checkpoint: RecoveryCheckpointV1) -> dict[str, object]:
    if type(checkpoint) is not RecoveryCheckpointV1:
        raise TypeError("checkpoint summary requires RecoveryCheckpointV1")
    return {
        "aggregate_sha256": (
            None
            if checkpoint.aggregate is None
            else checkpoint.aggregate.aggregate_sha256
        ),
        "checkpoint_sha256": checkpoint.checkpoint_sha256,
        "event_count": len(checkpoint.events),
        "latest_event": checkpoint.events[-1].as_dict(),
        "logical_work_unit_count": len(checkpoint.records),
        "plan_id": checkpoint.plan_id,
        "revision": checkpoint.revision,
        "schema_id": "KIRBY2_RECOVERY_STATUS_SUMMARY_V1",
        "schema_version": 1,
        "state_counts": {
            state.value: sum(item.state is state for item in checkpoint.records)
            for state in RecoveryWorkStateV1
        },
        "status": checkpoint.status.value,
    }


class _SimulatedCoordinatorRestart(BaseException):
    """Demo-only process boundary; deliberately bypasses normal failure cleanup."""


@dataclass(frozen=True, slots=True)
class _KilledWorkerBackendV1:
    """Kill one fixed worker mid-frame, then simulate coordinator process loss."""

    compatibility: WorkerCompatibilityV1

    def __post_init__(self) -> None:
        if type(self.compatibility) is not WorkerCompatibilityV1:
            raise TypeError("killed-worker demo requires WorkerCompatibilityV1")

    @property
    def backend_id(self) -> str:
        return "killed-local-worker-demo-v1"

    def execute_many(
        self,
        requests: tuple[WorkRequestV1, ...],
    ) -> tuple[WorkerResultV1, ...]:
        if type(requests) is not tuple or not requests or any(
            type(item) is not WorkRequestV1 for item in requests
        ):
            raise TypeError("killed-worker demo requires canonical work requests")
        request = min(
            requests,
            key=lambda item: item.logical_work_unit.logical_work_unit_id,
        )
        environment = fixed_local_worker_environment(os.environ)
        process = subprocess.Popen(
            fixed_local_worker_argv(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
        )
        if process.stdin is None:
            process.kill()
            process.wait()
            raise LocalWorkerProcessError("killed-worker demo did not receive stdin")
        raw = request.canonical_bytes()
        try:
            process.stdin.write(raw[: max(1, len(raw) // 2)])
            process.stdin.flush()
            process.kill()
            process.communicate()
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
        if process.returncode == 0:
            raise LocalWorkerProcessError("demo worker exited before it could be killed")
        raise _SimulatedCoordinatorRestart()


def _configure_distributed_demo(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--seed", type=_seed, default=42)
    parser.add_argument("--kill-worker", action="store_true")
    parser.add_argument("--workers", type=int, default=3)


def _handle_distributed_demo(args: argparse.Namespace) -> int:
    if type(args.workers) is not int or not 2 <= args.workers <= 64:
        raise ValueError("distributed demo requires between 2 and 64 local workers")
    template_path = Path(__file__).resolve().parent / "examples" / "small.toml"
    template = template_path.read_text(encoding="utf-8")
    if template.count("master_seed = 42") != 1:
        raise RuntimeError("distributed demo manifest template is not canonical")
    manifest_text = template.replace(
        "master_seed = 42",
        f"master_seed = {args.seed}",
    )
    with tempfile.TemporaryDirectory(prefix="kirby2-distributed-demo-") as temporary:
        root = Path(temporary).resolve(strict=True)
        manifest_path = root / "experiment.toml"
        manifest_path.write_text(manifest_text, encoding="utf-8", newline="\n")
        base_plan, compatibility = load_orchestration_experiment(manifest_path)
        strategy_identity = DigestReferenceV1(
            name="demo-strategy:passive-observer-v1",
            sha256=hashlib.sha256(
                canonical_json_bytes(
                    {
                        "decision_policy": "PASSIVE_OBSERVER",
                        "schema_id": "KIRBY2_DISTRIBUTED_DEMO_STRATEGY_V1",
                        "schema_version": 1,
                    }
                )
            ).hexdigest(),
        )
        strategy_units = tuple(
            sorted(
                (
                    replace(unit, strategies=(strategy_identity,))
                    for unit in base_plan.logical_units
                ),
                key=lambda item: item.logical_work_unit_id,
            )
        )
        plan = ExperimentWorkPlanV1(
            master_seed_identity=base_plan.master_seed_identity,
            experiment_identity=base_plan.experiment_identity,
            logical_units=strategy_units,
        )

        reference = RecoveryCoordinatorV1(DataPaths(root / "reference"))
        reference.submit(plan)
        reference_checkpoint = reference.resume(
            plan.plan_id,
            SingleProcessBackendV1(compatibility=compatibility),
        )

        recovered_root = root / "recovered"
        recovered = RecoveryCoordinatorV1(DataPaths(recovered_root))
        recovered.submit(plan)
        coordinator_restarted = False
        worker_killed = False
        if args.kill_worker:
            try:
                recovered.resume(
                    plan.plan_id,
                    _KilledWorkerBackendV1(compatibility=compatibility),
                )
            except _SimulatedCoordinatorRestart:
                worker_killed = True
                coordinator_restarted = True
            else:
                raise RuntimeError("distributed demo did not interrupt its worker")
            interrupted = RecoveryCoordinatorV1(
                DataPaths(recovered_root)
            ).status(plan.plan_id)
            if not all(
                item.state is RecoveryWorkStateV1.IN_FLIGHT
                for item in interrupted.records
            ):
                raise RuntimeError("interrupted coordinator did not retain in-flight work")
            recovered = RecoveryCoordinatorV1(DataPaths(recovered_root))
        recovered_checkpoint = recovered.resume(
            plan.plan_id,
            LocalSubprocessBackendV1(
                worker_count=args.workers,
                compatibility=compatibility,
            ),
            completion_order=RecoveryCompletionOrderV1.REVERSE,
        )

        if (
            reference_checkpoint.status is not RecoveryExperimentStatusV1.COMPLETED
            or recovered_checkpoint.status is not RecoveryExperimentStatusV1.COMPLETED
            or reference_checkpoint.aggregate is None
            or recovered_checkpoint.aggregate is None
        ):
            raise RuntimeError("distributed demo did not complete both whole experiments")
        if (
            reference_checkpoint.aggregate.as_dict()
            != recovered_checkpoint.aggregate.as_dict()
        ):
            raise RuntimeError("recovered experiment differs from its reference")
        seeds = tuple(unit.seed for unit in plan.logical_units)
        if len(seeds) != len(set(seeds)):
            raise RuntimeError("distributed demo reused a derived seed")
        recovered_ids = tuple(
            item.logical_work_unit_id for item in recovered_checkpoint.records
        )
        planned_ids = tuple(unit.logical_work_unit_id for unit in plan.logical_units)
        if recovered_ids != planned_ids:
            raise RuntimeError("distributed demo lost or invented logical work")
        event_kinds = tuple(item.kind for item in recovered_checkpoint.events)
        if args.kill_worker and (
            RecoveryEventKindV1.LEASE_EXPIRED not in event_kinds
            or RecoveryEventKindV1.ATTEMPT_REISSUED not in event_kinds
        ):
            raise RuntimeError("distributed demo did not record expiry and reissue")
        _print_json(
            {
                "aggregate_sha256": recovered_checkpoint.aggregate.aggregate_sha256,
                "completion_order": RecoveryCompletionOrderV1.REVERSE.value,
                "coordinator_restarted": coordinator_restarted,
                "lan": {
                    "reason_code": "NO_EXPLICIT_LAN_CONFIGURATION",
                    "status": "NOT_EXERCISED",
                },
                "local_worker_count": args.workers,
                "logical_work_unit_count": len(plan.logical_units),
                "plan_id": plan.plan_id,
                "reference_backend": "single-process-v1",
                "recovered_backend": "local-subprocess-v1",
                "retry_attempt_numbers": [
                    item.attempt_number for item in recovered_checkpoint.records
                ],
                "schema_id": "KIRBY2_DISTRIBUTED_RECOVERY_DEMO_V1",
                "schema_version": 1,
                "seed_count": len(seeds),
                "status": "PASS",
                "strategy_identity": strategy_identity.as_dict(),
                "worker_killed": worker_killed,
            }
        )
    return 0


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
        CommandSpec(
            command_id="DISTRIBUTED_DEMO",
            name="distributed-demo",
            help="demonstrate worker kill, coordinator restart, and exact recovery",
            handler=_handle_distributed_demo,
            configure=_configure_distributed_demo,
        ),
    ),
)


__all__ = [
    "ORCHESTRATION_COMMAND_MODULE",
    "ORCHESTRATION_EXPERIMENT_SCHEMA_ID",
    "ORCHESTRATION_EXPERIMENT_SCHEMA_VERSION",
    "load_orchestration_experiment",
]
