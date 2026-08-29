"""Authoring commands for validated, immutable scenario sources.

The command layer is deliberately thin: it resolves a confined source bundle,
loads one explicitly bound native target, compiles and validates an immutable
artifact, and only then permits persistence or runtime dispatch.  Source text is
never evaluated and runtime adapters never receive source paths.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, replace
from importlib.resources import files
from pathlib import Path, PurePosixPath

from kirby2.cli.registry import CommandModule, CommandSpec

from .capabilities import SCENARIO_TARGET_CAPABILITIES_V1
from .compiler import (
    DEFAULT_SCENARIO_TARGET_REGISTRY,
    ScenarioExecutionRefused,
    compile_resolved_scenario,
    replay_compiled_scenario,
    run_compiled_scenario,
)
from .models import (
    CompiledScenarioArtifactV1,
    ResolvedScenarioBundleV1,
    ScenarioTargetKindV1,
    ScenarioValidationFindingV1,
    ScenarioValidationReportV1,
    ScenarioValidationSeverityV1,
    ScenarioValueKindV1,
)
from .resolution import resolve_scenario_bundle
from .validation import finalize_compiled_scenario, validate_compiled_scenario


SCENARIO_TARGET_BINDING_RECORD_TYPE_V1 = "SCENARIO_TARGET_BINDING_V1"
SCENARIO_TARGET_BINDING_LOGICAL_NAME_V1 = "target_binding"
SCENARIO_NATIVE_PAYLOAD_MAX_BYTES_V1 = 16 * 1024 * 1024

SCENARIO_EXPLAIN_SECTION_NAMES_V1 = (
    "MARKET_CREATED",
    "POSSIBLE_CHANGES",
    "HIDDEN_STATE",
    "OBSERVABLE_STATE",
    "SCHEDULED_EVENTS",
    "STOCHASTIC_COMPONENTS",
    "TERMINATION",
    "INVALIDATION_CONDITIONS",
    "PROVENANCE",
    "MATERIALIZED_DEFAULTS",
    "CAPABILITIES",
    "UNITS",
    "RNG_POLICY",
    "SEMANTIC_IDENTITY",
)

VALID_SCENARIO_EXAMPLE_FILENAMES_V1 = (
    "full_day.toml",
    "opening_momentum.toml",
    "hidden_liquidity.toml",
    "fragmented_venue.toml",
    "historical_reconstruction.toml",
    "halt_reopening.toml",
)

SCENARIO_EXAMPLE_FIXTURE_TARGETS_V1 = {
    "QUIET_FULL_DAY_V1": ScenarioTargetKindV1.FULL_DAY_PLAN_V1,
    "OPENING_MOMENTUM_V1": ScenarioTargetKindV1.MARKET_SCENARIO_V1,
    "HIDDEN_LIQUIDITY_LESSON_V1": (
        ScenarioTargetKindV1.HIDDEN_LIQUIDITY_RECORDING_V1
    ),
    "FRAGMENTED_VENUE_TASK_V1": ScenarioTargetKindV1.MULTIVENUE_RECORDING_V1,
    "HISTORICAL_RECONSTRUCTION_V1": ScenarioTargetKindV1.HISTORICAL_LESSON_V1,
    "HALT_REOPEN_FULL_DAY_V1": ScenarioTargetKindV1.FULL_DAY_PLAN_V1,
}

_PACK_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$")
_TOML_LOCATION = re.compile(r"line (?P<line>[0-9]+), column (?P<column>[0-9]+)")


@dataclass(frozen=True, slots=True)
class ScenarioSourceSpanV1:
    start_line: int
    start_column: int
    end_line: int
    end_column: int

    def __post_init__(self) -> None:
        if (
            type(self.start_line) is not int
            or type(self.start_column) is not int
            or type(self.end_line) is not int
            or type(self.end_column) is not int
            or self.start_line <= 0
            or self.start_column <= 0
            or (self.end_line, self.end_column)
            < (self.start_line, self.start_column)
        ):
            raise ValueError("scenario diagnostic span is invalid")

    def as_dict(self) -> dict[str, int]:
        return {
            "end_column": self.end_column,
            "end_line": self.end_line,
            "start_column": self.start_column,
            "start_line": self.start_line,
        }


@dataclass(frozen=True, slots=True)
class ScenarioSourceDiagnosticV1:
    code: str
    severity: str
    source_file: str
    span: ScenarioSourceSpanV1
    semantic_path: str
    explanation: str
    correction: str | None
    blocks_execution: bool

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", self.code):
            raise ValueError("scenario diagnostic code must be an uppercase identifier")
        if self.severity not in {
            item.value for item in ScenarioValidationSeverityV1
        }:
            raise ValueError("scenario diagnostic severity is invalid")
        if not all(
            type(value) is str and value
            for value in (
                self.source_file,
                self.semantic_path,
                self.explanation,
            )
        ):
            raise ValueError("scenario diagnostic text fields must be nonempty")
        if self.correction is not None and (
            type(self.correction) is not str or not self.correction
        ):
            raise ValueError("scenario diagnostic correction must be useful text")
        if type(self.blocks_execution) is not bool:
            raise TypeError("scenario diagnostic blocking flag must be a bool")

    def as_dict(self) -> dict[str, object]:
        return {
            "blocks_execution": self.blocks_execution,
            "code": self.code,
            "correction": self.correction,
            "explanation": self.explanation,
            "semantic_path": self.semantic_path,
            "severity": self.severity,
            "source_file": self.source_file,
            "span": self.span.as_dict(),
        }


@dataclass(frozen=True, slots=True)
class ScenarioAuthoringResultV1:
    source_path: Path
    resolved: ResolvedScenarioBundleV1 | None
    artifact: CompiledScenarioArtifactV1 | None
    report: ScenarioValidationReportV1 | None
    diagnostics: tuple[ScenarioSourceDiagnosticV1, ...]

    @property
    def passed(self) -> bool:
        return (
            self.artifact is not None
            and self.artifact.execution_eligible
            and self.report is not None
            and self.report.passed
            and not any(item.blocks_execution for item in self.diagnostics)
        )


def inspect_scenario_source(
    source_path: Path,
    *,
    activated_pack_namespaces: Mapping[str, Path] | None = None,
    cli_seed_override: int | None = None,
) -> ScenarioAuthoringResultV1:
    """Resolve, compile, and statically validate one source without executing it."""

    try:
        canonical_path = _canonical_input_file(source_path)
    except (OSError, TypeError, ValueError) as error:
        display = Path(source_path).absolute()
        return ScenarioAuthoringResultV1(
            display,
            None,
            None,
            None,
            (_exception_diagnostic(display, error),),
        )
    source_root = canonical_path.parent
    try:
        resolved = resolve_scenario_bundle(
            source_root,
            canonical_path.name,
            activated_pack_namespaces=activated_pack_namespaces,
        )
        native_payload = _bound_native_payload(resolved, source_root)
        unvalidated = compile_resolved_scenario(
            resolved,
            native_payload,
            cli_seed_override=cli_seed_override,
        )
        report = validate_compiled_scenario(unvalidated)
        diagnostics = tuple(
            _finding_diagnostic(canonical_path, finding)
            for finding in report.findings
        )
        artifact = (
            finalize_compiled_scenario(unvalidated, report)
            if report.passed
            else unvalidated
        )
        return ScenarioAuthoringResultV1(
            canonical_path,
            resolved,
            artifact,
            report,
            diagnostics,
        )
    except (OSError, TypeError, ValueError, RuntimeError) as error:
        return ScenarioAuthoringResultV1(
            canonical_path,
            locals().get("resolved"),
            None,
            None,
            (_exception_diagnostic(canonical_path, error),),
        )


def explain_scenario_source(
    result: ScenarioAuthoringResultV1,
) -> tuple[tuple[str, object], ...]:
    """Return the fixed, execution-free explain sections for one compiled source."""

    artifact = _require_artifact(result)
    root = artifact.materialized_plan["root_source"]
    if not isinstance(root, Mapping):
        raise TypeError("compiled scenario root source is malformed")
    native = artifact.plan_envelope.payload
    target_contract = SCENARIO_TARGET_CAPABILITIES_V1[artifact.target_kind]
    section = lambda name: _section_records(root, name)
    reveal_records = section("reveal_policy")
    declared_hidden = sorted(
        {
            feature
            for record in reveal_records
            for feature in _field_strings(record, "hidden_features")
        }
    )
    declared_observable = sorted(
        {
            feature
            for record in reveal_records
            for feature in _field_strings(record, "observable_features")
        }
    )
    invalidation = [
        {
            "code": item.code,
            "condition": item.explanation,
            "correction": item.correction,
            "currently_triggered": item.blocks_execution,
        }
        for item in result.diagnostics
    ]
    invalidation.extend(
        {
            "code": code,
            "condition": condition,
            "correction": correction,
            "currently_triggered": False,
        }
        for code, condition, correction in _KNOWN_INVALIDATION_CONDITIONS
        if code not in {item["code"] for item in invalidation}
    )
    changes = {
        name: section(name)
        for name in (
            "regimes",
            "day_local_states",
            "volume",
            "liquidity",
            "latency",
            "agent_populations",
            "transition_rules",
        )
        if section(name)
    }
    scheduled = {
        "declared": section("scheduled_events"),
        "native": _native_schedule_summary(artifact.target_kind, native),
    }
    stochastic = {
        "declared_flow_models": section("flow_model"),
        "seed_policy": artifact.seed_policy.as_dict(),
        "remaining_components": _stochastic_components(
            artifact.target_kind,
            native,
        ),
    }
    sections = (
        (
            "MARKET_CREATED",
            {
                "instrument": section("instrument"),
                "market_profile": section("market_profile"),
                "native_target": _native_target_summary(artifact.target_kind, native),
                "target_kind": artifact.target_kind.value,
                "venues": section("venues"),
            },
        ),
        ("POSSIBLE_CHANGES", changes),
        (
            "HIDDEN_STATE",
            {
                "declared_hidden_features": declared_hidden,
                "target_hidden_boundary": _target_hidden_boundary(artifact.target_kind),
            },
        ),
        (
            "OBSERVABLE_STATE",
            {
                "declared_observable_features": declared_observable,
                "target_observable_features": list(target_contract.observable_features),
            },
        ),
        ("SCHEDULED_EVENTS", scheduled),
        ("STOCHASTIC_COMPONENTS", stochastic),
        ("TERMINATION", _termination_summary(artifact.target_kind, native, root)),
        ("INVALIDATION_CONDITIONS", invalidation),
        ("PROVENANCE", artifact.provenance),
        (
            "MATERIALIZED_DEFAULTS",
            artifact.materialized_plan.get("applied_defaults", []),
        ),
        (
            "CAPABILITIES",
            {
                "decisions": artifact.as_dict()["capability_decisions"],
                "target_contract": target_contract.as_dict(),
            },
        ),
        ("UNITS", _unit_inventory(artifact.materialized_plan)),
        ("RNG_POLICY", artifact.seed_policy.as_dict()),
        (
            "SEMANTIC_IDENTITY",
            {
                "compiled_artifact_digest": artifact.compiled_artifact_digest,
                "native_plan_digest": artifact.native_plan_digest,
                "run_identity_digest": artifact.run_identity_digest,
                "semantic_plan_digest": artifact.semantic_plan_digest,
                "source_bundle_digest": artifact.source_bundle_digest,
                "validation_report_digest": artifact.validation_report_digest,
            },
        ),
    )
    if tuple(name for name, _ in sections) != SCENARIO_EXPLAIN_SECTION_NAMES_V1:
        raise AssertionError("scenario explain section inventory changed")
    return sections


def diff_scenario_sources(
    left: ScenarioAuthoringResultV1,
    right: ScenarioAuthoringResultV1,
) -> dict[str, object]:
    left_artifact = _require_eligible_artifact(left)
    right_artifact = _require_eligible_artifact(right)
    semantic_changes: list[dict[str, object]] = []
    _semantic_diff(
        left_artifact.materialized_plan,
        right_artifact.materialized_plan,
        "materialized_plan",
        semantic_changes,
    )
    source_only: list[dict[str, object]] = []
    if not semantic_changes:
        left_docs = _document_digest_map(left_artifact)
        right_docs = _document_digest_map(right_artifact)
        for logical_path in sorted(set(left_docs) | set(right_docs)):
            if left_docs.get(logical_path) != right_docs.get(logical_path):
                source_only.append(
                    {
                        "left_raw_sha256": left_docs.get(logical_path),
                        "path": f"source:{logical_path}:presentation",
                        "right_raw_sha256": right_docs.get(logical_path),
                    }
                )
    return {
        "compiled_identity_changed": (
            left_artifact.compiled_artifact_digest
            != right_artifact.compiled_artifact_digest
        ),
        "left": str(left.source_path),
        "native_identity_changed": (
            left_artifact.native_plan_digest != right_artifact.native_plan_digest
        ),
        "right": str(right.source_path),
        "semantic_changes": semantic_changes,
        "semantic_identity_changed": (
            left_artifact.semantic_plan_digest
            != right_artifact.semantic_plan_digest
        ),
        "source_only_changes": source_only,
    }


def persist_compiled_artifact(
    artifact: CompiledScenarioArtifactV1,
    output_path: Path,
) -> Path:
    """Persist one eligible canonical artifact without overwriting another file."""

    if type(artifact) is not CompiledScenarioArtifactV1 or not artifact.execution_eligible:
        raise ValueError("only an execution-eligible compiled artifact may be persisted")
    target = _canonical_new_output(output_path)
    raw = artifact.canonical_bytes()
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=target.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, target)
        temporary.unlink()
        directory_descriptor = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except BaseException:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    restored = replay_compiled_scenario(target.read_bytes())
    if restored.canonical_bytes() != raw:
        raise RuntimeError("persisted scenario artifact failed canonical replay")
    return target


def _canonical_input_file(path: Path) -> Path:
    candidate = Path(path)
    if candidate.is_symlink():
        raise ValueError("scenario source entry must not be a symbolic link")
    resolved = candidate.resolve(strict=True)
    if not resolved.is_file():
        raise ValueError("scenario source entry must be a regular file")
    if resolved.suffix != ".toml":
        raise ValueError("scenario source entry must use lowercase .toml")
    return resolved


def _bound_native_payload(
    resolved: ResolvedScenarioBundleV1,
    source_root: Path,
) -> object:
    bindings = tuple(
        record
        for record in resolved.root_source.curriculum_metadata.records
        if record.record_type == SCENARIO_TARGET_BINDING_RECORD_TYPE_V1
    )
    if len(bindings) != 1:
        raise ValueError(
            "scenario source requires exactly one SCENARIO_TARGET_BINDING_V1 record"
        )
    binding = bindings[0]
    if binding.logical_name != SCENARIO_TARGET_BINDING_LOGICAL_NAME_V1:
        raise ValueError("scenario target binding logical name must be target_binding")
    if binding.reference is not None or binding.extends is not None:
        raise ValueError("scenario target binding cannot reference or inherit")
    fields_by_name = {field.name: field for field in binding.fields}
    target_kind = resolved.root_source.metadata.target_kind
    if set(fields_by_name) == {"fixture_id"}:
        field = fields_by_name["fixture_id"]
        if field.value_kind is not ScenarioValueKindV1.IDENTIFIER:
            raise ValueError("scenario fixture_id requires the identifier value tag")
        return _load_example_fixture(str(field.value), target_kind)
    if set(fields_by_name) != {"payload_path", "payload_sha256"}:
        raise ValueError(
            "scenario target binding requires fixture_id or payload_path plus payload_sha256"
        )
    path_field = fields_by_name["payload_path"]
    digest_field = fields_by_name["payload_sha256"]
    if (
        path_field.value_kind is not ScenarioValueKindV1.TEXT
        or digest_field.value_kind is not ScenarioValueKindV1.IDENTIFIER
    ):
        raise ValueError("scenario payload binding uses the wrong explicit value tags")
    raw = _read_confined_payload(source_root, str(path_field.value))
    expected_digest = str(digest_field.value)
    if not re.fullmatch(r"[0-9a-f]{64}", expected_digest):
        raise ValueError("scenario payload_sha256 must be a lowercase SHA-256 digest")
    if hashlib.sha256(raw).hexdigest() != expected_digest:
        raise ValueError("scenario native payload digest does not match payload_sha256")
    body = _canonical_payload_body(raw)
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("scenario native payload must be canonical UTF-8 JSON") from error
    if not isinstance(payload, Mapping):
        raise TypeError("scenario native payload root must be an object")
    adapter = DEFAULT_SCENARIO_TARGET_REGISTRY.adapter(target_kind)
    native = adapter.parse(payload)
    if adapter.persist(native) != body:
        raise ValueError("scenario native payload bytes are not canonical JSON")
    return native


def _read_confined_payload(source_root: Path, requested: str) -> bytes:
    if not requested or "\\" in requested or "\x00" in requested:
        raise ValueError("scenario payload path must be canonical POSIX text")
    pure = PurePosixPath(requested)
    if (
        pure.is_absolute()
        or any(part in {"", ".", ".."} for part in requested.split("/"))
        or pure.as_posix() != requested
        or pure.suffix != ".json"
        or re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", requested)
    ):
        raise ValueError("scenario payload path must be a confined relative .json path")
    root = source_root.resolve(strict=True)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory = getattr(os, "O_DIRECTORY", 0)
    if not nofollow or not directory:
        raise RuntimeError("scenario payload confinement requires no-follow directory opens")
    descriptors: list[int] = []
    file_descriptor: int | None = None
    try:
        descriptors.append(os.open(root, os.O_RDONLY | directory | nofollow))
        for part in pure.parts[:-1]:
            descriptors.append(
                os.open(
                    part,
                    os.O_RDONLY | directory | nofollow,
                    dir_fd=descriptors[-1],
                )
            )
        file_descriptor = os.open(
            pure.parts[-1],
            os.O_RDONLY | nofollow,
            dir_fd=descriptors[-1],
        )
        metadata = os.fstat(file_descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("scenario native payload must be a regular file")
        if (
            metadata.st_size <= 0
            or metadata.st_size > SCENARIO_NATIVE_PAYLOAD_MAX_BYTES_V1
        ):
            raise ValueError("scenario native payload exceeds its bounded byte limit")
        blocks: list[bytes] = []
        remaining = SCENARIO_NATIVE_PAYLOAD_MAX_BYTES_V1 + 1
        while remaining:
            block = os.read(file_descriptor, min(1024 * 1024, remaining))
            if not block:
                break
            blocks.append(block)
            remaining -= len(block)
        raw = b"".join(blocks)
        if len(raw) != metadata.st_size:
            raise RuntimeError("scenario native payload changed while it was read")
        return raw
    except FileNotFoundError as error:
        raise ValueError("scenario native payload path does not exist") from error
    except OSError as error:
        raise ValueError(
            "scenario payload path may not traverse a symbolic link"
        ) from error
    finally:
        if file_descriptor is not None:
            os.close(file_descriptor)
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _load_example_fixture(
    fixture_id: str,
    target_kind: ScenarioTargetKindV1,
) -> object:
    expected = SCENARIO_EXAMPLE_FIXTURE_TARGETS_V1.get(fixture_id)
    if expected is None:
        raise ValueError(f"unknown closed scenario example fixture: {fixture_id}")
    if expected is not target_kind:
        raise ValueError("scenario fixture target does not match the explicit source tag")
    if fixture_id in {"QUIET_FULL_DAY_V1", "HALT_REOPEN_FULL_DAY_V1"}:
        return _full_day_fixture(fixture_id)
    if fixture_id == "OPENING_MOMENTUM_V1":
        from kirby2.scenarios.market import load_scenario_definitions

        definition = load_scenario_definitions()["momentum_up"]
        return replace(
            definition,
            duration_seconds=1,
            accepted_replay_sha256=hashlib.sha256(
                b"KIRBY2_OPENING_MOMENTUM_AUTHORING_EXAMPLE_V1"
            ).hexdigest(),
        )
    if fixture_id == "HIDDEN_LIQUIDITY_LESSON_V1":
        return _resource_native_payload(
            target_kind,
            "examples/native/hidden_liquidity_recording.json",
        )
    if fixture_id == "FRAGMENTED_VENUE_TASK_V1":
        return _resource_native_payload(
            target_kind,
            "examples/native/fragmented_venue_recording.json",
        )
    if fixture_id == "HISTORICAL_RECONSTRUCTION_V1":
        from kirby2.historical.lesson_catalog import load_historical_lessons

        return load_historical_lessons()["reconstruction_liquidity_decision"]
    raise AssertionError(f"unhandled scenario fixture: {fixture_id}")


def _full_day_fixture(fixture_id: str) -> object:
    from kirby2.full_day.composition import (
        INITIAL_PROFILE_ID,
        executable_agent_mechanics_composition_matrix,
    )
    from kirby2.full_day.models import (
        FlowSideV1,
        FullDayPlanV1,
        IntegerParameterUnitV1,
        NamedIntegerParameterV1,
        ScheduledEventTypeV1,
        ScheduledEventV1,
        VersionedReferenceV1,
    )

    raw = files("kirby2.full_day").joinpath(
        "examples/audit_full_day_plan.json"
    ).read_bytes()
    base = FullDayPlanV1.from_json_bytes(raw)
    matrix = executable_agent_mechanics_composition_matrix()
    plan_id = {
        "QUIET_FULL_DAY_V1": "SCENARIO_QUIET_FULL_DAY_PLAN_V1",
        "HALT_REOPEN_FULL_DAY_V1": "SCENARIO_HALT_REOPEN_PLAN_V1",
    }[fixture_id]
    scheduled_events = ()
    if fixture_id == "HALT_REOPEN_FULL_DAY_V1":
        scheduled_events = (
            ScheduledEventV1(
                "SCENARIO_VOLATILITY_HALT_V1",
                base.calendar.phases[2].start.simulation_time_us + 100,
                ScheduledEventTypeV1.HALT,
                1,
                FlowSideV1.NONE,
                (
                    NamedIntegerParameterV1(
                        "halt_duration_us",
                        IntegerParameterUnitV1.MICROSECONDS,
                        100,
                    ),
                ),
                None,
                base.halt_reopen_rules.halt_trigger_reference,
            ),
            ScheduledEventV1(
                "SCENARIO_REOPENING_AUCTION_V1",
                base.calendar.phases[2].start.simulation_time_us + 200,
                ScheduledEventTypeV1.REOPENING,
                1,
                FlowSideV1.NONE,
                (
                    NamedIntegerParameterV1(
                        "reopening_auction_duration_us",
                        IntegerParameterUnitV1.MICROSECONDS,
                        10,
                    ),
                ),
                None,
                base.halt_reopen_rules.resume_trigger_reference,
            ),
        )
    return replace(
        base,
        plan_id=plan_id,
        composition_profile=VersionedReferenceV1(
            INITIAL_PROFILE_ID,
            2,
            matrix.sha256,
        ),
        participant_schedule=(),
        scheduled_events=scheduled_events,
        unscheduled_shock_policy=replace(
            base.unscheduled_shock_policy,
            enabled=False,
            maximum_accepted_shocks=0,
            acceptance_numerator=0,
            acceptance_denominator=1,
        ),
    )


def _resource_native_payload(
    target_kind: ScenarioTargetKindV1,
    resource_path: str,
) -> object:
    raw = files("kirby2.scenario_lang").joinpath(resource_path).read_bytes()
    body = _canonical_payload_body(raw)
    payload = json.loads(body.decode("utf-8"))
    if not isinstance(payload, Mapping):
        raise TypeError("packaged scenario native fixture must be an object")
    adapter = DEFAULT_SCENARIO_TARGET_REGISTRY.adapter(target_kind)
    native = adapter.parse(payload)
    if adapter.persist(native) != body:
        raise ValueError("packaged scenario native fixture is not canonical")
    return native


def _canonical_payload_body(raw: bytes) -> bytes:
    """Accept canonical JSON with the single POSIX text-file newline only."""

    body = raw[:-1] if raw.endswith(b"\n") else raw
    if not body or body.endswith((b"\n", b"\r")):
        raise ValueError("scenario native payload has noncanonical trailing bytes")
    return body


def _finding_diagnostic(
    source_path: Path,
    finding: ScenarioValidationFindingV1,
) -> ScenarioSourceDiagnosticV1:
    return ScenarioSourceDiagnosticV1(
        code=finding.code,
        severity=finding.severity.value,
        source_file=str(source_path),
        span=_source_span(source_path, finding.source_location),
        semantic_path=finding.source_location,
        explanation=finding.message,
        correction=finding.suggested_correction,
        blocks_execution=finding.blocks_execution,
    )


def _exception_diagnostic(
    source_path: Path,
    error: BaseException,
) -> ScenarioSourceDiagnosticV1:
    message = str(error) or type(error).__name__
    normalized = message.lower()
    code = "SCENARIO_SOURCE_INVALID"
    semantic_path = "source"
    correction = "Correct the source and rerun lint before compiling or running."
    rules = (
        (("valid toml", "strict utf-8 toml"), "SOURCE_TOML_INVALID", "source", "Use strict UTF-8 TOML with unique keys."),
        (("url and uri", "url or uri"), "IMPORT_URI_FORBIDDEN", "imports", "Use a confined relative lowercase .toml import path."),
        (("absolute scenario import",), "IMPORT_ABSOLUTE_PATH_FORBIDDEN", "imports", "Use a relative path beneath the activated source root."),
        (("traverse", "parent traversal"), "IMPORT_TRAVERSAL_FORBIDDEN", "imports", "Remove parent and empty path components."),
        (("windows drive", "unc scenario"), "IMPORT_WINDOWS_PATH_FORBIDDEN", "imports", "Use canonical POSIX relative paths."),
        (("posix separators", "backslash"), "IMPORT_SEPARATOR_INVALID", "imports", "Use forward slashes in canonical POSIX paths."),
        (("nul",), "IMPORT_NUL_FORBIDDEN", "imports", "Remove the NUL character from the path."),
        (("symbolic link", "escapes its source root"), "IMPORT_ROOT_ESCAPE", "imports", "Keep imported and payload files beneath the canonical source root without symlinks."),
        (("contains a cycle", "inheritance cycle", "references contain a cycle"), "IMPORT_OR_REFERENCE_CYCLE", "imports", "Break the cycle and retain a finite acyclic graph."),
        (("maximum depth",), "IMPORT_DEPTH_LIMIT_EXCEEDED", "imports", "Reduce the import or reference depth."),
        (("maximum document count",), "IMPORT_COUNT_LIMIT_EXCEEDED", "imports", "Reduce the number of imported documents."),
        (("expanded byte", "bounded byte limit"), "IMPORT_BYTE_LIMIT_EXCEEDED", "imports", "Reduce the bounded source or payload size."),
        (("repeats a canonical path",), "IMPORT_DUPLICATE_CANONICAL_PATH", "imports", "Import each canonical document exactly once."),
        (("pack namespace",), "IMPORT_PACK_UNAVAILABLE", "imports", "Activate one exact noncolliding pack namespace."),
        (("collision",), "IMPORT_LOGICAL_COLLISION", "imports", "Give paths and definitions unique case-stable NFC names."),
        (("unknown definition", "unknown parent"), "DEFINITION_REFERENCE_UNKNOWN", "resolved_definitions", "Reference an existing qualified definition name."),
        (("cross-type", "same definition type", "cannot cross definition types"), "DEFINITION_CROSS_TYPE_INHERITANCE", "resolved_definitions", "Inherit from exactly one definition of the same type."),
        (("multiple inheritance",), "DEFINITION_MULTIPLE_INHERITANCE", "resolved_definitions", "Use at most one extends target."),
        (("duplicate definition", "duplicate scenario definition"), "DEFINITION_DUPLICATE", "resolved_definitions", "Give each qualified definition one unique declaration."),
        (("import target does not exist",), "IMPORT_TARGET_MISSING", "imports", "Create the confined imported document or correct the relative path."),
        (("expression or code evaluator", "cannot import, reflect, or call code"), "UNSAFE_EVALUATOR_FORBIDDEN", "root_source", "Use only the existing bounded typed records; remove Python, shell, reflection, and evaluator fields."),
        (("requires exactly one scenario_target_binding_v1",), "TARGET_BINDING_REQUIRED", "root_source.curriculum_metadata", "Declare one target_binding record of type SCENARIO_TARGET_BINDING_V1."),
        (("target binding",), "TARGET_BINDING_INVALID", "root_source.curriculum_metadata.records[target_binding]", "Use one exact fixture_id or payload_path plus payload_sha256 binding."),
        (("fixture target",), "TARGET_FIXTURE_TAG_MISMATCH", "root_source.metadata.target_kind", "Use a fixture whose closed target kind equals the source tag."),
        (("unknown closed scenario example fixture",), "TARGET_FIXTURE_UNKNOWN", "root_source.curriculum_metadata.records[target_binding]", "Use one of the six packaged V1 example fixture IDs or bind a canonical payload file."),
        (("payload digest",), "TARGET_PAYLOAD_DIGEST_MISMATCH", "root_source.curriculum_metadata.records[target_binding]", "Update payload_sha256 from the exact canonical payload bytes."),
        (("payload path",), "TARGET_PAYLOAD_PATH_INVALID", "root_source.curriculum_metadata.records[target_binding]", "Use a confined relative lowercase .json path without symlinks."),
        (("native payload",), "TARGET_PAYLOAD_INVALID", "root_source.curriculum_metadata.records[target_binding]", "Bind the exact canonical JSON payload for the declared target adapter."),
        (("source does not permit a cli seed override",), "SEED_OVERRIDE_FORBIDDEN", "root_source.seed_policy", "Remove --seed or explicitly allow the override in the source seed policy."),
    )
    for fragments, selected_code, selected_path, selected_correction in rules:
        if any(fragment in normalized for fragment in fragments):
            code = selected_code
            semantic_path = selected_path
            correction = selected_correction
            break
    span = _source_span(source_path, semantic_path, message=message)
    return ScenarioSourceDiagnosticV1(
        code=code,
        severity=ScenarioValidationSeverityV1.ERROR.value,
        source_file=str(source_path),
        span=span,
        semantic_path=semantic_path,
        explanation=message,
        correction=correction,
        blocks_execution=True,
    )


def _source_span(
    source_path: Path,
    semantic_path: str,
    *,
    message: str = "",
) -> ScenarioSourceSpanV1:
    location = _TOML_LOCATION.search(message)
    if location is not None:
        line = int(location.group("line"))
        column = int(location.group("column"))
        return ScenarioSourceSpanV1(line, column, line, column + 1)
    try:
        lines = source_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return ScenarioSourceSpanV1(1, 1, 1, 2)
    clues: list[str] = []
    record_match = re.search(r"records\[([^\]]+)\]", semantic_path)
    if record_match is None:
        record_match = re.search(r"resolved_definitions\[([^\]]+)\]", semantic_path)
    field_match = re.search(r"fields\[([^\]]+)\]", semantic_path)
    if field_match:
        clues.append(str(field_match.group(1)))
    if record_match:
        record_name = str(record_match.group(1))
        clues.extend((record_name, record_name.rsplit(":", 1)[-1]))
    for token in (
        "imports",
        "curriculum_metadata",
        "seed_policy",
        "metadata",
        "transition_rules",
        "venues",
        "strategy",
    ):
        if token in semantic_path:
            clues.append(token)
    normalized_message = message.lower()
    if "inheritance" in normalized_message:
        clues.append("extends")
    if "expression or code evaluator" in normalized_message:
        clues.extend(("EVAL_V1", "record_type"))
    if "cannot import, reflect, or call code" in normalized_message:
        clues.extend(("python_symbol", "module_import"))
    duplicate = re.search(r"duplicate scenario definition: [^:]+:([^ ]+)", message)
    if duplicate is not None:
        clues.append(duplicate.group(1))
    for clue in clues:
        for ordinal, line in enumerate(lines, start=1):
            column = line.find(clue)
            if column >= 0:
                return ScenarioSourceSpanV1(
                    ordinal,
                    column + 1,
                    ordinal,
                    column + len(clue) + 1,
                )
    return ScenarioSourceSpanV1(1, 1, 1, 2)


def _require_artifact(result: ScenarioAuthoringResultV1) -> CompiledScenarioArtifactV1:
    if result.artifact is None:
        raise ValueError("scenario source did not compile into an artifact")
    return result.artifact


def _require_eligible_artifact(
    result: ScenarioAuthoringResultV1,
) -> CompiledScenarioArtifactV1:
    artifact = _require_artifact(result)
    if not result.passed:
        raise ValueError("scenario source did not pass complete static validation")
    return artifact


def _section_records(
    root: Mapping[str, object],
    name: str,
) -> list[object]:
    section = root.get(name, {})
    if not isinstance(section, Mapping) or type(section.get("records")) is not list:
        raise TypeError(f"compiled scenario section {name!r} is malformed")
    return list(section["records"])


def _record_field_map(record: object) -> dict[str, object]:
    if not isinstance(record, Mapping) or type(record.get("fields")) is not list:
        return {}
    result: dict[str, object] = {}
    for field in record["fields"]:
        if not isinstance(field, Mapping) or type(field.get("name")) is not str:
            continue
        values = [value for key, value in field.items() if key != "name"]
        if len(values) == 1:
            result[str(field["name"])] = values[0]
    return result


def _field_strings(record: object, name: str) -> tuple[str, ...]:
    value = _record_field_map(record).get(name, ())
    if type(value) is str:
        return (value,)
    if type(value) is list and all(type(item) is str for item in value):
        return tuple(value)
    return ()


def _native_target_summary(
    target_kind: ScenarioTargetKindV1,
    native: object,
) -> dict[str, object]:
    if target_kind is ScenarioTargetKindV1.FULL_DAY_PLAN_V1:
        return {
            "calendar_end_us": native.calendar.end_time_us,
            "calendar_id": native.calendar.calendar_id,
            "plan_id": native.plan_id,
            "session_phases": [item.phase_id for item in native.calendar.phases],
        }
    if target_kind is ScenarioTargetKindV1.MARKET_SCENARIO_V1:
        return {
            "duration_seconds": native.duration_seconds,
            "initial_depth": native.initial_depth,
            "initial_mid_ticks": native.initial_mid_ticks,
            "name": native.name,
            "regime": native.regime.value,
        }
    if target_kind is ScenarioTargetKindV1.HIDDEN_LIQUIDITY_RECORDING_V1:
        return {
            "command_count": len(native.commands),
            "completed_time_us": native.completed_time_us,
            "recording_sha256": native.sha256(),
        }
    if target_kind is ScenarioTargetKindV1.MULTIVENUE_RECORDING_V1:
        return {
            "command_count": len(native.commands),
            "completed_time_us": native.completed_time_us,
            "recording_sha256": native.sha256(),
            "venue_ids": [item["venue_id"] for item in native.venue_configs],
        }
    if target_kind is ScenarioTargetKindV1.HISTORICAL_LESSON_V1:
        return {
            "evidence_inventory": native.evidence_inventory(),
            "fixture_id": native.source.fixture_id,
            "lesson_id": native.lesson_id,
            "mode": native.mode.value,
            "source_locator": native.source.source_locator,
        }
    raise AssertionError(f"unhandled scenario target: {target_kind.value}")


def _native_schedule_summary(
    target_kind: ScenarioTargetKindV1,
    native: object,
) -> object:
    if target_kind is ScenarioTargetKindV1.FULL_DAY_PLAN_V1:
        return {
            "boundary_operations": len(native.calendar.boundary_operations),
            "participant_schedule": len(native.participant_schedule),
            "scheduled_events": len(native.scheduled_events),
        }
    if target_kind in {
        ScenarioTargetKindV1.HIDDEN_LIQUIDITY_RECORDING_V1,
        ScenarioTargetKindV1.MULTIVENUE_RECORDING_V1,
    }:
        return [
            {
                "command_type": item.command_type,
                "simulation_time_us": item.simulation_time_us,
            }
            for item in native.commands
        ]
    if target_kind is ScenarioTargetKindV1.HISTORICAL_LESSON_V1:
        return {"time_window": native.time_window.as_dict()}
    return {"duration_seconds": native.duration_seconds}


def _stochastic_components(
    target_kind: ScenarioTargetKindV1,
    native: object,
) -> list[str]:
    if target_kind is ScenarioTargetKindV1.FULL_DAY_PLAN_V1:
        return [
            "FULL_DAY_SEEDED_COMPONENTS"
            if native.seed_policy.substreams
            else "NO_DECLARED_SUBSTREAMS"
        ]
    if target_kind is ScenarioTargetKindV1.MARKET_SCENARIO_V1:
        return ["SEEDED_MARKET_ORDER_FLOW"]
    if target_kind is ScenarioTargetKindV1.MULTIVENUE_RECORDING_V1:
        return ["RECORDED_ROUTING_LATENCY_DRAWS"]
    if target_kind is ScenarioTargetKindV1.HISTORICAL_LESSON_V1:
        return (
            ["SEEDED_HISTORICAL_RECONSTRUCTION"]
            if native.mode.value == "RECONSTRUCTION"
            else []
        )
    return []


def _target_hidden_boundary(target_kind: ScenarioTargetKindV1) -> list[str]:
    if target_kind in {
        ScenarioTargetKindV1.HIDDEN_LIQUIDITY_RECORDING_V1,
        ScenarioTargetKindV1.MULTIVENUE_RECORDING_V1,
    }:
        return [
            "GROUND_TRUTH_EXCHANGE_STATE",
            "HIDDEN_ORDER_RESERVE",
            "FUTURE_EVENTS",
        ]
    if target_kind is ScenarioTargetKindV1.HISTORICAL_LESSON_V1:
        return ["POST_SESSION_OUTCOME_UNTIL_REVEAL", "FUTURE_EVENTS"]
    return ["FUTURE_EVENTS", "SIMULATOR_PRIVATE_STATE"]


def _termination_summary(
    target_kind: ScenarioTargetKindV1,
    native: object,
    root: Mapping[str, object],
) -> dict[str, object]:
    declared = _section_records(root, "session_schedule")
    if target_kind is ScenarioTargetKindV1.FULL_DAY_PLAN_V1:
        native_condition: object = {
            "calendar_end_us": native.calendar.end_time_us,
            "terminal_state": "CLOSED",
        }
    elif target_kind is ScenarioTargetKindV1.MARKET_SCENARIO_V1:
        native_condition = {"duration_seconds": native.duration_seconds}
    elif target_kind in {
        ScenarioTargetKindV1.HIDDEN_LIQUIDITY_RECORDING_V1,
        ScenarioTargetKindV1.MULTIVENUE_RECORDING_V1,
    }:
        native_condition = {
            "completed_time_us": native.completed_time_us,
            "terminal_command": native.commands[-1].command_type if native.commands else None,
        }
    else:
        native_condition = {
            "lesson_end_us": native.time_window.end_us,
            "session_initial_phase": "READY",
        }
    return {"declared": declared, "native": native_condition}


def _unit_inventory(value: object) -> list[dict[str, object]]:
    units = {
        "duration_us": "MICROSECONDS",
        "latency_us": "MICROSECONDS",
        "price_ticks": "PRICE_TICKS",
        "quantity_shares": "SHARES",
        "rate_per_second": "PER_SECOND",
        "volume_multiplier": "EXACT_RATIONAL",
        "fixed_point": "EXACT_FIXED_POINT",
        "probability_weight": "INTEGER_WEIGHT",
    }
    result: list[dict[str, object]] = []

    def visit(node: object, path: str) -> None:
        if isinstance(node, Mapping):
            for key, child in sorted(node.items(), key=lambda item: str(item[0])):
                child_path = f"{path}.{key}" if path else str(key)
                if str(key) in units:
                    result.append(
                        {
                            "path": child_path,
                            "unit": units[str(key)],
                            "value": child,
                        }
                    )
                visit(child, child_path)
        elif type(node) is list:
            for index, child in enumerate(node):
                visit(child, f"{path}[{index}]")

    visit(value, "")
    return result


def _semantic_diff(
    left: object,
    right: object,
    path: str,
    changes: list[dict[str, object]],
) -> None:
    if type(left) is not type(right):
        changes.append({"left": left, "path": path, "right": right})
        return
    if isinstance(left, Mapping):
        for key in sorted(set(left) | set(right), key=str):
            child_path = f"{path}.{key}"
            if key not in left:
                changes.append({"left": None, "path": child_path, "right": right[key]})
            elif key not in right:
                changes.append({"left": left[key], "path": child_path, "right": None})
            else:
                _semantic_diff(left[key], right[key], child_path, changes)
        return
    if type(left) is list:
        maximum = max(len(left), len(right))
        for index in range(maximum):
            child_path = f"{path}[{index}]"
            if index >= len(left):
                changes.append({"left": None, "path": child_path, "right": right[index]})
            elif index >= len(right):
                changes.append({"left": left[index], "path": child_path, "right": None})
            else:
                _semantic_diff(left[index], right[index], child_path, changes)
        return
    if left != right:
        changes.append({"left": left, "path": path, "right": right})


def _document_digest_map(
    artifact: CompiledScenarioArtifactV1,
) -> dict[str, str]:
    documents = artifact.provenance["import_bundle"]["documents"]
    if type(documents) is not list:
        raise TypeError("compiled source provenance documents are malformed")
    return {
        str(item["logical_path"]): str(item["raw_sha256"])
        for item in documents
        if isinstance(item, Mapping)
    }


def _canonical_new_output(path: Path) -> Path:
    candidate = Path(path)
    parent = candidate.parent.resolve(strict=True)
    if not parent.is_dir():
        raise ValueError("scenario artifact output parent must be a directory")
    target = parent / candidate.name
    if target.exists() or target.is_symlink():
        raise FileExistsError(f"scenario artifact output already exists: {target}")
    return target


def _runtime_summary(
    artifact: CompiledScenarioArtifactV1,
    runtime: object,
) -> dict[str, object]:
    summary: dict[str, object] = {
        "runtime_type": type(runtime).__name__,
        "target_kind": artifact.target_kind.value,
    }
    if hasattr(runtime, "canonical_state_bytes"):
        raw = runtime.canonical_state_bytes()
        summary["final_state_sha256"] = hashlib.sha256(raw).hexdigest()
        summary["event_count"] = len(getattr(runtime, "events", ()))
    elif hasattr(runtime, "replay_json_lines"):
        replay = runtime.replay_json_lines()
        summary["replay_sha256"] = hashlib.sha256(replay.encode("utf-8")).hexdigest()
    elif hasattr(runtime, "passed"):
        summary["replay_passed"] = bool(runtime.passed)
        owner = getattr(runtime, "venue", None) or getattr(runtime, "coordinator", None)
        if owner is not None and hasattr(owner, "state_sha256"):
            summary["final_state_sha256"] = owner.state_sha256()
    elif hasattr(runtime, "lesson"):
        summary["lesson_id"] = runtime.lesson.lesson_id
        summary["phase"] = runtime.phase.value
    return summary


_KNOWN_INVALIDATION_CONDITIONS = (
    (
        "SOURCE_PARSE_OR_IMPORT_REFUSAL",
        "The source is not strict TOML or its confined import graph is invalid.",
        "Fix the stable source diagnostic before compilation.",
    ),
    (
        "TARGET_BINDING_OR_TAG_MISMATCH",
        "The native payload binding, digest, target tag, or adapter contract differs.",
        "Bind canonical bytes for the exact declared closed target.",
    ),
    (
        "STATIC_VALIDATION_BLOCKED",
        "Any required ERROR or required NOT_PROVABLE_STATICALLY finding blocks finalization.",
        "Apply the finding correction or remove the unsupported required claim.",
    ),
    (
        "ARTIFACT_INELIGIBLE_OR_TAMPERED",
        "Runtime refuses an unfinalized, noncanonical, mismatched, or tampered artifact.",
        "Run only the exact persisted validation-finalized artifact.",
    ),
    (
        "TARGET_REPLAY_OR_SEED_MISMATCH",
        "Exact replay mismatch or a full-day native/source seed mismatch refuses runtime.",
        "Preserve recorded bytes and align the selected root seed.",
    ),
)


def _pack_binding(value: str) -> tuple[str, Path]:
    name, separator, raw_path = value.partition("=")
    if not separator or _PACK_NAME.fullmatch(name) is None:
        raise argparse.ArgumentTypeError("pack must use NAMESPACE=/absolute/path")
    path = Path(raw_path)
    if not path.is_absolute():
        raise argparse.ArgumentTypeError("pack root must be an absolute path")
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise argparse.ArgumentTypeError("pack root cannot be resolved") from error
    if resolved != path or not resolved.is_dir():
        raise argparse.ArgumentTypeError("pack root must be a resolved directory")
    return name, resolved


def _pack_mapping(values: list[tuple[str, Path]]) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for name, path in values:
        if name in result:
            raise ValueError(f"duplicate activated pack namespace: {name}")
        result[name] = path
    return result


def _add_source_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("source", type=Path, help="scenario source TOML path")
    parser.add_argument(
        "--pack",
        action="append",
        default=[],
        type=_pack_binding,
        metavar="NAMESPACE=/ABSOLUTE/PATH",
        help="activate one confined definition-pack namespace",
    )
    parser.add_argument("--seed", type=int, help="allowed explicit root-seed override")


def _configure_scenario_source(parser: argparse.ArgumentParser) -> None:
    actions = parser.add_subparsers(dest="scenario_source_action", required=True)
    lint = actions.add_parser("lint", help="parse, compile, and statically validate")
    _add_source_arguments(lint)
    compile_parser = actions.add_parser(
        "compile",
        help="persist one validation-finalized immutable artifact",
    )
    _add_source_arguments(compile_parser)
    compile_parser.add_argument("--output", required=True, type=Path)
    explain = actions.add_parser(
        "explain",
        help="answer the fixed authoring questions without execution",
    )
    _add_source_arguments(explain)
    diff = actions.add_parser(
        "diff",
        help="compare materialized semantic paths and source-only changes",
    )
    diff.add_argument("left", type=Path)
    diff.add_argument("right", type=Path)
    run = actions.add_parser(
        "run",
        help="persist, replay, and dispatch one finalized compiled artifact",
    )
    _add_source_arguments(run)
    run.add_argument("--artifact", required=True, type=Path)


def _configure_scenario_language_demo(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--source", required=True, type=Path)


def _print_json(value: object) -> None:
    print(json.dumps(value, allow_nan=False, sort_keys=True, separators=(",", ":")))


def _print_diagnostics(result: ScenarioAuthoringResultV1) -> None:
    for diagnostic in result.diagnostics:
        _print_json({"diagnostic": diagnostic.as_dict()})


def _inspect_args(args: argparse.Namespace) -> ScenarioAuthoringResultV1:
    try:
        packs = _pack_mapping(args.pack)
    except ValueError as error:
        path = Path(args.source).absolute()
        return ScenarioAuthoringResultV1(
            path,
            None,
            None,
            None,
            (_exception_diagnostic(path, error),),
        )
    return inspect_scenario_source(
        args.source,
        activated_pack_namespaces=packs,
        cli_seed_override=args.seed,
    )


def _handle_scenario_source(args: argparse.Namespace) -> int:
    action = args.scenario_source_action
    if action == "diff":
        left = inspect_scenario_source(args.left)
        right = inspect_scenario_source(args.right)
        _print_diagnostics(left)
        _print_diagnostics(right)
        if not left.passed or not right.passed:
            _print_json({"action": "diff", "status": "FAIL"})
            return 1
        _print_json(diff_scenario_sources(left, right))
        return 0
    result = _inspect_args(args)
    _print_diagnostics(result)
    if action == "lint":
        _print_json(
            {
                "action": "lint",
                "diagnostic_count": len(result.diagnostics),
                "source": str(result.source_path),
                "status": "PASS" if result.passed else "FAIL",
            }
        )
        return 0 if result.passed else 1
    if not result.passed:
        _print_json({"action": action, "status": "FAIL"})
        return 1
    artifact = _require_eligible_artifact(result)
    if action == "compile":
        output = persist_compiled_artifact(artifact, args.output)
        _print_json(
            {
                "action": "compile",
                "artifact": str(output),
                "compiled_artifact_digest": artifact.compiled_artifact_digest,
                "status": "PASS",
            }
        )
        return 0
    if action == "explain":
        for section_name, payload in explain_scenario_source(result):
            print(f"=== {section_name} ===")
            _print_json(payload)
        return 0
    if action == "run":
        output = persist_compiled_artifact(artifact, args.artifact)
        restored = replay_compiled_scenario(output.read_bytes())
        try:
            runtime = run_compiled_scenario(restored)
        except (ScenarioExecutionRefused, TypeError, ValueError, RuntimeError) as error:
            _print_json(
                {
                    "action": "run",
                    "artifact": str(output),
                    "reason": str(error),
                    "status": "REFUSED",
                }
            )
            return 1
        _print_json(
            {
                "action": "run",
                "artifact": str(output),
                "compiled_artifact_digest": restored.compiled_artifact_digest,
                "runtime": _runtime_summary(restored, runtime),
                "status": "PASS",
            }
        )
        return 0
    raise AssertionError(f"unhandled scenario-source action: {action}")


def _handle_scenario_language_demo(args: argparse.Namespace) -> int:
    result = inspect_scenario_source(args.source)
    _print_diagnostics(result)
    if not result.passed:
        _print_json({"command": "scenario-language-demo", "status": "FAIL"})
        return 1
    artifact = _require_eligible_artifact(result)
    _print_json(
        {
            "command": "scenario-language-demo",
            "compiled_artifact_digest": artifact.compiled_artifact_digest,
            "explain_sections": list(SCENARIO_EXPLAIN_SECTION_NAMES_V1),
            "source": str(result.source_path),
            "status": "PASS",
            "target_kind": artifact.target_kind.value,
        }
    )
    return 0


SCENARIO_SOURCE_COMMAND_MODULE = CommandModule(
    module_id="SCENARIO_SOURCE_AUTHORING",
    commands=(
        CommandSpec(
            command_id="SCENARIO_SOURCE",
            name="scenario-source",
            help="lint, compile, explain, diff, or run a declarative scenario source",
            handler=_handle_scenario_source,
            configure=_configure_scenario_source,
        ),
        CommandSpec(
            command_id="SCENARIO_LANGUAGE_DEMO",
            name="scenario-language-demo",
            help="exercise execution-free scenario source compilation and explanation",
            handler=_handle_scenario_language_demo,
            configure=_configure_scenario_language_demo,
        ),
    ),
)


__all__ = [
    "SCENARIO_EXAMPLE_FIXTURE_TARGETS_V1",
    "SCENARIO_EXPLAIN_SECTION_NAMES_V1",
    "SCENARIO_NATIVE_PAYLOAD_MAX_BYTES_V1",
    "SCENARIO_SOURCE_COMMAND_MODULE",
    "SCENARIO_TARGET_BINDING_LOGICAL_NAME_V1",
    "SCENARIO_TARGET_BINDING_RECORD_TYPE_V1",
    "VALID_SCENARIO_EXAMPLE_FILENAMES_V1",
    "ScenarioAuthoringResultV1",
    "ScenarioSourceDiagnosticV1",
    "ScenarioSourceSpanV1",
    "diff_scenario_sources",
    "explain_scenario_source",
    "inspect_scenario_source",
    "persist_compiled_artifact",
]
