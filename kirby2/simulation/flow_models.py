"""Composable arrival models for the six canonical order-flow channels."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Mapping, Protocol, runtime_checkable

from .flow import FlowEventFamily
from .rng import SeededRng


FLOW_CHANNELS = tuple(FlowEventFamily)
ACCEPTED_HAWKES_PATH = Path(__file__).with_name("accepted_hawkes.json")
HAWKES_STABILITY_METHOD = "SCC_SHIFTED_COLLATZ_WIELANDT"
HAWKES_STABILITY_ACCEPTANCE_MARGIN = 1e-9
HAWKES_STABILITY_TOLERANCE = 1e-12
HAWKES_STABILITY_MAX_ITERATIONS = 4_096


@dataclass(frozen=True, slots=True)
class ScheduledFlowArrival:
    simulation_time_us: int
    family: FlowEventFamily


@runtime_checkable
class FlowModel(Protocol):
    """Arrival-model boundary; matching remains entirely inside OrderBook."""

    @property
    def model_name(self) -> str: ...

    def schedule_next(
        self,
        current_time_us: int,
        baseline_intensities: Mapping[FlowEventFamily, float],
        rng: SeededRng,
    ) -> ScheduledFlowArrival | None: ...

    def observe(self, family: FlowEventFamily, simulation_time_us: int) -> None: ...

    def current_intensities(
        self,
        simulation_time_us: int,
        baseline_intensities: Mapping[FlowEventFamily, float],
    ) -> dict[FlowEventFamily, float]: ...

    def diagnostics(self) -> dict[str, object]: ...

    def replay_config(self) -> dict[str, object] | None: ...


class SimpleFlowModel:
    """Competing independent Poisson arrivals, retained as the baseline."""

    @property
    def model_name(self) -> str:
        return "simple"

    def schedule_next(
        self,
        current_time_us: int,
        baseline_intensities: Mapping[FlowEventFamily, float],
        rng: SeededRng,
    ) -> ScheduledFlowArrival | None:
        intensities = _validated_intensities(baseline_intensities)
        weights = tuple(intensities[family] for family in FLOW_CHANNELS)
        total_intensity = sum(weights)
        if total_intensity <= 0:
            return None
        arrival_time_us = (
            current_time_us
            + rng.exponential_interval_microseconds(total_intensity)
        )
        family = FLOW_CHANNELS[rng.weighted_float_index(weights)]
        return ScheduledFlowArrival(arrival_time_us, family)

    def observe(self, family: FlowEventFamily, simulation_time_us: int) -> None:
        return None

    def current_intensities(
        self,
        simulation_time_us: int,
        baseline_intensities: Mapping[FlowEventFamily, float],
    ) -> dict[FlowEventFamily, float]:
        return _validated_intensities(baseline_intensities)

    def diagnostics(self) -> dict[str, object]:
        return {
            "bounded_state_cells": 0,
            "model": self.model_name,
            "stability": "POISSON_BASELINE",
        }

    def replay_config(self) -> dict[str, object] | None:
        return None


@dataclass(frozen=True, slots=True)
class HawkesStabilityCertification:
    """Deterministic lower/upper stability evidence for a branching matrix."""

    method: str
    lower_bound: float
    upper_bound: float
    iterations: int
    component_count: int
    converged: bool
    acceptance_margin: float
    warning_threshold: float
    classification: str

    @property
    def accepted(self) -> bool:
        return self.classification in {
            "PASS_SUBCRITICAL",
            "WARNING_NEAR_CRITICAL",
        }

    @property
    def spectral_radius(self) -> float:
        return (self.lower_bound + self.upper_bound) / 2.0

    @property
    def safety_margin(self) -> float:
        return 1.0 - self.upper_bound

    def as_dict(self) -> dict[str, object]:
        return {
            "acceptance_margin": self.acceptance_margin,
            "classification": self.classification,
            "component_count": self.component_count,
            "converged": self.converged,
            "iterations": self.iterations,
            "lower_bound": _diagnostic_number(self.lower_bound),
            "method": self.method,
            "safety_margin": _diagnostic_number(self.safety_margin),
            "upper_bound": _diagnostic_number(self.upper_bound),
            "warning_threshold": self.warning_threshold,
        }


@dataclass(frozen=True, slots=True)
class HawkesConfig:
    profile_id: str
    baseline_mu: tuple[float, ...]
    alpha: tuple[tuple[float, ...], ...]
    beta: tuple[tuple[float, ...], ...]
    max_total_intensity: float = 120.0
    stability_warning_threshold: float = 0.80
    _stability_certification: HawkesStabilityCertification = field(
        init=False,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        channel_count = len(FLOW_CHANNELS)
        if not self.profile_id:
            raise ValueError("Hawkes profile ID must not be empty")
        if len(self.baseline_mu) != channel_count:
            raise ValueError("Hawkes baseline mu must cover every flow channel")
        if any(not math.isfinite(value) or value < 0 for value in self.baseline_mu):
            raise ValueError("Hawkes baseline mu values must be finite and nonnegative")
        for name, matrix in (("alpha", self.alpha), ("beta", self.beta)):
            if len(matrix) != channel_count or any(
                len(row) != channel_count for row in matrix
            ):
                raise ValueError(f"Hawkes {name} must be a square channel matrix")
        if any(
            not math.isfinite(value) or value < 0
            for row in self.alpha
            for value in row
        ):
            raise ValueError("Hawkes alpha values must be finite and nonnegative")
        if any(
            not math.isfinite(value) or value <= 0
            for row in self.beta
            for value in row
        ):
            raise ValueError("Hawkes beta values must be finite and positive")
        if not math.isfinite(self.max_total_intensity) or self.max_total_intensity <= 0:
            raise ValueError("Hawkes maximum total intensity must be finite and positive")
        if sum(self.baseline_mu) > self.max_total_intensity:
            raise ValueError("Hawkes baseline exceeds the configured intensity cap")
        if not 0 < self.stability_warning_threshold < 1:
            raise ValueError("Hawkes warning threshold must lie strictly between zero and one")
        certification = certify_hawkes_stability(
            self.branching_matrix,
            warning_threshold=self.stability_warning_threshold,
        )
        object.__setattr__(self, "_stability_certification", certification)
        if certification.classification == "REJECT_SUPERCRITICAL":
            raise ValueError(
                "supercritical Hawkes configuration rejected: "
                f"branching spectral radius lower bound="
                f"{certification.lower_bound:.12f}"
            )
        if certification.classification == "REJECT_AMBIGUOUS":
            raise ValueError(
                "near-critical Hawkes configuration rejected: stability bounds "
                f"[{certification.lower_bound:.12f}, "
                f"{certification.upper_bound:.12f}] do not preserve the "
                f"{certification.acceptance_margin:.1e} acceptance margin"
            )
        if certification.classification == "REJECT_UNVERIFIED":
            raise ValueError(
                "Hawkes configuration rejected: stability could not be verified "
                f"after {certification.iterations} deterministic iterations; "
                f"bounds=[{certification.lower_bound:.12f}, "
                f"{certification.upper_bound:.12f}]"
            )

    @property
    def branching_matrix(self) -> tuple[tuple[float, ...], ...]:
        return tuple(
            tuple(
                self.alpha[target][source] / self.beta[target][source]
                for source in range(len(FLOW_CHANNELS))
            )
            for target in range(len(FLOW_CHANNELS))
        )

    @property
    def spectral_radius(self) -> float:
        return self._stability_certification.spectral_radius

    @property
    def stability_certification(self) -> HawkesStabilityCertification:
        return self._stability_certification

    @property
    def stability_status(self) -> str:
        return self._stability_certification.classification

    def as_dict(self) -> dict[str, object]:
        return {
            "alpha": _matrix_dict(self.alpha),
            "baseline_mu": {
                family.value: self.baseline_mu[index]
                for index, family in enumerate(FLOW_CHANNELS)
            },
            "beta": _matrix_dict(self.beta),
            "branching_spectral_radius": round(self.spectral_radius, 9),
            "max_total_intensity": self.max_total_intensity,
            "profile_id": self.profile_id,
            "stability_status": self.stability_status,
        }


class HawkesFlowModel:
    """Multivariate exponential-kernel Hawkes arrivals using constant memory."""

    def __init__(
        self,
        config: HawkesConfig,
        *,
        use_runtime_baseline: bool = True,
    ) -> None:
        if type(use_runtime_baseline) is not bool:
            raise TypeError("Hawkes runtime-baseline selection must be boolean")
        self.config = config
        self.use_runtime_baseline = use_runtime_baseline
        size = len(FLOW_CHANNELS)
        self._excitation = [[0.0 for _ in range(size)] for _ in range(size)]
        self._state_time_us = 0
        self._observed_events = 0
        self._intensity_cap_hits = 0
        self._thinning_rejections = 0

    @property
    def model_name(self) -> str:
        return "hawkes"

    def schedule_next(
        self,
        current_time_us: int,
        baseline_intensities: Mapping[FlowEventFamily, float],
        rng: SeededRng,
    ) -> ScheduledFlowArrival | None:
        if current_time_us < self._state_time_us:
            raise ValueError("Hawkes scheduling time cannot precede model state")
        baseline = self._baseline(baseline_intensities)
        local_state = self._decayed_copy(current_time_us)
        cursor_us = current_time_us
        for _ in range(10_000):
            upper_values = self._bounded_values(
                baseline,
                local_state,
                count_cap_hit=False,
            )
            upper_total = sum(upper_values)
            if upper_total <= 0:
                return None
            candidate_us = (
                cursor_us + rng.exponential_interval_microseconds(upper_total)
            )
            self._decay_matrix(local_state, candidate_us - cursor_us)
            candidate_values = self._bounded_values(
                baseline,
                local_state,
                count_cap_hit=True,
            )
            candidate_total = sum(candidate_values)
            if rng.unit_interval() * upper_total <= candidate_total:
                family = FLOW_CHANNELS[
                    rng.weighted_float_index(candidate_values)
                ]
                return ScheduledFlowArrival(candidate_us, family)
            self._thinning_rejections += 1
            cursor_us = candidate_us
        raise RuntimeError("Hawkes thinning exceeded 10,000 proposals")

    def observe(self, family: FlowEventFamily, simulation_time_us: int) -> None:
        self._decay_to(simulation_time_us)
        source = FLOW_CHANNELS.index(family)
        for target in range(len(FLOW_CHANNELS)):
            self._excitation[target][source] += self.config.alpha[target][source]
        self._observed_events += 1

    def current_intensities(
        self,
        simulation_time_us: int,
        baseline_intensities: Mapping[FlowEventFamily, float],
    ) -> dict[FlowEventFamily, float]:
        baseline = self._baseline(baseline_intensities)
        state = self._decayed_copy(simulation_time_us)
        values = self._bounded_values(baseline, state, count_cap_hit=False)
        return dict(zip(FLOW_CHANNELS, values))

    def diagnostics(self) -> dict[str, object]:
        return {
            "bounded_state_cells": len(FLOW_CHANNELS) ** 2,
            "branching_spectral_radius": round(self.config.spectral_radius, 9),
            "baseline_source": (
                "runtime_regime_policy"
                if self.use_runtime_baseline
                else "accepted_profile_mu"
            ),
            "intensity_cap_hits": self._intensity_cap_hits,
            "model": self.model_name,
            "observed_events": self._observed_events,
            "profile_id": self.config.profile_id,
            "stability": self.config.stability_status,
            "stability_certification": self.config.stability_certification.as_dict(),
            "thinning_rejections": self._thinning_rejections,
        }

    def replay_config(self) -> dict[str, object]:
        return {
            "baseline_source": (
                "runtime_regime_policy"
                if self.use_runtime_baseline
                else "accepted_profile_mu"
            ),
            "model": self.model_name,
            **self.config.as_dict(),
        }

    def _baseline(
        self,
        runtime: Mapping[FlowEventFamily, float],
    ) -> dict[FlowEventFamily, float]:
        validated = _validated_intensities(runtime)
        if self.use_runtime_baseline:
            return validated
        return {
            family: self.config.baseline_mu[index]
            for index, family in enumerate(FLOW_CHANNELS)
        }

    def _decay_to(self, simulation_time_us: int) -> None:
        if simulation_time_us < self._state_time_us:
            raise ValueError("Hawkes observation time cannot move backward")
        self._decay_matrix(
            self._excitation,
            simulation_time_us - self._state_time_us,
        )
        self._state_time_us = simulation_time_us

    def _decayed_copy(self, simulation_time_us: int) -> list[list[float]]:
        if simulation_time_us < self._state_time_us:
            raise ValueError("Hawkes inspection time cannot precede model state")
        copied = [row.copy() for row in self._excitation]
        self._decay_matrix(copied, simulation_time_us - self._state_time_us)
        return copied

    def _decay_matrix(self, matrix: list[list[float]], delta_us: int) -> None:
        if delta_us <= 0:
            return
        delta_seconds = delta_us / 1_000_000.0
        for target in range(len(FLOW_CHANNELS)):
            for source in range(len(FLOW_CHANNELS)):
                matrix[target][source] *= math.exp(
                    -self.config.beta[target][source] * delta_seconds
                )

    def _bounded_values(
        self,
        baseline: Mapping[FlowEventFamily, float],
        state: list[list[float]],
        *,
        count_cap_hit: bool,
    ) -> tuple[float, ...]:
        values = tuple(
            baseline[family] + sum(state[target])
            for target, family in enumerate(FLOW_CHANNELS)
        )
        total = sum(values)
        if total <= self.config.max_total_intensity:
            return values
        if count_cap_hit:
            self._intensity_cap_hits += 1
        scale = self.config.max_total_intensity / total
        return tuple(value * scale for value in values)


def load_accepted_hawkes_configs(
    path: Path = ACCEPTED_HAWKES_PATH,
) -> dict[str, HawkesConfig]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != 1:
        raise ValueError("unsupported accepted Hawkes schema")
    channels = [family.value for family in FLOW_CHANNELS]
    if payload.get("channels") != channels:
        raise ValueError("accepted Hawkes channels do not match canonical flow order")
    profiles = payload.get("profiles")
    if not isinstance(profiles, list) or any(
        not isinstance(profile, dict) for profile in profiles
    ):
        raise ValueError("accepted Hawkes profiles must be objects")
    configs = [_hawkes_config(profile) for profile in profiles]
    by_id = {config.profile_id: config for config in configs}
    if len(by_id) != len(configs):
        raise ValueError("accepted Hawkes profile IDs must be unique")
    required = {"balanced", "momentum", "panic", "absorption"}
    if not required.issubset(by_id):
        raise ValueError("accepted Hawkes profiles omit a required configuration")
    return by_id


def _hawkes_config(payload: dict[str, object]) -> HawkesConfig:
    baseline = payload.get("baseline_mu")
    alpha = payload.get("alpha")
    beta = payload.get("beta")
    if not isinstance(baseline, dict) or not isinstance(alpha, dict) or not isinstance(beta, dict):
        raise ValueError("Hawkes baseline, alpha, and beta must be objects")
    return HawkesConfig(
        profile_id=str(payload["profile_id"]),
        baseline_mu=tuple(float(baseline[family.value]) for family in FLOW_CHANNELS),
        alpha=_parse_matrix(alpha, default=0.0),
        beta=_parse_matrix(beta, default=float(payload.get("default_beta", 4.0))),
        max_total_intensity=float(payload.get("max_total_intensity", 120.0)),
        stability_warning_threshold=float(
            payload.get("stability_warning_threshold", 0.80)
        ),
    )


def _parse_matrix(
    payload: dict[str, object],
    *,
    default: float,
) -> tuple[tuple[float, ...], ...]:
    rows: list[tuple[float, ...]] = []
    for target in FLOW_CHANNELS:
        raw_row = payload.get(target.value, {})
        if not isinstance(raw_row, dict):
            raise ValueError(f"Hawkes matrix row {target.value} must be an object")
        unknown = set(raw_row) - {family.value for family in FLOW_CHANNELS}
        if unknown:
            raise ValueError(f"unknown Hawkes source channels: {sorted(unknown)!r}")
        rows.append(
            tuple(float(raw_row.get(source.value, default)) for source in FLOW_CHANNELS)
        )
    return tuple(rows)


def _validated_intensities(
    intensities: Mapping[FlowEventFamily, float],
) -> dict[FlowEventFamily, float]:
    if set(intensities) != set(FLOW_CHANNELS):
        raise ValueError("flow intensities must cover all canonical channels")
    result = {family: float(intensities[family]) for family in FLOW_CHANNELS}
    if any(not math.isfinite(value) or value < 0 for value in result.values()):
        raise ValueError("flow intensities must be finite and nonnegative")
    return result


def _matrix_dict(matrix: tuple[tuple[float, ...], ...]) -> dict[str, object]:
    return {
        target.value: {
            source.value: matrix[target_index][source_index]
            for source_index, source in enumerate(FLOW_CHANNELS)
        }
        for target_index, target in enumerate(FLOW_CHANNELS)
    }


def certify_hawkes_stability(
    matrix: tuple[tuple[float, ...], ...],
    *,
    warning_threshold: float = 0.80,
    acceptance_margin: float = HAWKES_STABILITY_ACCEPTANCE_MARGIN,
    tolerance: float = HAWKES_STABILITY_TOLERANCE,
    max_iterations: int = HAWKES_STABILITY_MAX_ITERATIONS,
) -> HawkesStabilityCertification:
    """Bound the Perron root without assuming an aperiodic full matrix.

    A reducible nonnegative matrix is permutation-similar to block triangular form,
    and its spectral radius is the maximum radius of its strongly connected diagonal
    blocks. Each nontrivial irreducible block is shifted by the identity, making it
    primitive, before deterministic Collatz-Wielandt iteration. The lower and upper
    ratios from every iteration remain valid bounds; a configuration is accepted only
    when those bounds converge and preserve the critical safety margin.
    """

    normalized = _validated_nonnegative_matrix(matrix)
    if not math.isfinite(warning_threshold) or not 0 < warning_threshold < 1:
        raise ValueError("Hawkes warning threshold must lie strictly between zero and one")
    if not math.isfinite(acceptance_margin) or not 0 < acceptance_margin < 1:
        raise ValueError("Hawkes acceptance margin must lie strictly between zero and one")
    if not math.isfinite(tolerance) or tolerance <= 0:
        raise ValueError("Hawkes stability tolerance must be finite and positive")
    if type(max_iterations) is not int or max_iterations <= 0:
        raise ValueError("Hawkes stability iteration limit must be a positive integer")

    components = _strongly_connected_components(normalized)
    lower_bound = 0.0
    upper_bound = 0.0
    iterations = 0
    converged = True
    for component in components:
        block = tuple(
            tuple(normalized[row][column] for column in component)
            for row in component
        )
        block_lower, block_upper, block_iterations, block_converged = (
            _shifted_collatz_wielandt_bounds(
                block,
                tolerance=tolerance,
                max_iterations=max_iterations,
            )
        )
        lower_bound = max(lower_bound, block_lower)
        upper_bound = max(upper_bound, block_upper)
        iterations += block_iterations
        converged = converged and block_converged

    if lower_bound >= 1.0:
        classification = "REJECT_SUPERCRITICAL"
    elif not converged:
        classification = "REJECT_UNVERIFIED"
    elif upper_bound >= 1.0 - acceptance_margin:
        classification = "REJECT_AMBIGUOUS"
    elif upper_bound >= warning_threshold:
        classification = "WARNING_NEAR_CRITICAL"
    else:
        classification = "PASS_SUBCRITICAL"
    return HawkesStabilityCertification(
        method=HAWKES_STABILITY_METHOD,
        lower_bound=lower_bound,
        upper_bound=upper_bound,
        iterations=iterations,
        component_count=len(components),
        converged=converged,
        acceptance_margin=acceptance_margin,
        warning_threshold=warning_threshold,
        classification=classification,
    )


def _validated_nonnegative_matrix(
    matrix: tuple[tuple[float, ...], ...],
) -> tuple[tuple[float, ...], ...]:
    if not matrix:
        raise ValueError("Hawkes branching matrix must not be empty")
    size = len(matrix)
    if any(len(row) != size for row in matrix):
        raise ValueError("Hawkes branching matrix must be square")
    try:
        normalized = tuple(tuple(float(value) for value in row) for row in matrix)
    except (OverflowError, TypeError, ValueError) as error:
        raise ValueError("Hawkes branching values must be finite numbers") from error
    if any(
        not math.isfinite(value) or value < 0
        for row in normalized
        for value in row
    ):
        raise ValueError("Hawkes branching values must be finite and nonnegative")
    return normalized


def _strongly_connected_components(
    matrix: tuple[tuple[float, ...], ...],
) -> tuple[tuple[int, ...], ...]:
    size = len(matrix)
    adjacency = tuple(
        tuple(target for target in range(size) if matrix[target][source] > 0)
        for source in range(size)
    )
    next_index = 0
    indices = [-1 for _ in range(size)]
    low_links = [0 for _ in range(size)]
    stack: list[int] = []
    on_stack = [False for _ in range(size)]
    components: list[tuple[int, ...]] = []

    def visit(node: int) -> None:
        nonlocal next_index
        indices[node] = next_index
        low_links[node] = next_index
        next_index += 1
        stack.append(node)
        on_stack[node] = True
        for neighbor in adjacency[node]:
            if indices[neighbor] == -1:
                visit(neighbor)
                low_links[node] = min(low_links[node], low_links[neighbor])
            elif on_stack[neighbor]:
                low_links[node] = min(low_links[node], indices[neighbor])
        if low_links[node] != indices[node]:
            return
        component: list[int] = []
        while True:
            member = stack.pop()
            on_stack[member] = False
            component.append(member)
            if member == node:
                break
        components.append(tuple(sorted(component)))

    for node in range(size):
        if indices[node] == -1:
            visit(node)
    return tuple(sorted(components, key=lambda component: component[0]))


def _shifted_collatz_wielandt_bounds(
    block: tuple[tuple[float, ...], ...],
    *,
    tolerance: float,
    max_iterations: int,
) -> tuple[float, float, int, bool]:
    size = len(block)
    if size == 1:
        radius = block[0][0]
        return radius, radius, 0, True

    shifted = tuple(
        tuple(
            value + (1.0 if row == column else 0.0)
            for column, value in enumerate(values)
        )
        for row, values in enumerate(block)
    )
    vector = [1.0 / size for _ in range(size)]
    best_lower = 0.0
    best_upper = math.inf
    for iteration in range(1, max_iterations + 1):
        iteration_lower, iteration_upper = _exact_collatz_ratio_bounds(block, vector)
        best_lower = max(best_lower, iteration_lower)
        best_upper = min(best_upper, iteration_upper)
        if best_lower == math.inf:
            return math.inf, math.inf, iteration, True
        if best_lower > best_upper:
            return (
                max(0.0, best_upper - 1.0),
                max(0.0, best_lower - 1.0),
                iteration,
                False,
            )
        width = best_upper - best_lower
        if width <= tolerance * max(1.0, abs(best_upper)):
            return (
                max(0.0, best_lower - 1.0),
                max(0.0, best_upper - 1.0),
                iteration,
                True,
            )
        try:
            multiplied = [
                math.fsum(
                    shifted[row][column] * vector[column]
                    for column in range(size)
                )
                for row in range(size)
            ]
        except OverflowError:
            return max(0.0, best_lower - 1.0), math.inf, iteration, False
        if any(not math.isfinite(value) or value <= 0 for value in multiplied):
            return max(0.0, best_lower - 1.0), math.inf, iteration, False
        scale = max(multiplied)
        vector = [value / scale for value in multiplied]
    return (
        max(0.0, best_lower - 1.0),
        max(0.0, best_upper - 1.0),
        max_iterations,
        False,
    )


def _exact_collatz_ratio_bounds(
    block: tuple[tuple[float, ...], ...],
    vector: list[float],
) -> tuple[float, float]:
    """Return outward-rounded ratios for the exact binary-float inputs."""

    exact_vector = tuple(Fraction.from_float(value) for value in vector)
    ratios: list[Fraction] = []
    for row, values in enumerate(block):
        numerator = Fraction(0)
        for column, value in enumerate(values):
            coefficient = Fraction.from_float(value)
            if row == column:
                coefficient += 1
            numerator += coefficient * exact_vector[column]
        ratios.append(numerator / exact_vector[row])
    return _fraction_lower_float(min(ratios)), _fraction_upper_float(max(ratios))


def _fraction_lower_float(value: Fraction) -> float:
    try:
        result = float(value)
    except OverflowError:
        return math.inf
    if math.isfinite(result) and Fraction.from_float(result) > value:
        return math.nextafter(result, -math.inf)
    return result


def _fraction_upper_float(value: Fraction) -> float:
    try:
        result = float(value)
    except OverflowError:
        return math.inf
    if math.isfinite(result) and Fraction.from_float(result) < value:
        return math.nextafter(result, math.inf)
    return result


def _diagnostic_number(value: float) -> float | str:
    if math.isinf(value):
        return "INFINITY" if value > 0 else "-INFINITY"
    if math.isnan(value):
        return "NAN"
    return round(value, 12)
