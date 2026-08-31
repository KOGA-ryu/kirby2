"""WO39-E offline portability and hostile-pack qualification.

This module composes the existing pack builder, hostile preflight, atomic installer,
registry, and detached-signature contracts.  It does not define another pack format
or a cryptographic implementation.
"""

from __future__ import annotations

import hashlib
import json
import tomllib
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory

from kirby2.packs.archive import preflight_pack_archive_bytes
from kirby2.packs.builders import (
    DomainPackBuildV1,
    build_domain_pack,
    builtin_pack_runtime_environment_v1,
    runtime_environment_for_verified_pack_v1,
    verify_domain_pack_archive_bytes,
)
from kirby2.packs.dependencies import PackRuntimeEnvironmentV1
from kirby2.packs.formats import canonical_json_bytes, canonical_manifest_bytes
from kirby2.packs.hostile_fixtures import build_hostile_archive_fixtures
from kirby2.packs.install import (
    PackInstallRefusalCodeV1,
    PackInstallRefused,
    deactivate_pack,
    install_pack,
    lookup_installed_pack,
    read_pack_registry,
    remove_deactivated_pack,
)
from kirby2.packs.models import (
    PackContentFormatV1,
    PackContentModeV1,
    PackCreatorV1,
    PackDependencyV1,
    PackLicenseV1,
    PackRedistributionPolicyV1,
    PackTypeV1,
)
from kirby2.packs.signatures import (
    PackAuthenticityStatusV1,
    PackQualificationStatusV1,
    create_pack_signature,
    qualification_report_for_verified_pack,
    verify_pack_signature,
)
from kirby2.packs.staging import discard_pack_stage, stage_pack_archive_bytes
from kirby2.packs.types import (
    DomainPackRefusalCodeV1,
    DomainPackRefused,
    PackArtifactRoleV1,
    PackArtifactStorageModeV1,
)
from kirby2.packs.validation import PackValidationRefused
from kirby2.research.paths import DataAreaId, DataPaths


WO39E_AUDIT_CASE_COUNT = 5
PACK_SAMPLE_GROUP_SCHEMA_ID_V1 = "KIRBY2_PACK_SAMPLE_GROUP_V1"
HOSTILE_PACK_SOURCE_SET_SCHEMA_ID_V1 = "KIRBY2_HOSTILE_PACK_SOURCE_SET_V1"

_SAMPLE_GROUP_NAMES_V1 = (
    "starter_scenario",
    "five_lesson_curriculum",
    "traffic_light_strategy",
    "historical_reconstruction",
    "portable_completed_lesson",
)
_CUSTOM_SAMPLE_GROUP_IDS_V1 = {
    "traffic_light_strategy": "TRAFFIC_LIGHT_STRATEGY_V1",
    "historical_reconstruction": "HISTORICAL_RECONSTRUCTION_V1",
    "portable_completed_lesson": "PORTABLE_COMPLETED_LESSON_V1",
}
_HOSTILE_CASE_CONTRACTS_V1 = (
    ("path_traversal", "PATH_TRAVERSAL", "ARCHIVE_PREFLIGHT"),
    ("digest_mismatch", "DIGEST_MISMATCH", "ARCHIVE_PREFLIGHT"),
    ("undeclared_file", "UNDECLARED_FILE", "ARCHIVE_PREFLIGHT"),
    ("oversized_expansion", "OVERSIZED_EXPANSION", "ARCHIVE_PREFLIGHT"),
    ("unsupported_schema", "UNSUPPORTED_SCHEMA", "DOMAIN_ADAPTER"),
    ("missing_dependency", "MISSING_DEPENDENCY", "ATOMIC_INSTALL"),
    ("capability_lie", "CAPABILITY_LIE", "DOMAIN_ADAPTER"),
    ("embedded_executable", "EMBEDDED_EXECUTABLE", "DOMAIN_ADAPTER"),
    ("wo39b_archive_classes", "WO39B_ARCHIVE_CLASSES", "ARCHIVE_PREFLIGHT"),
    ("dependency_cycle", "DEPENDENCY_CYCLE", "ATOMIC_INSTALL"),
    ("malformed_toml", "MALFORMED_TOML", "CONTENT_FORMAT"),
    ("malformed_parquet", "MALFORMED_PARQUET", "CONTENT_FORMAT"),
    (
        "canonicalization_mismatch",
        "CANONICALIZATION_MISMATCH",
        "CONTENT_FORMAT",
    ),
    ("spoofed_extension", "SPOOFED_EXTENSION", "CONTENT_FORMAT"),
    ("signed_hostile_content", "SIGNED_HOSTILE_CONTENT", "ARCHIVE_PREFLIGHT"),
)
_HOSTILE_CASE_IDS_V1 = tuple(item[0] for item in _HOSTILE_CASE_CONTRACTS_V1)
_MAX_FIXTURE_BYTES_V1 = 2 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class PackPortabilityAuditCase:
    name: str
    detail: str
    evidence: dict[str, object]
    failures: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return not self.failures

    def as_dict(self) -> dict[str, object]:
        return {
            "detail": self.detail,
            "evidence": self.evidence,
            "failures": list(self.failures),
            "name": self.name,
            "status": "PASS" if self.passed else "FAIL",
        }


@dataclass(frozen=True, slots=True)
class _SampleGroupV1:
    name: str
    group_id: str
    root: Path
    manifest_raw: bytes
    manifest: dict[str, object]
    payloads: dict[str, bytes]

    @property
    def manifest_sha256(self) -> str:
        return hashlib.sha256(self.manifest_raw).hexdigest()


@dataclass(frozen=True, slots=True)
class _SampleBuildSetV1:
    groups: tuple[_SampleGroupV1, ...]
    builds: tuple[DomainPackBuildV1, ...]
    replay_sha256: str

    def build(self, pack_type: PackTypeV1) -> DomainPackBuildV1:
        matches = tuple(
            item for item in self.builds if item.manifest.pack_type is pack_type
        )
        if len(matches) != 1:
            raise RuntimeError(f"WO39-E sample pack type is ambiguous: {pack_type.value}")
        return matches[0]


@dataclass(frozen=True, slots=True)
class _LifecycleResultV1:
    checks: dict[str, bool]
    removal_checks: dict[str, bool]
    evidence: dict[str, object]


@dataclass(frozen=True, slots=True)
class _ExecutionV1:
    samples: _SampleBuildSetV1
    sample_checks: dict[str, bool]
    lifecycle: _LifecycleResultV1
    signature_checks: dict[str, bool]
    signature_evidence: dict[str, object]
    hostile_checks: dict[str, bool]
    hostile_evidence: dict[str, object]


class _NonCryptographicAuditProviderV1:
    """Inert interface probe; this is explicitly not a signature algorithm."""

    provider_id = "KIRBY2_WO39E_NON_CRYPTOGRAPHIC_AUDIT_PROVIDER_V1"
    algorithm_id = "NON_CRYPTOGRAPHIC_INTERFACE_PROBE_V1"

    def __init__(self) -> None:
        self.verify_calls = 0

    def sign(self, *, key_id: str, message: bytes) -> bytes:
        del key_id, message
        return b"KIRBY2_WO39E_INERT_SIGNATURE_INTERFACE_PROBE_V1"

    def verify(self, *, key_id: str, message: bytes, signature: bytes) -> bool:
        del key_id, message
        self.verify_calls += 1
        return signature == b"KIRBY2_WO39E_INERT_SIGNATURE_INTERFACE_PROBE_V1"


def audit_pack_portability(
    sample_set: Path | None = None,
    hostile_set: Path | None = None,
    *,
    seed: int = 42,
) -> tuple[PackPortabilityAuditCase, ...]:
    execution = _execute_portability(
        _default_sample_set() if sample_set is None else Path(sample_set),
        _default_hostile_set() if hostile_set is None else Path(hostile_set),
        seed=seed,
    )
    cases = (
        _case(
            "five_governed_sample_groups_build_with_exact_dependencies",
            f"groups={len(execution.samples.groups)} packs={len(execution.samples.builds)}",
            execution.sample_checks,
            {
                "group_ids": [item.group_id for item in execution.samples.groups],
                "pack_ids": {
                    item.manifest.pack_type.value: item.manifest.pack_id
                    for item in execution.samples.builds
                },
            },
        ),
        _case(
            "offline_clean_roots_retain_completed_lesson_replay_identity",
            f"replay_sha256={execution.samples.replay_sha256}",
            execution.lifecycle.checks,
            execution.lifecycle.evidence,
        ),
        _case(
            "optional_authenticity_never_overrides_pack_qualification",
            "authenticity is detached from structural and scientific status",
            execution.signature_checks,
            execution.signature_evidence,
        ),
        _case(
            "governed_hostile_matrix_is_refused_before_activation",
            f"hostile_cases={len(_HOSTILE_CASE_IDS_V1)}",
            execution.hostile_checks,
            execution.hostile_evidence,
        ),
        _case(
            "safe_removal_preserves_dependencies_and_completed_run_evidence",
            "dependency-first refusal and recovery removal remain separate",
            execution.lifecycle.removal_checks,
            execution.lifecycle.evidence,
        ),
    )
    expected_names = (
        "five_governed_sample_groups_build_with_exact_dependencies",
        "offline_clean_roots_retain_completed_lesson_replay_identity",
        "optional_authenticity_never_overrides_pack_qualification",
        "governed_hostile_matrix_is_refused_before_activation",
        "safe_removal_preserves_dependencies_and_completed_run_evidence",
    )
    if len(cases) != WO39E_AUDIT_CASE_COUNT:
        raise RuntimeError("WO39-E audit case inventory changed")
    if tuple(item.name for item in cases) != expected_names:
        raise RuntimeError("WO39-E audit case order or identity changed")
    return cases


def run_pack_portability_demo(
    sample_set: Path,
    hostile_set: Path,
    *,
    seed: int,
) -> dict[str, object]:
    cases = audit_pack_portability(sample_set, hostile_set, seed=seed)
    failures = tuple(
        f"{case.name}: {failure}"
        for case in cases
        for failure in case.failures
    )
    return {
        "audit_case_count": len(cases),
        "cases": [item.as_dict() for item in cases],
        "failure_count": len(failures),
        "failures": list(failures),
        "hostile_set": str(Path(hostile_set).resolve()),
        "sample_set": str(Path(sample_set).resolve()),
        "schema_id": "KIRBY2_PACK_PORTABILITY_DEMO_V1",
        "schema_version": 1,
        "seed": seed,
        "status": "PASS" if not failures else "FAIL",
    }


def _execute_portability(
    sample_set: Path,
    hostile_set: Path,
    *,
    seed: int,
) -> _ExecutionV1:
    if type(seed) is not int or seed < 0:
        raise ValueError("WO39-E portability seed must be a nonnegative integer")
    groups = _load_sample_groups(sample_set)
    samples = _build_sample_set(groups)
    sample_checks = _sample_checks(samples)
    lifecycle = _clean_root_lifecycle(samples)
    signature_checks, signature_evidence = _signature_matrix(samples)
    hostile_checks, hostile_evidence = _hostile_matrix(
        hostile_set,
        samples,
    )
    return _ExecutionV1(
        samples=samples,
        sample_checks=sample_checks,
        lifecycle=lifecycle,
        signature_checks=signature_checks,
        signature_evidence=signature_evidence,
        hostile_checks=hostile_checks,
        hostile_evidence=hostile_evidence,
    )


def _default_sample_set() -> Path:
    return Path(__file__).resolve().parents[1] / "packs" / "fixtures" / "samples"


def _default_hostile_set() -> Path:
    return Path(__file__).resolve().parents[1] / "packs" / "fixtures" / "hostile"


def _sample_creator() -> PackCreatorV1:
    return PackCreatorV1(
        display_name="Kirby2 Project",
        identity_uri="https://kirby2.local/project",
    )


def _sample_license() -> PackLicenseV1:
    return PackLicenseV1(
        license_id="KIRBY2-PROJECT",
        license_name="Kirby2 project data license",
        license_uri="https://kirby2.local/license",
        redistribution_policy=PackRedistributionPolicyV1.ALLOWED,
        content_mode=PackContentModeV1.SELF_CONTAINED,
    )


def _load_sample_groups(root: Path) -> tuple[_SampleGroupV1, ...]:
    source_root = _safe_directory(root, "sample set")
    observed_names = tuple(sorted(item.name for item in source_root.iterdir()))
    if observed_names != tuple(sorted(_SAMPLE_GROUP_NAMES_V1)):
        raise ValueError("WO39-E sample-set directory inventory differs")
    return tuple(_load_sample_group(source_root, name) for name in _SAMPLE_GROUP_NAMES_V1)


def _load_sample_group(root: Path, name: str) -> _SampleGroupV1:
    group_root = _safe_directory(root / name, f"sample group {name}")
    manifest_path = group_root / "manifest.toml"
    manifest_raw = _read_fixture_file(manifest_path)
    try:
        manifest = tomllib.loads(manifest_raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise ValueError(f"WO39-E sample manifest is invalid: {name}") from error
    if type(manifest) is not dict:
        raise ValueError(f"WO39-E sample manifest is not an object: {name}")

    if name == "starter_scenario":
        metadata = manifest.get("metadata")
        if (
            type(metadata) is not dict
            or metadata.get("scenario_id")
            != "KIRBY2_STARTER_PLACE_CANCEL_SCENARIO_V1"
        ):
            raise ValueError("WO39-E starter scenario identity differs")
        group_id = "STARTER_SCENARIO_V1"
        payloads: dict[str, bytes] = {}
    elif name == "five_lesson_curriculum":
        lessons = manifest.get("lessons")
        if type(lessons) is not list or len(lessons) != 5:
            raise ValueError("WO39-E starter curriculum must contain five lessons")
        matches = tuple(
            item
            for item in lessons
            if type(item) is dict
            and item.get("lesson_id") == "KIRBY2_STARTER_PLACE_CANCEL_V1"
            and item.get("checkpoint_selector")
            == "FIRST_QUIESCENT_CONTINUOUS_TWO_SIDED_V1"
        )
        if len(matches) != 1:
            raise ValueError("WO39-E starter curriculum binding differs")
        group_id = "FIVE_LESSON_CURRICULUM_V1"
        payloads = {}
    else:
        group_id, payloads = _load_custom_sample_manifest(
            group_root,
            name,
            manifest,
        )
    declared = {"manifest.toml", *payloads}
    observed = {item.name for item in group_root.iterdir()}
    if observed != declared:
        raise ValueError(f"WO39-E sample group has undeclared files: {name}")
    return _SampleGroupV1(
        name=name,
        group_id=group_id,
        root=group_root,
        manifest_raw=manifest_raw,
        manifest=manifest,
        payloads=payloads,
    )


def _load_custom_sample_manifest(
    root: Path,
    name: str,
    manifest: dict[str, object],
) -> tuple[str, dict[str, bytes]]:
    common_fields = {
        "capability_labels",
        "group_id",
        "pack_types",
        "payloads",
        "schema_id",
        "schema_version",
        "title",
    }
    additional_fields = {
        "traffic_light_strategy": set(),
        "historical_reconstruction": {
            "content_mode",
            "license_id",
            "redistribution_policy",
        },
        "portable_completed_lesson": {
            "checkpoint_selector",
            "expected_replay_sha256",
            "lesson_id",
        },
    }[name]
    if (
        set(manifest) != common_fields | additional_fields
        or manifest.get("schema_id") != PACK_SAMPLE_GROUP_SCHEMA_ID_V1
        or manifest.get("schema_version") != 1
        or manifest.get("group_id") != _CUSTOM_SAMPLE_GROUP_IDS_V1[name]
    ):
        raise ValueError(f"WO39-E custom sample contract differs: {name}")
    expected_pack_type = {
        "traffic_light_strategy": "STRATEGY",
        "historical_reconstruction": "HISTORICAL",
        "portable_completed_lesson": "LESSON",
    }[name]
    capabilities = manifest["capability_labels"]
    if (
        manifest["pack_types"] != [expected_pack_type]
        or type(capabilities) is not list
        or any(type(item) is not str for item in capabilities)
        or capabilities != sorted(set(capabilities))
        or "DATA_ONLY" not in capabilities
        or type(manifest["title"]) is not str
        or not manifest["title"]
    ):
        raise ValueError(f"WO39-E custom sample metadata differs: {name}")
    if name == "historical_reconstruction" and (
        manifest["content_mode"] != "SELF_CONTAINED"
        or manifest["license_id"] != "KIRBY2-PROJECT"
        or manifest["redistribution_policy"] != "ALLOWED"
    ):
        raise ValueError("WO39-E historical sample license contract differs")
    if name == "portable_completed_lesson" and (
        manifest["lesson_id"] != "KIRBY2_STARTER_PLACE_CANCEL_V1"
        or manifest["checkpoint_selector"]
        != "FIRST_QUIESCENT_CONTINUOUS_TWO_SIDED_V1"
    ):
        raise ValueError("WO39-E completed lesson contract differs")
    rows = manifest.get("payloads")
    if type(rows) is not list or not rows:
        raise ValueError(f"WO39-E custom sample has no declared payloads: {name}")
    paths: list[str] = []
    payloads: dict[str, bytes] = {}
    for row in rows:
        if type(row) is not dict or set(row) != {"media_type", "path", "sha256"}:
            raise ValueError(f"WO39-E custom sample payload fields differ: {name}")
        path = row["path"]
        media_type = row["media_type"]
        digest = row["sha256"]
        if type(path) is not str or PurePosixPath(path).name != path:
            raise ValueError(f"WO39-E custom sample payload path is unsafe: {name}")
        if type(media_type) is not str or not media_type:
            raise ValueError(f"WO39-E custom sample media type is invalid: {name}")
        if (
            type(digest) is not str
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise ValueError(f"WO39-E custom sample payload digest is invalid: {name}")
        raw = _read_fixture_file(root / path)
        if hashlib.sha256(raw).hexdigest() != digest:
            raise ValueError(f"WO39-E custom sample payload digest differs: {name}/{path}")
        paths.append(path)
        payloads[path] = raw
    if paths != sorted(set(paths)):
        raise ValueError(f"WO39-E custom sample payload order differs: {name}")
    return _CUSTOM_SAMPLE_GROUP_IDS_V1[name], payloads


def _build_sample_set(groups: tuple[_SampleGroupV1, ...]) -> _SampleBuildSetV1:
    by_name = {item.name: item for item in groups}
    scenario_source = by_name["starter_scenario"]
    curriculum_source = by_name["five_lesson_curriculum"]

    from kirby2.packs.scenario_pack import build_scenario_demo_inputs
    from kirby2.release.first_run import _curriculum_inputs, _parse_curriculum_resource

    scenario_specification, scenario_payloads = build_scenario_demo_inputs(
        scenario_source.root / "manifest.toml"
    )
    scenario_build = build_domain_pack(
        scenario_specification,
        scenario_payloads,
        source_definition_sha256=scenario_source.manifest_sha256,
    )
    curriculum_value = _parse_curriculum_resource(curriculum_source.manifest_raw)
    curriculum_specification, curriculum_payloads = _curriculum_inputs(
        curriculum_value,
        scenario_build,
    )
    curriculum_build = build_domain_pack(
        curriculum_specification,
        curriculum_payloads,
        source_definition_sha256=curriculum_source.manifest_sha256,
    )
    strategy_build = _build_strategy_sample(by_name["traffic_light_strategy"])
    historical_build = _build_historical_sample(
        by_name["historical_reconstruction"]
    )
    lesson_build, replay_sha256 = _build_completed_lesson_sample(
        by_name["portable_completed_lesson"]
    )
    return _SampleBuildSetV1(
        groups=groups,
        builds=(
            scenario_build,
            curriculum_build,
            strategy_build,
            historical_build,
            lesson_build,
        ),
        replay_sha256=replay_sha256,
    )


def _build_strategy_sample(group: _SampleGroupV1) -> DomainPackBuildV1:
    from kirby2.audit.packs import _strategy_pack_inputs

    specification, originals = _strategy_pack_inputs()
    if originals["strategy-legacy-source"] != group.payloads["traffic_light.strategy"]:
        raise ValueError("WO39-E traffic-light strategy source differs from its adapter fixture")
    specification = replace(
        specification,
        namespace="kirby2.examples",
        name="traffic-light-strategy",
        title=str(group.manifest["title"]),
        creator=_sample_creator(),
        license=_sample_license(),
        capability_labels=tuple(group.manifest["capability_labels"]),
    )
    return build_domain_pack(
        specification,
        originals,
        source_definition_sha256=group.manifest_sha256,
    )


def _build_historical_sample(group: _SampleGroupV1) -> DomainPackBuildV1:
    from kirby2.audit.evidence_packs import _historical_pack_inputs
    from kirby2.packs.historical_pack import (
        HistoricalProvenanceRecordV1,
        HistoricalSourceLicenseRecordV1,
    )

    specification, originals = _historical_pack_inputs(
        PackContentModeV1.SELF_CONTAINED
    )
    if originals["historical-source-0001"] != group.payloads["observed_trades.csv"]:
        raise ValueError("WO39-E historical source differs from its declared fixture")
    license_value = _sample_license()
    provenance = HistoricalProvenanceRecordV1.from_canonical_bytes(
        originals["historical-provenance"]
    )
    originals["historical-provenance"] = replace(
        provenance,
        sources=tuple(
            replace(item, license_id=license_value.license_id)
            for item in provenance.sources
        ),
    ).canonical_bytes()
    license_record = HistoricalSourceLicenseRecordV1.from_canonical_bytes(
        originals["historical-source-license"]
    )
    originals["historical-source-license"] = replace(
        license_record,
        license=license_value,
    ).canonical_bytes()
    specification = replace(
        specification,
        namespace="kirby2.examples",
        name="historical-reconstruction",
        title=str(group.manifest["title"]),
        creator=_sample_creator(),
        license=license_value,
        capability_labels=tuple(group.manifest["capability_labels"]),
    )
    return build_domain_pack(
        specification,
        originals,
        source_definition_sha256=group.manifest_sha256,
    )


def _build_completed_lesson_sample(
    group: _SampleGroupV1,
) -> tuple[DomainPackBuildV1, str]:
    from kirby2.audit.packs import _training_policy_pack_inputs

    specification, originals = _training_policy_pack_inputs(PackTypeV1.LESSON)
    lesson = canonical_json_bytes(
        json.loads(group.payloads["lesson.json"].decode("utf-8"))
    )
    analysis = canonical_json_bytes(
        json.loads(group.payloads["analysis.json"].decode("utf-8"))
    )
    replay = group.payloads["replay.jsonl"]
    replay_sha256 = hashlib.sha256(replay).hexdigest()
    if replay_sha256 != group.manifest["expected_replay_sha256"]:
        raise ValueError("WO39-E completed lesson replay digest differs")
    replacements = {
        "lesson-source": (
            lesson,
            "lesson.json",
            "KIRBY2_PORTABLE_COMPLETED_LESSON_V1",
            "application/json",
            PackContentFormatV1.CANONICAL_JSON,
        ),
        "lesson-embedded-run": (
            replay,
            "replay.jsonl",
            "KIRBY2_PORTABLE_COMPLETED_LESSON_REPLAY_V1",
            "application/x-ndjson",
            PackContentFormatV1.CANONICAL_EVENT_STREAM,
        ),
        "lesson-embedded-audit": (
            analysis,
            "analysis.json",
            "KIRBY2_PORTABLE_COMPLETED_LESSON_ANALYSIS_V1",
            "application/json",
            PackContentFormatV1.CANONICAL_JSON,
        ),
    }
    declarations = []
    for item in specification.artifacts:
        replacement = replacements.get(item.artifact_id)
        if replacement is None:
            declarations.append(item)
            continue
        raw, source_path, schema_id, media_type, content_format = replacement
        originals[item.artifact_id] = raw
        declarations.append(
            replace(
                item,
                source_path=source_path,
                original_schema_id=schema_id,
                original_schema_version=1,
                original_media_type=media_type,
                storage_mode=PackArtifactStorageModeV1.DIRECT,
                logical_identity_kind="OWNING_ARTIFACT_SHA256_V1",
                logical_identity_sha256=hashlib.sha256(raw).hexdigest(),
                direct_content_format=content_format,
            )
        )
    specification = replace(
        specification,
        namespace="kirby2.examples",
        name="portable-completed-lesson",
        title=str(group.manifest["title"]),
        creator=_sample_creator(),
        license=_sample_license(),
        capability_labels=tuple(group.manifest["capability_labels"]),
        artifacts=tuple(sorted(declarations, key=lambda item: item.artifact_id)),
    )
    return (
        build_domain_pack(
            specification,
            originals,
            source_definition_sha256=group.manifest_sha256,
        ),
        replay_sha256,
    )


def _sample_checks(samples: _SampleBuildSetV1) -> dict[str, bool]:
    scenario = samples.build(PackTypeV1.SCENARIO)
    curriculum = samples.build(PackTypeV1.CURRICULUM)
    strategy = samples.build(PackTypeV1.STRATEGY)
    historical = samples.build(PackTypeV1.HISTORICAL)
    lesson = samples.build(PackTypeV1.LESSON)
    groups = {item.name: item for item in samples.groups}
    dependency = curriculum.manifest.dependencies
    pack_ids = tuple(item.manifest.pack_id for item in samples.builds)
    repeat = _build_sample_set(samples.groups)
    replay_rows = lesson.index.artifacts_for(PackArtifactRoleV1.EMBEDDED_RUN)
    return {
        "sample_group_inventory_is_exact": (
            tuple(item.name for item in samples.groups) == _SAMPLE_GROUP_NAMES_V1
        ),
        "all_five_sample_groups_build_and_adapter_verify": (
            len(samples.builds) == 5
            and {item.manifest.pack_type for item in samples.builds}
            == {
                PackTypeV1.SCENARIO,
                PackTypeV1.CURRICULUM,
                PackTypeV1.STRATEGY,
                PackTypeV1.HISTORICAL,
                PackTypeV1.LESSON,
            }
        ),
        "sample_pack_ids_are_unique_lowercase_content_hashes": (
            len(set(pack_ids)) == 5
            and all(
                len(pack_id) == 64
                and all(character in "0123456789abcdef" for character in pack_id)
                for pack_id in pack_ids
            )
        ),
        "sample_packs_use_one_project_namespace_creator_and_license": all(
            item.manifest.namespace == "kirby2.examples"
            and item.manifest.creator == _sample_creator()
            and item.manifest.license == _sample_license()
            for item in samples.builds
        ),
        "custom_sample_capabilities_are_bound_to_their_source_manifests": (
            strategy.manifest.capability_labels
            == tuple(groups["traffic_light_strategy"].manifest["capability_labels"])
            and historical.manifest.capability_labels
            == tuple(groups["historical_reconstruction"].manifest["capability_labels"])
            and lesson.manifest.capability_labels
            == tuple(groups["portable_completed_lesson"].manifest["capability_labels"])
        ),
        "curriculum_binds_exact_content_derived_scenario_dependency": (
            len(dependency) == 1
            and dependency[0].creator_id == scenario.manifest.creator.creator_id
            and dependency[0].namespace == scenario.manifest.namespace
            and dependency[0].name == scenario.manifest.name
            and dependency[0].version_constraint == scenario.manifest.version
            and dependency[0].expected_pack_id == scenario.manifest.pack_id
        ),
        "completed_lesson_binds_replay_and_analysis_as_data": (
            len(replay_rows) == 1
            and replay_rows[0].original_sha256 == samples.replay_sha256
            and len(lesson.index.artifacts_for(PackArtifactRoleV1.EMBEDDED_AUDIT)) == 1
        ),
        "sample_archives_rebuild_byte_for_byte": all(
            first.archive_bytes == second.archive_bytes
            and first.manifest.pack_id == second.manifest.pack_id
            for first, second in zip(samples.builds, repeat.builds, strict=True)
        ),
    }


def _clean_root_lifecycle(samples: _SampleBuildSetV1) -> _LifecycleResultV1:
    environment = _combined_environment(samples.builds)
    lesson = samples.build(PackTypeV1.LESSON)
    curriculum = samples.build(PackTypeV1.CURRICULUM)
    scenario = samples.build(PackTypeV1.SCENARIO)
    with (
        TemporaryDirectory(prefix="kirby2-wo39e-root-a-") as raw_first,
        TemporaryDirectory(prefix="kirby2-wo39e-root-b-") as raw_second,
    ):
        first = DataPaths(Path(raw_first).resolve())
        second = DataPaths(Path(raw_second).resolve())
        first.ensure(DataAreaId.STAGING)
        first.ensure(DataAreaId.RUNS)
        second.ensure(DataAreaId.STAGING)
        receipts = tuple(
            _install_build(item, first, environment) for item in samples.builds
        )
        second_receipt = _install_build(lesson, second, environment)
        first_registry = read_pack_registry(paths=first)
        second_registry = read_pack_registry(paths=second)
        first_replay = _installed_original_bytes(
            first,
            lesson,
            PackArtifactRoleV1.EMBEDDED_RUN,
        )
        second_replay = _installed_original_bytes(
            second,
            lesson,
            PackArtifactRoleV1.EMBEDDED_RUN,
        )
        replay_valid = _validate_replay_stream(first_replay, samples.replay_sha256)
        opened = all(_installed_manifest_matches(first, item) for item in samples.builds)
        run_marker = first.area(DataAreaId.RUNS) / "completed-run-evidence.marker"
        run_marker.write_bytes(b"KIRBY2_WO39E_IMMUTABLE_RUN_EVIDENCE_V1\n")
        dependent_refusal = _capture_install_refusal(
            lambda: deactivate_pack(scenario.manifest.registry_key, paths=first)
        )
        dependency_bindings_preserved = (
            lookup_installed_pack(scenario.manifest.registry_key, paths=first)
            is not None
            and lookup_installed_pack(curriculum.manifest.registry_key, paths=first)
            is not None
        )
        for build in reversed(samples.builds):
            deactivate_pack(build.manifest.registry_key, paths=first)
            remove_deactivated_pack(build.manifest.registry_key, paths=first)
        deactivate_pack(lesson.manifest.registry_key, paths=second)
        remove_deactivated_pack(lesson.manifest.registry_key, paths=second)
        final_first_registry = read_pack_registry(paths=first)
        final_second_registry = read_pack_registry(paths=second)
        checks = {
            "all_sample_packs_install_offline_into_an_empty_root": (
                len(receipts) == 5
                and len(first_registry.entries) == 5
                and all(item.installed_new_object for item in receipts)
            ),
            "installed_packs_open_from_content_addressed_objects": opened,
            "completed_lesson_replay_is_canonical_and_ordered": replay_valid,
            "second_clean_root_retains_the_exact_replay_digest": (
                second_receipt.installed_new_object
                and len(second_registry.entries) == 1
                and hashlib.sha256(first_replay).hexdigest()
                == hashlib.sha256(second_replay).hexdigest()
                == samples.replay_sha256
            ),
            "portable_export_identity_is_the_same_archive_identity": (
                lesson.transport_sha256
                == hashlib.sha256(lesson.archive_bytes).hexdigest()
                and verify_domain_pack_archive_bytes(
                    lesson.archive_bytes,
                    expected_pack_id=lesson.manifest.pack_id,
                ).pack_id
                == lesson.manifest.pack_id
            ),
        }
        removal_checks = {
            "active_dependency_refuses_unsafe_removal": (
                dependent_refusal is PackInstallRefusalCodeV1.ACTIVE_DEPENDENTS
                and dependency_bindings_preserved
            ),
            "dependency_reverse_order_removes_every_registry_binding": (
                not final_first_registry.entries and not final_second_registry.entries
            ),
            "removal_moves_pack_objects_to_recovery_not_untracked_deletion": (
                first.area(DataAreaId.PACKS).joinpath("recovery").is_dir()
                and second.area(DataAreaId.PACKS).joinpath("recovery").is_dir()
            ),
            "completed_run_evidence_survives_pack_removal": (
                run_marker.read_bytes()
                == b"KIRBY2_WO39E_IMMUTABLE_RUN_EVIDENCE_V1\n"
            ),
        }
        evidence = {
            "dependent_refusal": (
                None if dependent_refusal is None else dependent_refusal.value
            ),
            "first_registry_sha256": first_registry.sha256,
            "installed_pack_count": len(receipts),
            "replay_sha256": samples.replay_sha256,
            "second_registry_sha256": second_registry.sha256,
        }
        return _LifecycleResultV1(
            checks=checks,
            removal_checks=removal_checks,
            evidence=evidence,
        )


def _signature_matrix(
    samples: _SampleBuildSetV1,
) -> tuple[dict[str, bool], dict[str, object]]:
    build = samples.build(PackTypeV1.LESSON)
    verification = verify_domain_pack_archive_bytes(build.archive_bytes)
    unsigned = verify_pack_signature(verification.preflight, None, providers={})
    unsigned_qualification = qualification_report_for_verified_pack(
        verification,
        unsigned,
    )
    provider = _NonCryptographicAuditProviderV1()
    envelope = create_pack_signature(
        verification.preflight,
        provider,
        key_id="WO39E_AUDIT_KEY_V1",
    )
    verified = verify_pack_signature(
        verification.preflight,
        envelope.canonical_bytes(),
        providers={provider.provider_id: provider},
    )
    verified_qualification = qualification_report_for_verified_pack(
        verification,
        verified,
    )
    mismatched = replace(
        envelope,
        pack_id=hashlib.sha256(b"different-pack").hexdigest(),
    )
    mismatch = verify_pack_signature(
        verification.preflight,
        mismatched.canonical_bytes(),
        providers={provider.provider_id: provider},
    )
    checks = {
        "unsigned_pack_is_explicit_and_still_structurally_qualified": (
            unsigned.status is PackAuthenticityStatusV1.UNSIGNED
            and unsigned_qualification.structural_safety
            is PackQualificationStatusV1.PASS
            and unsigned_qualification.digest_integrity
            is PackQualificationStatusV1.PASS
        ),
        "provider_verified_claim_is_only_an_authenticity_result": (
            verified.status is PackAuthenticityStatusV1.VERIFIED
            and verified.authenticated
        ),
        "qualification_keeps_every_status_dimension_explicit": (
            verified_qualification.structural_safety
            is PackQualificationStatusV1.PASS
            and verified_qualification.digest_integrity
            is PackQualificationStatusV1.PASS
            and verified_qualification.signer_authenticity.status
            is PackAuthenticityStatusV1.VERIFIED
            and verified_qualification.compatibility
            is PackQualificationStatusV1.PASS
            and verified_qualification.capability
            is PackQualificationStatusV1.PASS
            and verified_qualification.provenance
            is PackQualificationStatusV1.PASS
            and verified_qualification.privacy
            is PackQualificationStatusV1.NOT_APPLICABLE
            and verified_qualification.scientific_status
            is PackQualificationStatusV1.NOT_ASSESSED
        ),
        "binding_mismatch_is_refused_before_provider_authority": (
            mismatch.status is PackAuthenticityStatusV1.BINDING_MISMATCH
        ),
        "scientific_status_is_never_inferred_from_a_signature": (
            unsigned_qualification.scientific_status
            is PackQualificationStatusV1.NOT_ASSESSED
        ),
        "signature_remains_detached_from_pack_and_transport_identity": (
            envelope.pack_id == build.manifest.pack_id
            and envelope.transport_sha256 == build.transport_sha256
            and envelope.sha256
            not in {build.manifest.pack_id, build.transport_sha256}
        ),
    }
    return checks, {
        "qualification": verified_qualification.as_dict(),
        "signature_status": verified.status.value,
        "scientific_status": unsigned_qualification.scientific_status.value,
        "unsigned_status": unsigned.status.value,
    }


def _hostile_matrix(
    root: Path,
    samples: _SampleBuildSetV1,
) -> tuple[dict[str, bool], dict[str, object]]:
    manifest, payloads, case_ids = _load_hostile_set(root)
    from kirby2.audit.packs import (
        _fixture_pack,
        audit_hostile_archive_validation_and_staging,
    )

    baseline_manifest, baseline_payloads = _fixture_pack()
    wo39b_cases = audit_hostile_archive_validation_and_staging()
    wo39b_passed = all(not item.failures for item in wo39b_cases)
    archive_fixtures = build_hostile_archive_fixtures(
        baseline_manifest,
        baseline_payloads,
    )
    archive_by_kind = {
        item.spec.attack_kind: item for item in archive_fixtures
    }
    outcomes = {
        "path_traversal": wo39b_passed and "PARENT_TRAVERSAL" in archive_by_kind,
        "digest_mismatch": wo39b_passed and "DIGEST_MISMATCH" in archive_by_kind,
        "undeclared_file": wo39b_passed and "UNDECLARED_FILE" in archive_by_kind,
        "oversized_expansion": wo39b_passed and "COMPRESSION_RATIO" in archive_by_kind,
        "embedded_executable": wo39b_passed and "TYPE_SPOOFING" in archive_by_kind,
        "wo39b_archive_classes": wo39b_passed and len(archive_fixtures) == 19,
        "spoofed_extension": wo39b_passed and "TYPE_SPOOFING" in archive_by_kind,
    }
    outcomes.update(_domain_hostile_outcomes(samples, payloads))
    outcomes["signed_hostile_content"] = _signed_hostile_refusal(
        samples,
        archive_by_kind["PARENT_TRAVERSAL"].archive_bytes,
    )
    outcomes = {case_id: bool(outcomes.get(case_id)) for case_id in case_ids}
    checks = {
        "hostile_source_manifest_inventory_is_exact": (
            manifest["schema_id"] == HOSTILE_PACK_SOURCE_SET_SCHEMA_ID_V1
            and case_ids == _HOSTILE_CASE_IDS_V1
        ),
        "all_required_and_wo39b_hostile_classes_are_refused": all(
            outcomes.values()
        ),
        "no_hostile_case_is_activated": _hostile_install_root_remains_clean(
            archive_by_kind["PARENT_TRAVERSAL"].archive_bytes
        ),
        "signed_hostile_content_still_fails_structural_preflight": outcomes[
            "signed_hostile_content"
        ],
        "hostile_payload_inventory_is_digest_bound": len(payloads) == 4,
    }
    return checks, {
        "case_outcomes": outcomes,
        "hostile_manifest_sha256": hashlib.sha256(
            _read_fixture_file(_safe_directory(root, "hostile set") / "manifest.toml")
        ).hexdigest(),
        "wo39b_fixture_count": len(archive_fixtures),
    }


def _load_hostile_set(
    root: Path,
) -> tuple[dict[str, object], dict[str, bytes], tuple[str, ...]]:
    source = _safe_directory(root, "hostile set")
    raw = _read_fixture_file(source / "manifest.toml")
    try:
        manifest = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise ValueError("WO39-E hostile source manifest is invalid") from error
    if (
        type(manifest) is not dict
        or set(manifest) != {"cases", "payloads", "schema_id", "schema_version"}
        or manifest["schema_id"] != HOSTILE_PACK_SOURCE_SET_SCHEMA_ID_V1
        or manifest["schema_version"] != 1
    ):
        raise ValueError("WO39-E hostile source manifest fields differ")
    rows = manifest["cases"]
    if type(rows) is not list or any(
        type(item) is not dict
        or set(item) != {"attack_kind", "case_id", "expected_boundary"}
        for item in rows
    ):
        raise ValueError("WO39-E hostile case declarations differ")
    observed_contracts = tuple(
        (item["case_id"], item["attack_kind"], item["expected_boundary"])
        for item in rows
    )
    if observed_contracts != _HOSTILE_CASE_CONTRACTS_V1:
        raise ValueError("WO39-E hostile case inventory or contract differs")
    case_ids = tuple(item[0] for item in observed_contracts)
    payload_rows = manifest["payloads"]
    if type(payload_rows) is not list or any(
        type(item) is not dict or set(item) != {"path", "sha256"}
        for item in payload_rows
    ):
        raise ValueError("WO39-E hostile payload declarations differ")
    payloads: dict[str, bytes] = {}
    for row in payload_rows:
        path = row["path"]
        digest = row["sha256"]
        if type(path) is not str or PurePosixPath(path).name != path:
            raise ValueError("WO39-E hostile payload path is unsafe")
        if (
            type(digest) is not str
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise ValueError("WO39-E hostile payload digest is invalid")
        payload = _read_fixture_file(source / path)
        if hashlib.sha256(payload).hexdigest() != digest:
            raise ValueError("WO39-E hostile payload digest differs")
        payloads[path] = payload
    if tuple(payloads) != tuple(sorted(payloads)):
        raise ValueError("WO39-E hostile payload order differs")
    if {item.name for item in source.iterdir()} != {"manifest.toml", *payloads}:
        raise ValueError("WO39-E hostile directory contains undeclared files")
    return manifest, payloads, case_ids


def _domain_hostile_outcomes(
    samples: _SampleBuildSetV1,
    payloads: dict[str, bytes],
) -> dict[str, bool]:
    from kirby2.audit.packs import _training_policy_pack_inputs

    lesson_specification, lesson_originals = _training_policy_pack_inputs(
        PackTypeV1.LESSON
    )
    historical = samples.build(PackTypeV1.HISTORICAL)
    historical_capability = next(
        item
        for item in historical.index.artifacts
        if item.role is PackArtifactRoleV1.HISTORICAL_CAPABILITIES
    )
    from kirby2.audit.evidence_packs import _historical_pack_inputs

    historical_specification, historical_originals = _historical_pack_inputs(
        PackContentModeV1.SELF_CONTAINED
    )
    unsupported_artifacts = tuple(
        replace(item, original_schema_version=999)
        if item.artifact_id == historical_capability.artifact_id
        else item
        for item in historical_specification.artifacts
    )
    unsupported = _capture_domain_refusal(
        lambda: build_domain_pack(
            replace(historical_specification, artifacts=unsupported_artifacts),
            historical_originals,
        )
    )
    capability = _capture_domain_refusal(
        lambda: build_domain_pack(
            replace(
                lesson_specification,
                capability_labels=tuple(
                    sorted((*lesson_specification.capability_labels, "LIVE_BROKERAGE"))
                ),
            ),
            lesson_originals,
        )
    )
    malformed_toml = _format_refusal(
        lesson_specification,
        lesson_originals,
        "lesson-source",
        payloads["malformed.toml"],
        source_path="malformed.toml",
        schema_id="KIRBY2_HOSTILE_MALFORMED_TOML_V1",
        media_type="application/toml",
        content_format=PackContentFormatV1.TOML,
    )
    malformed_parquet = _format_refusal(
        lesson_specification,
        lesson_originals,
        "lesson-embedded-run",
        payloads["malformed.parquet"],
        source_path="malformed.parquet",
        schema_id="KIRBY2_HOSTILE_MALFORMED_PARQUET_V1",
        media_type="application/vnd.apache.parquet",
        content_format=PackContentFormatV1.PARQUET,
    )
    embedded_executable = _format_refusal(
        lesson_specification,
        lesson_originals,
        "lesson-embedded-run",
        payloads["embedded.js"],
        source_path="embedded.js",
        schema_id="KIRBY2_HOSTILE_EMBEDDED_EXECUTABLE_V1",
        media_type="application/javascript",
        content_format=None,
    )
    noncanonical = _format_refusal(
        lesson_specification,
        lesson_originals,
        "lesson-source",
        b'{ "schema_id": "KIRBY2_NONCANONICAL_V1", "schema_version": 1 }',
        source_path="noncanonical.json",
        schema_id="KIRBY2_NONCANONICAL_V1",
        media_type="application/json",
        content_format=PackContentFormatV1.CANONICAL_JSON,
    )
    spoofed_extension = _format_refusal(
        lesson_specification,
        lesson_originals,
        "lesson-embedded-audit",
        payloads["spoofed.png"],
        source_path="spoofed.png",
        schema_id="KIRBY2_HOSTILE_SPOOFED_IMAGE_V1",
        media_type="image/png",
        content_format=PackContentFormatV1.BINARY_EVIDENCE,
    )
    missing_dependency = _dependency_install_refusal(
        lesson_specification,
        lesson_originals,
        self_cycle=False,
    )
    dependency_cycle = _dependency_install_refusal(
        lesson_specification,
        lesson_originals,
        self_cycle=True,
    )
    return {
        "unsupported_schema": (
            unsupported is DomainPackRefusalCodeV1.ARTIFACT_FORMAT_INVALID
        ),
        "missing_dependency": (
            missing_dependency
            is PackInstallRefusalCodeV1.DEPENDENCY_RESOLUTION_FAILED
        ),
        "capability_lie": (
            capability is DomainPackRefusalCodeV1.ARTIFACT_INVENTORY_INVALID
        ),
        "embedded_executable": (
            embedded_executable
            is DomainPackRefusalCodeV1.RENDERER_INJECTION_REFUSED
        ),
        "dependency_cycle": (
            dependency_cycle
            is PackInstallRefusalCodeV1.DEPENDENCY_RESOLUTION_FAILED
        ),
        "malformed_toml": (
            malformed_toml is DomainPackRefusalCodeV1.ARTIFACT_FORMAT_INVALID
        ),
        "malformed_parquet": (
            malformed_parquet is DomainPackRefusalCodeV1.ARTIFACT_FORMAT_INVALID
        ),
        "canonicalization_mismatch": (
            noncanonical is DomainPackRefusalCodeV1.ARTIFACT_FORMAT_INVALID
        ),
        "spoofed_extension": (
            spoofed_extension is DomainPackRefusalCodeV1.ARTIFACT_FORMAT_INVALID
        ),
    }


def _format_refusal(
    specification,
    originals: dict[str, bytes],
    artifact_id: str,
    raw: bytes,
    *,
    source_path: str,
    schema_id: str,
    media_type: str,
    content_format: PackContentFormatV1 | None,
) -> DomainPackRefusalCodeV1 | None:
    artifacts = tuple(
        replace(
            item,
            source_path=source_path,
            original_schema_id=schema_id,
            original_schema_version=1,
            original_media_type=media_type,
            storage_mode=(
                PackArtifactStorageModeV1.EXACT_BYTES_ENVELOPE
                if content_format is None
                else PackArtifactStorageModeV1.DIRECT
            ),
            logical_identity_kind="HOSTILE_FIXTURE_SHA256_V1",
            logical_identity_sha256=hashlib.sha256(raw).hexdigest(),
            direct_content_format=content_format,
        )
        if item.artifact_id == artifact_id
        else item
        for item in specification.artifacts
    )
    changed = dict(originals)
    changed[artifact_id] = raw
    return _capture_domain_refusal(
        lambda: build_domain_pack(
            replace(specification, artifacts=artifacts),
            changed,
        )
    )


def _dependency_install_refusal(
    specification,
    originals: dict[str, bytes],
    *,
    self_cycle: bool,
) -> PackInstallRefusalCodeV1 | None:
    baseline = build_domain_pack(specification, originals)
    dependency = PackDependencyV1(
        creator_id=(
            baseline.manifest.creator.creator_id
            if self_cycle
            else hashlib.sha256(b"missing-creator").hexdigest()
        ),
        namespace=(baseline.manifest.namespace if self_cycle else "kirby2.missing"),
        name=(baseline.manifest.name if self_cycle else "missing-pack"),
        version_constraint=baseline.manifest.version,
        expected_pack_id=baseline.manifest.pack_id,
    )
    hostile = build_domain_pack(
        replace(specification, dependencies=(dependency,)),
        originals,
    )
    verification = verify_domain_pack_archive_bytes(hostile.archive_bytes)
    with TemporaryDirectory(prefix="kirby2-wo39e-dependency-refusal-") as raw_root:
        paths = DataPaths(Path(raw_root).resolve())
        paths.ensure(DataAreaId.STAGING)
        environment = runtime_environment_for_verified_pack_v1(verification)
        if self_cycle:
            _install_build(baseline, paths, environment)
        stage = stage_pack_archive_bytes(
            hostile.archive_bytes,
            paths.staging,
            expected_pack_id=hostile.manifest.pack_id,
        )
        try:
            return _capture_install_refusal(
                lambda: install_pack(
                    stage,
                    paths=paths,
                    environment=environment,
                )
            )
        finally:
            discard_pack_stage(stage)


def _signed_hostile_refusal(
    samples: _SampleBuildSetV1,
    hostile_archive: bytes,
) -> bool:
    build = samples.build(PackTypeV1.LESSON)
    provider = _NonCryptographicAuditProviderV1()
    envelope = create_pack_signature(
        build.preflight,
        provider,
        key_id="WO39E_SIGNED_HOSTILE_AUDIT_KEY_V1",
    )
    before = provider.verify_calls
    refused = False
    try:
        preflight = preflight_pack_archive_bytes(hostile_archive)
    except PackValidationRefused:
        refused = True
    else:
        verify_pack_signature(
            preflight,
            envelope.canonical_bytes(),
            providers={provider.provider_id: provider},
        )
    return refused and provider.verify_calls == before


def _hostile_install_root_remains_clean(hostile_archive: bytes) -> bool:
    with TemporaryDirectory(prefix="kirby2-wo39e-hostile-root-") as raw_root:
        paths = DataPaths(Path(raw_root).resolve())
        paths.ensure(DataAreaId.STAGING)
        refused = False
        try:
            stage_pack_archive_bytes(hostile_archive, paths.staging)
        except PackValidationRefused:
            refused = True
        return (
            refused
            and not any(paths.staging.iterdir())
            and not read_pack_registry(paths=paths).entries
        )


def _combined_environment(
    builds: tuple[DomainPackBuildV1, ...],
) -> PackRuntimeEnvironmentV1:
    base = builtin_pack_runtime_environment_v1()
    schemas = dict(base.schema_versions)
    for build in builds:
        for item in build.manifest.inventory:
            previous = schemas.get(item.schema_id)
            if previous is not None and previous != item.schema_version:
                raise ValueError("WO39-E sample schemas require conflicting versions")
            schemas[item.schema_id] = item.schema_version
    return PackRuntimeEnvironmentV1(
        engine_component_id=base.engine_component_id,
        engine_version=base.engine_version,
        compiler_versions=base.compiler_versions,
        schema_versions=tuple(sorted(schemas.items())),
    )


def _install_build(
    build: DomainPackBuildV1,
    paths: DataPaths,
    environment: PackRuntimeEnvironmentV1,
):
    stage = stage_pack_archive_bytes(
        build.archive_bytes,
        paths.staging,
        expected_pack_id=build.manifest.pack_id,
        expected_transport_sha256=build.transport_sha256,
    )
    try:
        receipt = install_pack(stage, paths=paths, environment=environment)
    except BaseException:
        try:
            discard_pack_stage(stage)
        except Exception:
            pass
        raise
    if not receipt.installed_new_object:
        discard_pack_stage(stage)
    return receipt


def _installed_manifest_matches(paths: DataPaths, build: DomainPackBuildV1) -> bool:
    entry = lookup_installed_pack(build.manifest.registry_key, paths=paths)
    if entry is None or entry.pack_id != build.manifest.pack_id or not entry.active:
        return False
    root = _installed_object_root(paths, entry.object_path)
    raw = (root / "manifest.toml").read_bytes()
    return raw == canonical_manifest_bytes(build.manifest)


def _installed_original_bytes(
    paths: DataPaths,
    build: DomainPackBuildV1,
    role: PackArtifactRoleV1,
) -> bytes:
    rows = build.index.artifacts_for(role)
    if len(rows) != 1:
        raise RuntimeError(f"WO39-E installed role is ambiguous: {role.value}")
    entry = lookup_installed_pack(build.manifest.registry_key, paths=paths)
    if entry is None:
        raise RuntimeError("WO39-E installed pack lookup failed")
    row = rows[0]
    if row.storage_mode is not PackArtifactStorageModeV1.DIRECT:
        raise RuntimeError("WO39-E replay fixture unexpectedly uses an envelope")
    return (_installed_object_root(paths, entry.object_path) / row.payload_path).read_bytes()


def _installed_object_root(paths: DataPaths, object_path: str) -> Path:
    return paths.area(DataAreaId.PACKS).joinpath(*PurePosixPath(object_path).parts)


def _validate_replay_stream(raw: bytes, expected_sha256: str) -> bool:
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        return False
    lines = raw.splitlines()
    try:
        rows = [json.loads(item.decode("utf-8")) for item in lines]
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    return (
        len(rows) == 3
        and [item.get("sequence") for item in rows] == [0, 1, 2]
        and [item.get("timestamp_us") for item in rows] == [0, 250_000, 500_000]
        and rows[-1].get("state_sha256") == "3" * 64
        and all(canonical_json_bytes(item) == line for item, line in zip(rows, lines))
    )


def _safe_directory(path: Path, label: str) -> Path:
    candidate = Path(path)
    if candidate.is_symlink():
        raise ValueError(f"WO39-E {label} cannot be a symlink")
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise ValueError(f"WO39-E {label} cannot be resolved") from error
    if not resolved.is_dir():
        raise ValueError(f"WO39-E {label} must be a directory")
    return resolved


def _read_fixture_file(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise ValueError("WO39-E fixture source must be one regular file")
    metadata = path.stat(follow_symlinks=False)
    if metadata.st_size <= 0 or metadata.st_size > _MAX_FIXTURE_BYTES_V1:
        raise ValueError("WO39-E fixture source is empty or oversized")
    raw = path.read_bytes()
    after = path.stat(follow_symlinks=False)
    if (
        len(raw) != metadata.st_size
        or (metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns)
        != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    ):
        raise ValueError("WO39-E fixture source changed while read")
    return raw


def _capture_domain_refusal(operation) -> DomainPackRefusalCodeV1 | None:
    try:
        operation()
    except DomainPackRefused as error:
        return error.code
    return None


def _capture_install_refusal(operation) -> PackInstallRefusalCodeV1 | None:
    try:
        operation()
    except PackInstallRefused as error:
        return error.code
    return None


def _case(
    name: str,
    detail: str,
    checks: dict[str, bool],
    evidence: dict[str, object],
) -> PackPortabilityAuditCase:
    return PackPortabilityAuditCase(
        name=name,
        detail=detail,
        evidence=evidence,
        failures=tuple(label for label, passed in checks.items() if not passed),
    )


__all__ = [
    "HOSTILE_PACK_SOURCE_SET_SCHEMA_ID_V1",
    "PACK_SAMPLE_GROUP_SCHEMA_ID_V1",
    "WO39E_AUDIT_CASE_COUNT",
    "PackPortabilityAuditCase",
    "audit_pack_portability",
    "run_pack_portability_demo",
]
