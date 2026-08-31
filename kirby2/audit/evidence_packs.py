"""Executable WO39-D2 audits for evidence-bearing portable packs."""

from __future__ import annotations

import argparse
import hashlib
from dataclasses import dataclass, replace
from pathlib import Path
from tempfile import TemporaryDirectory

from kirby2.historical.models import HistoricalDataMode
from kirby2.packs.analysis_pack import (
    ANALYSIS_PROVENANCE_SCHEMA_ID_V1,
    AnalysisProvenanceRecordV1,
)
from kirby2.packs.builders import (
    DomainPackBuildV1,
    DomainPackVerificationV1,
    build_domain_pack,
    build_registered_run_pack,
    supported_domain_pack_types_v1,
    verify_domain_pack_archive_bytes,
)
from kirby2.packs.commands import PACK_COMMAND_MODULE
from kirby2.packs.formats import canonical_json_bytes, load_canonical_json_bytes
from kirby2.packs.historical_pack import (
    HISTORICAL_CAPABILITY_SCHEMA_ID_V1,
    HISTORICAL_PROVENANCE_SCHEMA_ID_V1,
    HISTORICAL_SOURCE_LICENSE_SCHEMA_ID_V1,
    HISTORICAL_SOURCE_REFERENCE_SCHEMA_ID_V1,
    HistoricalCapabilityRecordV1,
    HistoricalProvenanceRecordV1,
    HistoricalSourceLicenseRecordV1,
    HistoricalSourceProvenanceV1,
    HistoricalSourceReferenceV1,
)
from kirby2.packs.models import (
    PackContentFormatV1,
    PackContentModeV1,
    PackCreatorV1,
    PackLicenseV1,
    PackRedistributionPolicyV1,
    PackTypeV1,
)
from kirby2.packs.research_pack import (
    RESEARCH_REDACTED_EVIDENCE_SCHEMA_ID_V1,
)
from kirby2.packs.types import (
    DomainPackRefusalCodeV1,
    DomainPackRefused,
    PackArtifactRoleV1,
    PackArtifactStorageModeV1,
    PackBuildSpecificationV1,
    PackSourceArtifactV1,
)


WO39D2_AUDIT_CASE_COUNT = 5


@dataclass(frozen=True, slots=True)
class EvidencePackAuditCase:
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
class _EvidencePackFixture:
    specification: PackBuildSpecificationV1
    originals: dict[str, bytes]
    build: DomainPackBuildV1
    verification: DomainPackVerificationV1


@dataclass(frozen=True, slots=True)
class _ReplayExportFixture:
    build: DomainPackBuildV1
    verification: DomainPackVerificationV1
    repeated_build: DomainPackBuildV1
    run_id: str
    registered_artifact_sha256s: tuple[str, ...]
    ambient_sha256: str
    tamper_refusal: DomainPackRefusalCodeV1 | None


def audit_evidence_domain_packs() -> tuple[EvidencePackAuditCase, ...]:
    """Exercise all WO39-D2 pack types and their explicit refusal boundaries."""

    historical_self = _verified_fixture(
        *_historical_pack_inputs(PackContentModeV1.SELF_CONTAINED)
    )
    historical_reference = _verified_fixture(
        *_historical_pack_inputs(PackContentModeV1.REFERENCE_ONLY)
    )
    replay = _registered_replay_export_fixture()
    analysis = _verified_fixture(*_analysis_pack_inputs())
    research = _verified_fixture(*_research_pack_inputs())
    cases = (
        _historical_pack_case(historical_self, historical_reference),
        _replay_export_case(replay),
        _analysis_pack_case(analysis),
        _research_pack_case(research),
        _all_evidence_pack_types_case(
            historical_self,
            replay,
            analysis,
            research,
        ),
    )
    expected_names = (
        "historical_packs_bind_capability_license_and_content_mode",
        "registered_run_export_preserves_replay_identity_without_ambient_copy",
        "analysis_pack_contains_data_and_annotations_without_renderer_code",
        "research_pack_retains_governed_consent_and_field_redaction",
        "all_four_evidence_pack_types_round_trip_identity_and_provenance",
    )
    if len(cases) != WO39D2_AUDIT_CASE_COUNT:
        raise RuntimeError("WO39-D2 audit case inventory changed")
    if tuple(item.name for item in cases) != expected_names:
        raise RuntimeError("WO39-D2 audit case order or identity changed")
    return cases


def _verified_fixture(
    specification: PackBuildSpecificationV1,
    originals: dict[str, bytes],
) -> _EvidencePackFixture:
    build = build_domain_pack(specification, originals)
    verification = verify_domain_pack_archive_bytes(
        build.archive_bytes,
        expected_pack_id=build.manifest.pack_id,
    )
    return _EvidencePackFixture(
        specification=specification,
        originals=originals,
        build=build,
        verification=verification,
    )


def _historical_pack_inputs(
    content_mode: PackContentModeV1,
) -> tuple[PackBuildSpecificationV1, dict[str, bytes]]:
    source_raw = b"timestamp_us,price_ticks,quantity\n0,100,5\n"
    source_sha256 = hashlib.sha256(source_raw).hexdigest()
    license_value = _evidence_license(content_mode=content_mode)
    provenance = HistoricalProvenanceRecordV1(
        dataset_id="WO39D2_HISTORICAL_DATASET_V1",
        sources=(
            HistoricalSourceProvenanceV1(
                source_id="historical-source-0001",
                source_uri="https://example.invalid/wo39d2/source-0001.csv",
                source_sha256=source_sha256,
                media_type="text/csv",
                license_id=license_value.license_id,
            ),
        ),
    )
    capability = HistoricalCapabilityRecordV1(
        dataset_id=provenance.dataset_id,
        historical_mode=HistoricalDataMode.RECONSTRUCTION,
        capability_labels=("OBSERVED_TRADES", "RECONSTRUCTED_BOOK"),
        content_mode=content_mode,
        source_payload_included=(
            content_mode is PackContentModeV1.SELF_CONTAINED
        ),
        source_count=1,
    )
    license_record = HistoricalSourceLicenseRecordV1(
        dataset_id=provenance.dataset_id,
        license=license_value,
    )
    derived = canonical_json_bytes(
        {
            "dataset_id": provenance.dataset_id,
            "evidence_class": "RECONSTRUCTED",
            "schema_id": "KIRBY2_WO39D2_HISTORICAL_DERIVED_V1",
            "schema_version": 1,
        }
    )
    originals = {
        "historical-capabilities": capability.canonical_bytes(),
        "historical-derived-evidence": derived,
        "historical-provenance": provenance.canonical_bytes(),
        "historical-source-license": license_record.canonical_bytes(),
    }
    declarations: list[PackSourceArtifactV1] = [
        _direct_json_artifact(
            artifact_id="historical-capabilities",
            role=PackArtifactRoleV1.HISTORICAL_CAPABILITIES,
            schema_id=HISTORICAL_CAPABILITY_SCHEMA_ID_V1,
        ),
        _direct_json_artifact(
            artifact_id="historical-derived-evidence",
            role=PackArtifactRoleV1.HISTORICAL_DERIVED_EVIDENCE,
            schema_id="KIRBY2_WO39D2_HISTORICAL_DERIVED_V1",
        ),
        _direct_json_artifact(
            artifact_id="historical-provenance",
            role=PackArtifactRoleV1.HISTORICAL_PROVENANCE,
            schema_id=HISTORICAL_PROVENANCE_SCHEMA_ID_V1,
        ),
        _direct_json_artifact(
            artifact_id="historical-source-license",
            role=PackArtifactRoleV1.HISTORICAL_SOURCE_LICENSE,
            schema_id=HISTORICAL_SOURCE_LICENSE_SCHEMA_ID_V1,
        ),
    ]
    if content_mode is PackContentModeV1.SELF_CONTAINED:
        originals["historical-source-0001"] = source_raw
        declarations.append(
            PackSourceArtifactV1(
                artifact_id="historical-source-0001",
                role=PackArtifactRoleV1.HISTORICAL_SOURCE_CONTENT,
                source_path="source/historical-source-0001.csv",
                original_schema_id="KIRBY2_WO39D2_HISTORICAL_SOURCE_V1",
                original_schema_version=1,
                original_media_type="text/csv",
                storage_mode=PackArtifactStorageModeV1.EXACT_BYTES_ENVELOPE,
                logical_identity_kind="HISTORICAL_SOURCE_SHA256_V1",
            )
        )
    else:
        reference = HistoricalSourceReferenceV1(
            source_id="historical-source-0001",
            source_uri="https://example.invalid/wo39d2/source-0001.csv",
            source_sha256=source_sha256,
            byte_count=len(source_raw),
            media_type="text/csv",
        )
        originals["historical-source-0001"] = reference.canonical_bytes()
        declarations.append(
            _direct_json_artifact(
                artifact_id="historical-source-0001",
                role=PackArtifactRoleV1.HISTORICAL_SOURCE_REFERENCE,
                schema_id=HISTORICAL_SOURCE_REFERENCE_SCHEMA_ID_V1,
                source_path="references/historical-source-0001.json",
            )
        )
    return (
        _evidence_pack_specification(
            pack_type=PackTypeV1.HISTORICAL,
            name=(
                "wo39d2-historical-self-contained"
                if content_mode is PackContentModeV1.SELF_CONTAINED
                else "wo39d2-historical-reference-only"
            ),
            title=f"WO39-D2 historical {content_mode.value} audit",
            primary_artifact_id="historical-provenance",
            artifacts=tuple(
                sorted(declarations, key=lambda item: item.artifact_id)
            ),
            license_value=license_value,
            capability_labels=("OBSERVED_TRADES", "RECONSTRUCTED_BOOK"),
        ),
        originals,
    )


def _historical_pack_case(
    self_contained: _EvidencePackFixture,
    reference_only: _EvidencePackFixture,
) -> EvidencePackAuditCase:
    prohibited_license = replace(
        self_contained.specification.license,
        redistribution_policy=PackRedistributionPolicyV1.PROHIBITED,
    )
    license_refusal = _capture_refusal(
        lambda: build_domain_pack(
            replace(
                self_contained.specification,
                license=prohibited_license,
            ),
            self_contained.originals,
        )
    )
    capability = HistoricalCapabilityRecordV1.from_canonical_bytes(
        self_contained.originals["historical-capabilities"]
    )
    capability_originals = dict(self_contained.originals)
    capability_originals["historical-capabilities"] = replace(
        capability,
        source_count=2,
    ).canonical_bytes()
    capability_refusal = _capture_refusal(
        lambda: build_domain_pack(
            self_contained.specification,
            capability_originals,
        )
    )
    reference_artifacts = tuple(
        replace(item, role=PackArtifactRoleV1.HISTORICAL_SOURCE_CONTENT)
        if item.artifact_id == "historical-source-0001"
        else item
        for item in reference_only.specification.artifacts
    )
    reference_refusal = _capture_refusal(
        lambda: build_domain_pack(
            replace(reference_only.specification, artifacts=reference_artifacts),
            reference_only.originals,
        )
    )
    self_roles = {item.role for item in self_contained.build.index.artifacts}
    reference_roles = {item.role for item in reference_only.build.index.artifacts}
    checks = {
        "self_contained_pack_carries_exact_source_bytes": (
            PackArtifactRoleV1.HISTORICAL_SOURCE_CONTENT in self_roles
            and PackArtifactRoleV1.HISTORICAL_SOURCE_REFERENCE not in self_roles
            and _fixture_identities_are_exact(self_contained)
        ),
        "reference_only_pack_carries_reference_and_no_source_payload": (
            PackArtifactRoleV1.HISTORICAL_SOURCE_REFERENCE in reference_roles
            and PackArtifactRoleV1.HISTORICAL_SOURCE_CONTENT not in reference_roles
            and _fixture_identities_are_exact(reference_only)
        ),
        "prohibited_self_contained_license_is_refused": (
            license_refusal is DomainPackRefusalCodeV1.LICENSE_CONTENT_REFUSED
        ),
        "capability_source_count_mismatch_is_refused": (
            capability_refusal
            is DomainPackRefusalCodeV1.ARTIFACT_INVENTORY_INVALID
        ),
        "reference_only_source_smuggling_is_refused": (
            reference_refusal
            is DomainPackRefusalCodeV1.HISTORICAL_REFERENCE_VIOLATION
        ),
    }
    return _case(
        "historical_packs_bind_capability_license_and_content_mode",
        (
            f"self={self_contained.build.manifest.pack_id} "
            f"reference={reference_only.build.manifest.pack_id}"
        ),
        checks,
        {
            "capability_refusal": _refusal_value(capability_refusal),
            "license_refusal": _refusal_value(license_refusal),
            "reference_refusal": _refusal_value(reference_refusal),
            "self_contained_pack_id": self_contained.build.manifest.pack_id,
            "reference_only_pack_id": reference_only.build.manifest.pack_id,
        },
    )


def _registered_replay_export_fixture() -> _ReplayExportFixture:
    from kirby2.research.store import RunStore
    from kirby2.scenarios import get_scenario_definition
    from kirby2.session.layouts import HotkeyLayout
    from kirby2.session.live import LiveMarketSession
    from kirby2.session.objectives import ObjectiveType, SessionObjective
    from kirby2.session.replay import SessionRecording

    with TemporaryDirectory(prefix="kirby2-wo39d2-run-export-") as raw_root:
        root = Path(raw_root).resolve()
        layout = HotkeyLayout.default()
        session = LiveMarketSession(
            get_scenario_definition("balanced"),
            seed=39_002,
            duration_seconds=1,
            initial_quantity=100,
            objective=SessionObjective(
                ObjectiveType.ACQUIRE,
                target_quantity=100,
                time_limit_us=1_000_000,
                preferred_slippage_ticks=2,
            ),
        )
        session.start()
        session.advance_by(500_000)
        session.handle_input("d", layout.bindings)
        session.advance_by(500_000)
        recording = SessionRecording.capture(session, layout, auto_start=True)
        store = RunStore(root)
        manifest = store.record_session(recording, session)
        run_directory = store.run_directory(manifest.run_id)
        ambient_raw = b"unregistered ambient data must never enter pack export\n"
        (run_directory / "ambient-unregistered.txt").write_bytes(ambient_raw)
        first = build_registered_run_pack(root, manifest.run_id)
        repeated = build_registered_run_pack(root, manifest.run_id)
        verification = verify_domain_pack_archive_bytes(
            first.archive_bytes,
            expected_pack_id=first.manifest.pack_id,
        )
        registered = tuple(sorted(item.sha256 for item in manifest.artifacts))
        tamper_target = run_directory / manifest.artifacts[0].relative_path
        tamper_target.write_bytes(tamper_target.read_bytes() + b"\n")
        tamper_refusal = _capture_refusal(
            lambda: build_registered_run_pack(root, manifest.run_id)
        )
        return _ReplayExportFixture(
            build=first,
            verification=verification,
            repeated_build=repeated,
            run_id=manifest.run_id,
            registered_artifact_sha256s=registered,
            ambient_sha256=hashlib.sha256(ambient_raw).hexdigest(),
            tamper_refusal=tamper_refusal,
        )


def _replay_export_case(
    fixture: _ReplayExportFixture,
) -> EvidencePackAuditCase:
    index_digests = {
        item.original_sha256 for item in fixture.verification.index.artifacts
    }
    run_manifest = fixture.verification.index.artifact(
        PackArtifactRoleV1.REPLAY_RUN_MANIFEST
    )
    pack_command = next(
        item for item in PACK_COMMAND_MODULE.commands if item.name == "pack"
    )
    if pack_command.configure is None:
        raise RuntimeError("WO39-D2 pack command lost its parser declaration")
    parser = argparse.ArgumentParser(add_help=False)
    pack_command.configure(parser)
    action = next(
        item
        for item in parser._actions
        if isinstance(item, argparse._SubParsersAction)
    )
    checks = {
        "export_run_action_is_declared_by_the_generic_pack_command": (
            "export-run" in action.choices
        ),
        "registered_run_identity_remains_authoritative": (
            run_manifest.logical_identity_sha256
            == hashlib.sha256(fixture.run_id.encode("ascii")).hexdigest()
        ),
        "every_registered_artifact_digest_is_preserved": (
            set(fixture.registered_artifact_sha256s) <= index_digests
        ),
        "ambient_run_directory_content_is_not_copied": (
            fixture.ambient_sha256 not in index_digests
            and all(
                item.source_sha256 != fixture.ambient_sha256
                for item in fixture.build.manifest.provenance
            )
        ),
        "repeated_export_is_byte_and_identity_identical": (
            fixture.build.archive_bytes == fixture.repeated_build.archive_bytes
            and fixture.build.manifest.pack_id
            == fixture.repeated_build.manifest.pack_id
            and fixture.build.transport_sha256
            == fixture.repeated_build.transport_sha256
        ),
        "tampered_registered_artifact_is_refused_before_export": (
            fixture.tamper_refusal
            is DomainPackRefusalCodeV1.RUN_VERIFICATION_FAILED
        ),
    }
    return _case(
        "registered_run_export_preserves_replay_identity_without_ambient_copy",
        f"run={fixture.run_id} pack={fixture.build.manifest.pack_id}",
        checks,
        {
            "artifact_count": len(fixture.verification.index.artifacts),
            "pack_id": fixture.build.manifest.pack_id,
            "run_id": fixture.run_id,
            "tamper_refusal": _refusal_value(fixture.tamper_refusal),
        },
    )


def _analysis_pack_inputs() -> tuple[PackBuildSpecificationV1, dict[str, bytes]]:
    report = canonical_json_bytes(
        {
            "metrics": {"event_count": 12, "invariant_failures": 0},
            "report_id": "WO39D2_ANALYSIS_REPORT_V1",
            "schema_id": "KIRBY2_WO39D2_ANALYSIS_REPORT_V1",
            "schema_version": 1,
        }
    )
    annotations = canonical_json_bytes(
        {
            "annotations": [
                {
                    "event_sequence": 7,
                    "label": "synthetic audit marker",
                }
            ],
            "schema_id": "KIRBY2_WO39D2_ANALYSIS_ANNOTATIONS_V1",
            "schema_version": 1,
        }
    )
    provenance = AnalysisProvenanceRecordV1(
        source_run_id=None,
        source_run_manifest_sha256=None,
        source_artifact_sha256s=tuple(
            sorted(
                (
                    hashlib.sha256(report).hexdigest(),
                    hashlib.sha256(annotations).hexdigest(),
                )
            )
        ),
    )
    originals = {
        "analysis-annotations": annotations,
        "analysis-provenance": provenance.canonical_bytes(),
        "analysis-report": report,
    }
    artifacts = (
        _direct_json_artifact(
            artifact_id="analysis-annotations",
            role=PackArtifactRoleV1.ANALYSIS_ANNOTATIONS,
            schema_id="KIRBY2_WO39D2_ANALYSIS_ANNOTATIONS_V1",
        ),
        _direct_json_artifact(
            artifact_id="analysis-provenance",
            role=PackArtifactRoleV1.ANALYSIS_PROVENANCE,
            schema_id=ANALYSIS_PROVENANCE_SCHEMA_ID_V1,
        ),
        _direct_json_artifact(
            artifact_id="analysis-report",
            role=PackArtifactRoleV1.ANALYSIS_REPORT_DATA,
            schema_id="KIRBY2_WO39D2_ANALYSIS_REPORT_V1",
            media_type="application/vnd.kirby2.report+json",
            content_format=PackContentFormatV1.REPORT_DATA,
        ),
    )
    return (
        _evidence_pack_specification(
            pack_type=PackTypeV1.ANALYSIS,
            name="wo39d2-analysis-audit",
            title="WO39-D2 canonical analysis audit",
            primary_artifact_id="analysis-report",
            artifacts=artifacts,
            license_value=_evidence_license(),
            capability_labels=("CANONICAL_REPORT_DATA_ONLY", "LOCAL_EVIDENCE"),
        ),
        originals,
    )


def _analysis_pack_case(
    fixture: _EvidencePackFixture,
) -> EvidencePackAuditCase:
    injected = dict(fixture.originals)
    injected["analysis-report"] = canonical_json_bytes(
        {
            "html": "<script>window.evil=true</script>",
            "schema_id": "KIRBY2_WO39D2_ANALYSIS_REPORT_V1",
            "schema_version": 1,
        }
    )
    renderer_refusal = _capture_refusal(
        lambda: build_domain_pack(fixture.specification, injected)
    )
    data_roles = {
        item.role
        for item in fixture.build.index.artifacts
        if item.role
        in {
            PackArtifactRoleV1.ANALYSIS_REPORT_DATA,
            PackArtifactRoleV1.ANALYSIS_ANNOTATIONS,
        }
    }
    checks = {
        "canonical_report_and_annotation_roles_are_separate": (
            data_roles
            == {
                PackArtifactRoleV1.ANALYSIS_REPORT_DATA,
                PackArtifactRoleV1.ANALYSIS_ANNOTATIONS,
            }
        ),
        "provenance_binds_the_complete_data_inventory": (
            _fixture_identities_are_exact(fixture)
            and _manifest_provenance_covers_index(fixture.build)
        ),
        "pack_declares_data_only_and_no_execution_support": (
            "DATA_ONLY" in fixture.build.manifest.capability_labels
            and not next(
                item
                for item in fixture.build.manifest.compatibility
                if item.level.value == "EXECUTABLE"
            ).supported
        ),
        "html_or_javascript_renderer_injection_is_refused": (
            renderer_refusal
            is DomainPackRefusalCodeV1.RENDERER_INJECTION_REFUSED
        ),
    }
    return _case(
        "analysis_pack_contains_data_and_annotations_without_renderer_code",
        f"pack={fixture.build.manifest.pack_id} artifacts={len(fixture.build.index.artifacts)}",
        checks,
        {
            "data_roles": sorted(item.value for item in data_roles),
            "pack_id": fixture.build.manifest.pack_id,
            "renderer_refusal": _refusal_value(renderer_refusal),
        },
    )


def _research_pack_inputs() -> tuple[PackBuildSpecificationV1, dict[str, bytes]]:
    from kirby2.instructor.export import (
        EVIDENCE_EXPORT_MANIFEST_SCHEMA_ID,
        EXPORT_CONSENT_DECISION_SCHEMA_ID,
        EXPORT_INVENTORY_SCHEMA_ID,
    )
    from kirby2.instructor.redaction import REDACTION_MANIFEST_SCHEMA_ID

    bundle = _research_export_bundle()
    originals = {
        "research-consent-decision": bundle.consent_decision.canonical_bytes(),
        "research-export-inventory": bundle.inventory.canonical_bytes(),
        "research-export-manifest": bundle.manifest.canonical_bytes(),
        "research-redacted-evidence": bundle.evidence_bytes,
        "research-redaction-manifest": bundle.redaction_manifest.canonical_bytes(),
    }
    artifacts = (
        _direct_json_artifact(
            artifact_id="research-consent-decision",
            role=PackArtifactRoleV1.RESEARCH_CONSENT_DECISION,
            schema_id=EXPORT_CONSENT_DECISION_SCHEMA_ID,
            logical_identity_sha256=bundle.consent_decision.sha256,
        ),
        _direct_json_artifact(
            artifact_id="research-export-inventory",
            role=PackArtifactRoleV1.RESEARCH_EXPORT_INVENTORY,
            schema_id=EXPORT_INVENTORY_SCHEMA_ID,
            logical_identity_sha256=bundle.inventory.sha256,
        ),
        _direct_json_artifact(
            artifact_id="research-export-manifest",
            role=PackArtifactRoleV1.RESEARCH_EXPORT_MANIFEST,
            schema_id=EVIDENCE_EXPORT_MANIFEST_SCHEMA_ID,
            logical_identity_sha256=bundle.manifest.sha256,
        ),
        _direct_json_artifact(
            artifact_id="research-redacted-evidence",
            role=PackArtifactRoleV1.RESEARCH_REDACTED_EVIDENCE,
            schema_id=RESEARCH_REDACTED_EVIDENCE_SCHEMA_ID_V1,
            logical_identity_sha256=hashlib.sha256(
                bundle.evidence_bytes
            ).hexdigest(),
        ),
        _direct_json_artifact(
            artifact_id="research-redaction-manifest",
            role=PackArtifactRoleV1.RESEARCH_REDACTION_MANIFEST,
            schema_id=REDACTION_MANIFEST_SCHEMA_ID,
            logical_identity_sha256=bundle.redaction_manifest.sha256,
        ),
    )
    return (
        _evidence_pack_specification(
            pack_type=PackTypeV1.RESEARCH,
            name="wo39d2-research-audit",
            title="WO39-D2 governed research export audit",
            primary_artifact_id="research-export-manifest",
            artifacts=artifacts,
            license_value=_evidence_license(),
            capability_labels=(
                "CONSENT_BOUND",
                "FIELD_REDACTED",
                "PSEUDONYMOUS_EVIDENCE",
            ),
        ),
        originals,
    )


def _research_export_bundle():
    from kirby2.instructor.commands import (
        _privacy_export_omissions,
        _selected_privacy_export_records,
        build_instructor_demo,
        load_privacy_export_fixture,
    )
    from kirby2.instructor.consent import revise_consent_record
    from kirby2.instructor.export import (
        build_export_bundle,
        create_selected_causal_trace,
    )
    from kirby2.instructor.redaction import (
        create_portable_evidence_redaction_policy,
    )
    from kirby2.instructor.statistics import VersionSignatureV1
    from kirby2.instructor.studies import (
        StudyDataExportPolicyV1,
        StudyRetentionPolicyV1,
    )

    fixture_path = (
        Path(__file__).resolve().parents[1]
        / "instructor"
        / "fixtures"
        / "privacy_export.toml"
    )
    fixture = load_privacy_export_fixture(fixture_path)
    redaction_policy = create_portable_evidence_redaction_policy(
        policy_id=fixture.redaction_policy_id,
        policy_version=fixture.redaction_policy_version,
        allowlisted_paths=fixture.allowlisted_paths,
        authorized_hidden_paths=fixture.authorized_hidden_paths,
    )
    demo = build_instructor_demo(
        39_002,
        study_retention_policy=StudyRetentionPolicyV1(
            policy=fixture.retention_policy,
            retention_until_utc=None,
            retain_after_profile_deletion=fixture.retain_after_profile_deletion,
        ),
        study_data_export_policy=StudyDataExportPolicyV1(
            permission=fixture.export_permission,
            redaction_policy_sha256=redaction_policy.sha256,
        ),
    )
    learner, attempt, score, review = _selected_privacy_export_records(
        demo,
        fixture,
    )
    base_consents = tuple(
        item
        for item in demo.consents
        if item.pseudonymous_profile_id == learner.profile_id
    )
    if len(base_consents) != 1:
        raise RuntimeError("WO39-D2 research fixture lost its exact consent head")
    consent = revise_consent_record(
        base_consents[0],
        scopes=fixture.scopes,
        recorded_at_utc=fixture.recorded_at_utc,
        retention_policy=fixture.retention_policy,
        retention_until_utc=None,
        retain_pseudonymous_evidence_after_profile_deletion=(
            fixture.retain_after_profile_deletion
        ),
        export_permission=fixture.export_permission,
        withdrawal_policy=fixture.withdrawal_policy,
    )
    trace_source = canonical_json_bytes(
        {
            "domain": "KIRBY2_INSTRUCTOR_DEMO_DERIVATION_V1",
            "label": f"trace:{fixture.selected_causal_trace_ordinal}",
            "seed": 39_002,
        }
    )
    selected_trace = create_selected_causal_trace(
        review.sidecar.attempt.causal_trace_id,
        trace_source,
    )
    if selected_trace.trace_sha256 != review.sidecar.attempt.causal_trace_sha256:
        raise RuntimeError("WO39-D2 selected trace differs from its review binding")
    versions = dict(fixture.compatibility_versions)
    compatibility = VersionSignatureV1(
        score_version=versions["rubric_score"],
        score_sha256=score.sha256,
        model_version=versions["attempt_manifest"],
        model_sha256=attempt.sha256,
        analysis_version=versions["review"],
        analysis_sha256=review.sha256,
    )
    return build_export_bundle(
        current_consent=consent,
        required_scope=fixture.required_scope,
        decision_time_utc=fixture.decision_time_utc,
        assignment=demo.assignment,
        attempt_manifest=attempt,
        study_revision=demo.study_revision,
        study_ledger=demo.study_ledger,
        scores=(score,),
        reviews=(review,),
        selected_causal_traces=(selected_trace,),
        compatibility_versions=(compatibility,),
        software_version=fixture.software_version,
        limitations=fixture.limitations,
        redaction_policy=redaction_policy,
        omissions=_privacy_export_omissions(fixture),
    )


def _research_pack_case(
    fixture: _EvidencePackFixture,
) -> EvidencePackAuditCase:
    consent_payload = load_canonical_json_bytes(
        fixture.originals["research-consent-decision"],
        "WO39-D2 research consent decision",
    )
    if type(consent_payload) is not dict:
        raise RuntimeError("WO39-D2 consent decision lost its object shape")
    consent_payload = dict(consent_payload)
    consent_payload["allowed"] = False
    refused_consent_raw = canonical_json_bytes(consent_payload)
    refused_consent_artifacts = tuple(
        replace(
            item,
            logical_identity_sha256=hashlib.sha256(
                refused_consent_raw
            ).hexdigest(),
        )
        if item.artifact_id == "research-consent-decision"
        else item
        for item in fixture.specification.artifacts
    )
    refused_consent_originals = dict(fixture.originals)
    refused_consent_originals["research-consent-decision"] = refused_consent_raw
    consent_refusal = _capture_refusal(
        lambda: build_domain_pack(
            replace(fixture.specification, artifacts=refused_consent_artifacts),
            refused_consent_originals,
        )
    )
    wrong_schema_artifacts = tuple(
        replace(
            item,
            original_schema_id="KIRBY2_WO39D2_UNGOVERNED_EVIDENCE_V1",
        )
        if item.artifact_id == "research-redacted-evidence"
        else item
        for item in fixture.specification.artifacts
    )
    redaction_refusal = _capture_refusal(
        lambda: build_domain_pack(
            replace(fixture.specification, artifacts=wrong_schema_artifacts),
            fixture.originals,
        )
    )
    roles = {item.role for item in fixture.build.index.artifacts}
    required_roles = {
        PackArtifactRoleV1.RESEARCH_EXPORT_MANIFEST,
        PackArtifactRoleV1.RESEARCH_EXPORT_INVENTORY,
        PackArtifactRoleV1.RESEARCH_REDACTED_EVIDENCE,
        PackArtifactRoleV1.RESEARCH_REDACTION_MANIFEST,
        PackArtifactRoleV1.RESEARCH_CONSENT_DECISION,
    }
    checks = {
        "governed_export_retains_all_five_privacy_artifacts": (
            roles == required_roles and _fixture_identities_are_exact(fixture)
        ),
        "owning_export_identities_remain_authoritative": all(
            item.logical_identity_sha256
            != fixture.build.index.domain_identity_sha256
            for item in fixture.build.index.artifacts
        ),
        "direct_identity_roles_are_absent": all(
            "DIRECT_IDENTITY" not in item.role.value
            for item in fixture.build.index.artifacts
        ),
        "denied_consent_is_explicitly_refused": (
            consent_refusal
            is DomainPackRefusalCodeV1.RESEARCH_CONSENT_REQUIRED
        ),
        "unbound_redaction_schema_is_explicitly_refused": (
            redaction_refusal
            is DomainPackRefusalCodeV1.RESEARCH_REDACTION_VIOLATION
        ),
    }
    return _case(
        "research_pack_retains_governed_consent_and_field_redaction",
        f"pack={fixture.build.manifest.pack_id} artifacts={len(roles)}",
        checks,
        {
            "consent_refusal": _refusal_value(consent_refusal),
            "pack_id": fixture.build.manifest.pack_id,
            "redaction_refusal": _refusal_value(redaction_refusal),
            "roles": sorted(item.value for item in roles),
        },
    )


def _all_evidence_pack_types_case(
    historical: _EvidencePackFixture,
    replay: _ReplayExportFixture,
    analysis: _EvidencePackFixture,
    research: _EvidencePackFixture,
) -> EvidencePackAuditCase:
    rebuildable = (historical, analysis, research)
    rebuilt = tuple(
        build_domain_pack(item.specification, item.originals)
        for item in rebuildable
    )
    builds = (
        historical.build,
        replay.build,
        analysis.build,
        research.build,
    )
    required_types = {
        PackTypeV1.HISTORICAL,
        PackTypeV1.REPLAY,
        PackTypeV1.ANALYSIS,
        PackTypeV1.RESEARCH,
    }
    checks = {
        "all_four_evidence_adapters_are_declared": (
            required_types <= set(supported_domain_pack_types_v1())
            and {item.manifest.pack_type for item in builds} == required_types
        ),
        "rebuildable_archives_repeat_byte_for_byte": all(
            first.build.archive_bytes == second.archive_bytes
            and first.build.manifest.pack_id == second.manifest.pack_id
            and first.build.transport_sha256 == second.transport_sha256
            and first.build.index == second.index
            for first, second in zip(rebuildable, rebuilt, strict=True)
        ),
        "registered_replay_archive_repeats_byte_for_byte": (
            replay.build.archive_bytes == replay.repeated_build.archive_bytes
            and replay.build.manifest.pack_id
            == replay.repeated_build.manifest.pack_id
        ),
        "all_four_manifest_provenance_records_every_domain_artifact": all(
            _manifest_provenance_covers_index(item) for item in builds
        ),
        "all_four_are_data_only_archives": all(
            "DATA_ONLY" in item.manifest.capability_labels for item in builds
        ),
        "pack_and_transport_identities_remain_separate": all(
            item.manifest.pack_id != item.transport_sha256 for item in builds
        ),
    }
    return _case(
        "all_four_evidence_pack_types_round_trip_identity_and_provenance",
        f"types={len(builds)} packs={len({item.manifest.pack_id for item in builds})}",
        checks,
        {
            "pack_ids": {
                item.manifest.pack_type.value: item.manifest.pack_id
                for item in builds
            },
            "types": sorted(item.value for item in required_types),
        },
    )


def _direct_json_artifact(
    *,
    artifact_id: str,
    role: PackArtifactRoleV1,
    schema_id: str,
    source_path: str | None = None,
    media_type: str = "application/json",
    content_format: PackContentFormatV1 = PackContentFormatV1.CANONICAL_JSON,
    logical_identity_sha256: str | None = None,
) -> PackSourceArtifactV1:
    return PackSourceArtifactV1(
        artifact_id=artifact_id,
        role=role,
        source_path=(
            f"generated/{artifact_id}.json"
            if source_path is None
            else source_path
        ),
        original_schema_id=schema_id,
        original_schema_version=1,
        original_media_type=media_type,
        storage_mode=PackArtifactStorageModeV1.DIRECT,
        logical_identity_kind="OWNING_ARTIFACT_SHA256_V1",
        logical_identity_sha256=logical_identity_sha256,
        direct_content_format=content_format,
    )


def _evidence_pack_specification(
    *,
    pack_type: PackTypeV1,
    name: str,
    title: str,
    primary_artifact_id: str,
    artifacts: tuple[PackSourceArtifactV1, ...],
    license_value: PackLicenseV1,
    capability_labels: tuple[str, ...],
) -> PackBuildSpecificationV1:
    return PackBuildSpecificationV1(
        namespace="kirby2.audit.wo39d2",
        name=name,
        title=title,
        version="1.0.0",
        creator=PackCreatorV1(
            display_name="Kirby2 WO39-D2 audit",
            identity_uri="urn:kirby2:audit:wo39d2",
        ),
        pack_type=pack_type,
        primary_artifact_id=primary_artifact_id,
        dependencies=(),
        license=license_value,
        capability_labels=capability_labels,
        artifacts=tuple(sorted(artifacts, key=lambda item: item.artifact_id)),
    )


def _evidence_license(
    *,
    content_mode: PackContentModeV1 = PackContentModeV1.SELF_CONTAINED,
) -> PackLicenseV1:
    return PackLicenseV1(
        license_id="KIRBY2_WO39D2_AUDIT_LICENSE_V1",
        license_name="Synthetic Kirby2 WO39-D2 audit evidence",
        license_uri="urn:kirby2:license:wo39d2-audit-v1",
        redistribution_policy=PackRedistributionPolicyV1.ALLOWED,
        content_mode=content_mode,
    )


def _fixture_identities_are_exact(fixture: _EvidencePackFixture) -> bool:
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


def _manifest_provenance_covers_index(build: DomainPackBuildV1) -> bool:
    provenance = {
        (item.source_kind, item.source_id, item.source_sha256)
        for item in build.manifest.provenance
    }
    return all(
        (item.role.value, item.artifact_id, item.original_sha256) in provenance
        for item in build.index.artifacts
    )


def _capture_refusal(operation) -> DomainPackRefusalCodeV1 | None:
    try:
        operation()
    except DomainPackRefused as error:
        return error.code
    return None


def _refusal_value(value: DomainPackRefusalCodeV1 | None) -> str | None:
    return None if value is None else value.value


def _case(
    name: str,
    detail: str,
    checks: dict[str, bool],
    evidence: dict[str, object],
) -> EvidencePackAuditCase:
    return EvidencePackAuditCase(
        name=name,
        detail=detail,
        evidence=evidence,
        failures=tuple(label for label, passed in checks.items() if not passed),
    )


__all__ = [
    "WO39D2_AUDIT_CASE_COUNT",
    "EvidencePackAuditCase",
    "audit_evidence_domain_packs",
]
