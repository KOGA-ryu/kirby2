"""Data-only worker execution shared by direct and fresh-process backends.

The stdio boundary in this module accepts one canonical JSON object and emits one
canonical JSON object. It never resolves an executable name from the request and
never accepts Python source, pickle, a shell command, or a dynamic import target.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import importlib.util
import json
import math
import platform
import sys
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final, TextIO

from kirby2.orchestration.models import DigestReferenceV1, LogicalWorkUnit, WorkKindV1
from kirby2.orchestration.protocol import (
    InlineArtifactMediaTypeV1,
    InlineArtifactV1,
    ProtocolDiagnosticV1,
    RuntimeAuditResultV1,
    RuntimeAuditStatusV1,
    WorkRequestV1,
    WorkerCompatibilityV1,
    WorkerResultManifestV1,
    WorkerResultStatusV1,
    WorkerResultV1,
)
from kirby2.scenarios import get_scenario_definition, run_market_scenario
from kirby2.simulation import LiquidityPreset, VolumePreset


ORDER_BOOK_INVARIANTS_AUDIT_ID_V1: Final = "ORDER_BOOK_INVARIANTS_V1"
DETERMINISTIC_REPLAY_AUDIT_ID_V1: Final = "DETERMINISTIC_REPLAY_V1"
COMPLETE_RUN_RUNTIME_AUDIT_IDS_V1: Final = (
    DETERMINISTIC_REPLAY_AUDIT_ID_V1,
    ORDER_BOOK_INVARIANTS_AUDIT_ID_V1,
)
COMPLETE_RUN_ARTIFACT_NAMES_V1: Final = ("metrics.json", "replay.jsonl")
DATA_ONLY_STDIO_MAX_BYTES_V1: Final = 16 * 1024 * 1024

_COMPLETE_RUN_CONFIGURATION_FIELDS_V1 = frozenset(
    {
        "duration_seconds",
        "liquidity",
        "relative_volume",
        "scenario_name",
    }
)
_IMPLEMENTATION_SOURCE_IDENTITY_NAME_V1 = "kirby2-implementation-source"
_PYTHON_RUNTIME_IDENTITY_NAME_V1 = "python-runtime"
_DEPENDENCY_IDENTITY_NAME_V1 = "installed-python-dependencies"
_COMPILER_IDENTITY_NAME_V1 = "python-bytecode-compiler"
_METRICS_SCHEMA_ID_V1 = "KIRBY2_COMPLETE_RUN_METRICS_V1"
_REPLAY_SCHEMA_ID_V1 = "KIRBY2_COMPLETE_RUN_REPLAY_JSONL_V1"
_COMPLETE_RUN_CAPABILITY_ID_V1 = "KIRBY2_COMPLETE_RUN_WORKER_V1"
_DATA_ONLY_STDIO_CAPABILITY_ID_V1 = "KIRBY2_CANONICAL_JSON_STDIO_V1"


class WorkerExecutionRefused(ValueError):
    """A stable fail-closed refusal before scientific execution begins."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


class WorkerDeterminismFailure(RuntimeError):
    """Duplicate execution produced different scientific artifact bytes."""

    def __init__(
        self,
        first_artifact_set_sha256: str,
        second_artifact_set_sha256: str,
    ) -> None:
        self.first_artifact_set_sha256 = first_artifact_set_sha256
        self.second_artifact_set_sha256 = second_artifact_set_sha256
        super().__init__(
            "duplicate COMPLETE_RUN execution produced different artifact bytes"
        )


class WorkerInvariantFailure(RuntimeError):
    """A complete-run execution failed the order-book invariant audit."""

    def __init__(self, execution_index: int) -> None:
        self.execution_index = execution_index
        super().__init__("COMPLETE_RUN failed order-book invariants")


@dataclass(frozen=True, slots=True)
class CompleteRunExecutionV1:
    """Exact artifact bytes and audit evidence produced by a complete run."""

    artifacts: tuple[tuple[str, bytes], ...]
    audit_evidence_sha256: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        if tuple(name for name, _raw in self.artifacts) != COMPLETE_RUN_ARTIFACT_NAMES_V1:
            raise ValueError("complete-run artifact inventory is not canonical")
        if any(type(raw) is not bytes for _name, raw in self.artifacts):
            raise TypeError("complete-run artifacts must contain exact bytes")
        if tuple(name for name, _digest in self.audit_evidence_sha256) != (
            COMPLETE_RUN_RUNTIME_AUDIT_IDS_V1
        ):
            raise ValueError("complete-run runtime-audit inventory is not canonical")
        if any(not _is_sha256(digest) for _name, digest in self.audit_evidence_sha256):
            raise ValueError("complete-run runtime-audit evidence digest is invalid")

    def artifact_bytes(self, name: str) -> bytes:
        return next(raw for candidate, raw in self.artifacts if candidate == name)


JsonObjectHandler = Callable[[dict[str, object]], dict[str, object]]


def run_data_only_stdio_worker(
    handler: JsonObjectHandler,
    *,
    input_stream: TextIO | None = None,
    output_stream: TextIO | None = None,
    max_input_bytes: int = DATA_ONLY_STDIO_MAX_BYTES_V1,
) -> None:
    """Run one statically supplied handler across a canonical JSON stdio frame.

    One optional final LF is accepted on input so both existing audit-lab callers
    and command-line pipes use the same framing. Output always contains exactly
    one final LF.
    """

    if not callable(handler):
        raise TypeError("data-only worker handler must be callable")
    if type(max_input_bytes) is not int or max_input_bytes <= 0:
        raise ValueError("data-only worker input limit must be positive")

    source = sys.stdin if input_stream is None else input_stream
    destination = sys.stdout if output_stream is None else output_stream
    raw = source.read(max_input_bytes + 1)
    if type(raw) is not str:
        raise TypeError("data-only worker stdin must provide text")
    try:
        raw_bytes = raw.encode("ascii")
    except UnicodeEncodeError as error:
        raise ValueError("data-only worker input must be canonical ASCII JSON") from error
    if not raw_bytes or len(raw_bytes) > max_input_bytes:
        raise ValueError("data-only worker input is empty or exceeds its byte limit")

    canonical_input = raw[:-1] if raw.endswith("\n") else raw
    if not canonical_input or "\n" in canonical_input or "\r" in canonical_input:
        raise ValueError("data-only worker input must contain exactly one JSON frame")
    try:
        payload = json.loads(
            canonical_input,
            object_pairs_hook=_pairs_without_duplicates,
            parse_constant=_reject_json_constant,
        )
    except (json.JSONDecodeError, RecursionError, ValueError) as error:
        raise ValueError("data-only worker input is not strict JSON") from error
    try:
        _validate_data_only_json(payload)
    except RecursionError as error:
        raise ValueError("data-only worker input exceeds its nesting limit") from error
    if type(payload) is not dict:
        raise ValueError("data-only worker input must be a JSON object")
    if _canonical_json(payload) != canonical_input:
        raise ValueError("data-only worker input is not canonical JSON")

    response = handler(payload)
    if type(response) is not dict:
        raise TypeError("data-only worker handler must return a JSON object")
    _validate_data_only_json(response)
    destination.write(_canonical_json(response) + "\n")


def complete_run_expected_output_identities() -> tuple[DigestReferenceV1, ...]:
    """Return fixed content identities for the two COMPLETE_RUN output contracts."""

    return (
        _digest_reference(
            "metrics.json",
            {
                "artifact_id": "metrics.json",
                "media_type": InlineArtifactMediaTypeV1.CANONICAL_JSON.value,
                "schema_id": _METRICS_SCHEMA_ID_V1,
                "schema_version": 1,
            },
        ),
        _digest_reference(
            "replay.jsonl",
            {
                "artifact_id": "replay.jsonl",
                "media_type": InlineArtifactMediaTypeV1.CANONICAL_JSONL.value,
                "schema_id": _REPLAY_SCHEMA_ID_V1,
                "schema_version": 1,
            },
        ),
    )


def complete_run_runtime_audit_identities() -> tuple[DigestReferenceV1, ...]:
    """Return the exact, canonical runtime-audit contracts supported by WO38-B."""

    return (
        _digest_reference(
            DETERMINISTIC_REPLAY_AUDIT_ID_V1,
            {
                "artifact_ids": list(COMPLETE_RUN_ARTIFACT_NAMES_V1),
                "audit_id": DETERMINISTIC_REPLAY_AUDIT_ID_V1,
                "comparison": "byte_exact_duplicate_execution",
                "execution_count": 2,
                "schema_version": 1,
            },
        ),
        _digest_reference(
            ORDER_BOOK_INVARIANTS_AUDIT_ID_V1,
            {
                "audit_id": ORDER_BOOK_INVARIANTS_AUDIT_ID_V1,
                "check": "post_run_order_book_invariants",
                "execution_count": 2,
                "schema_version": 1,
            },
        ),
    )


def measure_local_worker_compatibility() -> WorkerCompatibilityV1:
    """Measure the exact local implementation and its closed worker contracts."""

    engine, runtime, dependency, compiler = _measure_executable_identity_references()
    return WorkerCompatibilityV1(
        engine_identity=engine,
        runtime_identity=runtime,
        dependency_identity=dependency,
        compiler_identity=compiler,
        schemas=_complete_run_schema_identities(),
        capabilities=_complete_run_capability_identities(),
    )


def execute_work_request(
    request: WorkRequestV1,
    worker_compatibility: WorkerCompatibilityV1 | None = None,
) -> WorkerResultV1:
    """Validate and execute one typed WO38-B request with a static adapter."""

    if type(request) is not WorkRequestV1:
        raise TypeError("worker execution requires WorkRequestV1")
    if (
        worker_compatibility is not None
        and type(worker_compatibility) is not WorkerCompatibilityV1
    ):
        raise TypeError("worker compatibility must be WorkerCompatibilityV1")
    compatibility = measure_local_worker_compatibility()

    if compatibility != request.required_compatibility:
        return _pre_audit_result(
            request,
            compatibility,
            WorkerResultStatusV1.COMPATIBILITY_REFUSED,
            ProtocolDiagnosticV1(
                code="EXACT_COMPATIBILITY_MISMATCH",
                summary="Worker identities differ from the requested identities.",
                details={
                    "actual_compatibility_sha256": (
                        compatibility.compatibility_sha256
                    ),
                    "required_compatibility_sha256": (
                        request.required_compatibility.compatibility_sha256
                    ),
                },
            ),
        )

    logical_unit = request.logical_work_unit
    if logical_unit.work_kind is not WorkKindV1.COMPLETE_RUN:
        return _pre_audit_result(
            request,
            compatibility,
            WorkerResultStatusV1.WORK_KIND_REFUSED,
            ProtocolDiagnosticV1(
                code="UNSUPPORTED_WORK_KIND",
                summary="WO38-B workers execute COMPLETE_RUN work only.",
                details={"work_kind": logical_unit.work_kind.value},
            ),
        )

    required_audits = complete_run_runtime_audit_identities()
    if request.required_runtime_audits != required_audits:
        return _pre_audit_result(
            request,
            compatibility,
            WorkerResultStatusV1.EXECUTION_FAILED,
            ProtocolDiagnosticV1(
                code="RUNTIME_AUDIT_CONTRACT_MISMATCH",
                summary="The request does not require the fixed COMPLETE_RUN audits.",
                details={
                    "actual_audit_sha256": [
                        item.sha256 for item in request.required_runtime_audits
                    ],
                    "required_audit_sha256": [
                        item.sha256 for item in required_audits
                    ],
                },
            ),
        )

    expected_outputs = complete_run_expected_output_identities()
    if logical_unit.expected_outputs != expected_outputs:
        return _pre_audit_result(
            request,
            compatibility,
            WorkerResultStatusV1.EXECUTION_FAILED,
            ProtocolDiagnosticV1(
                code="EXPECTED_OUTPUT_CONTRACT_MISMATCH",
                summary="The logical unit declares a different output contract.",
                details={
                    "actual_output_sha256": [
                        item.sha256 for item in logical_unit.expected_outputs
                    ],
                    "required_output_sha256": [
                        item.sha256 for item in expected_outputs
                    ],
                },
            ),
        )

    try:
        execution = execute_complete_run(logical_unit)
        artifacts = _inline_complete_run_artifacts(execution)
    except WorkerDeterminismFailure as error:
        return _determinism_failed_result(request, compatibility, error)
    except WorkerInvariantFailure as error:
        return _invariant_failed_result(request, compatibility, error)
    except WorkerExecutionRefused as error:
        return _pre_audit_result(
            request,
            compatibility,
            WorkerResultStatusV1.EXECUTION_FAILED,
            ProtocolDiagnosticV1(
                code=error.code,
                summary=error.detail,
                details={"logical_work_unit_id": logical_unit.logical_work_unit_id},
            ),
        )
    except Exception as error:
        return _pre_audit_result(
            request,
            compatibility,
            WorkerResultStatusV1.EXECUTION_FAILED,
            ProtocolDiagnosticV1(
                code="COMPLETE_RUN_EXECUTION_FAILED",
                summary="The static COMPLETE_RUN adapter could not produce artifacts.",
                details={
                    "error_type": type(error).__name__,
                    "logical_work_unit_id": logical_unit.logical_work_unit_id,
                },
            ),
        )

    evidence_by_audit = dict(execution.audit_evidence_sha256)
    runtime_audits = tuple(
        RuntimeAuditResultV1(
            audit_identity=audit_identity,
            status=RuntimeAuditStatusV1.PASSED,
            evidence_digests=(
                DigestReferenceV1(
                    name=f"{audit_identity.name}:evidence",
                    sha256=evidence_by_audit[audit_identity.name],
                ),
            ),
            diagnostics=(),
        )
        for audit_identity in required_audits
    )
    manifest = WorkerResultManifestV1.for_success(
        request=request,
        worker_compatibility=compatibility,
        artifacts=artifacts,
        runtime_audit_results=runtime_audits,
    )
    return WorkerResultV1(
        request=request,
        worker_compatibility=compatibility,
        status=WorkerResultStatusV1.SUCCEEDED,
        manifest=manifest,
        artifacts=artifacts,
        runtime_audit_results=runtime_audits,
        diagnostics=(),
    )


def handle_work_request(payload: dict[str, object]) -> dict[str, object]:
    """Decode one protocol object and return its detached result object."""

    request = WorkRequestV1.from_dict(payload)
    return execute_work_request(request).as_dict()


def main() -> None:
    run_data_only_stdio_worker(handle_work_request)


def execute_complete_run(logical_unit: LogicalWorkUnit) -> CompleteRunExecutionV1:
    """Execute and duplicate-check one statically selected complete market run."""

    if type(logical_unit) is not LogicalWorkUnit:
        raise TypeError("complete-run execution requires LogicalWorkUnit")
    if logical_unit.work_kind is not WorkKindV1.COMPLETE_RUN:
        raise WorkerExecutionRefused(
            "UNSUPPORTED_WORK_KIND",
            f"WO38-B implements COMPLETE_RUN, not {logical_unit.work_kind.value}",
        )
    configuration = _complete_run_configuration(logical_unit.configuration)
    first = _execute_complete_run_once(logical_unit.seed, configuration, 1)
    second = _execute_complete_run_once(logical_unit.seed, configuration, 2)
    first_set_sha256 = _artifact_set_sha256(first)
    second_set_sha256 = _artifact_set_sha256(second)
    if first != second:
        raise WorkerDeterminismFailure(first_set_sha256, second_set_sha256)

    evidence = (
        (
            DETERMINISTIC_REPLAY_AUDIT_ID_V1,
            _audit_evidence_sha256(
                {
                    "artifact_set_sha256": first_set_sha256,
                    "audit_id": DETERMINISTIC_REPLAY_AUDIT_ID_V1,
                    "exact_match": True,
                    "execution_count": 2,
                    "logical_work_unit_id": logical_unit.logical_work_unit_id,
                    "status": RuntimeAuditStatusV1.PASSED.value,
                }
            ),
        ),
        (
            ORDER_BOOK_INVARIANTS_AUDIT_ID_V1,
            _audit_evidence_sha256(
                {
                    "artifact_set_sha256": first_set_sha256,
                    "audit_id": ORDER_BOOK_INVARIANTS_AUDIT_ID_V1,
                    "checked_execution_count": 2,
                    "logical_work_unit_id": logical_unit.logical_work_unit_id,
                    "status": RuntimeAuditStatusV1.PASSED.value,
                }
            ),
        ),
    )
    return CompleteRunExecutionV1(
        artifacts=first,
        audit_evidence_sha256=evidence,
    )


def _execute_complete_run_once(
    seed: int,
    configuration: Mapping[str, object],
    execution_index: int,
) -> tuple[tuple[str, bytes], ...]:
    definition = get_scenario_definition(str(configuration["scenario_name"]))
    run = run_market_scenario(
        definition,
        seed=seed,
        seconds=int(configuration["duration_seconds"]),
        relative_volume=VolumePreset.parse(str(configuration["relative_volume"])),
        liquidity=LiquidityPreset.parse(str(configuration["liquidity"])),
    )
    try:
        run.simulation.book.assert_invariants()
    except (AssertionError, RuntimeError, ValueError) as error:
        raise WorkerInvariantFailure(execution_index) from error
    metrics = _canonical_json_bytes(run.metrics())
    replay = (run.replay_json_lines() + "\n").encode("utf-8")
    return (("metrics.json", metrics), ("replay.jsonl", replay))


def _complete_run_configuration(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise WorkerExecutionRefused(
            "INVALID_COMPLETE_RUN_CONFIGURATION",
            "Complete-run configuration must be an object.",
        )
    if set(value) != _COMPLETE_RUN_CONFIGURATION_FIELDS_V1:
        raise WorkerExecutionRefused(
            "INVALID_COMPLETE_RUN_CONFIGURATION",
            "Complete-run configuration fields must be exactly "
            + repr(sorted(_COMPLETE_RUN_CONFIGURATION_FIELDS_V1))
            + ".",
        )
    scenario_name = value["scenario_name"]
    duration_seconds = value["duration_seconds"]
    relative_volume = value["relative_volume"]
    liquidity = value["liquidity"]
    if any(
        type(item) is not str or not item
        for item in (scenario_name, relative_volume, liquidity)
    ):
        raise WorkerExecutionRefused(
            "INVALID_COMPLETE_RUN_CONFIGURATION",
            "Scenario, volume, and liquidity must be nonempty text.",
        )
    if type(duration_seconds) is not int or not 1 <= duration_seconds <= 86_400:
        raise WorkerExecutionRefused(
            "INVALID_COMPLETE_RUN_CONFIGURATION",
            "duration_seconds must be an integer in [1, 86400].",
        )
    try:
        definition = get_scenario_definition(scenario_name)
        volume_preset = VolumePreset.parse(relative_volume)
        liquidity_preset = LiquidityPreset.parse(liquidity)
    except ValueError as error:
        raise WorkerExecutionRefused(
            "INVALID_COMPLETE_RUN_CONFIGURATION",
            "Scenario, volume, or liquidity is not supported.",
        ) from error
    if (
        definition.name != scenario_name
        or volume_preset.value != relative_volume
        or liquidity_preset.value != liquidity
    ):
        raise WorkerExecutionRefused(
            "INVALID_COMPLETE_RUN_CONFIGURATION",
            "Scenario, volume, and liquidity must use canonical spellings.",
        )
    return value


def _complete_run_schema_identities() -> tuple[DigestReferenceV1, ...]:
    declarations = (
        (
            _METRICS_SCHEMA_ID_V1,
            {
                "media_type": InlineArtifactMediaTypeV1.CANONICAL_JSON.value,
                "root_type": "object",
                "schema_id": _METRICS_SCHEMA_ID_V1,
                "schema_version": 1,
            },
        ),
        (
            _REPLAY_SCHEMA_ID_V1,
            {
                "media_type": InlineArtifactMediaTypeV1.CANONICAL_JSONL.value,
                "row_type": "object",
                "schema_id": _REPLAY_SCHEMA_ID_V1,
                "schema_version": 1,
            },
        ),
        (
            WorkRequestV1.schema_id,
            {
                "schema_id": WorkRequestV1.schema_id,
                "schema_version": WorkRequestV1.schema_version,
            },
        ),
        (
            WorkerResultV1.schema_id,
            {
                "schema_id": WorkerResultV1.schema_id,
                "schema_version": WorkerResultV1.schema_version,
            },
        ),
    )
    return tuple(
        sorted(
            (_digest_reference(name, declaration) for name, declaration in declarations),
            key=lambda item: item.sort_key,
        )
    )


def _complete_run_capability_identities() -> tuple[DigestReferenceV1, ...]:
    capabilities = (
        _digest_reference(
            _COMPLETE_RUN_CAPABILITY_ID_V1,
            {
                "artifact_contracts": [
                    item.as_dict() for item in complete_run_expected_output_identities()
                ],
                "audit_contracts": [
                    item.as_dict() for item in complete_run_runtime_audit_identities()
                ],
                "configuration_fields": sorted(_COMPLETE_RUN_CONFIGURATION_FIELDS_V1),
                "work_kind": WorkKindV1.COMPLETE_RUN.value,
            },
        ),
        _digest_reference(
            _DATA_ONLY_STDIO_CAPABILITY_ID_V1,
            {
                "encoding": "canonical_ascii_json",
                "frame_count": 1,
                "input_final_lf": "optional",
                "output_final_lf": "required",
                "schema_version": 1,
            },
        ),
    )
    return tuple(sorted(capabilities, key=lambda item: item.sort_key))


def _measure_executable_identity_references() -> tuple[
    DigestReferenceV1,
    DigestReferenceV1,
    DigestReferenceV1,
    DigestReferenceV1,
]:
    """Measure source, runtime, dependencies, and compiler without aliases."""

    package_root = Path(__file__).resolve().parents[1]
    repository_root = package_root.parent
    source_candidates = set(package_root.rglob("*.py"))
    source_candidates.add(package_root / "scenarios" / "accepted_scenarios.json")
    pyproject = repository_root / "pyproject.toml"
    if pyproject.is_file() and not pyproject.is_symlink():
        source_candidates.add(pyproject)
    source_manifest: dict[str, str] = {}
    for path in sorted(source_candidates):
        if path.is_symlink() or not path.is_file():
            raise RuntimeError(
                "worker implementation identity contains an unavailable or linked input"
            )
        try:
            relative = path.relative_to(repository_root).as_posix()
        except ValueError:
            relative = path.relative_to(package_root).as_posix()
        source_manifest[relative] = hashlib.sha256(path.read_bytes()).hexdigest()

    implementation_source = _digest_reference(
        _IMPLEMENTATION_SOURCE_IDENTITY_NAME_V1,
        {
            "files": source_manifest,
            "identity_id": "KIRBY2_IMPLEMENTATION_SOURCE_MANIFEST_V1",
        },
    )
    runtime = _digest_reference(
        _PYTHON_RUNTIME_IDENTITY_NAME_V1,
        {
            "abiflags": getattr(sys, "abiflags", ""),
            "byteorder": sys.byteorder,
            "cache_tag": sys.implementation.cache_tag,
            "hexversion": sys.hexversion,
            "implementation": sys.implementation.name,
            "implementation_version": list(sys.implementation.version),
            "machine": platform.machine(),
            "platform": sys.platform,
            "version_info": list(sys.version_info),
        },
    )

    installed: set[tuple[str, str]] = set()
    for distribution in importlib.metadata.distributions():
        name = distribution.metadata.get("Name")
        version = distribution.version
        if isinstance(name, str) and name and isinstance(version, str) and version:
            installed.add((name.casefold().replace("_", "-"), version))
    dependency = _digest_reference(
        _DEPENDENCY_IDENTITY_NAME_V1,
        {
            "distributions": [
                {"name": name, "version": version}
                for name, version in sorted(installed)
            ],
            "identity_id": "KIRBY2_INSTALLED_PYTHON_DEPENDENCIES_V1",
        },
    )
    compiler = _digest_reference(
        _COMPILER_IDENTITY_NAME_V1,
        {
            "cache_tag": sys.implementation.cache_tag,
            "compiler": platform.python_compiler(),
            "implementation": sys.implementation.name,
            "magic_number_hex": importlib.util.MAGIC_NUMBER.hex(),
        },
    )
    return implementation_source, runtime, dependency, compiler


def _pre_audit_result(
    request: WorkRequestV1,
    compatibility: WorkerCompatibilityV1,
    status: WorkerResultStatusV1,
    diagnostic: ProtocolDiagnosticV1,
) -> WorkerResultV1:
    return WorkerResultV1(
        request=request,
        worker_compatibility=compatibility,
        status=status,
        manifest=None,
        artifacts=(),
        runtime_audit_results=(),
        diagnostics=(diagnostic,),
    )


def _inline_complete_run_artifacts(
    execution: CompleteRunExecutionV1,
) -> tuple[InlineArtifactV1, ...]:
    return tuple(
        InlineArtifactV1(
            artifact_id=name,
            media_type=(
                InlineArtifactMediaTypeV1.CANONICAL_JSON
                if name == "metrics.json"
                else InlineArtifactMediaTypeV1.CANONICAL_JSONL
            ),
            payload_bytes=raw,
        )
        for name, raw in execution.artifacts
    )


def _determinism_failed_result(
    request: WorkRequestV1,
    compatibility: WorkerCompatibilityV1,
    error: WorkerDeterminismFailure,
) -> WorkerResultV1:
    diagnostic = ProtocolDiagnosticV1(
        code="DETERMINISTIC_REPLAY_MISMATCH",
        summary="Duplicate COMPLETE_RUN execution produced different artifacts.",
        details={
            "first_artifact_set_sha256": error.first_artifact_set_sha256,
            "second_artifact_set_sha256": error.second_artifact_set_sha256,
        },
    )
    evidence = {
        DETERMINISTIC_REPLAY_AUDIT_ID_V1: _audit_evidence_sha256(
            {
                "audit_id": DETERMINISTIC_REPLAY_AUDIT_ID_V1,
                "exact_match": False,
                "first_artifact_set_sha256": error.first_artifact_set_sha256,
                "logical_work_unit_id": request.logical_work_unit.logical_work_unit_id,
                "second_artifact_set_sha256": error.second_artifact_set_sha256,
                "status": RuntimeAuditStatusV1.FAILED.value,
            }
        ),
        ORDER_BOOK_INVARIANTS_AUDIT_ID_V1: _audit_evidence_sha256(
            {
                "audit_id": ORDER_BOOK_INVARIANTS_AUDIT_ID_V1,
                "checked_execution_count": 2,
                "logical_work_unit_id": request.logical_work_unit.logical_work_unit_id,
                "status": RuntimeAuditStatusV1.PASSED.value,
            }
        ),
    }
    runtime_audits = tuple(
        RuntimeAuditResultV1(
            audit_identity=audit_identity,
            status=(
                RuntimeAuditStatusV1.FAILED
                if audit_identity.name == DETERMINISTIC_REPLAY_AUDIT_ID_V1
                else RuntimeAuditStatusV1.PASSED
            ),
            evidence_digests=(
                DigestReferenceV1(
                    name=f"{audit_identity.name}:evidence",
                    sha256=evidence[audit_identity.name],
                ),
            ),
            diagnostics=(
                (diagnostic,)
                if audit_identity.name == DETERMINISTIC_REPLAY_AUDIT_ID_V1
                else ()
            ),
        )
        for audit_identity in request.required_runtime_audits
    )
    return WorkerResultV1(
        request=request,
        worker_compatibility=compatibility,
        status=WorkerResultStatusV1.RUNTIME_AUDIT_FAILED,
        manifest=None,
        artifacts=(),
        runtime_audit_results=runtime_audits,
        diagnostics=(diagnostic,),
    )


def _invariant_failed_result(
    request: WorkRequestV1,
    compatibility: WorkerCompatibilityV1,
    error: WorkerInvariantFailure,
) -> WorkerResultV1:
    deterministic_diagnostic = ProtocolDiagnosticV1(
        code="DETERMINISTIC_REPLAY_NOT_ESTABLISHED",
        summary="Determinism could not be established after an invariant failure.",
        details={"failed_execution_index": error.execution_index},
    )
    invariant_diagnostic = ProtocolDiagnosticV1(
        code="ORDER_BOOK_INVARIANTS_FAILED",
        summary="A COMPLETE_RUN execution failed order-book invariants.",
        details={"failed_execution_index": error.execution_index},
    )
    diagnostics_by_audit = {
        DETERMINISTIC_REPLAY_AUDIT_ID_V1: deterministic_diagnostic,
        ORDER_BOOK_INVARIANTS_AUDIT_ID_V1: invariant_diagnostic,
    }
    runtime_audits = tuple(
        RuntimeAuditResultV1(
            audit_identity=audit_identity,
            status=RuntimeAuditStatusV1.FAILED,
            evidence_digests=(
                DigestReferenceV1(
                    name=f"{audit_identity.name}:evidence",
                    sha256=_audit_evidence_sha256(
                        {
                            "audit_id": audit_identity.name,
                            "failed_execution_index": error.execution_index,
                            "logical_work_unit_id": (
                                request.logical_work_unit.logical_work_unit_id
                            ),
                            "status": RuntimeAuditStatusV1.FAILED.value,
                        }
                    ),
                ),
            ),
            diagnostics=(diagnostics_by_audit[audit_identity.name],),
        )
        for audit_identity in request.required_runtime_audits
    )
    diagnostics = tuple(
        sorted(
            (deterministic_diagnostic, invariant_diagnostic),
            key=lambda item: item.sort_key,
        )
    )
    return WorkerResultV1(
        request=request,
        worker_compatibility=compatibility,
        status=WorkerResultStatusV1.RUNTIME_AUDIT_FAILED,
        manifest=None,
        artifacts=(),
        runtime_audit_results=runtime_audits,
        diagnostics=diagnostics,
    )


def _artifact_set_sha256(artifacts: tuple[tuple[str, bytes], ...]) -> str:
    return hashlib.sha256(
        _canonical_json_bytes(
            [
                {"artifact_id": name, "sha256": hashlib.sha256(raw).hexdigest()}
                for name, raw in artifacts
            ]
        )
    ).hexdigest()


def _audit_evidence_sha256(payload: Mapping[str, object]) -> str:
    return hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()


def _pairs_without_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    payload: dict[str, object] = {}
    for key, value in pairs:
        if key in payload:
            raise ValueError("data-only worker input contains a duplicate key")
        payload[key] = value
    return payload


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"data-only worker input contains non-finite constant {value!r}")


def _validate_data_only_json(value: object) -> None:
    if value is None or type(value) in {bool, int}:
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("data-only JSON cannot contain non-finite numbers")
        return
    if type(value) is str:
        if unicodedata.normalize("NFC", value) != value:
            raise ValueError("data-only JSON text must be NFC-normalized")
        if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
            raise ValueError("data-only JSON text contains a surrogate code point")
        return
    if type(value) is list:
        for item in value:
            _validate_data_only_json(item)
        return
    if type(value) is dict:
        if any(type(key) is not str for key in value):
            raise TypeError("data-only JSON object keys must be text")
        for key, item in value.items():
            _validate_data_only_json(key)
            _validate_data_only_json(item)
        return
    raise TypeError(f"unsupported data-only JSON value: {type(value).__name__}")


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def _canonical_json_bytes(value: object) -> bytes:
    _validate_data_only_json(value)
    return _canonical_json(value).encode("ascii")


def _digest_reference(name: str, payload: object) -> DigestReferenceV1:
    return DigestReferenceV1(
        name=name,
        sha256=hashlib.sha256(_canonical_json_bytes(payload)).hexdigest(),
    )


def _is_sha256(value: object) -> bool:
    return (
        type(value) is str
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


__all__ = [
    "COMPLETE_RUN_ARTIFACT_NAMES_V1",
    "COMPLETE_RUN_RUNTIME_AUDIT_IDS_V1",
    "DATA_ONLY_STDIO_MAX_BYTES_V1",
    "DETERMINISTIC_REPLAY_AUDIT_ID_V1",
    "ORDER_BOOK_INVARIANTS_AUDIT_ID_V1",
    "CompleteRunExecutionV1",
    "WorkerDeterminismFailure",
    "WorkerExecutionRefused",
    "WorkerInvariantFailure",
    "complete_run_expected_output_identities",
    "complete_run_runtime_audit_identities",
    "execute_complete_run",
    "execute_work_request",
    "handle_work_request",
    "main",
    "measure_local_worker_compatibility",
    "run_data_only_stdio_worker",
]


if __name__ == "__main__":
    main()
