"""Immutable scenario compilation onto Kirby2's existing native plan contracts."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType

from .defaults import (
    ScenarioDefaultMaterializationV1,
    materialize_scenario_defaults,
)
from .identity import (
    canonical_semantic_plan_bytes,
    compiled_artifact_digest,
    semantic_plan_digest,
)
from .imports import ScenarioImportLimitsV1
from .models import (
    DEFINITION_TYPE_BY_SECTION_V1,
    SCENARIO_BEHAVIOR_SECTION_NAMES,
    SCENARIO_COMPILATION_PHASES_V1,
    SCENARIO_COMPILER_VERSION,
    SCENARIO_COMPILED_ARTIFACT_SCHEMA_VERSION,
    SCENARIO_EXECUTION_INELIGIBLE_REASON_V1,
    SCENARIO_PENDING_COMPILATION_PHASES_V1,
    SCENARIO_SOURCE_SCHEMA_VERSION,
    SCENARIO_TARGET_CONTRACTS_V1,
    CompiledScenarioArtifactV1,
    ResolvedScenarioBundleV1,
    ScenarioCapabilityDecisionStatusV1,
    ScenarioCapabilityDecisionV1,
    ScenarioCompiledSeedPolicyV1,
    ScenarioPlanEnvelopeV1,
    ScenarioRecordV1,
    ScenarioTargetKindV1,
    canonical_native_payload_bytes,
    parse_native_payload_bytes_v1,
    parse_native_payload_v1,
)
from .resolution import resolve_scenario_bundle
from .seeds import build_compiled_seed_policy, scenario_run_identity_digest


NativeParser = Callable[[Mapping[str, object]], object]
NativeValidator = Callable[[object], bytes]
NativeRunner = Callable[[object, ScenarioCompiledSeedPolicyV1], object]
NativePersister = Callable[[object], bytes]
NativeReplayer = Callable[[bytes], object]

_ADAPTER_OPERATION_NAMES = ("parse", "persist", "replay", "run", "validate")
SCENARIO_REFERENCE_MAX_DEPTH_V1 = 64
SCENARIO_TARGET_REPLAY_MISMATCH_REASON_V1 = "TARGET_REPLAY_MISMATCH"
SCENARIO_TARGET_SEED_MISMATCH_REASON_V1 = "TARGET_SEED_POLICY_MISMATCH"
_UNSAFE_RECORD_TOKENS = frozenset(
    {"EVAL", "EVALUATOR", "EXPRESSION", "PYTHON", "REFLECT", "REFLECTION", "SHELL"}
)
_RECORD_TYPE_TOKEN_SPLIT = re.compile(r"[_.:/-]+")
_UNSAFE_FIELD_TOKENS = (
    "callable",
    "evaluator",
    "expression",
    "module_import",
    "python_symbol",
    "reflection",
    "shell",
)


class ScenarioExecutionRefused(RuntimeError):
    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(f"scenario execution refused: {reason_code}")


@dataclass(frozen=True, slots=True)
class ScenarioTargetAdapterV1:
    target_kind: ScenarioTargetKindV1
    target_version: int
    adapter_id: str
    adapter_version: int
    operation_ids: Mapping[str, str]
    _parse: NativeParser = field(repr=False, compare=False)
    _validate: NativeValidator = field(repr=False, compare=False)
    _run: NativeRunner = field(repr=False, compare=False)
    _persist: NativePersister = field(repr=False, compare=False)
    _replay: NativeReplayer = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if type(self.target_kind) is not ScenarioTargetKindV1:
            raise TypeError("scenario target adapter requires a closed target kind")
        contract = SCENARIO_TARGET_CONTRACTS_V1[self.target_kind]
        if (
            self.target_version != contract.target_version
            or self.adapter_id != contract.adapter_id
            or self.adapter_version != contract.adapter_version
        ):
            raise ValueError("scenario target adapter differs from its frozen contract")
        if not isinstance(self.operation_ids, Mapping) or set(
            self.operation_ids
        ) != set(_ADAPTER_OPERATION_NAMES):
            raise ValueError("scenario target adapter operation inventory is incomplete")
        operation_ids = dict(self.operation_ids)
        if any(
            type(name) is not str
            or type(operation_id) is not str
            or not operation_id
            for name, operation_id in operation_ids.items()
        ):
            raise TypeError("scenario adapter operation IDs must be nonempty strings")
        if len(set(operation_ids.values())) != len(operation_ids):
            raise ValueError("scenario adapter operation IDs must be unique")
        expected_operations = {
            name: f"{self.adapter_id}_{name.upper()}_V1"
            for name in _ADAPTER_OPERATION_NAMES
        }
        if operation_ids != expected_operations:
            raise ValueError("scenario adapter operations are not the closed V1 set")
        if any(
            not callable(operation)
            for operation in (
                self._parse,
                self._validate,
                self._run,
                self._persist,
                self._replay,
            )
        ):
            raise TypeError("scenario target adapter operations must be callable")
        object.__setattr__(self, "operation_ids", MappingProxyType(operation_ids))

    def parse(self, payload: Mapping[str, object]) -> object:
        return self._parse(payload)

    def validate(self, payload: object) -> bytes:
        return self._validate(payload)

    def run(
        self,
        payload: object,
        seed_policy: ScenarioCompiledSeedPolicyV1 | None = None,
    ) -> object:
        if type(payload) is CompiledScenarioArtifactV1:
            if seed_policy is not None:
                raise TypeError(
                    "validated scenario adapter run derives its seed policy from the artifact"
                )
            artifact = payload
            if not artifact.execution_eligible:
                raise ScenarioExecutionRefused(artifact.execution_reason_code)
            if (
                artifact.target_kind is not self.target_kind
                or artifact.target_version != self.target_version
                or artifact.adapter_id != self.adapter_id
                or artifact.adapter_version != self.adapter_version
                or artifact.as_dict()["adapter_operations"]
                != dict(self.operation_ids)
            ):
                raise ScenarioExecutionRefused("TARGET_ADAPTER_MISMATCH")
            return self._run(
                artifact.plan_envelope.payload,
                artifact.seed_policy,
            )
        if type(seed_policy) is not ScenarioCompiledSeedPolicyV1:
            raise TypeError("scenario run adapter requires a compiled seed policy")
        self.validate(payload)
        raise ScenarioExecutionRefused(SCENARIO_EXECUTION_INELIGIBLE_REASON_V1)

    def persist(self, payload: object) -> bytes:
        return self._persist(payload)

    def replay(self, raw: bytes) -> object:
        return self._replay(raw)


class ScenarioTargetRegistry:
    """Explicit duplicate-refusing registry; no discovery or dynamic imports."""

    def __init__(self) -> None:
        self._by_kind: dict[ScenarioTargetKindV1, ScenarioTargetAdapterV1] = {}
        self._by_adapter_id: dict[str, ScenarioTargetAdapterV1] = {}
        self._sealed = False

    def register(self, adapter: ScenarioTargetAdapterV1) -> None:
        if self._sealed:
            raise ValueError("scenario target registry is sealed")
        if type(adapter) is not ScenarioTargetAdapterV1:
            raise TypeError("scenario target registry requires V1 adapters")
        if adapter.target_kind in self._by_kind:
            raise ValueError("duplicate scenario target kind registration")
        if adapter.adapter_id in self._by_adapter_id:
            raise ValueError("duplicate scenario adapter ID registration")
        self._by_kind[adapter.target_kind] = adapter
        self._by_adapter_id[adapter.adapter_id] = adapter

    def seal(self) -> None:
        if self._sealed:
            raise ValueError("scenario target registry is already sealed")
        if set(self._by_kind) != set(ScenarioTargetKindV1):
            raise ValueError("scenario target registry must cover exactly five targets")
        self._sealed = True

    @property
    def sealed(self) -> bool:
        return self._sealed

    @property
    def registered_kinds(self) -> tuple[ScenarioTargetKindV1, ...]:
        return tuple(kind for kind in ScenarioTargetKindV1 if kind in self._by_kind)

    def adapter(self, target_kind: ScenarioTargetKindV1) -> ScenarioTargetAdapterV1:
        if not self._sealed:
            raise ValueError("scenario target registry must be sealed before lookup")
        if type(target_kind) is not ScenarioTargetKindV1:
            raise TypeError("scenario target lookup requires a closed target kind")
        try:
            return self._by_kind[target_kind]
        except KeyError as error:  # defensive; seal requires complete coverage
            raise KeyError(f"unregistered scenario target: {target_kind.value}") from error

    def assert_closed_v1(self) -> None:
        if not self._sealed or self.registered_kinds != tuple(ScenarioTargetKindV1):
            raise ValueError("scenario target registry is not the closed V1 inventory")


def _target_adapter(target_kind: ScenarioTargetKindV1) -> ScenarioTargetAdapterV1:
    contract = SCENARIO_TARGET_CONTRACTS_V1[target_kind]
    operation_ids = {
        name: f"{contract.adapter_id}_{name.upper()}_V1"
        for name in _ADAPTER_OPERATION_NAMES
    }

    def parse(payload: Mapping[str, object]) -> object:
        return parse_native_payload_v1(target_kind, payload)

    def validate(payload: object) -> bytes:
        return canonical_native_payload_bytes(target_kind, payload)

    def run(
        payload: object,
        seed_policy: ScenarioCompiledSeedPolicyV1,
    ) -> object:
        canonical_native_payload_bytes(target_kind, payload)
        if type(seed_policy) is not ScenarioCompiledSeedPolicyV1:
            raise TypeError("scenario run adapter requires a compiled seed policy")
        if target_kind is ScenarioTargetKindV1.FULL_DAY_PLAN_V1:
            from kirby2.full_day.runtime import FullDayRuntime

            native_seed = payload.seed_policy.root_seed
            if native_seed != seed_policy.selected_root_seed:
                raise ScenarioExecutionRefused(
                    SCENARIO_TARGET_SEED_MISMATCH_REASON_V1
                )
            runtime = FullDayRuntime.create(payload)
            runtime.advance_to(payload.calendar.end_time_us)
            return runtime
        if target_kind is ScenarioTargetKindV1.MARKET_SCENARIO_V1:
            from kirby2.scenarios.market import run_market_scenario

            return run_market_scenario(
                payload,
                seed=seed_policy.selected_root_seed,
            )
        if target_kind is ScenarioTargetKindV1.HIDDEN_LIQUIDITY_RECORDING_V1:
            from kirby2.observability.replay import replay_observability_recording

            report = replay_observability_recording(payload)
            if not report.passed:
                raise ScenarioExecutionRefused(
                    SCENARIO_TARGET_REPLAY_MISMATCH_REASON_V1
                )
            return report
        if target_kind is ScenarioTargetKindV1.MULTIVENUE_RECORDING_V1:
            from kirby2.multivenue.replay import replay_multivenue_recording

            report = replay_multivenue_recording(payload)
            if not report.passed:
                raise ScenarioExecutionRefused(
                    SCENARIO_TARGET_REPLAY_MISMATCH_REASON_V1
                )
            return report
        if target_kind is ScenarioTargetKindV1.HISTORICAL_LESSON_V1:
            from kirby2.historical.lesson_runner import run_historical_lesson

            return run_historical_lesson(payload)
        raise AssertionError(f"unhandled scenario target kind: {target_kind.value}")

    def persist(payload: object) -> bytes:
        return canonical_native_payload_bytes(target_kind, payload)

    def replay(raw: bytes) -> object:
        return parse_native_payload_bytes_v1(target_kind, raw)

    return ScenarioTargetAdapterV1(
        target_kind=target_kind,
        target_version=contract.target_version,
        adapter_id=contract.adapter_id,
        adapter_version=contract.adapter_version,
        operation_ids=operation_ids,
        _parse=parse,
        _validate=validate,
        _run=run,
        _persist=persist,
        _replay=replay,
    )


def build_scenario_target_registry_v1() -> ScenarioTargetRegistry:
    registry = ScenarioTargetRegistry()
    for target_kind in (
        ScenarioTargetKindV1.FULL_DAY_PLAN_V1,
        ScenarioTargetKindV1.MARKET_SCENARIO_V1,
        ScenarioTargetKindV1.HIDDEN_LIQUIDITY_RECORDING_V1,
        ScenarioTargetKindV1.MULTIVENUE_RECORDING_V1,
        ScenarioTargetKindV1.HISTORICAL_LESSON_V1,
    ):
        registry.register(_target_adapter(target_kind))
    registry.seal()
    return registry


DEFAULT_SCENARIO_TARGET_REGISTRY = build_scenario_target_registry_v1()


def compile_scenario(
    source_root: Path,
    entry_path: str,
    native_payload: object,
    *,
    activated_pack_namespaces: Mapping[str, Path] | None = None,
    limits: ScenarioImportLimitsV1 = ScenarioImportLimitsV1(),
    cli_seed_override: int | None = None,
    warnings: tuple[str, ...] = (),
    target_registry: ScenarioTargetRegistry = DEFAULT_SCENARIO_TARGET_REGISTRY,
) -> CompiledScenarioArtifactV1:
    """Run parse/import/inheritance, then compile one immutable in-memory artifact."""

    resolved = resolve_scenario_bundle(
        source_root,
        entry_path,
        activated_pack_namespaces=activated_pack_namespaces,
        limits=limits,
    )
    return compile_resolved_scenario(
        resolved,
        native_payload,
        cli_seed_override=cli_seed_override,
        warnings=warnings,
        target_registry=target_registry,
    )


def compile_validated_scenario(
    source_root: Path,
    entry_path: str,
    native_payload: object,
    *,
    activated_pack_namespaces: Mapping[str, Path] | None = None,
    limits: ScenarioImportLimitsV1 = ScenarioImportLimitsV1(),
    cli_seed_override: int | None = None,
    warnings: tuple[str, ...] = (),
    target_registry: ScenarioTargetRegistry = DEFAULT_SCENARIO_TARGET_REGISTRY,
) -> CompiledScenarioArtifactV1:
    """Compile, statically validate, and finalize one execution-eligible artifact."""

    artifact = compile_scenario(
        source_root,
        entry_path,
        native_payload,
        activated_pack_namespaces=activated_pack_namespaces,
        limits=limits,
        cli_seed_override=cli_seed_override,
        warnings=warnings,
        target_registry=target_registry,
    )
    from .validation import finalize_compiled_scenario

    return finalize_compiled_scenario(
        artifact,
        target_registry=target_registry,
    )


def compile_resolved_scenario(
    resolved: ResolvedScenarioBundleV1,
    native_payload: object,
    *,
    cli_seed_override: int | None = None,
    warnings: tuple[str, ...] = (),
    target_registry: ScenarioTargetRegistry = DEFAULT_SCENARIO_TARGET_REGISTRY,
) -> CompiledScenarioArtifactV1:
    if type(resolved) is not ResolvedScenarioBundleV1:
        raise TypeError("scenario compilation requires a resolved V1 source bundle")
    if type(target_registry) is not ScenarioTargetRegistry:
        raise TypeError("scenario compilation requires ScenarioTargetRegistry")
    target_registry.assert_closed_v1()
    if type(warnings) is not tuple or any(
        type(item) is not str or not item for item in warnings
    ):
        raise TypeError("scenario compiler warnings must be a nonempty string tuple")
    normalized_warnings = tuple(sorted(set(warnings)))
    _refuse_unsafe_evaluator_surface(resolved)

    source = resolved.root_source
    target_kind = source.metadata.target_kind
    adapter = target_registry.adapter(target_kind)
    native_bytes = adapter.validate(native_payload)
    persisted = adapter.persist(native_payload)
    if native_bytes != persisted:
        raise ValueError("scenario target validate/persist adapters disagree")
    replayed = adapter.replay(persisted)
    if adapter.persist(replayed) != persisted:
        raise ValueError("scenario target replay adapter changed native plan bytes")
    envelope = ScenarioPlanEnvelopeV1(
        target_kind=target_kind,
        target_version=source.metadata.target_version,
        adapter_id=adapter.adapter_id,
        adapter_version=adapter.adapter_version,
        capability_digest=source.metadata.capability_digest,
        payload=native_payload,
    )
    if envelope.native_plan_digest != _sha256(native_bytes):
        raise ValueError("native scenario payload and envelope digests disagree")

    default_materialization = materialize_scenario_defaults(source)
    seed_policy = build_compiled_seed_policy(
        default_materialization.seed_policy_record,
        cli_seed_override=cli_seed_override,
    )
    materialized_plan = _materialized_plan(
        resolved,
        default_materialization,
    )
    semantic_digest = semantic_plan_digest(materialized_plan)
    declarations, decisions = _capability_inventory(materialized_plan)
    provenance = resolved.provenance_projection()
    run_digest = scenario_run_identity_digest(
        envelope.native_plan_digest,
        seed_policy,
    )
    body: dict[str, object] = {
        "adapter_id": adapter.adapter_id,
        "adapter_operations": dict(adapter.operation_ids),
        "adapter_version": adapter.adapter_version,
        "capability_decisions": list(decisions),
        "compiler_version": SCENARIO_COMPILER_VERSION,
        "completed_phases": list(SCENARIO_COMPILATION_PHASES_V1),
        "execution_eligible": False,
        "execution_reason_code": SCENARIO_EXECUTION_INELIGIBLE_REASON_V1,
        "materialized_plan": materialized_plan,
        "native_plan_digest": envelope.native_plan_digest,
        "native_plan_envelope_json": envelope.canonical_bytes().decode("utf-8"),
        "pending_phases": list(SCENARIO_PENDING_COMPILATION_PHASES_V1),
        "required_capability_declarations": list(declarations),
        "run_identity_digest": run_digest,
        "schema_version": SCENARIO_COMPILED_ARTIFACT_SCHEMA_VERSION,
        "seed_policy": seed_policy.as_dict(),
        "semantic_plan_digest": semantic_digest,
        "source_bundle_digest": resolved.import_bundle.source_bundle_digest,
        "source_schema_version": SCENARIO_SOURCE_SCHEMA_VERSION,
        "target_kind": target_kind.value,
        "target_version": adapter.target_version,
        "validation_report_digest": None,
        "validation_report_json": None,
        "warnings": list(normalized_warnings),
    }
    artifact_digest = compiled_artifact_digest(
        canonical_semantic_plan_bytes(body),
        provenance,
    )
    artifact_payload = {
        **body,
        "compiled_artifact_digest": artifact_digest,
        "provenance": provenance,
    }
    return CompiledScenarioArtifactV1(
        canonical_semantic_plan_bytes(artifact_payload)
    )


def run_compiled_scenario(
    artifact: CompiledScenarioArtifactV1,
    *,
    target_registry: ScenarioTargetRegistry = DEFAULT_SCENARIO_TARGET_REGISTRY,
) -> object:
    """Dispatch only a self-verifying WO32-D validation-finalized artifact."""

    if type(artifact) is not CompiledScenarioArtifactV1:
        raise TypeError("scenario runtime requires a compiled V1 artifact")
    if not artifact.execution_eligible:
        raise ScenarioExecutionRefused(artifact.execution_reason_code)
    target_registry.assert_closed_v1()
    adapter = target_registry.adapter(artifact.target_kind)
    expected_operations = artifact.as_dict()["adapter_operations"]
    if expected_operations != dict(adapter.operation_ids):
        raise ScenarioExecutionRefused("TARGET_ADAPTER_MISMATCH")
    return adapter.run(artifact)


def replay_compiled_scenario(raw: bytes) -> CompiledScenarioArtifactV1:
    return CompiledScenarioArtifactV1.from_bytes(raw)


def _materialized_plan(
    resolved: ResolvedScenarioBundleV1,
    defaults: ScenarioDefaultMaterializationV1,
) -> dict[str, object]:
    definitions = {
        definition.qualified_name: definition for definition in resolved.definitions
    }
    resolving: set[str] = set()

    def bind_definition(qualified_name: str) -> dict[str, object]:
        if qualified_name in resolving:
            raise ValueError("scenario definition references contain a cycle")
        if len(resolving) >= SCENARIO_REFERENCE_MAX_DEPTH_V1:
            raise ValueError("scenario definition references exceed maximum depth")
        try:
            definition = definitions[qualified_name]
        except KeyError as error:
            raise ValueError(
                f"scenario record references an unknown definition: {qualified_name}"
            ) from error
        resolving.add(qualified_name)
        try:
            return {
                "definition_type": definition.definition_type.value,
                "qualified_name": definition.qualified_name,
                "record": bind_record(definition.record),
            }
        finally:
            resolving.remove(qualified_name)

    def bind_record(record: ScenarioRecordV1) -> dict[str, object]:
        if record.extends is not None:
            raise ValueError("compiled scenario encountered unresolved inheritance")
        payload = record.as_dict(semantic=True)
        reference = payload.pop("reference", None)
        if reference is not None:
            if type(reference) is not str or reference.count(":") != 1:
                raise ValueError(
                    "compiled scenario references require stable qualified names"
                )
            payload["bound_reference"] = bind_definition(reference)
        return payload

    source = resolved.root_source
    root_payload: dict[str, object] = {
        "metadata": source.metadata.semantic_dict(),
        "schema_version": source.schema_version,
    }
    definition_sections = set(DEFINITION_TYPE_BY_SECTION_V1)
    for section_name in SCENARIO_BEHAVIOR_SECTION_NAMES:
        if section_name in definition_sections:
            root_payload[section_name] = {"records": []}
            continue
        if section_name == "seed_policy":
            records = (defaults.seed_policy_record,)
        else:
            records = getattr(source, section_name).records
        root_payload[section_name] = {
            "records": [bind_record(record) for record in records]
        }

    root_path = resolved.import_bundle.root_logical_path
    selected_definitions = tuple(
        definition
        for definition in resolved.definitions
        if definition.source_logical_path == root_path
    )
    return {
        "applied_defaults": [
            item.as_dict() for item in defaults.applied_defaults
        ],
        "resolved_definitions": [
            bind_definition(definition.qualified_name)
            for definition in selected_definitions
        ],
        "root_source": root_payload,
        "schema_version": 1,
    }


def _capability_inventory(
    materialized_plan: Mapping[str, object],
) -> tuple[tuple[dict[str, object], ...], tuple[dict[str, object], ...]]:
    root = materialized_plan["root_source"]
    if not isinstance(root, Mapping):
        raise TypeError("materialized scenario root must be an object")
    section = root["required_source_capabilities"]
    if not isinstance(section, Mapping) or type(section.get("records")) is not list:
        raise TypeError("materialized capability section is malformed")
    declarations_list: list[dict[str, object]] = []
    for record in section["records"]:
        if not isinstance(record, Mapping):
            raise TypeError("materialized capability record is not an object")
        declaration_id = record.get("logical_name")
        if type(declaration_id) is not str:
            raise TypeError("materialized capability declaration ID is invalid")
        fields = record.get("fields")
        if type(fields) is not list or any(
            not isinstance(field, Mapping) for field in fields
        ):
            raise TypeError("materialized capability fields are malformed")
        field_values = {
            str(field["name"]): next(
                value
                for key, value in field.items()
                if key != "name"
            )
            for field in fields
            if type(field.get("name")) is str and len(field) == 2
        }
        if len(field_values) != len(fields):
            raise ValueError("materialized capability fields are not exact")
        capability_id = field_values.get("capability")
        required = field_values.get("required", True)
        if type(capability_id) is not str:
            raise ValueError("capability declaration requires an identifier capability")
        if type(required) is not bool:
            raise ValueError("capability declaration required flag must be a bool")
        declarations_list.append(
            {
                "capability_id": capability_id,
                "declaration_id": declaration_id,
                "record": dict(record),
                "required": required,
                "source_location": (
                    "root_source.required_source_capabilities.records"
                    f"[{declaration_id}]"
                ),
            }
        )
    declarations = tuple(
        sorted(declarations_list, key=lambda item: str(item["declaration_id"]))
    )
    declaration_ids = tuple(item["declaration_id"] for item in declarations)
    if len(declaration_ids) != len(set(declaration_ids)):
        raise ValueError("capability declaration IDs must be unique")
    decisions = tuple(
        ScenarioCapabilityDecisionV1(
            declaration_id=str(declaration["declaration_id"]),
            capability_id=str(declaration["capability_id"]),
            required=bool(declaration["required"]),
            decision=ScenarioCapabilityDecisionStatusV1.PENDING_VALIDATOR,
            reason_code=SCENARIO_EXECUTION_INELIGIBLE_REASON_V1,
            source_location=str(declaration["source_location"]),
        ).as_dict()
        for declaration in declarations
    )
    return declarations, decisions


def _refuse_unsafe_evaluator_surface(resolved: ResolvedScenarioBundleV1) -> None:
    for document in resolved.import_bundle.documents:
        for section_name in SCENARIO_BEHAVIOR_SECTION_NAMES:
            for record in getattr(document.source, section_name).records:
                normalized_type = record.record_type.upper()
                if set(_RECORD_TYPE_TOKEN_SPLIT.split(normalized_type)).intersection(
                    _UNSAFE_RECORD_TOKENS
                ):
                    raise ValueError(
                        "scenario source cannot declare an expression or code evaluator"
                    )
                for source_field in record.fields:
                    normalized_name = source_field.name.lower()
                    if any(
                        token in normalized_name for token in _UNSAFE_FIELD_TOKENS
                    ):
                        raise ValueError(
                            "scenario source cannot import, reflect, or call code"
                        )


def _sha256(raw: bytes) -> str:
    import hashlib

    return hashlib.sha256(raw).hexdigest()


__all__ = [
    "DEFAULT_SCENARIO_TARGET_REGISTRY",
    "SCENARIO_REFERENCE_MAX_DEPTH_V1",
    "SCENARIO_TARGET_REPLAY_MISMATCH_REASON_V1",
    "SCENARIO_TARGET_SEED_MISMATCH_REASON_V1",
    "ScenarioExecutionRefused",
    "ScenarioTargetAdapterV1",
    "ScenarioTargetRegistry",
    "build_scenario_target_registry_v1",
    "compile_resolved_scenario",
    "compile_scenario",
    "compile_validated_scenario",
    "replay_compiled_scenario",
    "run_compiled_scenario",
]
