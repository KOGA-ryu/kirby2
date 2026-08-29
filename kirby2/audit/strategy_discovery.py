"""Executable WO35-A audit for canonical strategy identity and lineage."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from kirby2.discovery.ast import (
    parse_strategy_ast,
    render_canonical_strategy_ast,
    strategy_ast_round_trip,
)
from kirby2.discovery.identity import (
    STRATEGY_CANONICALIZATION_POLICY_SHA256_V1,
    STRATEGY_IDENTITY_MIGRATION_ID_V1,
    StrategyImportOriginV1,
    canonical_identity_bytes,
    canonical_render_identity,
    strategy_semantic_sha256,
)
from kirby2.discovery.lineage import (
    StrategyRngSubstreamV1,
    build_strategy_lineage_node,
)
from kirby2.experiments.models import StrategyVariant
from kirby2.immutable import thaw_json
from kirby2.strategy.language import (
    RuleSyntaxError,
    parse_strategy_semantic_ast,
    render_canonical_strategy_source,
)


WO35A_AUDIT_CASE_COUNT = 5
WO35A_CANONICALIZATION_POLICY_SHA256 = (
    "fa2df709734780a866d9c8f9554004ebaad510869be961d5c655a1a19c52c2b8"
)
WO35A_FIXTURE_SHA256 = (
    "84733a622bc99a389823f97c47c57f9d093801b9e27cfd089741f534530d14c5"
)
WO35A_LINEAGE_FIXTURE_SHA256 = (
    "f331b6624aa2fb6c60705d292d25d1c6a3ae0938555ddd78026282b94c2b7cd6"
)

_TRAFFIC_A = """\
setup semantic_probe
# The 5s window and REFUSE policy are both defaults.
GREEN when
    spread_ticks <= 2.00
    book_imbalance >= 0.200
WAIT when
    spread_ticks <= 4.0
RED otherwise
"""

_TRAFFIC_B = """\
SeTuP semantic_probe
WINDOW 5000ms
UNAVAILABLE refuse
green WHEN
    book_imbalance >= 2e-1
    spread_ticks <= 2
    spread_ticks <= 2.000
wait when
    spread_ticks <= 4.000
red otherwise
"""

_TRAFFIC_CHILD = """\
setup semantic_probe
window 5s
unavailable REFUSE
GREEN when
    book_imbalance >= 0.3
    spread_ticks <= 2
WAIT when
    spread_ticks <= 4
RED otherwise
"""

_MACHINE_A = """\
machine priority_probe
window 1s
initial IDLE
state IDLE signal WAIT entry DENY exit ALLOW
state ARMED signal GREEN entry ALLOW exit ALLOW
state HALT signal RED entry DENY exit ALLOW cooldown 1s
transition IDLE -> ARMED when
    spread_ticks <= 2.00
    book_imbalance >= 0.0
transition IDLE -> HALT when
    spread_ticks <= 2
"""

_MACHINE_B = """\
MACHINE priority_probe
WINDOW 1000ms
UNAVAILABLE REFUSE
INITIAL IDLE
STATE HALT SIGNAL RED ENTRY DENY EXIT ALLOW COOLDOWN 1000ms
STATE IDLE SIGNAL WAIT ENTRY DENY EXIT ALLOW
STATE ARMED SIGNAL GREEN ENTRY ALLOW EXIT ALLOW
TRANSITION IDLE -> ARMED WHEN
    book_imbalance >= -0.000
    spread_ticks <= 2
TRANSITION IDLE -> HALT WHEN
    spread_ticks <= 2.000
"""

_MACHINE_PRIORITY_SWAPPED = """\
machine priority_probe
window 1s
unavailable REFUSE
initial IDLE
state ARMED signal GREEN entry ALLOW exit ALLOW
state HALT signal RED entry DENY exit ALLOW cooldown 1s
state IDLE signal WAIT entry DENY exit ALLOW
transition IDLE -> HALT when
    spread_ticks <= 2
transition IDLE -> ARMED when
    spread_ticks <= 2
    book_imbalance >= 0
"""

_ACTUAL_FIXTURE_SHA256 = hashlib.sha256(
    canonical_identity_bytes(
        {
            "machine_a": _MACHINE_A,
            "machine_b": _MACHINE_B,
            "machine_priority_swapped": _MACHINE_PRIORITY_SWAPPED,
            "traffic_a": _TRAFFIC_A,
            "traffic_b": _TRAFFIC_B,
            "traffic_child": _TRAFFIC_CHILD,
        }
    )
).hexdigest()


@dataclass(frozen=True, slots=True)
class StrategyDiscoveryAuditCase:
    name: str
    detail: str
    failures: tuple[str, ...]
    required: bool = True


def _parse_render_parse_case() -> StrategyDiscoveryAuditCase:
    failures: list[str] = []
    traffic = parse_strategy_ast(_TRAFFIC_A)
    machine = parse_strategy_ast(_MACHINE_A)
    traffic_rendered = render_canonical_strategy_ast(traffic)
    machine_rendered = render_canonical_strategy_ast(machine)
    if strategy_ast_round_trip(traffic) != traffic:
        failures.append("traffic-light parse/render/parse changed semantic AST")
    if strategy_ast_round_trip(machine) != machine:
        failures.append("state-machine parse/render/parse changed semantic AST")
    if render_canonical_strategy_ast(parse_strategy_ast(traffic_rendered)) != traffic_rendered:
        failures.append("traffic-light canonical rendering is not a fixed point")
    if render_canonical_strategy_ast(parse_strategy_ast(machine_rendered)) != machine_rendered:
        failures.append("state-machine canonical rendering is not a fixed point")
    if (
        parse_strategy_semantic_ast(_TRAFFIC_A) != traffic
        or render_canonical_strategy_source(_MACHINE_A) != machine_rendered
    ):
        failures.append("legacy strategy-language semantic adapters are inconsistent")
    if _ACTUAL_FIXTURE_SHA256 != WO35A_FIXTURE_SHA256:
        failures.append("WO35-A source fixtures differ from their frozen digest")
    return StrategyDiscoveryAuditCase(
        "a_parse_render_parse_is_semantically_stable_for_both_grammars",
        (
            f"traffic_sha256={strategy_semantic_sha256(traffic)} "
            f"machine_sha256={strategy_semantic_sha256(machine)} "
            "round_trips=2 canonical_fixed_points=2"
        ),
        tuple(failures),
    )


def _equivalence_and_priority_case() -> StrategyDiscoveryAuditCase:
    failures: list[str] = []
    traffic_a = parse_strategy_ast(_TRAFFIC_A)
    traffic_b = parse_strategy_ast(_TRAFFIC_B)
    machine_a = parse_strategy_ast(_MACHINE_A)
    machine_b = parse_strategy_ast(_MACHINE_B)
    swapped = parse_strategy_ast(_MACHINE_PRIORITY_SWAPPED)
    if traffic_a != traffic_b or strategy_semantic_sha256(
        traffic_a
    ) != strategy_semantic_sha256(traffic_b):
        failures.append(
            "format, unit, decimal, duplicate, or conjunction ordering did not collapse"
        )
    if render_canonical_strategy_ast(traffic_a) != render_canonical_strategy_ast(
        traffic_b
    ):
        failures.append("equivalent traffic strategies rendered differently")
    if machine_a != machine_b or strategy_semantic_sha256(
        machine_a
    ) != strategy_semantic_sha256(machine_b):
        failures.append("commutative state-machine representation did not collapse")
    if strategy_semantic_sha256(machine_a) == strategy_semantic_sha256(swapped):
        failures.append("transition priority was incorrectly treated as commutative")
    if (
        STRATEGY_CANONICALIZATION_POLICY_SHA256_V1
        != WO35A_CANONICALIZATION_POLICY_SHA256
    ):
        failures.append("canonicalization policy differs from its frozen V1 digest")
    return StrategyDiscoveryAuditCase(
        "a_supported_equivalences_collapse_but_transition_priority_remains_semantic",
        (
            f"policy_sha256={STRATEGY_CANONICALIZATION_POLICY_SHA256_V1} "
            "traffic_duplicates=COLLAPSED machine_formatting=COLLAPSED "
            "transition_priority=PRESERVED"
        ),
        tuple(failures),
    )


def _dual_identity_case() -> StrategyDiscoveryAuditCase:
    failures: list[str] = []
    inline_a = StrategyVariant("inline_a", _TRAFFIC_A)
    inline_b = StrategyVariant("inline_b", _TRAFFIC_B)
    file_variant = StrategyVariant(
        "file_variant",
        _TRAFFIC_A,
        "strategies/semantic_probe.k2",
    )
    identity_a = inline_a.strategy_identity
    identity_b = inline_b.strategy_identity
    file_identity = file_variant.strategy_identity
    expected_legacy = hashlib.sha256(_TRAFFIC_A.encode("utf-8")).hexdigest()
    if inline_a.source_sha256 != expected_legacy:
        failures.append("experiment legacy source-byte digest changed")
    if inline_a.as_dict() != {
        "name": "inline_a",
        "source": _TRAFFIC_A,
        "source_path": None,
        "source_sha256": expected_legacy,
    }:
        failures.append("legacy experiment serialization was silently replaced")
    if (
        identity_a.legacy_source_sha256 == identity_b.legacy_source_sha256
        or identity_a.semantic_ast_sha256 != identity_b.semantic_ast_sha256
    ):
        failures.append("dual identity did not distinguish source bytes from semantics")
    if inline_a.discovery_identity_dict() != identity_a.as_dict():
        failures.append("experiment discovery identity sidecar is not reproducible")
    if (
        identity_a.provenance.import_origin
        is not StrategyImportOriginV1.EXPERIMENT_INLINE_SOURCE
        or file_identity.provenance.import_origin
        is not StrategyImportOriginV1.EXPERIMENT_RULE_FILE
        or identity_a.provenance.as_dict()["migration_id"]
        != STRATEGY_IDENTITY_MIGRATION_ID_V1
    ):
        failures.append("strategy migration/import provenance is incomplete")
    rendered_identity = canonical_render_identity(parse_strategy_ast(_TRAFFIC_A))
    if (
        rendered_identity.semantic_ast_sha256 != identity_a.semantic_ast_sha256
        or rendered_identity.provenance.import_origin
        is not StrategyImportOriginV1.CANONICAL_AST_RENDER
    ):
        failures.append("canonical render identity lacks explicit provenance")
    return StrategyDiscoveryAuditCase(
        "a_legacy_source_and_semantic_ast_identities_remain_separate_and_inspectable",
        (
            f"legacy_source_sha256={identity_a.legacy_source_sha256} "
            f"semantic_ast_sha256={identity_a.semantic_ast_sha256} "
            "legacy_projection=PRESERVED origins=INLINE,RULE_FILE,CANONICAL_RENDER"
        ),
        tuple(failures),
    )


def _lineage_case() -> StrategyDiscoveryAuditCase:
    failures: list[str] = []
    parent = parse_strategy_ast(_TRAFFIC_A)
    child = parse_strategy_ast(_TRAFFIC_CHILD)
    mutable_parameters: dict[str, object] = {
        "path": "/green_conditions/0/threshold",
        "threshold": {"coefficient": 3, "scale": 1},
    }
    substream = StrategyRngSubstreamV1(35_000_001, "mutation/threshold/0")
    node = build_strategy_lineage_node(
        parent,
        child,
        operation_id="THRESHOLD",
        operation_version=1,
        parameters=mutable_parameters,
        rng_substream=substream,
        valid=True,
    )
    repeated = build_strategy_lineage_node(
        parent,
        child,
        operation_id="THRESHOLD",
        operation_version=1,
        parameters=mutable_parameters,
        rng_substream=substream,
        valid=True,
    )
    mutable_parameters["threshold"] = {"coefficient": 999, "scale": 0}
    diff_paths = tuple(entry.path for entry in node.semantic_diff)
    if diff_paths != ("/green_conditions/0/threshold/coefficient",):
        failures.append("lineage semantic diff is not the exact canonical field change")
    if node.parent_semantic_sha256 != strategy_semantic_sha256(parent):
        failures.append("lineage parent semantic digest is not bound")
    if node.child_semantic_sha256 != strategy_semantic_sha256(child):
        failures.append("lineage child semantic digest is not bound")
    if node.lineage_sha256 != repeated.lineage_sha256:
        failures.append("identical lineage inputs did not reproduce identity")
    if node.lineage_sha256 != WO35A_LINEAGE_FIXTURE_SHA256:
        failures.append("lineage fixture differs from its frozen V1 identity")
    if thaw_json(node.parameters)["threshold"] != {
        "coefficient": 3,
        "scale": 1,
    }:
        failures.append("lineage parameters retained mutable caller ownership")
    no_op = build_strategy_lineage_node(
        parent,
        parent,
        operation_id="THRESHOLD",
        operation_version=1,
        parameters={"path": "/green_conditions/0/threshold"},
        rng_substream=substream,
        valid=False,
    )
    if no_op.valid or no_op.semantic_diff:
        failures.append("rejected no-op lineage did not retain explicit invalidity")
    if not _raises(
        lambda: build_strategy_lineage_node(
            parent,
            child,
            operation_id="THRESHOLD",
            operation_version=1,
            parameters={"binary_float": 0.3},
            rng_substream=substream,
            valid=True,
        ),
        TypeError,
    ):
        failures.append("lineage accepted a binary-float identity parameter")
    changed_rng = build_strategy_lineage_node(
        parent,
        child,
        operation_id="THRESHOLD",
        operation_version=1,
        parameters={
            "path": "/green_conditions/0/threshold",
            "threshold": {"coefficient": 3, "scale": 1},
        },
        rng_substream=StrategyRngSubstreamV1(
            35_000_001,
            "mutation/threshold/1",
        ),
        valid=True,
    )
    if changed_rng.lineage_sha256 == node.lineage_sha256:
        failures.append("lineage identity omitted the RNG substream")
    return StrategyDiscoveryAuditCase(
        "a_lineage_binds_parent_operation_parameters_rng_child_validity_and_diff",
        (
            f"lineage_sha256={node.lineage_sha256} diff_paths={','.join(diff_paths)} "
            f"rng_sha256={substream.sha256} no_op_valid={str(no_op.valid).lower()}"
        ),
        tuple(failures),
    )


def _unsupported_before_mutation_case() -> StrategyDiscoveryAuditCase:
    failures: list[str] = []
    unsupported_sources = (
        _TRAFFIC_A.replace(
            "spread_ticks <= 2.00",
            "spread_ticks <= 2 OR book_imbalance >= 0",
        ),
        _TRAFFIC_A.replace("spread_ticks <= 2.00", "spread_ticks <= 1e999999"),
    )
    mutation_attempts: list[str] = []
    errors: list[str] = []
    for unsupported in unsupported_sources:
        for _ in range(2):
            try:
                parse_strategy_ast(unsupported)
                mutation_attempts.append("MUTATION_STARTED")
            except (RuleSyntaxError, ValueError) as error:
                errors.append(f"{type(error).__name__}:{error}")
    if mutation_attempts:
        failures.append("unsupported grammar reached the mutation boundary")
    if (
        len(errors) != 4
        or errors[0] != errors[1]
        or errors[2] != errors[3]
    ):
        failures.append("unsupported grammar refusal was not deterministic")
    child = parse_strategy_ast(_TRAFFIC_CHILD)
    if not _raises(
        lambda: build_strategy_lineage_node(  # type: ignore[arg-type]
            _TRAFFIC_A,
            child,
            operation_id="THRESHOLD",
            operation_version=1,
            parameters={},
            rng_substream=StrategyRngSubstreamV1(1, "invalid/raw-parent"),
            valid=True,
        ),
        TypeError,
    ):
        failures.append("lineage builder accepted unparsed source text")
    return StrategyDiscoveryAuditCase(
        "a_unsupported_grammar_fails_deterministically_before_mutation",
        (
            f"parse_refusals={len(errors)}/4 mutation_attempts={len(mutation_attempts)} "
            "raw_lineage_parent=REFUSED"
        ),
        tuple(failures),
    )


def _raises(operation, expected: type[BaseException]) -> bool:
    try:
        operation()
    except expected:
        return True
    return False


def audit_wo35a_strategy_discovery() -> tuple[StrategyDiscoveryAuditCase, ...]:
    return (
        _parse_render_parse_case(),
        _equivalence_and_priority_case(),
        _dual_identity_case(),
        _lineage_case(),
        _unsupported_before_mutation_case(),
    )


__all__ = [
    "WO35A_AUDIT_CASE_COUNT",
    "WO35A_CANONICALIZATION_POLICY_SHA256",
    "WO35A_FIXTURE_SHA256",
    "WO35A_LINEAGE_FIXTURE_SHA256",
    "StrategyDiscoveryAuditCase",
    "audit_wo35a_strategy_discovery",
]
