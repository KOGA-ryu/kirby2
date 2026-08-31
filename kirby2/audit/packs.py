"""Executable audits for the canonical Kirby2 pack substrate."""

from __future__ import annotations

import hashlib
import io
import stat
import zipfile
from dataclasses import dataclass, replace

from kirby2.packs.formats import (
    K2PACK_MANIFEST_PATH,
    K2PACK_ZIP_COMPRESSION,
    K2PACK_ZIP_COMPRESSLEVEL,
    K2PACK_ZIP_TIMESTAMP,
    canonical_json_bytes,
    canonical_manifest_bytes,
    canonical_toml_bytes,
    inspect_payload_format_claim,
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


WO39A_AUDIT_CASE_COUNT = 5

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


def _normalized_archive(
    manifest: PackManifestV1,
    payloads: dict[str, bytes],
    *,
    reverse_input: bool,
) -> bytes:
    pairs = [(K2PACK_MANIFEST_PATH, canonical_manifest_bytes(manifest)), *payloads.items()]
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
    "WO39A_AUDIT_CASE_COUNT",
    "PackAuditCase",
    "audit_canonical_pack_identity",
]
