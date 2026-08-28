"""Deterministic participant-schedule orchestration for a composed full day.

The coordinator in this module owns no clock, exchange, order book, or random
stream.  It applies the immutable :class:`FullDayPlanV1` participant schedule
through the injected ``AgentScheduler`` lifecycle API and retains only the
configuration registry and exact lifecycle history needed to restore future
retunes without rewriting earlier participant state.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from kirby2.agents.models import AgentSpec

from .models import (
    FullDayPlanV1,
    ParticipantScheduleActionV1,
    VersionedReferenceV1,
    _require_exact_fields,
    canonical_json_bytes,
    canonical_sha256,
    validate_strict_json,
)


PARTICIPANT_SCHEDULE_RUNTIME_SCHEMA_VERSION = 1


def _exact_int(value: object, field: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{field} must be an integer at least {minimum}")
    return value


def _exact_string(value: object, field: str) -> str:
    if type(value) is not str or not value:
        raise ValueError(f"{field} must be a nonempty string")
    return value


@dataclass(frozen=True, slots=True)
class ParticipantLifecycleRecordV1:
    """One completed immutable participant-schedule row."""

    schedule_id: str
    simulation_time_us: int
    participant_id: str
    action: ParticipantScheduleActionV1
    prior_specification: VersionedReferenceV1
    resulting_specification: VersionedReferenceV1
    replacement_generation: int
    discarded_pending_sequences: tuple[int, ...]
    cancelled_order_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _exact_string(self.schedule_id, "participant lifecycle schedule ID")
        _exact_string(self.participant_id, "participant lifecycle participant ID")
        _exact_int(self.simulation_time_us, "participant lifecycle time")
        if type(self.action) is not ParticipantScheduleActionV1:
            raise TypeError("participant lifecycle action uses the wrong enum")
        if type(self.prior_specification) is not VersionedReferenceV1 or type(
            self.resulting_specification
        ) is not VersionedReferenceV1:
            raise TypeError("participant lifecycle specifications must be versioned")
        _exact_int(
            self.replacement_generation,
            "participant lifecycle replacement generation",
        )
        if (
            type(self.discarded_pending_sequences) is not tuple
            or any(
                type(value) is not int or value < 0
                for value in self.discarded_pending_sequences
            )
            or self.discarded_pending_sequences
            != tuple(sorted(set(self.discarded_pending_sequences)))
        ):
            raise ValueError(
                "discarded participant sequences must be sorted unique integers"
            )
        if (
            type(self.cancelled_order_ids) is not tuple
            or any(type(value) is not str or not value for value in self.cancelled_order_ids)
            or self.cancelled_order_ids != tuple(sorted(set(self.cancelled_order_ids)))
        ):
            raise ValueError(
                "cancelled participant order IDs must be sorted and unique"
            )
        if self.action is not ParticipantScheduleActionV1.DEACTIVATE and (
            self.discarded_pending_sequences or self.cancelled_order_ids
        ):
            raise ValueError(
                "only participant deactivation may discard pending work or cancel orders"
            )
        if self.action is ParticipantScheduleActionV1.RETUNE:
            if (
                self.prior_specification.reference_id
                != self.resulting_specification.reference_id
                or self.resulting_specification.version
                <= self.prior_specification.version
                or self.prior_specification.sha256
                == self.resulting_specification.sha256
            ):
                raise ValueError(
                    "participant retune must advance one immutable spec identity"
                )
        elif self.prior_specification != self.resulting_specification:
            raise ValueError("activate/deactivate cannot change participant specification")

    def as_dict(self) -> dict[str, object]:
        return {
            "action": self.action.value,
            "cancelled_order_ids": list(self.cancelled_order_ids),
            "discarded_pending_sequences": list(
                self.discarded_pending_sequences
            ),
            "participant_id": self.participant_id,
            "prior_specification": self.prior_specification.as_dict(),
            "replacement_generation": self.replacement_generation,
            "resulting_specification": self.resulting_specification.as_dict(),
            "schedule_id": self.schedule_id,
            "simulation_time_us": self.simulation_time_us,
        }

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, object]
    ) -> ParticipantLifecycleRecordV1:
        _require_exact_fields(
            payload,
            {
                "action",
                "cancelled_order_ids",
                "discarded_pending_sequences",
                "participant_id",
                "prior_specification",
                "replacement_generation",
                "resulting_specification",
                "schedule_id",
                "simulation_time_us",
            },
            "participant lifecycle record",
        )
        prior = payload["prior_specification"]
        resulting = payload["resulting_specification"]
        discarded = payload["discarded_pending_sequences"]
        cancelled = payload["cancelled_order_ids"]
        if not isinstance(prior, Mapping) or not isinstance(resulting, Mapping):
            raise TypeError("participant lifecycle specifications must be objects")
        if type(discarded) is not list or type(cancelled) is not list:
            raise TypeError("participant lifecycle arrays must use JSON arrays")
        return cls(
            schedule_id=_exact_string(payload["schedule_id"], "schedule_id"),
            simulation_time_us=_exact_int(
                payload["simulation_time_us"], "simulation_time_us"
            ),
            participant_id=_exact_string(
                payload["participant_id"], "participant_id"
            ),
            action=ParticipantScheduleActionV1(
                _exact_string(payload["action"], "action")
            ),
            prior_specification=VersionedReferenceV1.from_dict(prior),
            resulting_specification=VersionedReferenceV1.from_dict(resulting),
            replacement_generation=_exact_int(
                payload["replacement_generation"], "replacement_generation"
            ),
            discarded_pending_sequences=tuple(
                _exact_int(value, "discarded pending sequence")
                for value in discarded
            ),
            cancelled_order_ids=tuple(
                _exact_string(value, "cancelled order ID") for value in cancelled
            ),
        )


class ParticipantScheduleRuntimeV1:
    """Apply and restore one plan-bound participant lifecycle schedule."""

    def __init__(
        self,
        plan: FullDayPlanV1,
        specifications: Sequence[AgentSpec] = (),
        *,
        next_index: int = 0,
        active: Mapping[str, bool] | None = None,
        specification_bindings: Mapping[str, VersionedReferenceV1] | None = None,
        replacement_generations: Mapping[str, int] | None = None,
        history: Sequence[ParticipantLifecycleRecordV1] = (),
    ) -> None:
        if type(plan) is not FullDayPlanV1:
            raise TypeError("participant schedule runtime requires FullDayPlanV1")
        self.plan_sha256 = plan.semantic_sha256
        self._plan = plan
        participant_ids = tuple(
            participant.participant_id for participant in plan.participant_definitions
        )
        self._definitions = {
            participant.participant_id: participant
            for participant in plan.participant_definitions
        }
        declared_bindings = {
            participant.participant_id: participant.specification
            for participant in plan.participant_definitions
        }
        for entry in plan.participant_schedule:
            replacement = entry.replacement_specification
            if replacement is None:
                continue
            prior = declared_bindings[entry.participant_id]
            if (
                prior.reference_id != replacement.reference_id
                or replacement.version <= prior.version
                or prior.sha256 == replacement.sha256
            ):
                raise ValueError(
                    "participant retune schedule must advance an immutable spec "
                    "identity"
                )
            declared_bindings[entry.participant_id] = replacement
        rows = tuple(specifications)
        if any(type(specification) is not AgentSpec for specification in rows):
            raise TypeError("participant specification registry requires AgentSpec")
        registry: dict[tuple[str, str], AgentSpec] = {}
        allowed_references = {
            participant.participant_id: {
                participant.specification,
                *(
                    entry.replacement_specification
                    for entry in plan.participant_schedule
                    if entry.participant_id == participant.participant_id
                    and entry.replacement_specification is not None
                ),
            }
            for participant in plan.participant_definitions
        }
        for specification in rows:
            if specification.agent_id not in self._definitions:
                raise ValueError(
                    "participant specification registry contains an unknown identity"
                )
            digest = canonical_sha256(specification.identity_dict())
            if digest not in {
                reference.sha256
                for reference in allowed_references[specification.agent_id]
            }:
                raise ValueError(
                    "participant specification bytes are absent from plan references"
                )
            key = (specification.agent_id, digest)
            prior = registry.get(key)
            if prior is not None and prior.identity_dict() != specification.identity_dict():
                raise ValueError("participant specification registry digest collision")
            registry[key] = specification
        self._specification_registry = registry
        self.next_index = _exact_int(next_index, "participant schedule next index")
        self.active = (
            {
                participant.participant_id: participant.initially_active
                for participant in plan.participant_definitions
            }
            if active is None
            else dict(active)
        )
        self.specification_bindings = (
            {
                participant.participant_id: participant.specification
                for participant in plan.participant_definitions
            }
            if specification_bindings is None
            else dict(specification_bindings)
        )
        self.replacement_generations = (
            {participant_id: 0 for participant_id in participant_ids}
            if replacement_generations is None
            else dict(replacement_generations)
        )
        self.history = list(history)
        self.assert_invariants()

    @classmethod
    def create(
        cls,
        plan: FullDayPlanV1,
        *,
        scheduler: object | None,
        additional_specifications: Sequence[AgentSpec] = (),
    ) -> ParticipantScheduleRuntimeV1:
        initial = ()
        if scheduler is not None:
            definition = getattr(scheduler, "definition", None)
            initial = tuple(getattr(definition, "agents", ()))
        return cls(plan, (*initial, *tuple(additional_specifications)))

    @property
    def completed_schedule_ids(self) -> tuple[str, ...]:
        return tuple(record.schedule_id for record in self.history)

    @property
    def specification_registry(self) -> tuple[AgentSpec, ...]:
        return tuple(
            self._specification_registry[key]
            for key in sorted(self._specification_registry)
        )

    def apply_due(
        self,
        schedule_id: str,
        *,
        simulation_time_us: int,
        scheduler: object,
    ) -> ParticipantLifecycleRecordV1:
        """Apply exactly the next plan row through ordinary scheduler hooks."""

        if self.next_index >= len(self._plan.participant_schedule):
            raise RuntimeError("participant schedule is already exhausted")
        entry = self._plan.participant_schedule[self.next_index]
        if entry.schedule_id != schedule_id:
            raise RuntimeError("participant schedule cursor is not canonical")
        if entry.simulation_time_us != simulation_time_us:
            raise RuntimeError("participant schedule work executed at the wrong time")
        prior_reference = self.specification_bindings[entry.participant_id]
        pending_before = {
            int(getattr(row, "sequence"))
            for row in getattr(scheduler, "_pending", ())
            if getattr(row, "agent_id", None) == entry.participant_id
        }
        cancelled_order_ids: tuple[str, ...] = ()
        if entry.action is ParticipantScheduleActionV1.ACTIVATE:
            method = getattr(scheduler, "activate_agent", None)
            if not callable(method):
                raise RuntimeError("scheduler omits participant activation")
            method(entry.participant_id, simulation_time_us=simulation_time_us)
            resulting_reference = prior_reference
            self.active[entry.participant_id] = True
        elif entry.action is ParticipantScheduleActionV1.DEACTIVATE:
            method = getattr(scheduler, "deactivate_agent", None)
            if not callable(method):
                raise RuntimeError("scheduler omits participant deactivation")
            result = method(
                entry.participant_id,
                simulation_time_us=simulation_time_us,
                cancel_working=True,
            )
            cancelled_order_ids = tuple(sorted(result))
            resulting_reference = prior_reference
            self.active[entry.participant_id] = False
        else:
            replacement = entry.replacement_specification
            if replacement is None:  # pragma: no cover - model contract prevents it
                raise RuntimeError("participant retune omits replacement specification")
            specification = self._specification_registry.get(
                (entry.participant_id, replacement.sha256)
            )
            if specification is None:
                raise RuntimeError(
                    "participant retune specification bytes are not registered"
                )
            method = getattr(scheduler, "retune_agent", None)
            if not callable(method):
                raise RuntimeError("scheduler omits participant retuning")
            method(
                entry.participant_id,
                specification,
                simulation_time_us=simulation_time_us,
            )
            resulting_reference = replacement
            self.specification_bindings[entry.participant_id] = replacement
            self.replacement_generations[entry.participant_id] += 1
        pending_after = {
            int(getattr(row, "sequence"))
            for row in getattr(scheduler, "_pending", ())
            if getattr(row, "agent_id", None) == entry.participant_id
        }
        discarded = tuple(sorted(pending_before - pending_after))
        record = ParticipantLifecycleRecordV1(
            schedule_id=entry.schedule_id,
            simulation_time_us=simulation_time_us,
            participant_id=entry.participant_id,
            action=entry.action,
            prior_specification=prior_reference,
            resulting_specification=resulting_reference,
            replacement_generation=self.replacement_generations[
                entry.participant_id
            ],
            discarded_pending_sequences=discarded,
            cancelled_order_ids=cancelled_order_ids,
        )
        self.history.append(record)
        self.next_index += 1
        self.assert_invariants(scheduler=scheduler)
        return record

    def assert_invariants(self, *, scheduler: object | None = None) -> None:
        participant_ids = tuple(sorted(self._definitions))
        if self.plan_sha256 != self._plan.semantic_sha256:
            raise RuntimeError("participant schedule runtime belongs to another plan")
        if not 0 <= self.next_index <= len(self._plan.participant_schedule):
            raise RuntimeError("participant schedule cursor exceeds the plan")
        if tuple(sorted(self.active)) != participant_ids or any(
            type(value) is not bool for value in self.active.values()
        ):
            raise RuntimeError("participant activation state is incomplete")
        if tuple(sorted(self.specification_bindings)) != participant_ids or any(
            type(value) is not VersionedReferenceV1
            for value in self.specification_bindings.values()
        ):
            raise RuntimeError("participant specification bindings are incomplete")
        if tuple(sorted(self.replacement_generations)) != participant_ids or any(
            type(value) is not int or value < 0
            for value in self.replacement_generations.values()
        ):
            raise RuntimeError("participant replacement generations are incomplete")
        if any(type(record) is not ParticipantLifecycleRecordV1 for record in self.history):
            raise RuntimeError("participant lifecycle history uses the wrong records")

        expected_active = {
            participant.participant_id: participant.initially_active
            for participant in self._plan.participant_definitions
        }
        expected_bindings = {
            participant.participant_id: participant.specification
            for participant in self._plan.participant_definitions
        }
        expected_generations = {participant_id: 0 for participant_id in participant_ids}
        prefix = self._plan.participant_schedule[: self.next_index]
        if len(prefix) != len(self.history):
            raise RuntimeError("participant lifecycle history length differs from cursor")
        for entry, record in zip(prefix, self.history, strict=True):
            if (
                record.schedule_id != entry.schedule_id
                or record.simulation_time_us != entry.simulation_time_us
                or record.participant_id != entry.participant_id
                or record.action is not entry.action
                or record.prior_specification != expected_bindings[entry.participant_id]
            ):
                raise RuntimeError("participant lifecycle history differs from the plan")
            if entry.action is ParticipantScheduleActionV1.ACTIVATE:
                expected_active[entry.participant_id] = True
            elif entry.action is ParticipantScheduleActionV1.DEACTIVATE:
                expected_active[entry.participant_id] = False
            else:
                replacement = entry.replacement_specification
                assert replacement is not None
                expected_bindings[entry.participant_id] = replacement
                expected_generations[entry.participant_id] += 1
            if (
                record.resulting_specification
                != expected_bindings[entry.participant_id]
                or record.replacement_generation
                != expected_generations[entry.participant_id]
            ):
                raise RuntimeError(
                    "participant lifecycle resulting specification is inconsistent"
                )
        if (
            self.active != expected_active
            or self.specification_bindings != expected_bindings
            or self.replacement_generations != expected_generations
        ):
            raise RuntimeError("participant lifecycle state differs from its exact prefix")
        if scheduler is not None:
            if getattr(scheduler, "_active", None) != self.active:
                raise RuntimeError(
                    "participant coordinator activation differs from scheduler"
                )
            agents = getattr(scheduler, "agents", None)
            if not isinstance(agents, Mapping) or set(agents) != set(participant_ids):
                raise RuntimeError("participant coordinator cannot reconcile scheduler agents")
            for participant_id, reference in self.specification_bindings.items():
                specification = getattr(agents[participant_id], "spec", None)
                identity = getattr(specification, "identity_dict", None)
                if not callable(identity) or canonical_sha256(identity()) != reference.sha256:
                    raise RuntimeError(
                        "participant coordinator spec binding differs from scheduler"
                    )

    def checkpoint_state(self) -> dict[str, object]:
        self.assert_invariants()
        state = {
            "active": dict(sorted(self.active.items())),
            "history": [record.as_dict() for record in self.history],
            "next_index": self.next_index,
            "plan_sha256": self.plan_sha256,
            "replacement_generations": dict(
                sorted(self.replacement_generations.items())
            ),
            "schema_version": PARTICIPANT_SCHEDULE_RUNTIME_SCHEMA_VERSION,
            "specification_bindings": {
                participant_id: reference.as_dict()
                for participant_id, reference in sorted(
                    self.specification_bindings.items()
                )
            },
            "specification_registry": [
                {
                    "participant_id": specification.agent_id,
                    "specification": specification.identity_dict(),
                    "specification_sha256": canonical_sha256(
                        specification.identity_dict()
                    ),
                }
                for specification in self.specification_registry
            ],
        }
        validate_strict_json(state)
        return state

    @classmethod
    def from_checkpoint_state(
        cls,
        payload: Mapping[str, object],
        *,
        plan: FullDayPlanV1,
        scheduler: object | None = None,
    ) -> ParticipantScheduleRuntimeV1:
        validate_strict_json(payload)
        _require_exact_fields(
            payload,
            {
                "active",
                "history",
                "next_index",
                "plan_sha256",
                "replacement_generations",
                "schema_version",
                "specification_bindings",
                "specification_registry",
            },
            "participant schedule runtime",
        )
        if payload["schema_version"] != PARTICIPANT_SCHEDULE_RUNTIME_SCHEMA_VERSION:
            raise ValueError("participant schedule runtime schema is unsupported")
        if payload["plan_sha256"] != plan.semantic_sha256:
            raise ValueError("participant schedule runtime plan identity mismatch")
        active = payload["active"]
        bindings = payload["specification_bindings"]
        generations = payload["replacement_generations"]
        history = payload["history"]
        registry = payload["specification_registry"]
        if not all(isinstance(value, Mapping) for value in (active, bindings, generations)):
            raise TypeError("participant schedule maps must be objects")
        if type(history) is not list or any(not isinstance(row, Mapping) for row in history):
            raise TypeError("participant schedule history must be an object array")
        if type(registry) is not list or any(not isinstance(row, Mapping) for row in registry):
            raise TypeError("participant specification registry must be an object array")
        specifications: list[AgentSpec] = []
        for row in registry:
            _require_exact_fields(
                row,
                {"participant_id", "specification", "specification_sha256"},
                "participant specification registry row",
            )
            raw_specification = row["specification"]
            if not isinstance(raw_specification, Mapping):
                raise TypeError("participant specification must be an object")
            specification = AgentSpec.from_dict(raw_specification)
            if (
                row["participant_id"] != specification.agent_id
                or row["specification_sha256"]
                != canonical_sha256(specification.identity_dict())
            ):
                raise ValueError("participant specification registry digest mismatch")
            specifications.append(specification)
        runtime = cls(
            plan,
            specifications,
            next_index=_exact_int(payload["next_index"], "next_index"),
            active={
                _exact_string(key, "active participant ID"): value
                for key, value in active.items()
            },
            specification_bindings={
                _exact_string(key, "spec binding participant ID"):
                VersionedReferenceV1.from_dict(value)
                for key, value in bindings.items()
                if isinstance(value, Mapping)
            },
            replacement_generations={
                _exact_string(key, "generation participant ID"):
                _exact_int(value, "replacement generation")
                for key, value in generations.items()
            },
            history=tuple(ParticipantLifecycleRecordV1.from_dict(row) for row in history),
        )
        if len(runtime.specification_bindings) != len(bindings):
            raise TypeError("participant specification binding must be an object")
        runtime.assert_invariants(scheduler=scheduler)
        if canonical_json_bytes(runtime.checkpoint_state()) != canonical_json_bytes(payload):
            raise ValueError("participant schedule checkpoint is not a canonical fixed point")
        return runtime

    def canonical_state_bytes(self) -> bytes:
        return canonical_json_bytes(self.checkpoint_state())


__all__ = [
    "PARTICIPANT_SCHEDULE_RUNTIME_SCHEMA_VERSION",
    "ParticipantLifecycleRecordV1",
    "ParticipantScheduleRuntimeV1",
]
