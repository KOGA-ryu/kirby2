"""Adversarial audit cases for deterministic Hawkes stability certification."""

from __future__ import annotations

from dataclasses import dataclass

from kirby2.simulation.flow_models import (
    FLOW_CHANNELS,
    HawkesConfig,
    HawkesStabilityCertification,
    certify_hawkes_stability,
    load_accepted_hawkes_configs,
)


@dataclass(frozen=True, slots=True)
class HawkesStabilityAuditCase:
    name: str
    expected_classification: str
    certification: HawkesStabilityCertification
    constructor_rejected: bool | None = None

    @property
    def passed(self) -> bool:
        classification_matches = (
            self.certification.classification == self.expected_classification
        )
        rejection_matches = (
            self.constructor_rejected is None or self.constructor_rejected
        )
        return classification_matches and rejection_matches

    def as_dict(self) -> dict[str, object]:
        return {
            "actual": self.certification.classification,
            "constructor_rejected": self.constructor_rejected,
            "expected": self.expected_classification,
            "name": self.name,
            "passed": self.passed,
            "stability_certification": self.certification.as_dict(),
        }


def audit_hawkes_stability() -> tuple[HawkesStabilityAuditCase, ...]:
    cases = [
        _case(
            "zero_matrix",
            ((0.0, 0.0), (0.0, 0.0)),
            "PASS_SUBCRITICAL",
        ),
        _case(
            "periodic_stable",
            ((0.0, 0.5), (0.5, 0.0)),
            "PASS_SUBCRITICAL",
        ),
        _case(
            "near_critical_subcritical",
            ((0.95, 0.0), (0.0, 0.2)),
            "WARNING_NEAR_CRITICAL",
        ),
        _case(
            "critical_margin_ambiguous",
            ((1.0 - 5e-10, 0.0), (0.0, 0.2)),
            "REJECT_AMBIGUOUS",
        ),
        _case(
            "reducible_matrix",
            (
                (0.0, 0.64, 0.0),
                (0.25, 0.0, 0.0),
                (0.0, 0.0, 0.3),
            ),
            "PASS_SUBCRITICAL",
        ),
    ]

    supercritical_matrix = ((0.0, 4.0), (0.4, 0.0))
    supercritical = certify_hawkes_stability(supercritical_matrix)
    cases.append(
        HawkesStabilityAuditCase(
            name="periodic_supercritical",
            expected_classification="REJECT_SUPERCRITICAL",
            certification=supercritical,
            constructor_rejected=_constructor_rejects_periodic_cycle(),
        )
    )
    cases.append(
        HawkesStabilityAuditCase(
            name="forced_nonconvergence",
            expected_classification="REJECT_UNVERIFIED",
            certification=certify_hawkes_stability(
                ((0.2, 0.6), (0.1, 0.2)),
                max_iterations=1,
            ),
        )
    )

    expected_profiles = {
        "absorption": "PASS_SUBCRITICAL",
        "balanced": "PASS_SUBCRITICAL",
        "momentum": "PASS_SUBCRITICAL",
        "panic": "WARNING_NEAR_CRITICAL",
    }
    for profile_id, config in sorted(load_accepted_hawkes_configs().items()):
        cases.append(
            HawkesStabilityAuditCase(
                name=f"accepted_profile_{profile_id}",
                expected_classification=expected_profiles.get(
                    profile_id,
                    "EXPECTED_CLASSIFICATION_NOT_REGISTERED",
                ),
                certification=config.stability_certification,
            )
        )
    return tuple(cases)


def _case(
    name: str,
    matrix: tuple[tuple[float, ...], ...],
    expected: str,
) -> HawkesStabilityAuditCase:
    return HawkesStabilityAuditCase(
        name=name,
        expected_classification=expected,
        certification=certify_hawkes_stability(matrix),
    )


def _constructor_rejects_periodic_cycle() -> bool:
    size = len(FLOW_CHANNELS)
    alpha = [[0.0 for _ in range(size)] for _ in range(size)]
    alpha[0][1] = 4.0
    alpha[1][0] = 0.4
    try:
        HawkesConfig(
            profile_id="audit_periodic_supercritical",
            baseline_mu=tuple(0.1 for _ in range(size)),
            alpha=tuple(tuple(row) for row in alpha),
            beta=tuple(tuple(1.0 for _ in range(size)) for _ in range(size)),
        )
    except ValueError as error:
        return "supercritical Hawkes configuration rejected" in str(error)
    return False
