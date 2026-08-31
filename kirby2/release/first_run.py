"""Offline first-run flow and the bundled two-pack release starter set.

This module performs no discovery outside the installed Kirby2 package and the
explicit :class:`~kirby2.research.paths.DataPaths` root.  The starter content is
ordinary data-only WO39 packs built from committed resources; it has no account,
network, brokerage, or real-market execution capability.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
import tomllib
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import ClassVar

from kirby2.exchange import Order, OrderBook, OrderOwner, OrderStatus, Side
from kirby2.packs.builders import (
    DomainPackBuildV1,
    build_domain_pack,
    builtin_pack_runtime_environment_v1,
    verify_domain_pack_archive_bytes,
)
from kirby2.packs.dependencies import PackRuntimeEnvironmentV1
from kirby2.packs.formats import canonical_json_bytes
from kirby2.packs.install import install_pack, read_pack_registry
from kirby2.packs.models import (
    PackContentFormatV1,
    PackContentModeV1,
    PackCreatorV1,
    PackDependencyV1,
    PackLicenseV1,
    PackRedistributionPolicyV1,
    PackTypeV1,
)
from kirby2.packs.scenario_pack import build_scenario_demo_inputs
from kirby2.packs.staging import discard_pack_stage, stage_pack_archive_bytes
from kirby2.packs.types import (
    PackArtifactRoleV1,
    PackArtifactStorageModeV1,
    PackBuildSpecificationV1,
    PackSourceArtifactV1,
)
from kirby2.research.paths import DataAreaId, DataPaths


RELEASE_STARTER_SET_ID_V1 = "RELEASE_STARTER_SET_V1"
RELEASE_STARTER_SET_SCHEMA_VERSION_V1 = 1
RELEASE_FIRST_RUN_SCHEMA_ID_V1 = "KIRBY2_RELEASE_FIRST_RUN_V1"
RELEASE_FIRST_RUN_SCHEMA_VERSION_V1 = 1
RELEASE_STARTER_INSTALL_SCHEMA_ID_V1 = "KIRBY2_RELEASE_STARTER_INSTALL_V1"
RELEASE_STARTER_DEMO_SCHEMA_ID_V1 = "KIRBY2_RELEASE_STARTER_DEMO_V1"
RELEASE_STARTER_LESSON_ID_V1 = "KIRBY2_STARTER_PLACE_CANCEL_V1"
RELEASE_STARTER_CHECKPOINT_SELECTOR_V1 = (
    "FIRST_QUIESCENT_CONTINUOUS_TWO_SIDED_V1"
)
RELEASE_STARTER_NAMESPACE_V1 = "kirby2.examples"

STARTER_SCENARIO_MANIFEST_PATH_V1 = (
    "kirby2/packs/fixtures/samples/starter_scenario/manifest.toml"
)
STARTER_CURRICULUM_MANIFEST_PATH_V1 = (
    "kirby2/packs/fixtures/samples/five_lesson_curriculum/manifest.toml"
)

_MAX_STARTER_RESOURCE_BYTES_V1 = 2 * 1024 * 1024


class StarterSetRoleV1(str, Enum):
    SCENARIO = "SCENARIO"
    CURRICULUM = "CURRICULUM"


class StarterInstallDispositionV1(str, Enum):
    INSTALLED = "INSTALLED"
    ALREADY_PRESENT = "ALREADY_PRESENT"
    OFFERED_CONFLICT = "OFFERED_CONFLICT"
    INCOMPLETE = "INCOMPLETE"


@dataclass(frozen=True, slots=True)
class ReleaseStarterSetEntryV1:
    role: StarterSetRoleV1
    manifest_path: str
    manifest_sha256: str
    pack_id: str
    archive_sha256: str

    def __post_init__(self) -> None:
        if type(self.role) is not StarterSetRoleV1:
            raise TypeError("starter-set role is invalid")
        if not self.manifest_path or Path(self.manifest_path).is_absolute():
            raise ValueError("starter-set manifest path must be repository-relative")
        for value, label in (
            (self.manifest_sha256, "starter manifest"),
            (self.pack_id, "starter pack"),
            (self.archive_sha256, "starter archive"),
        ):
            _require_sha256(value, label)

    def layout_dict(self) -> dict[str, object]:
        """Return the exact four-field projection frozen by WO40-D."""

        return {
            "manifest_path": self.manifest_path,
            "manifest_sha256": self.manifest_sha256,
            "pack_id": self.pack_id,
            "role": self.role.value,
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.layout_dict(), "archive_sha256": self.archive_sha256}


@dataclass(frozen=True, slots=True)
class ReleaseStarterSetV1:
    entries: tuple[ReleaseStarterSetEntryV1, ...]
    builds: tuple[DomainPackBuildV1, ...]

    schema_version: ClassVar[int] = RELEASE_STARTER_SET_SCHEMA_VERSION_V1
    set_id: ClassVar[str] = RELEASE_STARTER_SET_ID_V1

    def __post_init__(self) -> None:
        if (
            type(self.entries) is not tuple
            or len(self.entries) != 2
            or any(type(item) is not ReleaseStarterSetEntryV1 for item in self.entries)
        ):
            raise TypeError("release starter set must contain exactly two typed entries")
        if tuple(item.role for item in self.entries) != (
            StarterSetRoleV1.SCENARIO,
            StarterSetRoleV1.CURRICULUM,
        ):
            raise ValueError("release starter set order must be scenario then curriculum")
        if type(self.builds) is not tuple or len(self.builds) != 2 or any(
            type(item) is not DomainPackBuildV1 for item in self.builds
        ):
            raise TypeError("release starter builds are invalid")
        if tuple(item.manifest.pack_id for item in self.builds) != tuple(
            item.pack_id for item in self.entries
        ):
            raise ValueError("release starter entries differ from their built packs")
        dependency = self.builds[1].manifest.dependencies
        if len(dependency) != 1 or dependency[0].expected_pack_id != self.entries[0].pack_id:
            raise ValueError("starter curriculum does not bind the exact scenario pack")

    @property
    def entries_sha256(self) -> str:
        return hashlib.sha256(
            canonical_json_bytes([item.layout_dict() for item in self.entries])
        ).hexdigest()

    def layout_dict(self) -> dict[str, object]:
        return {
            "entries": [item.layout_dict() for item in self.entries],
            "entries_sha256": self.entries_sha256,
            "schema_version": self.schema_version,
            "set_id": self.set_id,
        }

    def as_dict(self) -> dict[str, object]:
        return {
            **self.layout_dict(),
            "archive_sha256": [item.archive_sha256 for item in self.entries],
        }


@dataclass(frozen=True, slots=True)
class StarterInstallReportV1:
    disposition: StarterInstallDispositionV1
    complete: bool
    entries: tuple[dict[str, object], ...]
    conflict_entries: tuple[dict[str, object], ...]
    detail: str

    schema_id: ClassVar[str] = RELEASE_STARTER_INSTALL_SCHEMA_ID_V1
    schema_version: ClassVar[int] = 1

    def as_dict(self) -> dict[str, object]:
        return {
            "complete": self.complete,
            "conflict_entries": list(self.conflict_entries),
            "detail": self.detail,
            "disposition": self.disposition.value,
            "entries": list(self.entries),
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "set_id": RELEASE_STARTER_SET_ID_V1,
        }


@dataclass(frozen=True, slots=True)
class StarterPlaceCancelDemoV1:
    seed: int
    lesson_id: str
    checkpoint_selector: str
    order_id: str
    cancel_command_id: str
    best_bid_ticks: int
    best_ask_ticks: int
    state_before_sha256: str
    state_after_sha256: str
    event_stream_sha256: str
    event_count: int
    status: str

    schema_id: ClassVar[str] = RELEASE_STARTER_DEMO_SCHEMA_ID_V1
    schema_version: ClassVar[int] = 1

    def as_dict(self) -> dict[str, object]:
        return {
            "best_ask_ticks": self.best_ask_ticks,
            "best_bid_ticks": self.best_bid_ticks,
            "cancel_command_id": self.cancel_command_id,
            "checkpoint_selector": self.checkpoint_selector,
            "event_count": self.event_count,
            "event_stream_sha256": self.event_stream_sha256,
            "lesson_id": self.lesson_id,
            "order_id": self.order_id,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "seed": self.seed,
            "state_after_sha256": self.state_after_sha256,
            "state_before_sha256": self.state_before_sha256,
            "status": self.status,
        }


@dataclass(frozen=True, slots=True)
class FirstRunReportV1:
    complete: bool
    identity: dict[str, object]
    created_paths: tuple[dict[str, object], ...]
    writable_checks: tuple[dict[str, object], ...]
    health: dict[str, object]
    starter_set: dict[str, object]
    starter_install: StarterInstallReportV1
    demonstration: StarterPlaceCancelDemoV1
    data_paths: dict[str, object]

    schema_id: ClassVar[str] = RELEASE_FIRST_RUN_SCHEMA_ID_V1
    schema_version: ClassVar[int] = RELEASE_FIRST_RUN_SCHEMA_VERSION_V1

    def as_dict(self) -> dict[str, object]:
        return {
            "complete": self.complete,
            "created_paths": list(self.created_paths),
            "data_paths": self.data_paths,
            "demonstration": self.demonstration.as_dict(),
            "health": self.health,
            "identity": self.identity,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "starter_install": self.starter_install.as_dict(),
            "starter_set": self.starter_set,
            "writable_checks": list(self.writable_checks),
        }


def build_release_starter_set() -> ReleaseStarterSetV1:
    """Build and fully adapter-verify both committed starter resources in memory."""

    scenario_path = _installed_resource(STARTER_SCENARIO_MANIFEST_PATH_V1)
    curriculum_path = _installed_resource(STARTER_CURRICULUM_MANIFEST_PATH_V1)
    scenario_raw = _read_resource(scenario_path)
    curriculum_raw = _read_resource(curriculum_path)

    scenario_specification, scenario_payloads = build_scenario_demo_inputs(
        scenario_path
    )
    scenario_build = build_domain_pack(
        scenario_specification,
        scenario_payloads,
        source_definition_sha256=hashlib.sha256(scenario_raw).hexdigest(),
    )

    curriculum_source = _parse_curriculum_resource(curriculum_raw)
    curriculum_specification, curriculum_payloads = _curriculum_inputs(
        curriculum_source,
        scenario_build,
    )
    curriculum_build = build_domain_pack(
        curriculum_specification,
        curriculum_payloads,
        source_definition_sha256=hashlib.sha256(curriculum_raw).hexdigest(),
    )
    entries = (
        ReleaseStarterSetEntryV1(
            role=StarterSetRoleV1.SCENARIO,
            manifest_path=STARTER_SCENARIO_MANIFEST_PATH_V1,
            manifest_sha256=hashlib.sha256(scenario_raw).hexdigest(),
            pack_id=scenario_build.manifest.pack_id,
            archive_sha256=scenario_build.transport_sha256,
        ),
        ReleaseStarterSetEntryV1(
            role=StarterSetRoleV1.CURRICULUM,
            manifest_path=STARTER_CURRICULUM_MANIFEST_PATH_V1,
            manifest_sha256=hashlib.sha256(curriculum_raw).hexdigest(),
            pack_id=curriculum_build.manifest.pack_id,
            archive_sha256=curriculum_build.transport_sha256,
        ),
    )
    return ReleaseStarterSetV1(
        entries=entries,
        builds=(scenario_build, curriculum_build),
    )


def install_release_starter_set(
    paths: DataPaths,
    starter_set: ReleaseStarterSetV1 | None = None,
) -> StarterInstallReportV1:
    """Install missing exact starter members, or explicitly offer on conflict."""

    if type(paths) is not DataPaths:
        raise TypeError("starter installation requires exact DataPaths")
    selected = build_release_starter_set() if starter_set is None else starter_set
    if type(selected) is not ReleaseStarterSetV1:
        raise TypeError("starter installation requires ReleaseStarterSetV1")

    registry = read_pack_registry(paths=paths)
    expected_by_key = {
        build.manifest.registry_key: build for build in selected.builds
    }
    namespace_entries = tuple(
        entry
        for entry in registry.entries
        if entry.key.namespace == RELEASE_STARTER_NAMESPACE_V1
    )
    conflicts = tuple(
        entry
        for entry in namespace_entries
        if entry.key not in expected_by_key
        or entry.pack_id != expected_by_key[entry.key].manifest.pack_id
        or not entry.active
    )
    if conflicts:
        return StarterInstallReportV1(
            disposition=StarterInstallDispositionV1.OFFERED_CONFLICT,
            complete=False,
            entries=tuple(_registry_entry_summary(item) for item in namespace_entries),
            conflict_entries=tuple(_registry_entry_summary(item) for item in conflicts),
            detail=(
                "The starter namespace already contains a conflicting or inactive "
                "binding. The bundled set was offered but nothing was overwritten."
            ),
        )

    present = {
        entry.key: entry
        for entry in namespace_entries
        if entry.key in expected_by_key
        and entry.pack_id == expected_by_key[entry.key].manifest.pack_id
        and entry.active
    }
    missing = tuple(
        build for build in selected.builds if build.manifest.registry_key not in present
    )
    if not missing:
        return StarterInstallReportV1(
            disposition=StarterInstallDispositionV1.ALREADY_PRESENT,
            complete=True,
            entries=tuple(
                _registry_entry_summary(present[build.manifest.registry_key])
                for build in selected.builds
            ),
            conflict_entries=(),
            detail="Both exact starter packs are already active.",
        )

    paths.ensure((DataAreaId.PACKS, DataAreaId.STAGING))
    environment = _starter_runtime_environment(selected)
    receipts: list[dict[str, object]] = []
    try:
        for build in missing:
            verification = verify_domain_pack_archive_bytes(
                build.archive_bytes,
                expected_pack_id=build.manifest.pack_id,
            )
            if verification.preflight != build.preflight:
                raise ValueError("starter archive verification changed before staging")
            stage = stage_pack_archive_bytes(
                build.archive_bytes,
                paths.staging,
                expected_pack_id=build.manifest.pack_id,
                expected_transport_sha256=build.transport_sha256,
            )
            try:
                receipt = install_pack(stage, paths=paths, environment=environment)
            except Exception as install_error:
                try:
                    discard_pack_stage(stage)
                except Exception as cleanup_error:
                    raise RuntimeError(
                        "starter install failed and its exact stage could not be "
                        f"discarded: {cleanup_error}"
                    ) from install_error
                raise
            receipts.append(receipt.as_dict())
    except Exception as error:
        current = read_pack_registry(paths=paths)
        return StarterInstallReportV1(
            disposition=StarterInstallDispositionV1.INCOMPLETE,
            complete=False,
            entries=tuple(
                _registry_entry_summary(entry)
                for entry in current.entries
                if entry.key in expected_by_key
            ),
            conflict_entries=(),
            detail=f"Starter installation stopped with an explicit failure: {error}",
        )

    final_registry = read_pack_registry(paths=paths)
    final_entries = tuple(
        final_registry.get(build.manifest.registry_key) for build in selected.builds
    )
    complete = all(
        entry is not None
        and entry.active
        and entry.pack_id == build.manifest.pack_id
        for entry, build in zip(final_entries, selected.builds, strict=True)
    )
    return StarterInstallReportV1(
        disposition=(
            StarterInstallDispositionV1.INSTALLED
            if complete
            else StarterInstallDispositionV1.INCOMPLETE
        ),
        complete=complete,
        entries=tuple(
            _registry_entry_summary(entry)
            for entry in final_entries
            if entry is not None
        ),
        conflict_entries=(),
        detail=(
            f"Installed {len(receipts)} missing starter pack(s) in dependency order."
            if complete
            else "Starter activation did not produce both exact active bindings."
        ),
    )


def run_starter_place_cancel_demo(seed: int) -> StarterPlaceCancelDemoV1:
    """Run one deterministic simulated place/cancel cycle with no external I/O."""

    if type(seed) is not int:
        raise TypeError("starter demonstration seed must be an integer")
    book = OrderBook()
    bid_ticks = 10_000 + abs(seed % 101)
    ask_ticks = bid_ticks + 2
    book.process(Order.limit("starter-sim-bid", Side.BUY, 100, bid_ticks))
    book.process(Order.limit("starter-sim-ask", Side.SELL, 100, ask_ticks))
    state_before = book.state_sha256()
    order_id = f"starter-player-{seed}"
    cancel_id = f"starter-cancel-{seed}"
    book.process(
        Order.limit(
            order_id,
            Side.BUY,
            1,
            bid_ticks,
            owner=OrderOwner.PLAYER,
        )
    )
    if book.all_orders[order_id].status is not OrderStatus.ACTIVE:
        raise RuntimeError("starter player order did not rest in the simulated book")
    book.cancel(order_id, cancel_id)
    book.assert_invariants()
    order = book.all_orders[order_id]
    if (
        order.status is not OrderStatus.CANCELLED
        or order.filled_quantity != 0
        or book.best_bid != bid_ticks
        or book.best_ask != ask_ticks
    ):
        raise RuntimeError("starter place/cancel conservation check failed")
    event_bytes = canonical_json_bytes(
        [event.as_dict() for event in book.journal.events]
    )
    return StarterPlaceCancelDemoV1(
        seed=seed,
        lesson_id=RELEASE_STARTER_LESSON_ID_V1,
        checkpoint_selector=RELEASE_STARTER_CHECKPOINT_SELECTOR_V1,
        order_id=order_id,
        cancel_command_id=cancel_id,
        best_bid_ticks=bid_ticks,
        best_ask_ticks=ask_ticks,
        state_before_sha256=state_before,
        state_after_sha256=book.state_sha256(),
        event_stream_sha256=hashlib.sha256(event_bytes).hexdigest(),
        event_count=len(book.journal.events),
        status="PASS",
    )


def run_first_run(paths: DataPaths, *, seed: int = 42) -> FirstRunReportV1:
    """Perform the complete committed offline first-run flow."""

    if type(paths) is not DataPaths:
        raise TypeError("first run requires exact DataPaths")
    if type(seed) is not int:
        raise TypeError("first-run seed must be an integer")

    from .doctor import release_identity, run_doctor

    created = paths.ensure(tuple(DataAreaId))
    created_rows = tuple(
        {
            "area_id": area_id.value,
            "path": str(paths.area(area_id)),
            "status": "READY",
        }
        for area_id in DataAreaId
        if paths.area(area_id) in created
    )
    writable = tuple(_write_probe(paths, area_id) for area_id in DataAreaId)
    identity = release_identity().as_dict()
    health = run_doctor(paths, strict=False, require_starter_set=False)
    starter = build_release_starter_set()
    installation = install_release_starter_set(paths, starter)
    demo = run_starter_place_cancel_demo(seed)
    complete = (
        all(row["status"] == "PASS" for row in writable)
        and health.status.value != "FAIL"
        and installation.complete
        and demo.status == "PASS"
    )
    return FirstRunReportV1(
        complete=complete,
        identity=identity,
        created_paths=created_rows,
        writable_checks=writable,
        health=health.as_dict(),
        starter_set=starter.as_dict(),
        starter_install=installation,
        demonstration=demo,
        data_paths=paths.as_dict(),
    )


def _curriculum_inputs(
    source: dict[str, object],
    scenario_build: DomainPackBuildV1,
) -> tuple[PackBuildSpecificationV1, dict[str, bytes]]:
    lessons = source["lessons"]
    if type(lessons) is not list:
        raise TypeError("starter curriculum lessons are invalid")
    lesson_rows = [dict(item) for item in lessons]
    payload_values: dict[str, dict[str, object]] = {
        "curriculum-source": source,
        "curriculum-detector": {
            "detector_id": "KIRBY2_STARTER_DETECTOR_V1",
            "required_checkpoint_selector": RELEASE_STARTER_CHECKPOINT_SELECTOR_V1,
            "schema_id": "KIRBY2_CURRICULUM_DETECTOR_V1",
            "schema_version": 1,
        },
        "curriculum-capabilities": {
            "capabilities": [
                "DETERMINISTIC_SIMULATION",
                "LOCAL_OFFLINE",
                "PLACE_CANCEL",
                "REPLAY_REVIEW",
            ],
            "forbidden_capabilities": [
                "BROKERAGE",
                "INTERNET_ACCESS",
                "REAL_MARKET_EXECUTION",
            ],
            "schema_id": "KIRBY2_CURRICULUM_CAPABILITIES_V1",
            "schema_version": 1,
        },
        "curriculum-observable-policy": {
            "observable_features": [
                "BEST_ASK",
                "BEST_BID",
                "DISPLAYED_DEPTH",
                "ORDER_ACKNOWLEDGEMENT",
                "SPREAD",
            ],
            "policy_id": "KIRBY2_STARTER_OBSERVABLE_POLICY_V1",
            "schema_id": "KIRBY2_CURRICULUM_OBSERVABLE_POLICY_V1",
            "schema_version": 1,
        },
        "curriculum-reveal-policy": {
            "hidden_until_review": [
                "FUTURE_SIMULATED_EVENTS",
                "LESSON_SCORING_TRUTH",
            ],
            "policy_id": "KIRBY2_STARTER_REVEAL_POLICY_V1",
            "schema_id": "KIRBY2_CURRICULUM_REVEAL_POLICY_V1",
            "schema_version": 1,
        },
        "curriculum-skills": {
            "lesson_skills": [
                {
                    "lesson_id": row["lesson_id"],
                    "skill_id": f"STARTER_SKILL_{row['ordinal']}_V1",
                }
                for row in lesson_rows
            ],
            "schema_id": "KIRBY2_CURRICULUM_SKILLS_V1",
            "schema_version": 1,
        },
        "curriculum-scoring": {
            "conservation_required": True,
            "deterministic_replay_required": True,
            "real_world_profit_metric": None,
            "schema_id": "KIRBY2_CURRICULUM_SCORING_V1",
            "schema_version": 1,
        },
        "curriculum-review-sidecar": {
            "review_fields": [
                "ACTION_SEQUENCE",
                "EVENT_CONSERVATION",
                "SIMULATOR_STATE_DIGEST",
            ],
            "schema_id": "KIRBY2_CURRICULUM_REVIEW_SIDECAR_V1",
            "schema_version": 1,
            "student_visible_during_attempt": False,
        },
    }
    roles = {
        "curriculum-source": PackArtifactRoleV1.CURRICULUM_SOURCE,
        "curriculum-detector": PackArtifactRoleV1.CURRICULUM_DETECTOR,
        "curriculum-capabilities": PackArtifactRoleV1.CURRICULUM_CAPABILITIES,
        "curriculum-observable-policy": (
            PackArtifactRoleV1.CURRICULUM_OBSERVABLE_POLICY
        ),
        "curriculum-reveal-policy": PackArtifactRoleV1.CURRICULUM_REVEAL_POLICY,
        "curriculum-skills": PackArtifactRoleV1.CURRICULUM_SKILLS,
        "curriculum-scoring": PackArtifactRoleV1.CURRICULUM_SCORING,
        "curriculum-review-sidecar": (
            PackArtifactRoleV1.CURRICULUM_REVIEW_SIDECAR
        ),
    }
    payloads = {
        artifact_id: canonical_json_bytes(value)
        for artifact_id, value in payload_values.items()
    }
    artifacts = tuple(
        sorted(
            (
                PackSourceArtifactV1(
                    artifact_id=artifact_id,
                    role=roles[artifact_id],
                    source_path=f"generated/{artifact_id}.json",
                    original_schema_id=str(value["schema_id"]),
                    original_schema_version=1,
                    original_media_type="application/json",
                    storage_mode=PackArtifactStorageModeV1.DIRECT,
                    logical_identity_kind="CANONICAL_JSON_SHA256_V1",
                    logical_identity_sha256=hashlib.sha256(
                        payloads[artifact_id]
                    ).hexdigest(),
                    direct_content_format=PackContentFormatV1.CANONICAL_JSON,
                )
                for artifact_id, value in payload_values.items()
            ),
            key=lambda item: item.artifact_id,
        )
    )
    creator = PackCreatorV1(
        display_name="Kirby2 Project",
        identity_uri="https://kirby2.local/project",
    )
    license_value = PackLicenseV1(
        license_id="KIRBY2-PROJECT",
        license_name="Kirby2 project data license",
        license_uri="https://kirby2.local/license",
        redistribution_policy=PackRedistributionPolicyV1.ALLOWED,
        content_mode=PackContentModeV1.SELF_CONTAINED,
    )
    dependency = PackDependencyV1(
        creator_id=scenario_build.manifest.creator.creator_id,
        namespace=scenario_build.manifest.namespace,
        name=scenario_build.manifest.name,
        version_constraint=scenario_build.manifest.version,
        expected_pack_id=scenario_build.manifest.pack_id,
    )
    return (
        PackBuildSpecificationV1(
            namespace=RELEASE_STARTER_NAMESPACE_V1,
            name="five-lesson-curriculum",
            title=str(source["title"]),
            version="1.0.0",
            creator=creator,
            pack_type=PackTypeV1.CURRICULUM,
            primary_artifact_id="curriculum-source",
            dependencies=(dependency,),
            license=license_value,
            capability_labels=(
                "DETERMINISTIC_SIMULATION",
                "LOCAL_OFFLINE",
                "PLACE_CANCEL",
            ),
            artifacts=artifacts,
        ),
        payloads,
    )


def _parse_curriculum_resource(raw: bytes) -> dict[str, object]:
    try:
        value = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise ValueError("starter curriculum resource is not valid UTF-8 TOML") from error
    expected = {
        "curriculum_id",
        "curriculum_version",
        "description",
        "lesson_count",
        "lessons",
        "scenario_role",
        "schema_id",
        "schema_version",
        "title",
    }
    if type(value) is not dict or set(value) != expected:
        raise ValueError("starter curriculum source fields differ")
    lessons = value["lessons"]
    if type(lessons) is not list or len(lessons) != 5 or value["lesson_count"] != 5:
        raise ValueError("starter curriculum must contain exactly five lessons")
    lesson_fields = {
        "checkpoint_selector",
        "lesson_id",
        "objective",
        "ordinal",
        "title",
    }
    if any(type(item) is not dict or set(item) != lesson_fields for item in lessons):
        raise ValueError("starter curriculum lesson fields differ")
    if [item["ordinal"] for item in lessons] != [1, 2, 3, 4, 5]:
        raise ValueError("starter curriculum lesson order differs")
    lesson_ids = [item["lesson_id"] for item in lessons]
    if len(set(lesson_ids)) != 5 or lesson_ids[0] != RELEASE_STARTER_LESSON_ID_V1:
        raise ValueError("starter curriculum lesson identities differ")
    if any(
        item["checkpoint_selector"] != RELEASE_STARTER_CHECKPOINT_SELECTOR_V1
        for item in lessons
    ):
        raise ValueError("starter curriculum checkpoint selector differs")
    if (
        value["schema_id"] != "KIRBY2_STARTER_CURRICULUM_SOURCE_V1"
        or value["schema_version"] != 1
        or value["curriculum_version"] != 1
        or value["scenario_role"] != "SCENARIO"
    ):
        raise ValueError("starter curriculum contract differs")
    return value


def _starter_runtime_environment(
    starter_set: ReleaseStarterSetV1,
) -> PackRuntimeEnvironmentV1:
    base = builtin_pack_runtime_environment_v1()
    schema_versions: dict[str, int] = dict(base.schema_versions)
    for build in starter_set.builds:
        for item in build.manifest.inventory:
            previous = schema_versions.get(item.schema_id)
            if previous is not None and previous != item.schema_version:
                raise ValueError(
                    f"starter packs require conflicting schema versions: {item.schema_id}"
                )
            schema_versions[item.schema_id] = item.schema_version
    return PackRuntimeEnvironmentV1(
        engine_component_id=base.engine_component_id,
        engine_version=base.engine_version,
        compiler_versions=base.compiler_versions,
        schema_versions=tuple(sorted(schema_versions.items())),
    )


def _write_probe(paths: DataPaths, area_id: DataAreaId) -> dict[str, object]:
    paths.validate(area_id)
    area = paths.area(area_id)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".kirby2-write-probe-",
        dir=area,
    )
    temporary = Path(temporary_name)
    try:
        try:
            os.write(descriptor, b"KIRBY2_WRITE_PROBE_V1")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        temporary.unlink()
        directory_descriptor = os.open(
            area,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return {"area_id": area_id.value, "path": str(area), "status": "PASS"}


def _installed_resource(repository_relative_path: str) -> Path:
    package_root = Path(__file__).resolve(strict=True).parents[1]
    prefix = "kirby2/"
    if not repository_relative_path.startswith(prefix):
        raise ValueError("starter resource must be package-relative")
    candidate = (
        package_root / repository_relative_path.removeprefix(prefix)
    ).resolve(strict=True)
    try:
        candidate.relative_to(package_root)
    except ValueError as error:
        raise ValueError("starter resource escaped the installed package") from error
    if not candidate.is_file() or candidate.is_symlink():
        raise ValueError("starter resource must be one real regular file")
    return candidate


def _read_resource(path: Path) -> bytes:
    metadata = path.stat(follow_symlinks=False)
    if metadata.st_size <= 0 or metadata.st_size > _MAX_STARTER_RESOURCE_BYTES_V1:
        raise ValueError("starter resource is empty or oversized")
    raw = path.read_bytes()
    after = path.stat(follow_symlinks=False)
    if (
        len(raw) != metadata.st_size
        or (metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns)
        != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    ):
        raise ValueError("starter resource changed during read")
    return raw


def _registry_entry_summary(entry: object) -> dict[str, object]:
    from kirby2.packs.registry import PackRegistryEntryV1

    if type(entry) is not PackRegistryEntryV1:
        raise TypeError("starter registry summary requires PackRegistryEntryV1")
    return {
        "active": entry.active,
        "key": entry.key.as_dict(),
        "pack_id": entry.pack_id,
        "pack_type": entry.manifest.pack_type.value,
    }


def _require_sha256(value: object, label: str) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{label} digest is invalid")
    return value


__all__ = [
    "RELEASE_FIRST_RUN_SCHEMA_ID_V1",
    "RELEASE_FIRST_RUN_SCHEMA_VERSION_V1",
    "RELEASE_STARTER_CHECKPOINT_SELECTOR_V1",
    "RELEASE_STARTER_LESSON_ID_V1",
    "RELEASE_STARTER_SET_ID_V1",
    "RELEASE_STARTER_SET_SCHEMA_VERSION_V1",
    "STARTER_CURRICULUM_MANIFEST_PATH_V1",
    "STARTER_SCENARIO_MANIFEST_PATH_V1",
    "FirstRunReportV1",
    "ReleaseStarterSetEntryV1",
    "ReleaseStarterSetV1",
    "StarterInstallDispositionV1",
    "StarterInstallReportV1",
    "StarterPlaceCancelDemoV1",
    "StarterSetRoleV1",
    "build_release_starter_set",
    "install_release_starter_set",
    "run_first_run",
    "run_starter_place_cancel_demo",
]
