"""Executable audits for the canonical Kirby2 pack substrate."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import stat
import zipfile
from dataclasses import dataclass, replace
from pathlib import Path
from tempfile import TemporaryDirectory

from kirby2.calibration.profiles import MarketProfile
from kirby2.discovery.identity import (
    canonical_strategy_ast_bytes,
    legacy_strategy_source_sha256,
    lineage_payload_sha256,
    strategy_semantic_sha256,
)
from kirby2.packs.archive import preflight_pack_archive_bytes
from kirby2.packs.builders import (
    DomainPackBuildV1,
    DomainPackVerificationV1,
    build_domain_pack,
    runtime_environment_for_verified_pack_v1,
    supported_domain_pack_types_v1,
    verify_domain_pack_archive_bytes,
)
from kirby2.packs.commands import PACK_COMMAND_MODULE
from kirby2.packs.formats import (
    K2PACK_MANIFEST_PATH,
    K2PACK_ZIP_COMPRESSION,
    K2PACK_ZIP_COMPRESSLEVEL,
    K2PACK_ZIP_TIMESTAMP,
    canonical_json_bytes,
    canonical_manifest_bytes,
    canonical_toml_bytes,
    inspect_payload_format_claim,
    load_canonical_json_bytes,
    load_manifest_bytes,
    normalized_archive_paths,
    normalized_zip_info,
    require_content_declaration,
)
from kirby2.packs.identity import (
    PACK_IDENTITY_ALGORITHM,
    describe_archive_transport,
    derive_creator_id,
    derive_pack_id,
    pack_identity_projection,
    verify_pack_payload_identity,
)
from kirby2.packs.hostile_fixtures import (
    HOSTILE_ARCHIVE_FIXTURE_SCHEMA_ID,
    build_hostile_archive_fixtures,
    load_hostile_archive_fixture_specs,
)
from kirby2.packs.dependencies import (
    PackRuntimeEnvironmentV1,
    resolve_pack_dependencies,
    semver_satisfies,
    validate_installability,
)
from kirby2.packs.install import (
    PACK_REGISTRY_LOCK_FILENAME,
    PackInstallOperationV1,
    PackInstallRefusalCodeV1,
    PackInstallRefused,
    deactivate_pack,
    install_pack,
    lookup_installed_pack,
    read_pack_registry,
    remove_deactivated_pack,
)
from kirby2.packs.models import (
    PackCompatibilityLevelV1,
    PackCompatibilityV1,
    PackContentFormatV1,
    PackContentModeV1,
    PackCreatorV1,
    PackDependencyV1,
    PackEntrypointV1,
    PackFileV1,
    PackLicenseV1,
    PackManifestV1,
    PackProvenanceV1,
    PackRedistributionPolicyV1,
    PackSchemaRequirementV1,
    PackTypeV1,
    PackVersionRequirementV1,
)
from kirby2.packs.staging import (
    PACK_STAGE_CAPABILITY_SCHEMA_ID,
    PackStageVerificationV1,
    discard_pack_stage,
    revalidate_pack_stage,
    stage_pack_archive_bytes,
)
from kirby2.packs.registry import (
    PACK_REGISTRY_FILENAME,
    PackRegistryEntryV1,
    PackRegistryV1,
    canonical_pack_registry_bytes,
    load_pack_registry_bytes,
    pack_object_relative_path,
)
from kirby2.packs.scenario_pack import build_scenario_demo_inputs
from kirby2.packs.types import (
    DomainPackRefusalCodeV1,
    DomainPackRefused,
    PackArtifactRoleV1,
    PackArtifactStorageModeV1,
    PackBuildSpecificationV1,
    PackSourceArtifactV1,
)
from kirby2.packs.validation import (
    DEFAULT_PACK_VALIDATION_LIMITS_V1,
    PackRefusalCodeV1,
    PackValidationLimitsV1,
    PackValidationPhaseV1,
    PackValidationRefused,
    validate_manifest_complexity,
    validate_pack_member_path,
    validate_pack_member_paths,
    validate_parse_complexity,
    validate_structural_payload,
    validation_policy_id,
)
from kirby2.research.paths import DataAreaId, DataPaths
from kirby2.strategy.language import parse_strategy_semantic_ast


WO39A_AUDIT_CASE_COUNT = 5
WO39B_AUDIT_CASE_COUNT = 4
WO39C_AUDIT_CASE_COUNT = 4
WO39D1_AUDIT_CASE_COUNT = 5
WO38C_PACK_AUDIT_CASE_COUNT = 1

_JSON_PATH = "data/scenario.json"
_JSON_SCHEMA_ID = "KIRBY2_WO39A_AUDIT_SCENARIO_V1"
_TOML_PATH = "data/metadata.toml"
_TOML_SCHEMA_ID = "KIRBY2_WO39A_AUDIT_METADATA_V1"


@dataclass(frozen=True, slots=True)
class PackAuditCase:
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


def audit_canonical_pack_identity() -> tuple[PackAuditCase, ...]:
    """Exercise the fixed WO39-A logical and transport identity inventory."""

    manifest, payloads = _fixture_pack()
    normalized_a = _normalized_archive(manifest, payloads, reverse_input=False)
    normalized_b = _normalized_archive(manifest, payloads, reverse_input=True)
    cases = (
        _manifest_contract_case(manifest, payloads),
        _normalized_archive_case(manifest, payloads, normalized_a, normalized_b),
        _logical_identity_sensitivity_case(manifest, payloads),
        _transport_separation_case(manifest, payloads, normalized_a),
        _data_only_refusal_case(manifest),
    )
    expected_names = (
        "manifest_creator_registry_and_inventory_identity_are_complete",
        "normalized_archive_builds_are_byte_identical",
        "semantic_and_payload_changes_alter_logical_identity",
        "incidental_archive_metadata_changes_only_transport_identity",
        "executable_unknown_and_noncanonical_pack_claims_are_refused",
    )
    if len(cases) != WO39A_AUDIT_CASE_COUNT:
        raise RuntimeError("WO39-A audit case inventory changed")
    if tuple(item.name for item in cases) != expected_names:
        raise RuntimeError("WO39-A audit case order or identity changed")
    return cases


def _fixture_pack() -> tuple[PackManifestV1, dict[str, bytes]]:
    json_payload = canonical_json_bytes(
        {
            "records": [
                {
                    "instrument_id": "SYNTHETIC.AUDIT",
                    "price_tick": 101,
                    "quantity": 7,
                }
            ],
            "schema_id": _JSON_SCHEMA_ID,
            "schema_version": 1,
        }
    )
    toml_payload = canonical_toml_bytes(
        {
            "schema_id": _TOML_SCHEMA_ID,
            "schema_version": 1,
            "title": "Synthetic offline pack audit fixture",
        }
    )
    payloads = {
        _TOML_PATH: toml_payload,
        _JSON_PATH: json_payload,
    }
    inventory = tuple(
        sorted(
            (
                _file(
                    path=_JSON_PATH,
                    raw=json_payload,
                    content_format=PackContentFormatV1.CANONICAL_JSON,
                    media_type="application/json",
                    schema_id=_JSON_SCHEMA_ID,
                ),
                _file(
                    path=_TOML_PATH,
                    raw=toml_payload,
                    content_format=PackContentFormatV1.TOML,
                    media_type="application/toml",
                    schema_id=_TOML_SCHEMA_ID,
                ),
            ),
            key=lambda item: item.sort_key,
        )
    )
    schema_requirements = tuple(
        sorted(
            (
                PackSchemaRequirementV1(
                    schema_id=_JSON_SCHEMA_ID,
                    supported_versions=(1,),
                ),
                PackSchemaRequirementV1(
                    schema_id=_TOML_SCHEMA_ID,
                    supported_versions=(1,),
                ),
            ),
            key=lambda item: item.sort_key,
        )
    )
    engine = PackVersionRequirementV1(
        component_id="KIRBY2_ENGINE_V1",
        version_constraint=">=0.1.0,<1.0.0",
    )
    compiler = PackVersionRequirementV1(
        component_id="KIRBY2_SCENARIO_COMPILER_V1",
        version_constraint="1.0.0",
    )
    compatibility = tuple(
        PackCompatibilityV1(
            level=level,
            supported=True,
            engine=engine,
            compilers=() if level is PackCompatibilityLevelV1.READABLE else (compiler,),
            schemas=schema_requirements,
        )
        for level in PackCompatibilityLevelV1
    )
    creator = PackCreatorV1(
        display_name="Kirby2 deterministic audit fixture",
        identity_uri="https://example.invalid/kirby2/wo39a-audit-creator",
    )
    manifest = PackManifestV1(
        namespace="org.kirby2.audit",
        name="canonical-identity",
        title="Canonical data-only identity audit",
        version="1.0.0",
        creator=creator,
        pack_type=PackTypeV1.SCENARIO,
        compatibility=compatibility,
        dependencies=(
            PackDependencyV1(
                creator_id=_digest("WO39-A dependency creator"),
                namespace="org.kirby2.foundation",
                name="base-data",
                version_constraint=">=1.0.0,<2.0.0",
                expected_pack_id=_digest("WO39-A dependency pack"),
            ),
        ),
        provenance=(
            PackProvenanceV1(
                source_kind="SYNTHETIC_FIXTURE",
                source_id="WO39A_CANONICAL_IDENTITY",
                source_sha256=_digest("WO39-A synthetic source"),
            ),
        ),
        license=PackLicenseV1(
            license_id="CC0-1.0",
            license_name="CC0 1.0 Universal",
            license_uri="https://creativecommons.org/publicdomain/zero/1.0/",
            redistribution_policy=PackRedistributionPolicyV1.ALLOWED,
            content_mode=PackContentModeV1.SELF_CONTAINED,
        ),
        capability_labels=("DATA_ONLY", "OFFLINE_SIMULATOR"),
        inventory=inventory,
        entrypoints=(
            PackEntrypointV1(
                entrypoint_id="scenario.primary",
                data_id="synthetic.audit.scenario.v1",
                path=_JSON_PATH,
            ),
        ),
    )
    return manifest, payloads


def _file(
    *,
    path: str,
    raw: bytes,
    content_format: PackContentFormatV1,
    media_type: str,
    schema_id: str,
) -> PackFileV1:
    return PackFileV1(
        path=path,
        byte_count=len(raw),
        sha256=hashlib.sha256(raw).hexdigest(),
        content_format=content_format,
        media_type=media_type,
        schema_id=schema_id,
        schema_version=1,
    )


def _manifest_contract_case(
    manifest: PackManifestV1,
    payloads: dict[str, bytes],
) -> PackAuditCase:
    raw = canonical_manifest_bytes(manifest)
    restored = load_manifest_bytes(raw)
    verified = verify_pack_payload_identity(manifest, payloads)
    projection = pack_identity_projection(manifest)
    expected_fields = frozenset(
        {
            "canonicalization_id",
            "canonicalization_version",
            "capability_labels",
            "compatibility",
            "creator",
            "dependencies",
            "entrypoints",
            "inventory",
            "license",
            "name",
            "namespace",
            "pack_format_id",
            "pack_format_version",
            "pack_id",
            "pack_type",
            "provenance",
            "schema_id",
            "schema_version",
            "title",
            "version",
        }
    )
    checks = {
        "manifest_has_exact_complete_field_inventory": (
            frozenset(manifest.as_dict()) == expected_fields
        ),
        "canonical_manifest_round_trips_exactly": (
            restored == manifest and canonical_manifest_bytes(restored) == raw
        ),
        "creator_id_is_content_derived_not_authorship_proof": (
            manifest.creator_id == derive_creator_id(manifest.creator)
            and manifest.registry_key.creator_id == manifest.creator_id
        ),
        "registry_key_is_creator_qualified": (
            manifest.registry_key.sort_key
            == (
                manifest.creator_id,
                manifest.namespace,
                manifest.name,
                manifest.version,
            )
        ),
        "compatibility_has_four_distinct_levels": (
            tuple(item.level for item in manifest.compatibility)
            == tuple(PackCompatibilityLevelV1)
        ),
        "dependency_provenance_license_and_data_entrypoint_are_explicit": (
            len(manifest.dependencies) == 1
            and len(manifest.provenance) == 1
            and manifest.license.content_mode is PackContentModeV1.SELF_CONTAINED
            and len(manifest.entrypoints) == 1
            and manifest.entrypoints[0].path in payloads
        ),
        "logical_identity_projection_is_exact": (
            projection["algorithm"] == PACK_IDENTITY_ALGORITHM
            and projection["inventory"]
            == [item.as_dict() for item in manifest.inventory]
            and derive_pack_id(manifest) == manifest.pack_id == verified.pack_id
        ),
        "complete_payload_inventory_is_verified": (
            verified.file_count == len(payloads)
            and verified.total_byte_count == sum(len(raw) for raw in payloads.values())
        ),
    }
    return _case(
        "manifest_creator_registry_and_inventory_identity_are_complete",
        (
            f"pack={manifest.pack_id} creator={manifest.creator_id} "
            f"files={verified.file_count}"
        ),
        checks,
        {
            "creator_id": manifest.creator_id,
            "manifest_byte_count": len(raw),
            "pack_id": manifest.pack_id,
        },
    )


def _normalized_archive_case(
    manifest: PackManifestV1,
    payloads: dict[str, bytes],
    first: bytes,
    second: bytes,
) -> PackAuditCase:
    expected_values = {
        K2PACK_MANIFEST_PATH: canonical_manifest_bytes(manifest),
        **payloads,
    }
    with zipfile.ZipFile(io.BytesIO(first), mode="r") as archive:
        infos = tuple(archive.infolist())
        restored_values = {item.filename: archive.read(item.filename) for item in infos}
    modes = tuple(item.external_attr >> 16 for item in infos)
    checks = {
        "input_mapping_order_does_not_change_archive_bytes": first == second,
        "archive_paths_use_canonical_utf8_order": (
            tuple(item.filename for item in infos)
            == normalized_archive_paths(tuple(expected_values))
        ),
        "archive_members_preserve_exact_declared_bytes": restored_values == expected_values,
        "timestamps_owners_permissions_and_compression_are_normalized": all(
            item.date_time == K2PACK_ZIP_TIMESTAMP
            and item.create_system == 3
            and item.compress_type == K2PACK_ZIP_COMPRESSION
            and item.extra == b""
            and item.comment == b""
            and stat.S_ISREG(mode)
            and stat.S_IMODE(mode) == 0o644
            for item, mode in zip(infos, modes, strict=True)
        ),
        "normalized_manifest_retains_logical_identity": (
            load_manifest_bytes(restored_values[K2PACK_MANIFEST_PATH]).pack_id
            == manifest.pack_id
        ),
    }
    return _case(
        "normalized_archive_builds_are_byte_identical",
        f"pack={manifest.pack_id} archive_bytes={len(first)} members={len(infos)}",
        checks,
        {
            "archive_byte_count": len(first),
            "member_paths": [item.filename for item in infos],
        },
    )


def _logical_identity_sensitivity_case(
    manifest: PackManifestV1,
    payloads: dict[str, bytes],
) -> PackAuditCase:
    semantic_change = replace(
        manifest,
        title="Canonical data-only identity audit revised",
    )
    changed_payloads = dict(payloads)
    changed_json = canonical_json_bytes(
        {
            "records": [
                {
                    "instrument_id": "SYNTHETIC.AUDIT",
                    "price_tick": 102,
                    "quantity": 7,
                }
            ],
            "schema_id": _JSON_SCHEMA_ID,
            "schema_version": 1,
        }
    )
    changed_payloads[_JSON_PATH] = changed_json
    changed_inventory = tuple(
        replace(
            item,
            byte_count=len(changed_json),
            sha256=hashlib.sha256(changed_json).hexdigest(),
        )
        if item.path == _JSON_PATH
        else item
        for item in manifest.inventory
    )
    payload_change = replace(manifest, inventory=changed_inventory)
    alternate_creator = replace(
        manifest,
        creator=PackCreatorV1(
            display_name="Second creator with the same textual namespace",
            identity_uri="https://example.invalid/kirby2/second-creator",
        ),
    )
    checks = {
        "semantic_manifest_change_alters_pack_id": (
            semantic_change.pack_id != manifest.pack_id
        ),
        "payload_byte_change_alters_inventory_and_pack_id": (
            payload_change.pack_id != manifest.pack_id
            and verify_pack_payload_identity(payload_change, changed_payloads).pack_id
            == payload_change.pack_id
        ),
        "undeclared_payload_tamper_is_refused": _raises(
            lambda: verify_pack_payload_identity(manifest, changed_payloads)
        ),
        "same_namespace_under_distinct_creator_does_not_collide": (
            alternate_creator.namespace == manifest.namespace
            and alternate_creator.name == manifest.name
            and alternate_creator.version == manifest.version
            and alternate_creator.creator_id != manifest.creator_id
            and alternate_creator.registry_key != manifest.registry_key
            and alternate_creator.pack_id != manifest.pack_id
        ),
    }
    return _case(
        "semantic_and_payload_changes_alter_logical_identity",
        (
            f"base={manifest.pack_id} semantic={semantic_change.pack_id} "
            f"payload={payload_change.pack_id}"
        ),
        checks,
        {
            "base_pack_id": manifest.pack_id,
            "payload_change_pack_id": payload_change.pack_id,
            "semantic_change_pack_id": semantic_change.pack_id,
        },
    )


def _transport_separation_case(
    manifest: PackManifestV1,
    payloads: dict[str, bytes],
    normalized: bytes,
) -> PackAuditCase:
    incidental = _incidental_archive(manifest, payloads)
    normalized_descriptor = describe_archive_transport(manifest, normalized)
    incidental_descriptor = describe_archive_transport(manifest, incidental)
    expected_values = {
        K2PACK_MANIFEST_PATH: canonical_manifest_bytes(manifest),
        **payloads,
    }
    checks = {
        "archives_contain_the_same_exact_logical_members": (
            _archive_values(normalized) == expected_values
            and _archive_values(incidental) == expected_values
        ),
        "incidental_archive_metadata_changes_raw_bytes": incidental != normalized,
        "logical_pack_id_is_independent_of_transport_bytes": (
            normalized_descriptor.pack_id
            == incidental_descriptor.pack_id
            == manifest.pack_id
        ),
        "transport_digest_tracks_exact_archive_bytes": (
            normalized_descriptor.transport_sha256
            != incidental_descriptor.transport_sha256
        ),
        "manifest_digest_is_stable_across_transports": (
            normalized_descriptor.manifest_sha256
            == incidental_descriptor.manifest_sha256
        ),
        "transport_descriptor_remains_explicitly_unverified": (
            normalized_descriptor.as_dict()["schema_id"]
            == "KIRBY2_UNVERIFIED_PACK_TRANSPORT_IDENTITY_V1"
        ),
    }
    return _case(
        "incidental_archive_metadata_changes_only_transport_identity",
        (
            f"pack={manifest.pack_id} normalized_transport="
            f"{normalized_descriptor.transport_sha256} incidental_transport="
            f"{incidental_descriptor.transport_sha256}"
        ),
        checks,
        {
            "incidental_transport_sha256": incidental_descriptor.transport_sha256,
            "normalized_transport_sha256": normalized_descriptor.transport_sha256,
            "pack_id": manifest.pack_id,
        },
    )


def _data_only_refusal_case(manifest: PackManifestV1) -> PackAuditCase:
    probes = {
        "executable_suffix": lambda: PackFileV1(
            path="data/runner.py",
            byte_count=2,
            sha256=_digest("executable suffix"),
            content_format=PackContentFormatV1.CANONICAL_JSON,
            media_type="application/json",
            schema_id=_JSON_SCHEMA_ID,
            schema_version=1,
        ),
        "unknown_content_format": lambda: require_content_declaration(
            path="data/unknown.bin",
            content_format="PYTHON",
            media_type="text/x-python",
            schema_id="PYTHON_MODULE_V1",
        ),
        "executable_entrypoint_token": lambda: PackEntrypointV1(
            entrypoint_id="python.module",
            data_id="synthetic.audit.scenario.v1",
            path=_JSON_PATH,
        ),
        "executable_magic_disguised_as_binary_evidence": lambda: (
            inspect_payload_format_claim(
                b"\x7fELF" + b"audit payload",
                path="data/figure.png",
                content_format=PackContentFormatV1.BINARY_EVIDENCE.value,
                media_type="image/png",
                schema_id="KIRBY2_AUDIT_FIGURE_V1",
            )
        ),
        "noncanonical_namespace": lambda: replace(
            manifest,
            namespace="Org.Kirby2.Audit",
        ),
        "leading_zero_semver_alias": lambda: replace(manifest, version="01.0.0"),
        "unknown_manifest_field": lambda: _restore_with_unknown_field(manifest),
    }
    checks = {
        f"{name}_is_refused": _raises(operation)
        for name, operation in probes.items()
    }
    return _case(
        "executable_unknown_and_noncanonical_pack_claims_are_refused",
        f"refused={sum(checks.values())}/{len(checks)}",
        checks,
        {"refusal_probe_names": sorted(probes)},
    )


def audit_hostile_archive_validation_and_staging() -> tuple[PackAuditCase, ...]:
    """Exercise the fixed WO39-B preflight, limit, and private-stage boundary."""

    manifest, payloads = _fixture_pack()
    archive_bytes = _normalized_archive(
        manifest,
        payloads,
        reverse_input=False,
    )
    cases = (
        _hostile_archive_fixture_case(manifest, payloads),
        _archive_path_policy_case(),
        _archive_resource_policy_case(manifest, payloads, archive_bytes),
        _private_stage_lifecycle_case(manifest, payloads, archive_bytes),
    )
    expected_names = (
        "every_governed_hostile_archive_is_refused_before_staging",
        "portable_path_and_collision_policy_is_closed",
        "archive_resource_and_parse_budgets_fail_with_stable_codes",
        "safe_nested_pack_stages_revalidates_detects_tamper_and_discards",
    )
    if len(cases) != WO39B_AUDIT_CASE_COUNT:
        raise RuntimeError("WO39-B audit case inventory changed")
    if tuple(item.name for item in cases) != expected_names:
        raise RuntimeError("WO39-B audit case order or identity changed")
    return cases


def _hostile_archive_fixture_case(
    manifest: PackManifestV1,
    payloads: dict[str, bytes],
) -> PackAuditCase:
    specs = load_hostile_archive_fixture_specs()
    fixtures = build_hostile_archive_fixtures(manifest, payloads)
    observed: list[dict[str, object]] = []
    stable = True
    roots_clean = True
    with TemporaryDirectory(prefix="kirby2-wo39b-hostile-") as raw_root:
        root = Path(raw_root).resolve()
        root.chmod(0o700)
        for fixture in fixtures:
            stage = None
            try:
                stage = stage_pack_archive_bytes(
                    fixture.archive_bytes,
                    root,
                    limits=fixture.limits,
                )
            except PackValidationRefused as error:
                refusal = error.refusal
                matches = (
                    refusal.code is fixture.spec.expected_code
                    and refusal.phase is fixture.spec.expected_phase
                )
                stable = stable and matches
                observed.append(
                    {
                        "code": refusal.code.value,
                        "fixture_id": fixture.fixture_id,
                        "phase": refusal.phase.value,
                        "transport_sha256": fixture.transport_sha256,
                    }
                )
            else:
                stable = False
                observed.append(
                    {
                        "code": "UNEXPECTED_STAGE",
                        "fixture_id": fixture.fixture_id,
                        "phase": "STAGE_WRITE",
                        "transport_sha256": fixture.transport_sha256,
                    }
                )
            finally:
                if stage is not None:
                    discard_pack_stage(stage, limits=fixture.limits)
            roots_clean = roots_clean and not any(root.iterdir())
    checks = {
        "fixture_manifest_uses_governed_schema": (
            HOSTILE_ARCHIVE_FIXTURE_SCHEMA_ID
            == "KIRBY2_HOSTILE_ARCHIVE_FIXTURE_SET_V1"
        ),
        "fixture_specs_and_generated_archives_are_one_to_one": (
            len(fixtures) == len(specs) == 19
            and tuple(item.fixture_id for item in fixtures)
            == tuple(item.fixture_id for item in specs)
        ),
        "every_fixture_returns_its_exact_stable_code_and_phase": stable,
        "no_hostile_failure_leaves_a_partial_stage": roots_clean,
        "fixture_transports_are_unique": (
            len({item.transport_sha256 for item in fixtures}) == len(fixtures)
        ),
    }
    return _case(
        "every_governed_hostile_archive_is_refused_before_staging",
        f"fixtures={len(fixtures)} stable={stable} clean={roots_clean}",
        checks,
        {"observed_refusals": observed},
    )


def _archive_path_policy_case() -> PackAuditCase:
    default = DEFAULT_PACK_VALIDATION_LIMITS_V1
    path_probes = {
        "absolute": (
            lambda: validate_pack_member_path("/escape.json", limits=default),
            PackRefusalCodeV1.PATH_ABSOLUTE,
        ),
        "parent": (
            lambda: validate_pack_member_path("../escape.json", limits=default),
            PackRefusalCodeV1.PATH_PARENT_TRAVERSAL,
        ),
        "backslash": (
            lambda: validate_pack_member_path("data\\escape.json", limits=default),
            PackRefusalCodeV1.PATH_BACKSLASH,
        ),
        "windows_drive": (
            lambda: validate_pack_member_path("C:/escape.json", limits=default),
            PackRefusalCodeV1.PATH_WINDOWS_DRIVE,
        ),
        "unc": (
            lambda: validate_pack_member_path("//server/share.json", limits=default),
            PackRefusalCodeV1.PATH_UNC,
        ),
        "nul": (
            lambda: validate_pack_member_path("data/evil\x00.json", limits=default),
            PackRefusalCodeV1.PATH_NUL,
        ),
        "length": (
            lambda: validate_pack_member_path(
                "data/long.json",
                limits=replace(default, maximum_path_bytes=8),
            ),
            PackRefusalCodeV1.PATH_LENGTH_LIMIT,
        ),
        "depth": (
            lambda: validate_pack_member_path(
                "a/b/c/value.json",
                limits=replace(default, maximum_path_depth=2),
            ),
            PackRefusalCodeV1.PATH_DEPTH_LIMIT,
        ),
        "duplicate": (
            lambda: validate_pack_member_paths(
                ("data/value.json", "data/value.json"),
                limits=default,
            ),
            PackRefusalCodeV1.PATH_DUPLICATE,
        ),
        "casefold": (
            lambda: validate_pack_member_paths(
                ("data/Value.json", "data/value.json"),
                limits=default,
            ),
            PackRefusalCodeV1.PATH_CASEFOLD_COLLISION,
        ),
        "unicode": (
            lambda: validate_pack_member_paths(
                ("data/caf\u00e9.json", "data/cafe\u0301.json"),
                limits=default,
            ),
            PackRefusalCodeV1.PATH_UNICODE_COLLISION,
        ),
        "file_directory": (
            lambda: validate_pack_member_paths(
                ("data/collision", "data/collision/value.json"),
                limits=default,
            ),
            PackRefusalCodeV1.PATH_FILE_DIRECTORY_COLLISION,
        ),
    }
    outcomes = {
        name: _capture_refusal(operation)
        for name, (operation, _) in path_probes.items()
    }
    checks = {
        f"{name}_uses_{expected.value}": (
            outcomes[name]
            == (expected, PackValidationPhaseV1.CENTRAL_DIRECTORY)
        )
        for name, (_, expected) in path_probes.items()
    }
    return _case(
        "portable_path_and_collision_policy_is_closed",
        f"refused={sum(checks.values())}/{len(checks)}",
        checks,
        {
            "observed_codes": {
                name: None if outcome is None else outcome[0].value
                for name, outcome in outcomes.items()
            }
        },
    )


def _archive_resource_policy_case(
    manifest: PackManifestV1,
    payloads: dict[str, bytes],
    archive_bytes: bytes,
) -> PackAuditCase:
    default = DEFAULT_PACK_VALIDATION_LIMITS_V1
    manifest_bytes = canonical_manifest_bytes(manifest)
    total_expanded = len(manifest_bytes) + sum(len(raw) for raw in payloads.values())
    maximum_member = max(len(manifest_bytes), *(len(raw) for raw in payloads.values()))
    total_limits = replace(
        default,
        maximum_manifest_bytes=len(manifest_bytes),
        maximum_file_expanded_bytes=maximum_member,
        maximum_total_expanded_bytes=total_expanded - 1,
    )
    extra_dependency = PackDependencyV1(
        creator_id=_digest("WO39-B extra dependency creator"),
        namespace="org.kirby2.extra",
        name="second-base",
        version_constraint="1.0.0",
        expected_pack_id=_digest("WO39-B extra dependency pack"),
    )
    dependency_manifest = replace(
        manifest,
        dependencies=tuple(
            sorted(
                (*manifest.dependencies, extra_dependency),
                key=lambda item: item.sort_key,
            )
        ),
    )
    selected = manifest.inventory[0]
    resource_probes = {
        "archive_bytes": (
            lambda: preflight_pack_archive_bytes(
                archive_bytes,
                limits=replace(
                    default,
                    maximum_archive_bytes=len(archive_bytes) - 1,
                ),
            ),
            PackRefusalCodeV1.ARCHIVE_TOO_LARGE,
            PackValidationPhaseV1.TRANSPORT,
        ),
        "entry_count": (
            lambda: preflight_pack_archive_bytes(
                archive_bytes,
                limits=replace(default, maximum_entries=2),
            ),
            PackRefusalCodeV1.ENTRY_COUNT_LIMIT,
            PackValidationPhaseV1.CENTRAL_DIRECTORY,
        ),
        "central_directory": (
            lambda: preflight_pack_archive_bytes(
                archive_bytes,
                limits=replace(default, maximum_central_directory_bytes=1),
            ),
            PackRefusalCodeV1.CENTRAL_DIRECTORY_LIMIT,
            PackValidationPhaseV1.CENTRAL_DIRECTORY,
        ),
        "file_expansion": (
            lambda: validate_structural_payload(
                selected,
                payloads[selected.path],
                limits=replace(
                    default,
                    maximum_manifest_bytes=1,
                    maximum_file_expanded_bytes=1,
                ),
            ),
            PackRefusalCodeV1.FILE_EXPANDED_SIZE_LIMIT,
            PackValidationPhaseV1.CONTENT_STREAM,
        ),
        "total_expansion": (
            lambda: preflight_pack_archive_bytes(
                archive_bytes,
                limits=total_limits,
            ),
            PackRefusalCodeV1.TOTAL_EXPANDED_SIZE_LIMIT,
            PackValidationPhaseV1.CENTRAL_DIRECTORY,
        ),
        "dependency_count": (
            lambda: validate_manifest_complexity(
                dependency_manifest,
                limits=replace(default, maximum_dependencies=1),
            ),
            PackRefusalCodeV1.DEPENDENCY_COUNT_LIMIT,
            PackValidationPhaseV1.MANIFEST,
        ),
        "parse_depth": (
            lambda: validate_parse_complexity(
                {"a": {"b": {"c": {"d": 1}}}},
                limits=replace(default, maximum_parse_depth=2),
            ),
            PackRefusalCodeV1.PARSE_COMPLEXITY_LIMIT,
            PackValidationPhaseV1.CONTENT_STREAM,
        ),
        "event_rows": (
            lambda: validate_parse_complexity(
                {},
                limits=replace(default, maximum_event_rows=1),
                event_rows=2,
            ),
            PackRefusalCodeV1.PARSE_COMPLEXITY_LIMIT,
            PackValidationPhaseV1.CONTENT_STREAM,
        ),
        "expected_pack_id": (
            lambda: preflight_pack_archive_bytes(
                archive_bytes,
                expected_pack_id=_digest("wrong expected pack"),
            ),
            PackRefusalCodeV1.EXPECTED_PACK_ID_MISMATCH,
            PackValidationPhaseV1.MANIFEST,
        ),
        "expected_transport": (
            lambda: preflight_pack_archive_bytes(
                archive_bytes,
                expected_transport_sha256=_digest("wrong expected transport"),
            ),
            PackRefusalCodeV1.EXPECTED_TRANSPORT_DIGEST_MISMATCH,
            PackValidationPhaseV1.TRANSPORT,
        ),
    }
    outcomes = {
        name: _capture_refusal(operation)
        for name, (operation, _, _) in resource_probes.items()
    }
    checks = {
        f"{name}_uses_{code.value}": outcomes[name] == (code, phase)
        for name, (_, code, phase) in resource_probes.items()
    }
    checks.update(
        {
            "validation_policy_round_trips_exactly": (
                PackValidationLimitsV1.from_dict(default.as_dict()) == default
            ),
            "validation_policy_id_is_content_derived": (
                default.validation_policy_id == validation_policy_id(default)
            ),
            "safe_archive_preflight_binds_policy_and_identities": (
                _safe_preflight_binds(manifest, archive_bytes, default)
            ),
        }
    )
    return _case(
        "archive_resource_and_parse_budgets_fail_with_stable_codes",
        f"bounded={sum(checks.values())}/{len(checks)}",
        checks,
        {
            "observed_codes": {
                name: None if outcome is None else outcome[0].value
                for name, outcome in outcomes.items()
            },
            "validation_policy_id": default.validation_policy_id,
        },
    )


def _private_stage_lifecycle_case(
    manifest: PackManifestV1,
    payloads: dict[str, bytes],
    archive_bytes: bytes,
) -> PackAuditCase:
    with TemporaryDirectory(prefix="kirby2-wo39b-safe-") as raw_root:
        root = Path(raw_root).resolve()
        root.chmod(0o700)
        stage = stage_pack_archive_bytes(
            archive_bytes,
            root,
            expected_pack_id=manifest.pack_id,
            expected_transport_sha256=hashlib.sha256(archive_bytes).hexdigest(),
        )
        initial = revalidate_pack_stage(stage)
        expected_files = {
            K2PACK_MANIFEST_PATH: canonical_manifest_bytes(manifest),
            **payloads,
        }
        actual_files = {
            path.relative_to(stage.stage_path).as_posix(): path.read_bytes()
            for path in stage.stage_path.rglob("*")
            if path.is_file() and not path.is_symlink()
        }
        tree_entries = tuple(stage.stage_path.rglob("*"))
        safe_modes = all(
            not path.is_symlink()
            and stat.S_IMODE(path.stat(follow_symlinks=False).st_mode)
            == (0o700 if path.is_dir() else 0o600)
            for path in tree_entries
        )
        round_trip = PackStageVerificationV1.from_canonical_bytes(
            initial.canonical_bytes()
        )
        target = stage.stage_path.joinpath(*manifest.inventory[0].path.split("/"))
        original = target.read_bytes()
        target.write_bytes(bytes((original[0] ^ 1,)) + original[1:])
        tamper = _capture_refusal(lambda: revalidate_pack_stage(stage))
        target.write_bytes(original)
        restored = revalidate_pack_stage(stage)
        checks = {
            "stage_is_only_activation_eligible_not_active": (
                stage.schema_id == PACK_STAGE_CAPABILITY_SCHEMA_ID
                and stage.as_dict()["schema_id"]
                == "KIRBY2_ACTIVATION_ELIGIBLE_PACK_STAGE_V1"
            ),
            "stage_binds_manifest_transport_inventory_and_policy": (
                stage.pack_id == manifest.pack_id
                and stage.preflight.transport_sha256
                == hashlib.sha256(archive_bytes).hexdigest()
                and stage.inventory_sha256 == stage.preflight.inventory_sha256
                and stage.validation_policy_id
                == DEFAULT_PACK_VALIDATION_LIMITS_V1.validation_policy_id
            ),
            "nested_tree_contains_only_exact_declared_regular_bytes": (
                actual_files == expected_files and safe_modes
            ),
            "stage_verification_is_canonical_and_repeatable": (
                initial == round_trip == restored == stage.verification
            ),
            "post_extraction_tamper_is_refused_on_revalidation": (
                tamper
                == (
                    PackRefusalCodeV1.PAYLOAD_DIGEST_MISMATCH,
                    PackValidationPhaseV1.STAGE_REVALIDATION,
                )
            ),
            "stage_counts_bind_exact_payload_inventory": (
                stage.file_count == len(payloads)
                and stage.total_byte_count
                == sum(len(raw) for raw in payloads.values())
            ),
        }
        evidence = {
            "pack_id": stage.pack_id,
            "stage_verification_sha256": stage.verification_sha256,
            "staged_tree_sha256": stage.staged_tree_sha256,
            "tamper_code": None if tamper is None else tamper[0].value,
        }
        discard_pack_stage(stage)
        checks["discard_removes_the_exact_private_stage"] = not any(root.iterdir())
    return _case(
        "safe_nested_pack_stages_revalidates_detects_tamper_and_discards",
        (
            f"pack={manifest.pack_id} files={len(payloads)} "
            f"tamper={evidence['tamper_code']}"
        ),
        checks,
        evidence,
    )


def _capture_refusal(
    operation,
) -> tuple[PackRefusalCodeV1, PackValidationPhaseV1] | None:
    try:
        operation()
    except PackValidationRefused as error:
        return error.refusal.code, error.refusal.phase
    return None


def _safe_preflight_binds(
    manifest: PackManifestV1,
    archive_bytes: bytes,
    limits: PackValidationLimitsV1,
) -> bool:
    preflight = preflight_pack_archive_bytes(
        archive_bytes,
        limits=limits,
        expected_pack_id=manifest.pack_id,
        expected_transport_sha256=hashlib.sha256(archive_bytes).hexdigest(),
    )
    return (
        preflight.manifest == manifest
        and preflight.pack_id == manifest.pack_id
        and preflight.validation_policy_id == limits.validation_policy_id
        and len(preflight.payload_members) == len(manifest.inventory)
    )


def audit_atomic_pack_installation() -> tuple[PackAuditCase, ...]:
    """Exercise the fixed WO39-C local resolution and mutation boundary."""

    base, payloads = _fixture_pack()
    provider, consumer = _installation_manifests(base)
    environment = _runtime_environment(provider)
    workflow_cases = _installation_workflow_cases(
        provider,
        consumer,
        payloads,
        environment,
    )
    cases = (
        _local_dependency_resolution_case(base, environment),
        *workflow_cases,
    )
    expected_names = (
        "local_dependency_resolution_is_exact_ordered_and_fail_closed",
        "installation_is_atomic_idempotent_content_addressed_and_read_only",
        "failed_installations_preserve_the_prior_registry_and_stage",
        "dependent_refusal_and_recoverable_removal_preserve_run_evidence",
    )
    if len(cases) != WO39C_AUDIT_CASE_COUNT:
        raise RuntimeError("WO39-C audit case inventory changed")
    if tuple(item.name for item in cases) != expected_names:
        raise RuntimeError("WO39-C audit case order or identity changed")
    return cases


def audit_clean_root_pack_transfer() -> tuple[PackAuditCase, ...]:
    """Exercise the WO38-C bridge through the existing WO39-B/C boundaries."""

    cases = (_clean_root_pack_transfer_case(),)
    if len(cases) != WO38C_PACK_AUDIT_CASE_COUNT:
        raise RuntimeError("WO38-C pack audit case inventory changed")
    if (
        cases[0].name
        != "clean_root_pack_transfer_is_exact_atomic_and_fail_closed"
    ):
        raise RuntimeError("WO38-C pack audit case identity changed")
    return cases


def build_clean_root_transfer_audit_fixture(
) -> tuple[PackManifestV1, dict[str, bytes]]:
    """Return the shared self-contained, dependency-free WO38-C audit pack."""

    base, payloads = _fixture_pack()
    return (
        replace(
            base,
            name="clean-root-transfer",
            title="Clean-root transfer audit pack",
            dependencies=(),
        ),
        payloads,
    )


def _clean_root_pack_transfer_case() -> PackAuditCase:
    from kirby2.orchestration.artifacts import (
        ContentRequestV1,
        PackTransferBundleV1,
        build_normalized_pack_archive,
    )
    from kirby2.orchestration.compatibility import (
        PackCapabilityIdentityBindingV1,
        PackSchemaIdentityBindingV1,
        build_content_request,
        pack_redistribution_decision_identity,
    )
    from kirby2.orchestration.content_store import OrchestrationContentStoreV1
    from kirby2.orchestration.models import (
        DigestReferenceV1,
        LogicalWorkCellV1,
        WorkKindV1,
    )
    from kirby2.orchestration.planner import build_experiment_work_plan
    from kirby2.orchestration.protocol import WorkerCompatibilityV1
    from kirby2.orchestration.seeds import build_master_seed_identity

    manifest, payloads = build_clean_root_transfer_audit_fixture()
    environment = _runtime_environment(manifest)
    installable = next(
        item
        for item in manifest.compatibility
        if item.level is PackCompatibilityLevelV1.INSTALLABLE
    )
    schema_identities = tuple(
        sorted(
            (
                DigestReferenceV1(
                    name=requirement.schema_id,
                    sha256=_digest(
                        f"WO38-C worker schema {requirement.schema_id}"
                    ),
                )
                for requirement in installable.schemas
            ),
            key=lambda item: item.sort_key,
        )
    )
    capability_identities = tuple(
        sorted(
            (
                DigestReferenceV1(
                    name=f"capability:{label}",
                    sha256=_digest(f"WO38-C worker capability {label}"),
                )
                for label in manifest.capability_labels
            ),
            key=lambda item: item.sort_key,
        )
    )
    worker_compatibility = WorkerCompatibilityV1(
        engine_identity=DigestReferenceV1(
            name="engine.clean-root-audit",
            sha256=_digest("WO38-C worker engine"),
        ),
        runtime_identity=DigestReferenceV1(
            name="runtime.clean-root-audit",
            sha256=_digest("WO38-C worker runtime"),
        ),
        dependency_identity=DigestReferenceV1(
            name="dependencies.clean-root-audit",
            sha256=_digest("WO38-C worker dependencies"),
        ),
        compiler_identity=DigestReferenceV1(
            name="compiler.clean-root-audit",
            sha256=_digest("WO38-C worker compiler"),
        ),
        schemas=schema_identities,
        capabilities=capability_identities,
    )
    schema_bindings = tuple(
        sorted(
            (
                PackSchemaIdentityBindingV1(
                    schema_id=requirement.schema_id,
                    schema_version=next(
                        version
                        for schema_id, version in environment.schema_versions
                        if schema_id == requirement.schema_id
                    ),
                    worker_schema_identity=next(
                        item
                        for item in schema_identities
                        if item.name == requirement.schema_id
                    ),
                )
                for requirement in installable.schemas
            ),
            key=lambda item: item.sort_key,
        )
    )
    capability_bindings = tuple(
        sorted(
            (
                PackCapabilityIdentityBindingV1(
                    capability_label=label,
                    worker_capability_identity=next(
                        item
                        for item in capability_identities
                        if item.name == f"capability:{label}"
                    ),
                )
                for label in manifest.capability_labels
            ),
            key=lambda item: item.sort_key,
        )
    )
    cell = LogicalWorkCellV1(
        partition_id="clean-root-transfer",
        cell_id="pack-install",
        work_kind=WorkKindV1.COMPLETE_RUN,
        configuration={"fixture_id": "WO38-C"},
    )
    plan = build_experiment_work_plan(
        master_seed_identity=build_master_seed_identity(38_003),
        experiment_identity=DigestReferenceV1(
            name="experiment.clean-root-transfer",
            sha256=_digest("WO38-C transfer experiment"),
        ),
        cells=(cell,),
        scenario=DigestReferenceV1(
            name="scenario.clean-root-transfer",
            sha256=_digest("WO38-C transfer scenario"),
        ),
        market_profile=DigestReferenceV1(
            name="market-profile.clean-root-transfer",
            sha256=_digest("WO38-C transfer market profile"),
        ),
        datasets=(),
        strategies=(),
        packs=(
            DigestReferenceV1(
                name="pack.clean-root-transfer",
                sha256=manifest.pack_id,
            ),
        ),
        software_version="0.1.0",
        source_version="source-v1",
        engine_identity=worker_compatibility.engine_identity,
        runtime_identity=worker_compatibility.runtime_identity,
        dependency_identity=worker_compatibility.dependency_identity,
        compiler_identity=worker_compatibility.compiler_identity,
        schemas=worker_compatibility.schemas,
        capabilities=worker_compatibility.capabilities,
        expected_outputs=(
            DigestReferenceV1(
                name="output.clean-root-transfer",
                sha256=_digest("WO38-C transfer output"),
            ),
        ),
        resource_class="cpu-small",
    )
    logical_unit = plan.logical_units[0]
    request = build_content_request(logical_unit)
    redistribution = pack_redistribution_decision_identity(manifest)
    bundle = build_normalized_pack_archive(
        manifest,
        payloads,
        redistribution,
    )

    with TemporaryDirectory(prefix="kirby2-wo38c-source-") as source_raw:
        with TemporaryDirectory(prefix="kirby2-wo38c-target-") as target_raw:
            source_root = Path(source_raw).resolve()
            target_root = Path(target_raw).resolve()
            source_root.chmod(0o700)
            target_root.chmod(0o700)
            source_paths = DataPaths(source_root)
            target_paths = DataPaths(target_root)
            source_store = OrchestrationContentStoreV1(paths=source_paths)
            target_store = OrchestrationContentStoreV1(paths=target_paths)

            first_registration = source_store.register_source_transport(bundle)
            repeated_registration = source_store.register_source_transport(bundle)
            served = source_store.serve_source_transport(
                request,
                bundle.descriptor,
                logical_work_unit=logical_unit,
            )
            installation = target_store.receive_and_install_pack(
                request,
                served,
                logical_work_unit=logical_unit,
                environment=environment,
                worker_compatibility=worker_compatibility,
                schema_bindings=schema_bindings,
                capability_bindings=capability_bindings,
            )
            registry = read_pack_registry(paths=target_paths)
            installed_object = target_paths.packs.joinpath(
                *installation.receipt.object_path.split("/")
            )

            corrupted_raw = bytearray(bundle.archive_bytes)
            corrupted_raw[len(corrupted_raw) // 2] ^= 0x01
            corrupted_bytes = bytes(corrupted_raw)
            corrupted_descriptor = replace(
                bundle.descriptor,
                transport_sha256=hashlib.sha256(corrupted_bytes).hexdigest(),
            )
            corrupted_bundle = PackTransferBundleV1(
                descriptor=corrupted_descriptor,
                archive_bytes=corrupted_bytes,
            )
            corrupted_refusal = _capture_exception_text(
                lambda: target_store.receive_and_install_pack(
                    request,
                    corrupted_bundle,
                    logical_work_unit=logical_unit,
                    environment=environment,
                    worker_compatibility=worker_compatibility,
                    schema_bindings=schema_bindings,
                    capability_bindings=capability_bindings,
                )
            )
            policy_bundle = PackTransferBundleV1(
                descriptor=replace(
                    bundle.descriptor,
                    validation_policy_id=_digest(
                        "WO38-C wrong validation policy"
                    ),
                ),
                archive_bytes=bundle.archive_bytes,
            )
            policy_refusal = _capture_exception_text(
                lambda: target_store.receive_and_install_pack(
                    request,
                    policy_bundle,
                    logical_work_unit=logical_unit,
                    environment=environment,
                    worker_compatibility=worker_compatibility,
                    schema_bindings=schema_bindings,
                    capability_bindings=capability_bindings,
                )
            )
            bad_capability_bindings = (
                replace(
                    capability_bindings[0],
                    worker_capability_identity=replace(
                        capability_bindings[0].worker_capability_identity,
                        sha256=_digest("WO38-C unavailable capability"),
                    ),
                ),
                *capability_bindings[1:],
            )
            capability_refusal = _capture_exception_text(
                lambda: target_store.receive_and_install_pack(
                    request,
                    bundle,
                    logical_work_unit=logical_unit,
                    environment=environment,
                    worker_compatibility=worker_compatibility,
                    schema_bindings=schema_bindings,
                    capability_bindings=bad_capability_bindings,
                )
            )
            path_refusal = _capture_exception_text(
                lambda: ContentRequestV1(
                    content_references=(
                        DigestReferenceV1(
                            name="escape/path",
                            sha256=_digest("WO38-C path request"),
                        ),
                    )
                )
            )
            registry_after_refusals = read_pack_registry(paths=target_paths)
            checks = {
                "content_request_and_descriptor_are_path_free": (
                    str(source_root) not in repr(request.as_dict())
                    and str(target_root) not in repr(request.as_dict())
                    and frozenset(bundle.descriptor.identity_dict())
                    == {
                        "byte_count",
                        "inventory_sha256",
                        "manifest_sha256",
                        "pack_id",
                        "redistribution_decision_identity",
                        "schema_id",
                        "schema_version",
                        "transport_sha256",
                        "validation_policy_id",
                    }
                ),
                "source_registration_is_immutable_and_idempotent": (
                    not first_registration.already_registered
                    and repeated_registration.already_registered
                    and served == bundle
                ),
                "bundle_round_trips_exact_canonical_bytes": (
                    PackTransferBundleV1.from_canonical_bytes(
                        bundle.canonical_bytes()
                    )
                    == bundle
                ),
                "clean_root_installs_exact_read_only_content_address": (
                    installation.receipt.pack_id == manifest.pack_id
                    and registry.require(manifest.registry_key).pack_id
                    == manifest.pack_id
                    and _installed_tree_is_read_only(installed_object)
                    and not target_paths.cache.exists()
                ),
                "corruption_policy_capability_and_path_attacks_are_refused": (
                    corrupted_refusal is not None
                    and policy_refusal is not None
                    and capability_refusal is not None
                    and path_refusal is not None
                ),
                "refusals_leave_no_partial_stage_or_registry_change": (
                    registry_after_refusals == registry
                    and not any(target_paths.staging.iterdir())
                ),
            }
            evidence = {
                "bundle_sha256": bundle.bundle_sha256,
                "capability_refusal": capability_refusal,
                "corrupted_refusal": corrupted_refusal,
                "installed_pack_id": installation.receipt.pack_id,
                "path_refusal": path_refusal,
                "policy_refusal": policy_refusal,
                "request_id": request.content_request_id,
                "target_registry_sha256": registry.sha256,
            }
    return _case(
        "clean_root_pack_transfer_is_exact_atomic_and_fail_closed",
        f"pack={manifest.pack_id} request={request.content_request_id}",
        checks,
        evidence,
    )


def _installation_manifests(
    base: PackManifestV1,
) -> tuple[PackManifestV1, PackManifestV1]:
    provider = replace(
        base,
        name="local-provider",
        title="Local dependency provider",
        dependencies=(),
    )
    dependency = PackDependencyV1(
        creator_id=provider.creator_id,
        namespace=provider.namespace,
        name=provider.name,
        version_constraint="1.0.0",
        expected_pack_id=provider.pack_id,
    )
    consumer = replace(
        base,
        name="local-consumer",
        title="Local dependency consumer",
        dependencies=(dependency,),
    )
    return provider, consumer


def _runtime_environment(manifest: PackManifestV1) -> PackRuntimeEnvironmentV1:
    installable = next(
        item
        for item in manifest.compatibility
        if item.level is PackCompatibilityLevelV1.INSTALLABLE
    )
    return PackRuntimeEnvironmentV1(
        engine_component_id="KIRBY2_ENGINE_V1",
        engine_version="0.1.0",
        compiler_versions=tuple(
            sorted(
                (item.component_id, "1.0.0")
                for item in installable.compilers
            )
        ),
        schema_versions=tuple(
            sorted(
                (item.schema_id, item.supported_versions[0])
                for item in installable.schemas
            )
        ),
    )


def _local_dependency_resolution_case(
    base: PackManifestV1,
    environment: PackRuntimeEnvironmentV1,
) -> PackAuditCase:
    provider_a = replace(
        base,
        name="resolution-provider-a",
        title="Resolution provider A",
        dependencies=(),
    )
    provider_b = replace(
        base,
        name="resolution-provider-b",
        title="Resolution provider B",
        dependencies=(),
    )
    provider_a_newer = replace(
        provider_a,
        title="Resolution provider A newer compatible version",
        version="1.1.0",
    )
    provider_a_entry = PackRegistryEntryV1.from_manifest(
        provider_a,
        (),
        active=True,
    )
    provider_b_entry = PackRegistryEntryV1.from_manifest(
        provider_b,
        (),
        active=True,
    )
    selected_entries = tuple(
        sorted(
            (provider_a_entry, provider_b_entry),
            key=lambda item: item.sort_key,
        )
    )
    entries = tuple(
        sorted(
            (
                *selected_entries,
                PackRegistryEntryV1.from_manifest(
                    provider_a_newer,
                    (),
                    active=True,
                ),
            ),
            key=lambda item: item.sort_key,
        )
    )
    registry = PackRegistryV1(entries=entries)
    dependencies = tuple(
        sorted(
            (
                PackDependencyV1(
                    creator_id=item.creator_id,
                    namespace=item.namespace,
                    name=item.name,
                    version_constraint=">=1.0.0,<2.0.0",
                    expected_pack_id=item.pack_id,
                )
                for item in (provider_b, provider_a)
            ),
            key=lambda item: item.sort_key,
        )
    )
    root = replace(
        base,
        name="resolution-root",
        title="Deterministic dependency resolution root",
        dependencies=dependencies,
    )
    first = resolve_pack_dependencies(root, registry, environment)
    second = resolve_pack_dependencies(root, registry, environment)
    root_entry = PackRegistryEntryV1.from_manifest(
        root,
        first.registry_edges,
        active=True,
    )
    complete_registry = PackRegistryV1(
        entries=tuple(sorted((*entries, root_entry), key=lambda item: item.sort_key))
    )
    registry_raw = canonical_pack_registry_bytes(complete_registry)

    digest_conflict_dependency = replace(
        dependencies[0],
        expected_pack_id=_digest("WO39-C wrong dependency digest"),
    )
    digest_conflict = replace(
        root,
        dependencies=tuple(
            sorted(
                (digest_conflict_dependency, *dependencies[1:]),
                key=lambda item: item.sort_key,
            )
        ),
    )
    version_conflict_dependency = replace(
        dependencies[0],
        version_constraint=">=2.0.0,<3.0.0",
    )
    version_conflict = replace(
        root,
        dependencies=tuple(
            sorted(
                (version_conflict_dependency, *dependencies[1:]),
                key=lambda item: item.sort_key,
            )
        ),
    )
    self_requirement = PackDependencyV1(
        creator_id=provider_a.creator_id,
        namespace=provider_a.namespace,
        name=provider_a.name,
        version_constraint=provider_a.version,
        expected_pack_id=provider_a.pack_id,
    )
    self_cycle = replace(provider_a, dependencies=(self_requirement,))
    incompatible_environment = replace(environment, engine_version="9.0.0")

    refusal_text = {
        "missing": _capture_exception_text(
            lambda: resolve_pack_dependencies(
                root,
                PackRegistryV1.empty(),
                environment,
            )
        ),
        "digest": _capture_exception_text(
            lambda: resolve_pack_dependencies(
                digest_conflict,
                registry,
                environment,
            )
        ),
        "version": _capture_exception_text(
            lambda: resolve_pack_dependencies(
                version_conflict,
                registry,
                environment,
            )
        ),
        "cycle": _capture_exception_text(
            lambda: resolve_pack_dependencies(
                self_cycle,
                PackRegistryV1(entries=(provider_a_entry,)),
                environment,
            )
        ),
        "engine": _capture_exception_text(
            lambda: validate_installability(root, incompatible_environment)
        ),
        "ambiguous_key": _capture_exception_text(
            lambda: PackRegistryV1(entries=(provider_a_entry, provider_a_entry))
        ),
    }
    expected_order = tuple(item.key for item in selected_entries)
    checks = {
        "resolution_repeats_byte_for_byte": first.as_dict() == second.as_dict(),
        "dependency_first_order_uses_full_registry_keys": (
            tuple(item.key for item in first.dependency_first_order)
            == expected_order
        ),
        "direct_edges_are_digest_bound_and_canonical": (
            first.registry_edges == root_entry.resolved_dependencies
            and tuple(item.key for item in first.direct_dependencies)
            == expected_order
        ),
        "exact_digest_beats_a_newer_semver_compatible_candidate": (
            any(
                item.key == provider_a.registry_key
                and item.pack_id == provider_a.pack_id
                for item in first.direct_dependencies
            )
            and all(
                item.pack_id != provider_a_newer.pack_id
                for item in first.direct_dependencies
            )
        ),
        "canonical_registry_round_trips_and_sorts": (
            load_pack_registry_bytes(registry_raw) == complete_registry
            and complete_registry.keys
            == tuple(sorted(complete_registry.keys, key=lambda item: item.sort_key))
        ),
        "semver_range_is_closed_and_exact": (
            semver_satisfies("1.5.0", ">=1.0.0,<2.0.0")
            and not semver_satisfies("2.0.0", ">=1.0.0,<2.0.0")
        ),
        "missing_dependency_has_no_remote_fallback": (
            refusal_text["missing"] is not None
            and "MISSING_PACK_DEPENDENCY" in refusal_text["missing"]
        ),
        "digest_conflict_is_refused": (
            refusal_text["digest"] is not None
            and "PACK_DEPENDENCY_DIGEST_CONFLICT" in refusal_text["digest"]
        ),
        "version_conflict_is_refused": (
            refusal_text["version"] is not None
            and "PACK_DEPENDENCY_VERSION_CONFLICT" in refusal_text["version"]
        ),
        "cycle_is_refused": (
            refusal_text["cycle"] is not None
            and "PACK_DEPENDENCY_CYCLE" in refusal_text["cycle"]
        ),
        "incompatible_engine_is_refused": (
            refusal_text["engine"] is not None
            and "INCOMPATIBLE_PACK_ENGINE" in refusal_text["engine"]
        ),
        "duplicate_provider_key_cannot_enter_registry": (
            refusal_text["ambiguous_key"] is not None
            and "keys must be unique" in refusal_text["ambiguous_key"]
        ),
    }
    return _case(
        "local_dependency_resolution_is_exact_ordered_and_fail_closed",
        f"root={root.pack_id} dependencies={len(expected_order)}",
        checks,
        {
            "dependency_order": [item.as_dict() for item in expected_order],
            "installed_candidates": [
                item.key.as_dict() for item in registry.entries
            ],
            "registry_sha256": complete_registry.sha256,
            "refusals": refusal_text,
        },
    )


def _installation_workflow_cases(
    provider: PackManifestV1,
    consumer: PackManifestV1,
    payloads: dict[str, bytes],
    environment: PackRuntimeEnvironmentV1,
) -> tuple[PackAuditCase, PackAuditCase, PackAuditCase]:
    with TemporaryDirectory(prefix="kirby2-wo39c-install-") as raw_root:
        root = Path(raw_root).resolve()
        root.chmod(0o700)
        paths = DataPaths(root)
        paths.ensure_pack_installation_areas()
        paths.ensure(DataAreaId.RUNS)
        run_evidence = paths.runs / "completed-run-evidence.json"
        run_bytes = canonical_json_bytes(
            {
                "run_id": "WO39C_COMPLETED_RUN_EVIDENCE",
                "schema_id": "KIRBY2_WO39C_RUN_EVIDENCE_V1",
                "schema_version": 1,
            }
        )
        run_evidence.write_bytes(run_bytes)

        provider_archive = _normalized_archive(
            provider,
            payloads,
            reverse_input=False,
        )
        provider_stage = stage_pack_archive_bytes(provider_archive, paths.staging)
        provider_receipt = install_pack(
            provider_stage,
            paths=paths,
            environment=environment,
        )
        provider_object = paths.packs.joinpath(
            *provider_receipt.object_path.split("/")
        )
        provider_stage_moved = not provider_stage.stage_path.exists()

        repeated_stage = stage_pack_archive_bytes(provider_archive, paths.staging)
        repeated_receipt = install_pack(
            repeated_stage,
            paths=paths,
            environment=environment,
        )
        discard_pack_stage(repeated_stage)

        consumer_archive = _normalized_archive(
            consumer,
            payloads,
            reverse_input=False,
        )
        consumer_stage = stage_pack_archive_bytes(consumer_archive, paths.staging)
        consumer_receipt = install_pack(
            consumer_stage,
            paths=paths,
            environment=environment,
        )
        consumer_object = paths.packs.joinpath(
            *consumer_receipt.object_path.split("/")
        )
        registry_after_install = read_pack_registry(paths=paths)
        registry_after_install_sha256 = registry_after_install.sha256
        registry_path = paths.packs / PACK_REGISTRY_FILENAME
        lock_path = paths.packs / PACK_REGISTRY_LOCK_FILENAME
        lock_metadata = lock_path.stat(follow_symlinks=False)

        installed_modes_safe = _installed_tree_is_read_only(
            provider_object
        ) and _installed_tree_is_read_only(consumer_object)
        installation_checks = {
            "new_stages_publish_by_atomic_inode_move": (
                provider_receipt.installed_new_object
                and consumer_receipt.installed_new_object
                and provider_stage_moved
                and not consumer_stage.stage_path.exists()
            ),
            "published_objects_use_canonical_content_addresses": (
                provider_receipt.object_path
                == pack_object_relative_path(provider.pack_id)
                and consumer_receipt.object_path
                == pack_object_relative_path(consumer.pack_id)
                and provider_object.is_dir()
                and consumer_object.is_dir()
            ),
            "installed_objects_are_exact_read_only_trees": installed_modes_safe,
            "registry_contains_sorted_active_exact_bindings": (
                len(registry_after_install.entries) == 2
                and all(item.active for item in registry_after_install.entries)
                and registry_after_install.require(provider.registry_key).pack_id
                == provider.pack_id
                and registry_after_install.require(consumer.registry_key).pack_id
                == consumer.pack_id
            ),
            "registry_file_contains_exact_canonical_snapshot_bytes": (
                registry_path.read_bytes()
                == canonical_pack_registry_bytes(registry_after_install)
            ),
            "registry_lock_is_one_owner_only_empty_regular_file": (
                not lock_path.is_symlink()
                and stat.S_ISREG(lock_metadata.st_mode)
                and stat.S_IMODE(lock_metadata.st_mode) == 0o600
                and lock_metadata.st_nlink == 1
                and lock_metadata.st_size == 0
            ),
            "consumer_records_exact_local_dependency_edge": (
                consumer_receipt.resolved_dependencies
                == registry_after_install.require(
                    consumer.registry_key
                ).resolved_dependencies
                and len(consumer_receipt.resolved_dependencies) == 1
                and consumer_receipt.resolved_dependencies[0].pack_id
                == provider.pack_id
            ),
            "repeat_install_is_idempotent": (
                not repeated_receipt.installed_new_object
                and not repeated_receipt.registry_changed
                and repeated_receipt.registry_before_sha256
                == repeated_receipt.registry_after_sha256
            ),
            "installed_lookup_returns_exact_entry": (
                lookup_installed_pack(provider.registry_key, paths=paths)
                == registry_after_install.require(provider.registry_key)
            ),
        }
        installation_case = _case(
            "installation_is_atomic_idempotent_content_addressed_and_read_only",
            (
                f"registry={registry_after_install.sha256} "
                f"objects={len(registry_after_install.entries)}"
            ),
            installation_checks,
            {
                "consumer_receipt_sha256": consumer_receipt.sha256,
                "provider_receipt_sha256": provider_receipt.sha256,
                "registry_sha256": registry_after_install.sha256,
            },
        )

        conflict_manifest = replace(
            consumer,
            title="Conflicting bytes under one immutable registry key",
        )
        conflict_archive = _normalized_archive(
            conflict_manifest,
            payloads,
            reverse_input=False,
        )
        conflict_stage = stage_pack_archive_bytes(conflict_archive, paths.staging)
        conflict = _capture_install_refusal(
            lambda: install_pack(
                conflict_stage,
                paths=paths,
                environment=environment,
            )
        )
        conflict_stage_intact = revalidate_pack_stage(conflict_stage) is not None
        conflict_registry_unchanged = (
            read_pack_registry(paths=paths).sha256 == registry_after_install_sha256
        )
        discard_pack_stage(conflict_stage)

        missing_dependency = PackDependencyV1(
            creator_id=_digest("WO39-C missing creator"),
            namespace="org.kirby2.missing",
            name="absent-provider",
            version_constraint="1.0.0",
            expected_pack_id=_digest("WO39-C missing pack"),
        )
        missing_manifest = replace(
            provider,
            name="missing-dependency-consumer",
            title="Missing dependency refusal",
            dependencies=(missing_dependency,),
        )
        missing_archive = _normalized_archive(
            missing_manifest,
            payloads,
            reverse_input=False,
        )
        missing_stage = stage_pack_archive_bytes(missing_archive, paths.staging)
        missing = _capture_install_refusal(
            lambda: install_pack(
                missing_stage,
                paths=paths,
                environment=environment,
            )
        )
        missing_stage_intact = revalidate_pack_stage(missing_stage) is not None
        missing_registry_unchanged = (
            read_pack_registry(paths=paths).sha256 == registry_after_install_sha256
        )
        discard_pack_stage(missing_stage)

        with TemporaryDirectory(prefix="kirby2-wo39c-foreign-") as foreign_raw:
            foreign_root = Path(foreign_raw).resolve()
            foreign_root.chmod(0o700)
            foreign_stage = stage_pack_archive_bytes(
                provider_archive,
                foreign_root,
            )
            foreign = _capture_install_refusal(
                lambda: install_pack(
                    foreign_stage,
                    paths=paths,
                    environment=environment,
                )
            )
            foreign_stage_intact = revalidate_pack_stage(foreign_stage) is not None
            discard_pack_stage(foreign_stage)
        foreign_registry_unchanged = (
            read_pack_registry(paths=paths).sha256 == registry_after_install_sha256
        )
        failure_checks = {
            "immutable_key_conflict_is_refused": conflict
            == (
                PackInstallRefusalCodeV1.REGISTRY_KEY_CONFLICT,
                PackInstallOperationV1.INSTALL,
            ),
            "missing_dependency_is_refused_without_fetch": missing
            == (
                PackInstallRefusalCodeV1.DEPENDENCY_RESOLUTION_FAILED,
                PackInstallOperationV1.INSTALL,
            ),
            "foreign_staging_root_is_refused": foreign
            == (
                PackInstallRefusalCodeV1.STAGING_ROOT_MISMATCH,
                PackInstallOperationV1.INSTALL,
            ),
            "failed_stages_remain_intact_and_recoverable": (
                conflict_stage_intact
                and missing_stage_intact
                and foreign_stage_intact
            ),
            "all_failures_preserve_prior_registry_digest": (
                conflict_registry_unchanged
                and missing_registry_unchanged
                and foreign_registry_unchanged
            ),
            "failure_cleanup_leaves_staging_area_empty": (
                not any(paths.staging.iterdir())
            ),
        }
        failure_case = _case(
            "failed_installations_preserve_the_prior_registry_and_stage",
            (
                f"registry={registry_after_install_sha256} "
                f"conflict={None if conflict is None else conflict[0].value}"
            ),
            failure_checks,
            {
                "conflict_code": None if conflict is None else conflict[0].value,
                "foreign_code": None if foreign is None else foreign[0].value,
                "missing_code": None if missing is None else missing[0].value,
                "registry_sha256": registry_after_install_sha256,
            },
        )

        dependent_deactivation = _capture_install_refusal(
            lambda: deactivate_pack(provider.registry_key, paths=paths)
        )
        active_removal = _capture_install_refusal(
            lambda: remove_deactivated_pack(provider.registry_key, paths=paths)
        )
        consumer_deactivation = deactivate_pack(consumer.registry_key, paths=paths)
        consumer_removal = remove_deactivated_pack(
            consumer.registry_key,
            paths=paths,
        )
        provider_deactivation = deactivate_pack(provider.registry_key, paths=paths)
        provider_removal = remove_deactivated_pack(
            provider.registry_key,
            paths=paths,
        )
        final_registry = read_pack_registry(paths=paths)
        consumer_recovery = paths.packs.joinpath(
            *consumer_removal.recovery_path.split("/")
        )
        provider_recovery = paths.packs.joinpath(
            *provider_removal.recovery_path.split("/")
        )
        removal_checks = {
            "active_dependency_cannot_be_deactivated": dependent_deactivation
            == (
                PackInstallRefusalCodeV1.ACTIVE_DEPENDENTS,
                PackInstallOperationV1.DEACTIVATE,
            ),
            "active_pack_cannot_be_removed": active_removal
            == (
                PackInstallRefusalCodeV1.PACK_STILL_ACTIVE,
                PackInstallOperationV1.REMOVE,
            ),
            "dependent_is_deactivated_before_removal": (
                not consumer_deactivation.already_inactive
                and consumer_deactivation.registry_before_sha256
                != consumer_deactivation.registry_after_sha256
            ),
            "provider_is_removable_only_after_dependent_is_gone": (
                not provider_deactivation.already_inactive
                and provider_deactivation.registry_before_sha256
                != provider_deactivation.registry_after_sha256
            ),
            "removal_is_recoverable_not_destructive": (
                consumer_recovery.is_dir()
                and provider_recovery.is_dir()
                and not consumer_object.exists()
                and not provider_object.exists()
            ),
            "registry_is_empty_after_ordered_removal": final_registry.entries == (),
            "completed_run_evidence_is_byte_identical": (
                run_evidence.read_bytes() == run_bytes
            ),
            "registry_and_run_areas_remain_separate": (
                paths.runs != paths.packs
                and paths.runs not in paths.packs.parents
                and paths.packs not in paths.runs.parents
            ),
        }
        removal_case = _case(
            "dependent_refusal_and_recoverable_removal_preserve_run_evidence",
            (
                f"final_registry={final_registry.sha256} "
                f"run_bytes={len(run_bytes)}"
            ),
            removal_checks,
            {
                "consumer_removal_sha256": consumer_removal.sha256,
                "final_registry_sha256": final_registry.sha256,
                "provider_removal_sha256": provider_removal.sha256,
                "run_evidence_sha256": hashlib.sha256(run_bytes).hexdigest(),
            },
        )
        return installation_case, failure_case, removal_case


def _installed_tree_is_read_only(root: Path) -> bool:
    if root.is_symlink() or not root.is_dir():
        return False
    entries = (root, *tuple(root.rglob("*")))
    return all(
        not path.is_symlink()
        and stat.S_IMODE(path.stat(follow_symlinks=False).st_mode)
        == (0o500 if path.is_dir() else 0o400)
        for path in entries
    )


def _capture_install_refusal(
    operation,
) -> tuple[PackInstallRefusalCodeV1, PackInstallOperationV1] | None:
    try:
        operation()
    except PackInstallRefused as error:
        return error.code, error.operation
    return None


@dataclass(frozen=True, slots=True)
class _DomainPackAuditFixture:
    specification: PackBuildSpecificationV1
    originals: dict[str, bytes]
    build: DomainPackBuildV1
    verification: DomainPackVerificationV1


def audit_training_domain_packs() -> tuple[PackAuditCase, ...]:
    """Exercise every WO39-D1 adapter and its fail-closed public boundary."""

    fixtures = _training_domain_pack_fixtures()
    by_type = {item.specification.pack_type: item for item in fixtures}
    cases = (
        _scenario_domain_pack_case(by_type[PackTypeV1.SCENARIO]),
        _lesson_and_curriculum_domain_pack_case(
            by_type[PackTypeV1.LESSON],
            by_type[PackTypeV1.CURRICULUM],
        ),
        _strategy_domain_pack_case(by_type[PackTypeV1.STRATEGY]),
        _profile_domain_pack_case(by_type[PackTypeV1.MARKET_PROFILE]),
        _generic_domain_pack_lifecycle_case(fixtures),
    )
    expected_names = (
        "scenario_pack_preserves_compiled_validation_source_and_capability_identity",
        "lesson_and_curriculum_preserve_training_and_review_boundaries",
        "strategy_pack_preserves_legacy_semantic_ast_and_experiment_lineage",
        "profile_pack_preserves_profile_preregistration_and_review_status",
        "generic_domain_pack_lifecycle_is_declared_and_all_five_types_round_trip",
    )
    if len(cases) != WO39D1_AUDIT_CASE_COUNT:
        raise RuntimeError("WO39-D1 audit case inventory changed")
    if tuple(item.name for item in cases) != expected_names:
        raise RuntimeError("WO39-D1 audit case order or identity changed")
    return cases


def _training_domain_pack_fixtures() -> tuple[_DomainPackAuditFixture, ...]:
    scenario_source = (
        Path(__file__).resolve().parents[1]
        / "scenario_lang"
        / "examples"
        / "full_day.toml"
    )
    inputs = (
        build_scenario_demo_inputs(scenario_source),
        _training_policy_pack_inputs(PackTypeV1.LESSON),
        _training_policy_pack_inputs(PackTypeV1.CURRICULUM),
        _strategy_pack_inputs(),
        _profile_pack_inputs(),
    )
    return tuple(_verified_domain_fixture(*item) for item in inputs)


def _verified_domain_fixture(
    specification: PackBuildSpecificationV1,
    originals: dict[str, bytes],
) -> _DomainPackAuditFixture:
    build = build_domain_pack(specification, originals)
    verification = verify_domain_pack_archive_bytes(
        build.archive_bytes,
        expected_pack_id=build.manifest.pack_id,
    )
    return _DomainPackAuditFixture(
        specification=specification,
        originals=originals,
        build=build,
        verification=verification,
    )


def _training_policy_pack_inputs(
    pack_type: PackTypeV1,
) -> tuple[PackBuildSpecificationV1, dict[str, bytes]]:
    if pack_type is PackTypeV1.LESSON:
        prefix = "lesson"
        role_pairs = (
            ("lesson-source", PackArtifactRoleV1.LESSON_SOURCE),
            ("lesson-detector", PackArtifactRoleV1.LESSON_DETECTOR),
            ("lesson-capabilities", PackArtifactRoleV1.LESSON_CAPABILITIES),
            (
                "lesson-observable-policy",
                PackArtifactRoleV1.LESSON_OBSERVABLE_POLICY,
            ),
            ("lesson-reveal-policy", PackArtifactRoleV1.LESSON_REVEAL_POLICY),
            ("lesson-skills", PackArtifactRoleV1.LESSON_SKILLS),
            ("lesson-scoring", PackArtifactRoleV1.LESSON_SCORING),
            (
                "lesson-review-sidecar",
                PackArtifactRoleV1.LESSON_REVIEW_SIDECAR,
            ),
            ("lesson-embedded-run", PackArtifactRoleV1.EMBEDDED_RUN),
            ("lesson-embedded-audit", PackArtifactRoleV1.EMBEDDED_AUDIT),
        )
    elif pack_type is PackTypeV1.CURRICULUM:
        prefix = "curriculum"
        role_pairs = (
            ("curriculum-source", PackArtifactRoleV1.CURRICULUM_SOURCE),
            ("curriculum-detector", PackArtifactRoleV1.CURRICULUM_DETECTOR),
            (
                "curriculum-capabilities",
                PackArtifactRoleV1.CURRICULUM_CAPABILITIES,
            ),
            (
                "curriculum-observable-policy",
                PackArtifactRoleV1.CURRICULUM_OBSERVABLE_POLICY,
            ),
            (
                "curriculum-reveal-policy",
                PackArtifactRoleV1.CURRICULUM_REVEAL_POLICY,
            ),
            ("curriculum-skills", PackArtifactRoleV1.CURRICULUM_SKILLS),
            ("curriculum-scoring", PackArtifactRoleV1.CURRICULUM_SCORING),
            (
                "curriculum-review-sidecar",
                PackArtifactRoleV1.CURRICULUM_REVIEW_SIDECAR,
            ),
        )
    else:
        raise TypeError("training policy fixture requires lesson or curriculum")

    originals: dict[str, bytes] = {}
    artifacts: list[PackSourceArtifactV1] = []
    for artifact_id, role in role_pairs:
        schema_id = f"KIRBY2_WO39D1_{role.value}_V1"
        raw = canonical_json_bytes(
            {
                "fixture_id": "WO39D1_ADAPTER_AUDIT_V1",
                "role": role.value,
                "schema_id": schema_id,
                "schema_version": 1,
            }
        )
        originals[artifact_id] = raw
        artifacts.append(
            _direct_json_source_artifact(
                artifact_id=artifact_id,
                role=role,
                schema_id=schema_id,
            )
        )
    return (
        _domain_pack_specification(
            pack_type=pack_type,
            name=f"wo39d1-{prefix}-audit",
            title=f"WO39-D1 {prefix} adapter audit",
            primary_artifact_id=f"{prefix}-source",
            artifacts=tuple(sorted(artifacts, key=lambda item: item.artifact_id)),
        ),
        originals,
    )


def _strategy_pack_inputs() -> tuple[PackBuildSpecificationV1, dict[str, bytes]]:
    source = (
        "setup momentum_long\n"
        "window 5s\n\n"
        "GREEN when\n"
        "    book_imbalance > 0.20\n"
        "    buy_sell_ratio > 1.10\n"
        "    ask_depletion_rate > 20\n"
        "    spread_ticks <= 3\n\n"
        "WAIT when\n"
        "    book_imbalance > -0.20\n"
        "    spread_ticks <= 5\n\n"
        "RED otherwise\n"
    )
    source_raw = source.encode("utf-8")
    ast = parse_strategy_semantic_ast(source)
    ast_raw = canonical_strategy_ast_bytes(ast)
    semantic_identity = strategy_semantic_sha256(ast)
    lineage = {
        "experiment_id": "WO39D1_STRATEGY_PACK_AUDIT_V1",
        "schema_id": "KIRBY2_WO39D1_STRATEGY_LINEAGE_V1",
        "schema_version": 1,
        "strategy_semantic_sha256": semantic_identity,
    }
    lineage_raw = canonical_json_bytes(lineage)
    originals = {
        "strategy-legacy-source": source_raw,
        "strategy-canonical-ast": ast_raw,
        "strategy-experiment-lineage": lineage_raw,
    }
    artifacts = (
        PackSourceArtifactV1(
            artifact_id="strategy-canonical-ast",
            role=PackArtifactRoleV1.STRATEGY_CANONICAL_AST,
            source_path="generated/strategy-canonical-ast.json",
            original_schema_id="KIRBY2_WO39D1_STRATEGY_AST_V1",
            original_schema_version=1,
            original_media_type="application/json",
            storage_mode=PackArtifactStorageModeV1.DIRECT,
            logical_identity_kind="STRATEGY_SEMANTIC_AST_SHA256_V1",
            logical_identity_sha256=semantic_identity,
            direct_content_format=PackContentFormatV1.CANONICAL_JSON,
        ),
        PackSourceArtifactV1(
            artifact_id="strategy-experiment-lineage",
            role=PackArtifactRoleV1.STRATEGY_EXPERIMENT_LINEAGE,
            source_path="generated/strategy-experiment-lineage.json",
            original_schema_id="KIRBY2_WO39D1_STRATEGY_LINEAGE_V1",
            original_schema_version=1,
            original_media_type="application/json",
            storage_mode=PackArtifactStorageModeV1.DIRECT,
            logical_identity_kind="STRATEGY_LINEAGE_SHA256_V1",
            logical_identity_sha256=lineage_payload_sha256(lineage),
            direct_content_format=PackContentFormatV1.CANONICAL_JSON,
        ),
        PackSourceArtifactV1(
            artifact_id="strategy-legacy-source",
            role=PackArtifactRoleV1.STRATEGY_LEGACY_SOURCE,
            source_path="strategy/momentum-long.txt",
            original_schema_id="KIRBY2_WO39D1_LEGACY_STRATEGY_SOURCE_V1",
            original_schema_version=1,
            original_media_type="text/plain",
            storage_mode=PackArtifactStorageModeV1.EXACT_BYTES_ENVELOPE,
            logical_identity_kind="LEGACY_STRATEGY_SOURCE_SHA256_V1",
            logical_identity_sha256=legacy_strategy_source_sha256(source),
        ),
    )
    return (
        _domain_pack_specification(
            pack_type=PackTypeV1.STRATEGY,
            name="wo39d1-strategy-audit",
            title="WO39-D1 strategy adapter audit",
            primary_artifact_id="strategy-canonical-ast",
            artifacts=tuple(sorted(artifacts, key=lambda item: item.artifact_id)),
        ),
        originals,
    )


def _profile_pack_inputs() -> tuple[PackBuildSpecificationV1, dict[str, bytes]]:
    profile = MarketProfile(
        profile_id="WO39D1_MARKET_PROFILE_AUDIT_V1",
        scenario_name="WO39D1_SYNTHETIC_SCENARIO_V1",
        regime="SYNTHETIC_AUDIT",
        parameters={"arrival_rate": 0.25, "spread_ticks": 2.0},
        fixed_parameters=("arrival_rate",),
        reference_dataset_id="WO39D1_SYNTHETIC_REFERENCE_V1",
        objective_id="WO39D1_CALIBRATION_OBJECTIVE_V1",
    )
    originals = {
        "market-profile": profile.canonical_json().encode("utf-8"),
        "profile-preregistration": canonical_json_bytes(
            {
                "preregistered": True,
                "schema_id": "KIRBY2_WO39D1_PROFILE_PREREGISTRATION_V1",
                "schema_version": 1,
                "status": "LOCKED_BEFORE_REVIEW",
            }
        ),
        "profile-review-status": canonical_json_bytes(
            {
                "decision": "AUDIT_FIXTURE_ONLY",
                "review_status": "REVIEWED",
                "schema_id": "KIRBY2_WO39D1_PROFILE_REVIEW_STATUS_V1",
                "schema_version": 1,
            }
        ),
    }
    artifacts = (
        PackSourceArtifactV1(
            artifact_id="market-profile",
            role=PackArtifactRoleV1.MARKET_PROFILE,
            source_path="profiles/wo39d1-market-profile.json",
            original_schema_id="KIRBY2_WO39D1_MARKET_PROFILE_V1",
            original_schema_version=1,
            original_media_type="application/json",
            storage_mode=PackArtifactStorageModeV1.EXACT_BYTES_ENVELOPE,
            logical_identity_kind="MARKET_PROFILE_CANONICAL_SHA256_V1",
        ),
        _direct_json_source_artifact(
            artifact_id="profile-preregistration",
            role=PackArtifactRoleV1.PROFILE_PREREGISTRATION,
            schema_id="KIRBY2_WO39D1_PROFILE_PREREGISTRATION_V1",
        ),
        _direct_json_source_artifact(
            artifact_id="profile-review-status",
            role=PackArtifactRoleV1.PROFILE_REVIEW_STATUS,
            schema_id="KIRBY2_WO39D1_PROFILE_REVIEW_STATUS_V1",
        ),
    )
    return (
        _domain_pack_specification(
            pack_type=PackTypeV1.MARKET_PROFILE,
            name="wo39d1-market-profile-audit",
            title="WO39-D1 market-profile adapter audit",
            primary_artifact_id="market-profile",
            artifacts=tuple(sorted(artifacts, key=lambda item: item.artifact_id)),
        ),
        originals,
    )


def _direct_json_source_artifact(
    *,
    artifact_id: str,
    role: PackArtifactRoleV1,
    schema_id: str,
) -> PackSourceArtifactV1:
    return PackSourceArtifactV1(
        artifact_id=artifact_id,
        role=role,
        source_path=f"generated/{artifact_id}.json",
        original_schema_id=schema_id,
        original_schema_version=1,
        original_media_type="application/json",
        storage_mode=PackArtifactStorageModeV1.DIRECT,
        logical_identity_kind="CANONICAL_JSON_SHA256_V1",
        direct_content_format=PackContentFormatV1.CANONICAL_JSON,
    )


def _domain_pack_specification(
    *,
    pack_type: PackTypeV1,
    name: str,
    title: str,
    primary_artifact_id: str,
    artifacts: tuple[PackSourceArtifactV1, ...],
) -> PackBuildSpecificationV1:
    return PackBuildSpecificationV1(
        namespace="kirby2.audit.wo39d1",
        name=name,
        title=title,
        version="1.0.0",
        creator=PackCreatorV1(
            display_name="Kirby2 WO39-D1 audit",
            identity_uri="urn:kirby2:audit:wo39d1",
        ),
        pack_type=pack_type,
        primary_artifact_id=primary_artifact_id,
        dependencies=(),
        license=PackLicenseV1(
            license_id="KIRBY2_WO39D1_AUDIT_LICENSE_V1",
            license_name="Synthetic Kirby2 WO39-D1 audit data",
            license_uri="urn:kirby2:license:wo39d1-audit-v1",
            redistribution_policy=PackRedistributionPolicyV1.ALLOWED,
            content_mode=PackContentModeV1.SELF_CONTAINED,
        ),
        capability_labels=("DETERMINISTIC_SIMULATION", "LOCAL_OFFLINE"),
        artifacts=artifacts,
    )


def _scenario_domain_pack_case(
    fixture: _DomainPackAuditFixture,
) -> PackAuditCase:
    index = fixture.verification.index
    demo_command = next(
        item for item in PACK_COMMAND_MODULE.commands if item.name == "pack-build-demo"
    )
    source = (
        Path(__file__).resolve().parents[1]
        / "scenario_lang"
        / "examples"
        / "full_day.toml"
    )
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        demo_status = demo_command.handler(
            argparse.Namespace(
                pack_demo_type="scenario",
                source=source,
                output=None,
            )
        )
    demo = load_canonical_json_bytes(
        output.getvalue().strip().encode("ascii"),
        "WO39-D1 scenario demo result",
    )
    expected_roles = {
        PackArtifactRoleV1.SCENARIO_SOURCE,
        PackArtifactRoleV1.SCENARIO_COMPILED,
        PackArtifactRoleV1.SCENARIO_VALIDATION,
        PackArtifactRoleV1.SCENARIO_CAPABILITIES,
    }
    checks = {
        "scenario_roles_are_complete_and_separate": (
            expected_roles == {item.role for item in index.artifacts}
        ),
        "owning_adapter_reverified_every_original": (
            _fixture_identities_are_exact(fixture)
        ),
        "compiled_validation_source_and_capability_identities_are_distinct": (
            len({item.logical_identity_sha256 for item in index.artifacts})
            == len(index.artifacts)
        ),
        "public_demo_rebuilds_the_same_logical_and_transport_identity": (
            demo_status == 0
            and type(demo) is dict
            and demo.get("status") == "PASS"
            and demo.get("pack_id") == fixture.build.manifest.pack_id
            and demo.get("transport_sha256") == fixture.build.transport_sha256
            and demo.get("domain_identity_sha256")
            == index.domain_identity_sha256
        ),
    }
    return _case(
        "scenario_pack_preserves_compiled_validation_source_and_capability_identity",
        f"pack={fixture.build.manifest.pack_id} artifacts={len(index.artifacts)}",
        checks,
        {
            "demo_status": demo_status,
            "domain_identity_sha256": index.domain_identity_sha256,
            "pack_id": fixture.build.manifest.pack_id,
            "roles": sorted(item.role.value for item in index.artifacts),
        },
    )


def _lesson_and_curriculum_domain_pack_case(
    lesson: _DomainPackAuditFixture,
    curriculum: _DomainPackAuditFixture,
) -> PackAuditCase:
    lesson_required = {
        PackArtifactRoleV1.LESSON_SOURCE,
        PackArtifactRoleV1.LESSON_DETECTOR,
        PackArtifactRoleV1.LESSON_CAPABILITIES,
        PackArtifactRoleV1.LESSON_OBSERVABLE_POLICY,
        PackArtifactRoleV1.LESSON_REVEAL_POLICY,
        PackArtifactRoleV1.LESSON_SKILLS,
        PackArtifactRoleV1.LESSON_SCORING,
        PackArtifactRoleV1.LESSON_REVIEW_SIDECAR,
    }
    curriculum_required = {
        PackArtifactRoleV1.CURRICULUM_SOURCE,
        PackArtifactRoleV1.CURRICULUM_DETECTOR,
        PackArtifactRoleV1.CURRICULUM_CAPABILITIES,
        PackArtifactRoleV1.CURRICULUM_OBSERVABLE_POLICY,
        PackArtifactRoleV1.CURRICULUM_REVEAL_POLICY,
        PackArtifactRoleV1.CURRICULUM_SKILLS,
        PackArtifactRoleV1.CURRICULUM_SCORING,
        PackArtifactRoleV1.CURRICULUM_REVIEW_SIDECAR,
    }
    lesson_refusal = _reveal_policy_refusal(
        lesson,
        "lesson-observable-policy",
        "lesson-reveal-policy",
    )
    curriculum_refusal = _reveal_policy_refusal(
        curriculum,
        "curriculum-observable-policy",
        "curriculum-reveal-policy",
    )
    embedded = tuple(
        item
        for item in lesson.verification.index.artifacts
        if item.role
        in {PackArtifactRoleV1.EMBEDDED_RUN, PackArtifactRoleV1.EMBEDDED_AUDIT}
    )
    checks = {
        "lesson_retains_all_policy_and_review_roles": (
            lesson_required
            <= {item.role for item in lesson.verification.index.artifacts}
        ),
        "curriculum_retains_all_policy_and_review_roles": (
            curriculum_required
            <= {item.role for item in curriculum.verification.index.artifacts}
        ),
        "both_adapters_reverify_exact_original_and_logical_identity": (
            _fixture_identities_are_exact(lesson)
            and _fixture_identities_are_exact(curriculum)
        ),
        "embedded_run_and_audit_keep_their_original_identities": (
            len(embedded) == 2
            and all(
                item.logical_identity_sha256
                == hashlib.sha256(lesson.originals[item.artifact_id]).hexdigest()
                for item in embedded
            )
        ),
        "observable_and_reveal_identity_cannot_collapse": (
            lesson_refusal is DomainPackRefusalCodeV1.REVEAL_POLICY_VIOLATION
            and curriculum_refusal
            is DomainPackRefusalCodeV1.REVEAL_POLICY_VIOLATION
        ),
    }
    return _case(
        "lesson_and_curriculum_preserve_training_and_review_boundaries",
        (
            f"lesson={lesson.build.manifest.pack_id} "
            f"curriculum={curriculum.build.manifest.pack_id}"
        ),
        checks,
        {
            "curriculum_reveal_refusal": (
                None if curriculum_refusal is None else curriculum_refusal.value
            ),
            "embedded_roles": sorted(item.role.value for item in embedded),
            "lesson_reveal_refusal": (
                None if lesson_refusal is None else lesson_refusal.value
            ),
        },
    )


def _strategy_domain_pack_case(
    fixture: _DomainPackAuditFixture,
) -> PackAuditCase:
    index = fixture.verification.index
    source_raw = fixture.originals["strategy-legacy-source"]
    source = source_raw.decode("utf-8")
    ast = parse_strategy_semantic_ast(source)
    semantic_identity = strategy_semantic_sha256(ast)
    lineage_payload = load_canonical_json_bytes(
        fixture.originals["strategy-experiment-lineage"],
        "WO39-D1 strategy lineage",
    )
    source_row = index.artifact(PackArtifactRoleV1.STRATEGY_LEGACY_SOURCE)
    ast_row = index.artifact(PackArtifactRoleV1.STRATEGY_CANONICAL_AST)
    lineage_row = index.artifact(PackArtifactRoleV1.STRATEGY_EXPERIMENT_LINEAGE)
    mismatch = _strategy_lineage_refusal(fixture)
    checks = {
        "legacy_source_exact_bytes_and_identity_are_preserved": (
            source_row.original_sha256 == hashlib.sha256(source_raw).hexdigest()
            and source_row.logical_identity_sha256
            == legacy_strategy_source_sha256(source)
        ),
        "canonical_ast_is_recomputed_by_the_owning_parser": (
            fixture.originals["strategy-canonical-ast"]
            == canonical_strategy_ast_bytes(ast)
            and ast_row.logical_identity_sha256 == semantic_identity
        ),
        "lineage_binds_the_semantic_ast_and_owning_lineage_digest": (
            type(lineage_payload) is dict
            and lineage_payload.get("strategy_semantic_sha256")
            == semantic_identity
            and lineage_row.logical_identity_sha256
            == lineage_payload_sha256(lineage_payload)
        ),
        "legacy_and_semantic_identities_remain_dual": (
            source_row.logical_identity_sha256 != ast_row.logical_identity_sha256
        ),
        "lineage_mismatch_is_explicitly_refused": (
            mismatch is DomainPackRefusalCodeV1.STRATEGY_IDENTITY_MISMATCH
        ),
    }
    return _case(
        "strategy_pack_preserves_legacy_semantic_ast_and_experiment_lineage",
        f"pack={fixture.build.manifest.pack_id} semantic={semantic_identity}",
        checks,
        {
            "legacy_source_sha256": source_row.logical_identity_sha256,
            "lineage_refusal": None if mismatch is None else mismatch.value,
            "semantic_ast_sha256": semantic_identity,
        },
    )


def _profile_domain_pack_case(
    fixture: _DomainPackAuditFixture,
) -> PackAuditCase:
    index = fixture.verification.index
    profile_raw = fixture.originals["market-profile"]
    profile_payload = json.loads(profile_raw.decode("utf-8"))
    profile = MarketProfile.from_dict(profile_payload)
    preregistration = load_canonical_json_bytes(
        fixture.originals["profile-preregistration"],
        "WO39-D1 profile preregistration",
    )
    review = load_canonical_json_bytes(
        fixture.originals["profile-review-status"],
        "WO39-D1 profile review status",
    )
    invalid_status = _profile_status_refusal(fixture)
    profile_row = index.artifact(PackArtifactRoleV1.MARKET_PROFILE)
    checks = {
        "profile_round_trips_through_the_owning_calibration_schema": (
            profile.canonical_json().encode("utf-8") == profile_raw
            and profile_row.logical_identity_sha256
            == hashlib.sha256(profile_raw).hexdigest()
        ),
        "profile_native_float_bytes_use_an_exact_data_envelope": (
            profile_row.storage_mode
            is PackArtifactStorageModeV1.EXACT_BYTES_ENVELOPE
        ),
        "preregistration_and_review_status_are_explicit_and_separate": (
            type(preregistration) is dict
            and preregistration.get("preregistered") is True
            and type(review) is dict
            and review.get("review_status") == "REVIEWED"
            and hashlib.sha256(
                fixture.originals["profile-preregistration"]
            ).hexdigest()
            != hashlib.sha256(
                fixture.originals["profile-review-status"]
            ).hexdigest()
        ),
        "owning_adapter_reverified_every_original": (
            _fixture_identities_are_exact(fixture)
        ),
        "missing_review_status_is_explicitly_refused": (
            invalid_status is DomainPackRefusalCodeV1.PROFILE_STATUS_INVALID
        ),
    }
    return _case(
        "profile_pack_preserves_profile_preregistration_and_review_status",
        f"pack={fixture.build.manifest.pack_id} profile={profile.profile_id}",
        checks,
        {
            "profile_id": profile.profile_id,
            "status_refusal": (
                None if invalid_status is None else invalid_status.value
            ),
            "storage_mode": profile_row.storage_mode.value,
        },
    )


def _generic_domain_pack_lifecycle_case(
    fixtures: tuple[_DomainPackAuditFixture, ...],
) -> PackAuditCase:
    rebuilt = tuple(
        build_domain_pack(item.specification, item.originals) for item in fixtures
    )
    pack_command = next(
        item for item in PACK_COMMAND_MODULE.commands if item.name == "pack"
    )
    if pack_command.configure is None:
        raise RuntimeError("WO39-D1 pack command lost its parser declaration")
    parser = argparse.ArgumentParser(add_help=False)
    pack_command.configure(parser)
    action = next(
        item
        for item in parser._actions
        if isinstance(item, argparse._SubParsersAction)
    )
    lifecycle_actions = tuple(action.choices)
    required_actions = {"build", "inspect", "verify", "install", "list", "remove"}
    demo_command = next(
        item for item in PACK_COMMAND_MODULE.commands if item.name == "pack-build-demo"
    )
    unsupported = _capture_domain_refusal_code(
        lambda: demo_command.handler(
            argparse.Namespace(
                pack_demo_type="lesson",
                source=Path("unused-for-unsupported-type"),
                output=None,
            )
        )
    )
    scenario = fixtures[0]
    dependency = PackDependencyV1(
        creator_id=_digest("WO39-D1 missing creator"),
        namespace="kirby2.audit.wo39d1",
        name="missing-provider",
        version_constraint="1.0.0",
        expected_pack_id=_digest("WO39-D1 missing provider"),
    )
    dependent_specification = replace(
        scenario.specification,
        name="wo39d1-missing-dependency",
        dependencies=(dependency,),
    )
    dependent_build = build_domain_pack(
        dependent_specification,
        scenario.originals,
    )
    dependent_verification = verify_domain_pack_archive_bytes(
        dependent_build.archive_bytes,
        expected_pack_id=dependent_build.manifest.pack_id,
    )
    missing_dependency = _capture_exception_text(
        lambda: resolve_pack_dependencies(
            dependent_build.manifest,
            PackRegistryV1.empty(),
            runtime_environment_for_verified_pack_v1(dependent_verification),
        )
    )
    required_pack_types = {
        PackTypeV1.SCENARIO,
        PackTypeV1.LESSON,
        PackTypeV1.CURRICULUM,
        PackTypeV1.STRATEGY,
        PackTypeV1.MARKET_PROFILE,
    }
    checks = {
        "all_five_domain_adapters_are_declared": (
            required_pack_types <= set(supported_domain_pack_types_v1())
            and {item.specification.pack_type for item in fixtures}
            == required_pack_types
        ),
        "all_five_rebuilds_are_byte_and_identity_identical": all(
            first.build.archive_bytes == second.archive_bytes
            and first.build.manifest.pack_id == second.manifest.pack_id
            and first.build.transport_sha256 == second.transport_sha256
            and first.build.index == second.index
            for first, second in zip(fixtures, rebuilt, strict=True)
        ),
        "all_five_round_trip_original_identity_and_provenance": all(
            _fixture_identities_are_exact(item)
            and _manifest_provenance_covers_domain_index(item)
            for item in fixtures
        ),
        "generic_lifecycle_and_demo_commands_are_declared": (
            tuple(item.name for item in PACK_COMMAND_MODULE.commands)
            == ("pack", "pack-build-demo")
            and required_actions <= set(lifecycle_actions)
        ),
        "unsupported_type_is_explicitly_refused": (
            unsupported is DomainPackRefusalCodeV1.UNSUPPORTED_PACK_TYPE
        ),
        "missing_dependency_is_local_only_and_explicit": (
            missing_dependency is not None
            and "MISSING_PACK_DEPENDENCY" in missing_dependency
        ),
    }
    return _case(
        "generic_domain_pack_lifecycle_is_declared_and_all_five_types_round_trip",
        f"types={len(fixtures)} actions={len(lifecycle_actions)}",
        checks,
        {
            "lifecycle_actions": list(lifecycle_actions),
            "missing_dependency": missing_dependency,
            "pack_ids": {
                item.specification.pack_type.value: item.build.manifest.pack_id
                for item in fixtures
            },
            "unsupported_type_refusal": (
                None if unsupported is None else unsupported.value
            ),
        },
    )


def _fixture_identities_are_exact(fixture: _DomainPackAuditFixture) -> bool:
    return (
        fixture.verification.index == fixture.build.index
        and fixture.verification.original_artifact_count
        == len(fixture.originals)
        and all(
            item.original_byte_count == len(fixture.originals[item.artifact_id])
            and item.original_sha256
            == hashlib.sha256(fixture.originals[item.artifact_id]).hexdigest()
            for item in fixture.verification.index.artifacts
        )
    )


def _manifest_provenance_covers_domain_index(
    fixture: _DomainPackAuditFixture,
) -> bool:
    provenance = {
        (item.source_kind, item.source_id, item.source_sha256)
        for item in fixture.build.manifest.provenance
    }
    return all(
        (item.role.value, item.artifact_id, item.original_sha256) in provenance
        for item in fixture.build.index.artifacts
    )


def _reveal_policy_refusal(
    fixture: _DomainPackAuditFixture,
    observable_artifact_id: str,
    reveal_artifact_id: str,
) -> DomainPackRefusalCodeV1 | None:
    originals = dict(fixture.originals)
    originals[reveal_artifact_id] = originals[observable_artifact_id]
    return _capture_domain_refusal_code(
        lambda: build_domain_pack(fixture.specification, originals)
    )


def _strategy_lineage_refusal(
    fixture: _DomainPackAuditFixture,
) -> DomainPackRefusalCodeV1 | None:
    invalid_lineage = {
        "experiment_id": "WO39D1_STRATEGY_PACK_MISMATCH_V1",
        "schema_id": "KIRBY2_WO39D1_STRATEGY_LINEAGE_V1",
        "schema_version": 1,
        "strategy_semantic_sha256": _digest("WO39-D1 wrong semantic AST"),
    }
    invalid_raw = canonical_json_bytes(invalid_lineage)
    invalid_artifacts = tuple(
        replace(
            item,
            logical_identity_sha256=lineage_payload_sha256(invalid_lineage),
        )
        if item.artifact_id == "strategy-experiment-lineage"
        else item
        for item in fixture.specification.artifacts
    )
    originals = dict(fixture.originals)
    originals["strategy-experiment-lineage"] = invalid_raw
    return _capture_domain_refusal_code(
        lambda: build_domain_pack(
            replace(fixture.specification, artifacts=invalid_artifacts),
            originals,
        )
    )


def _profile_status_refusal(
    fixture: _DomainPackAuditFixture,
) -> DomainPackRefusalCodeV1 | None:
    originals = dict(fixture.originals)
    originals["profile-review-status"] = canonical_json_bytes(
        {
            "note": "deliberately missing an explicit review status",
            "schema_id": "KIRBY2_WO39D1_PROFILE_REVIEW_STATUS_V1",
            "schema_version": 1,
        }
    )
    return _capture_domain_refusal_code(
        lambda: build_domain_pack(fixture.specification, originals)
    )


def _capture_domain_refusal_code(
    operation,
) -> DomainPackRefusalCodeV1 | None:
    try:
        operation()
    except DomainPackRefused as error:
        return error.code
    return None


def _capture_exception_text(operation) -> str | None:
    try:
        operation()
    except (KeyError, LookupError, RuntimeError, TypeError, ValueError) as error:
        return str(error)
    return None


def _normalized_archive(
    manifest: PackManifestV1,
    payloads: dict[str, bytes],
    *,
    reverse_input: bool,
) -> bytes:
    pairs = [
        (K2PACK_MANIFEST_PATH, canonical_manifest_bytes(manifest)),
        *payloads.items(),
    ]
    if reverse_input:
        pairs.reverse()
    values = dict(pairs)
    output = io.BytesIO()
    with zipfile.ZipFile(
        output,
        mode="w",
        compression=K2PACK_ZIP_COMPRESSION,
        compresslevel=K2PACK_ZIP_COMPRESSLEVEL,
        allowZip64=False,
        strict_timestamps=True,
    ) as archive:
        for path in normalized_archive_paths(tuple(values)):
            archive.writestr(
                normalized_zip_info(path),
                values[path],
                compress_type=K2PACK_ZIP_COMPRESSION,
                compresslevel=K2PACK_ZIP_COMPRESSLEVEL,
            )
    return output.getvalue()


def _incidental_archive(
    manifest: PackManifestV1,
    payloads: dict[str, bytes],
) -> bytes:
    values = {
        K2PACK_MANIFEST_PATH: canonical_manifest_bytes(manifest),
        **payloads,
    }
    output = io.BytesIO()
    with zipfile.ZipFile(output, mode="w", compression=zipfile.ZIP_STORED) as archive:
        archive.comment = b"incidental transport metadata"
        for path in reversed(normalized_archive_paths(tuple(values))):
            info = zipfile.ZipInfo(path, date_time=(2001, 2, 3, 4, 5, 6))
            info.compress_type = zipfile.ZIP_STORED
            info.create_system = 0
            info.comment = b"not logical identity"
            archive.writestr(info, values[path])
    return output.getvalue()


def _archive_values(raw: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(raw), mode="r") as archive:
        return {item.filename: archive.read(item.filename) for item in archive.infolist()}


def _restore_with_unknown_field(manifest: PackManifestV1) -> PackManifestV1:
    payload = manifest.as_dict()
    payload["python_module"] = "forbidden.module"
    return PackManifestV1.from_dict(payload)


def _raises(operation) -> bool:
    try:
        operation()
    except (
        AttributeError,
        KeyError,
        OSError,
        PermissionError,
        RuntimeError,
        TypeError,
        ValueError,
        zipfile.BadZipFile,
        zipfile.LargeZipFile,
    ):
        return True
    return False


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode("ascii")).hexdigest()


def _case(
    name: str,
    detail: str,
    checks: dict[str, bool],
    evidence: dict[str, object],
) -> PackAuditCase:
    return PackAuditCase(
        name=name,
        detail=detail,
        evidence=evidence,
        failures=tuple(label for label, passed in checks.items() if not passed),
    )


__all__ = [
    "WO38C_PACK_AUDIT_CASE_COUNT",
    "WO39A_AUDIT_CASE_COUNT",
    "WO39B_AUDIT_CASE_COUNT",
    "WO39C_AUDIT_CASE_COUNT",
    "WO39D1_AUDIT_CASE_COUNT",
    "PackAuditCase",
    "audit_clean_root_pack_transfer",
    "audit_atomic_pack_installation",
    "audit_canonical_pack_identity",
    "audit_hostile_archive_validation_and_staging",
    "audit_training_domain_packs",
    "build_clean_root_transfer_audit_fixture",
]
