"""Order-independent generation of constrained strategy mutation batches."""

from __future__ import annotations

import hashlib
import re
import struct
from dataclasses import dataclass

from kirby2.immutable import thaw_json
from kirby2.strategy.language import FeatureName
from kirby2.strategy.state_machine import PositionFeature

from .ast import StrategyAstV1
from .identity import canonical_identity_bytes, strategy_semantic_sha256
from .lineage import StrategyRngSubstreamV1
from .mutations import (
    MutationOperationIdV1,
    MutationRejectionReasonV1,
    MutationRequestV1,
    MutationResourceLimitsV1,
    MutationStatusV1,
    StrategyMutationRecordV1,
    StrategyMutationResultV1,
    apply_strategy_mutation,
)


STRATEGY_MUTATION_BATCH_SCHEMA_ID_V1 = "KIRBY2_STRATEGY_MUTATION_BATCH_V1"
STRATEGY_MUTATION_BATCH_SCHEMA_VERSION_V1 = 1
STRATEGY_MUTATION_BATCH_DIGEST_DOMAIN_V1 = b"KIRBY2_STRATEGY_MUTATION_BATCH_V1\x00"
STRATEGY_MUTATION_GENERATION_ORDER_V1 = (
    "OPERATION_ENUM_THEN_VERSION_THEN_CANONICAL_REQUEST_V1"
)
STRATEGY_MUTATION_SUBSTREAM_LABEL_V1 = (
    "PREFIX_ORDINAL_OPERATION_VERSION_REQUEST_SHA256_V1"
)
MAX_STRATEGY_MUTATION_BATCH_REQUESTS_V1 = 4_096
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_LABEL_PREFIX = re.compile(
    r"^[a-z0-9][a-z0-9_-]{0,31}(?:/[a-z0-9][a-z0-9_-]{0,31}){0,7}$"
)
_INDEX_RETRY_DOMAIN_V1 = b"KIRBY2_STRATEGY_MUTATION_INDEX_RETRY_V1\x00"
_OPERATION_RANK = {
    operation: index for index, operation in enumerate(MutationOperationIdV1)
}
_AVAILABLE_FEATURES = frozenset(item.value for item in FeatureName) | frozenset(
    item.value for item in PositionFeature
)


@dataclass(frozen=True, slots=True)
class MutationGenerationContextV1:
    root_seed: int
    available_features: tuple[str, ...]
    known_semantic_sha256: tuple[str, ...] = ()
    resource_limits: MutationResourceLimitsV1 = MutationResourceLimitsV1()
    label_prefix: str = "strategy-discovery/mutation"

    def __post_init__(self) -> None:
        if (
            type(self.root_seed) is not int
            or self.root_seed < 0
            or self.root_seed > (1 << 64) - 1
        ):
            raise ValueError("mutation generation root seed must be unsigned 64-bit")
        if type(self.available_features) is not tuple or any(
            type(item) is not str or item not in _AVAILABLE_FEATURES
            for item in self.available_features
        ):
            raise ValueError("mutation generation features are outside the grammar")
        ordered_features = tuple(sorted(set(self.available_features)))
        if len(ordered_features) != len(self.available_features):
            raise ValueError("mutation generation features must be unique")
        object.__setattr__(self, "available_features", ordered_features)
        _require_digest_tuple(self.known_semantic_sha256)
        ordered_digests = tuple(sorted(self.known_semantic_sha256))
        object.__setattr__(self, "known_semantic_sha256", ordered_digests)
        if not isinstance(self.resource_limits, MutationResourceLimitsV1):
            raise TypeError("mutation generation resource limits are invalid")
        if (
            type(self.label_prefix) is not str
            or _LABEL_PREFIX.fullmatch(self.label_prefix) is None
            or self.label_prefix.endswith("/")
        ):
            raise ValueError("mutation generation label prefix is invalid")
        canonical_identity_bytes(self.label_prefix)

    def as_dict(self) -> dict[str, object]:
        return {
            "available_features": list(self.available_features),
            "known_semantic_sha256": list(self.known_semantic_sha256),
            "label_prefix": self.label_prefix,
            "resource_limits": self.resource_limits.as_dict(),
            "root_seed": self.root_seed,
        }


@dataclass(frozen=True, slots=True)
class StrategyMutationBatchV1:
    parent_semantic_sha256: str
    context: MutationGenerationContextV1
    results: tuple[StrategyMutationResultV1, ...]
    schema_version: int = STRATEGY_MUTATION_BATCH_SCHEMA_VERSION_V1

    def __post_init__(self) -> None:
        _require_digest(self.parent_semantic_sha256, "mutation batch parent digest")
        if not isinstance(self.context, MutationGenerationContextV1):
            raise TypeError("mutation batch generation context is invalid")
        if type(self.results) is not tuple or any(
            not isinstance(item, StrategyMutationResultV1) for item in self.results
        ):
            raise TypeError("mutation batch results must be a typed tuple")
        if len(self.results) > MAX_STRATEGY_MUTATION_BATCH_REQUESTS_V1:
            raise ValueError("mutation batch results exceed the fixed batch bound")
        if (
            type(self.schema_version) is not int
            or self.schema_version != STRATEGY_MUTATION_BATCH_SCHEMA_VERSION_V1
        ):
            raise ValueError("unsupported mutation batch schema")
        requests = tuple(_record_request(item.record) for item in self.results)
        if tuple(map(_request_order_key, requests)) != tuple(
            sorted(map(_request_order_key, requests))
        ):
            raise ValueError("mutation batch results are not canonically ordered")
        expected_labels = tuple(
            _request_substream_label(self.context.label_prefix, ordinal, request)
            for ordinal, request in enumerate(requests)
        )
        labels = tuple(item.record.rng_substream.label for item in self.results)
        if labels != expected_labels:
            raise ValueError("mutation batch results are not in canonical generation order")
        if any(
            item.record.parent_semantic_sha256 != self.parent_semantic_sha256
            or item.record.rng_substream.root_seed != self.context.root_seed
            for item in self.results
        ):
            raise ValueError("mutation batch result does not bind to its context")
        seen = set(self.context.known_semantic_sha256)
        for item in self.results:
            digest = item.record.child_semantic_sha256
            if item.record.status is MutationStatusV1.ACCEPTED:
                if digest in seen:
                    raise ValueError("mutation batch accepted a duplicate child")
                seen.add(digest)
            elif (
                item.record.rejection_reason is MutationRejectionReasonV1.DUPLICATE
                and digest not in seen
            ):
                raise ValueError("mutation batch duplicate refusal has no prior child")

    @property
    def accepted(self) -> tuple[StrategyMutationResultV1, ...]:
        return tuple(
            item
            for item in self.results
            if item.record.status is MutationStatusV1.ACCEPTED
        )

    @property
    def rejected(self) -> tuple[StrategyMutationResultV1, ...]:
        return tuple(
            item
            for item in self.results
            if item.record.status is MutationStatusV1.REJECTED
        )

    @property
    def batch_sha256(self) -> str:
        raw = self.canonical_bytes()
        digest = hashlib.sha256()
        digest.update(STRATEGY_MUTATION_BATCH_DIGEST_DOMAIN_V1)
        digest.update(struct.pack(">Q", len(raw)))
        digest.update(raw)
        return digest.hexdigest()

    def as_dict(self) -> dict[str, object]:
        return {
            "accepted_count": len(self.accepted),
            "context": self.context.as_dict(),
            "generation_order": STRATEGY_MUTATION_GENERATION_ORDER_V1,
            "parent_semantic_sha256": self.parent_semantic_sha256,
            "rejected_count": len(self.rejected),
            "results": [item.record.as_dict() for item in self.results],
            "schema_id": STRATEGY_MUTATION_BATCH_SCHEMA_ID_V1,
            "schema_version": self.schema_version,
            "substream_label_policy": STRATEGY_MUTATION_SUBSTREAM_LABEL_V1,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_identity_bytes(self.as_dict())


def generate_mutation_batch(
    parent: StrategyAstV1,
    requests: tuple[MutationRequestV1, ...],
    *,
    context: MutationGenerationContextV1,
) -> StrategyMutationBatchV1:
    if type(requests) is not tuple or any(
        not isinstance(item, MutationRequestV1) for item in requests
    ):
        raise TypeError("mutation generation requests must be a typed tuple")
    if len(requests) > MAX_STRATEGY_MUTATION_BATCH_REQUESTS_V1:
        raise ValueError("mutation generation request batch exceeds its fixed bound")
    if not isinstance(context, MutationGenerationContextV1):
        raise TypeError("mutation generation context is invalid")
    ordered = tuple(sorted(requests, key=_request_order_key))
    seen = set(context.known_semantic_sha256)
    results: list[StrategyMutationResultV1] = []
    for ordinal, request in enumerate(ordered):
        label = _request_substream_label(context.label_prefix, ordinal, request)
        result = apply_strategy_mutation(
            parent,
            request,
            rng_substream=StrategyRngSubstreamV1(context.root_seed, label),
            available_features=context.available_features,
            resource_limits=context.resource_limits,
            known_semantic_sha256=tuple(sorted(seen)),
        )
        results.append(result)
        if result.record.status is MutationStatusV1.ACCEPTED:
            seen.add(result.record.child_semantic_sha256)
    return StrategyMutationBatchV1(
        parent_semantic_sha256=strategy_semantic_sha256(parent),
        context=context,
        results=tuple(results),
    )


def labeled_substream_uint64(substream: StrategyRngSubstreamV1) -> int:
    if not isinstance(substream, StrategyRngSubstreamV1):
        raise TypeError("labeled RNG draw requires a strategy substream")
    return int.from_bytes(bytes.fromhex(substream.sha256)[:8], "big")


def labeled_substream_index(
    substream: StrategyRngSubstreamV1,
    upper_bound: int,
) -> int:
    if (
        type(upper_bound) is not int
        or upper_bound <= 0
        or upper_bound > (1 << 64)
    ):
        raise ValueError("labeled RNG index bound must be in the unsigned 64-bit range")
    range_size = 1 << 64
    acceptance_limit = range_size - (range_size % upper_bound)
    candidate = labeled_substream_uint64(substream)
    retry = 0
    while candidate >= acceptance_limit:
        retry += 1
        digest = hashlib.sha256()
        digest.update(_INDEX_RETRY_DOMAIN_V1)
        digest.update(bytes.fromhex(substream.sha256))
        digest.update(struct.pack(">Q", retry))
        candidate = int.from_bytes(digest.digest()[:8], "big")
    return candidate % upper_bound


def _request_order_key(request: MutationRequestV1) -> tuple[int, int, bytes]:
    return (
        _OPERATION_RANK[request.operation_id],
        request.operation_version,
        request.canonical_bytes(),
    )


def _request_substream_label(
    prefix: str,
    ordinal: int,
    request: MutationRequestV1,
) -> str:
    return (
        f"{prefix}/{ordinal:08d}/{request.operation_id.value.lower()}"
        f"/v{request.operation_version}/{request.request_sha256}"
    )


def _record_request(record: StrategyMutationRecordV1) -> MutationRequestV1:
    return MutationRequestV1(
        record.operation_id,
        record.operation_version,
        thaw_json(record.parameters),
    )


def _require_digest(value: object, context: str) -> None:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{context} must be lowercase SHA-256")


def _require_digest_tuple(values: object) -> None:
    if type(values) is not tuple or any(
        type(item) is not str or _SHA256.fullmatch(item) is None for item in values
    ):
        raise ValueError("known strategy digests must be a lowercase SHA-256 tuple")
    if len(values) != len(set(values)):
        raise ValueError("known strategy digests must be unique")


__all__ = [
    "MAX_STRATEGY_MUTATION_BATCH_REQUESTS_V1",
    "STRATEGY_MUTATION_BATCH_SCHEMA_ID_V1",
    "STRATEGY_MUTATION_GENERATION_ORDER_V1",
    "STRATEGY_MUTATION_SUBSTREAM_LABEL_V1",
    "MutationGenerationContextV1",
    "StrategyMutationBatchV1",
    "generate_mutation_batch",
    "labeled_substream_index",
    "labeled_substream_uint64",
]
