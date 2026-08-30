"""Shared single-process and fixed-subprocess execution backends for WO38-B."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Protocol

from kirby2.packs.formats import load_canonical_json_bytes

from .protocol import WorkRequestV1, WorkerCompatibilityV1, WorkerResultV1
from .worker import execute_work_request, measure_local_worker_compatibility


LOCAL_BACKEND_MAX_WORKERS_V1 = 64
LOCAL_WORKER_MODULE_V1 = "kirby2.orchestration.worker"


class LocalWorkerProcessError(RuntimeError):
    """A fixed local worker process violated its closed stdio contract."""


class ExecutionBackendV1(Protocol):
    """The one execution surface shared by direct and local-process backends."""

    @property
    def backend_id(self) -> str: ...

    @property
    def compatibility(self) -> WorkerCompatibilityV1: ...

    def execute_many(
        self,
        requests: tuple[WorkRequestV1, ...],
    ) -> tuple[WorkerResultV1, ...]: ...


@dataclass(frozen=True, slots=True)
class SingleProcessBackendV1:
    """Execute the wire protocol directly through the production worker adapter."""

    compatibility: WorkerCompatibilityV1

    def __post_init__(self) -> None:
        if type(self.compatibility) is not WorkerCompatibilityV1:
            raise TypeError("single-process compatibility must be WorkerCompatibilityV1")

    @classmethod
    def measured(cls) -> SingleProcessBackendV1:
        return cls(compatibility=measure_local_worker_compatibility())

    @property
    def backend_id(self) -> str:
        return "single-process-v1"

    def execute_many(
        self,
        requests: tuple[WorkRequestV1, ...],
    ) -> tuple[WorkerResultV1, ...]:
        supplied = _canonical_requests(requests)
        measured = measure_local_worker_compatibility()
        if self.compatibility != measured:
            raise LocalWorkerProcessError(
                "single-process declared compatibility differs from the "
                "measured local executable"
            )
        results = tuple(
            execute_work_request(request)
            for request in supplied
        )
        return _canonical_results(results, supplied)


@dataclass(frozen=True, slots=True)
class LocalSubprocessBackendV1:
    """Execute canonical requests through fixed Python subprocesses over stdio.

    Threads coordinate process I/O only.  No Python object enters a multiprocessing
    queue, and the worker argv is static rather than request-controlled.
    """

    worker_count: int
    compatibility: WorkerCompatibilityV1

    def __post_init__(self) -> None:
        if (
            type(self.worker_count) is not int
            or not 1 <= self.worker_count <= LOCAL_BACKEND_MAX_WORKERS_V1
        ):
            raise ValueError(
                f"local worker count must be in [1, {LOCAL_BACKEND_MAX_WORKERS_V1}]"
            )
        if type(self.compatibility) is not WorkerCompatibilityV1:
            raise TypeError("local-process compatibility must be WorkerCompatibilityV1")

    @classmethod
    def measured(cls, worker_count: int) -> LocalSubprocessBackendV1:
        return cls(
            worker_count=worker_count,
            compatibility=measure_local_worker_compatibility(),
        )

    @property
    def backend_id(self) -> str:
        return "local-subprocess-v1"

    def execute_many(
        self,
        requests: tuple[WorkRequestV1, ...],
    ) -> tuple[WorkerResultV1, ...]:
        supplied = _canonical_requests(requests)
        child_environment = _worker_environment(os.environ)
        results: list[WorkerResultV1] = []
        with ThreadPoolExecutor(max_workers=self.worker_count) as executor:
            futures = {
                executor.submit(
                    _execute_fixed_subprocess,
                    request,
                    child_environment,
                ): request
                for request in supplied
            }
            for future in as_completed(futures):
                request = futures[future]
                try:
                    result = future.result()
                except Exception as error:
                    raise LocalWorkerProcessError(
                        "local worker failed for logical work "
                        f"{request.logical_work_unit.logical_work_unit_id}"
                    ) from error
                results.append(result)
        return _canonical_results(tuple(results), supplied)


def _execute_fixed_subprocess(
    request: WorkRequestV1,
    child_environment: Mapping[str, str],
) -> WorkerResultV1:
    completed = subprocess.run(
        (sys.executable, "-m", LOCAL_WORKER_MODULE_V1),
        input=request.canonical_bytes(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        env=dict(child_environment),
    )
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise LocalWorkerProcessError(
            f"fixed worker exited {completed.returncode}: {detail[:4096]}"
        )
    if completed.stderr:
        raise LocalWorkerProcessError("successful fixed worker emitted stderr")
    if not completed.stdout.endswith(b"\n") or completed.stdout.endswith(b"\n\n"):
        raise LocalWorkerProcessError(
            "fixed worker stdout must contain one canonical object and one final LF"
        )
    raw = completed.stdout[:-1]
    if not raw or b"\n" in raw or b"\r" in raw:
        raise LocalWorkerProcessError("fixed worker stdout contains multiple frames")
    payload = load_canonical_json_bytes(raw, "local worker result")
    result = WorkerResultV1.from_dict(payload)
    if result.canonical_bytes() != raw:
        raise LocalWorkerProcessError("fixed worker result is not canonical")
    if result.request.work_request_id != request.work_request_id:
        raise LocalWorkerProcessError("fixed worker returned a foreign request")
    return result


def _canonical_requests(
    requests: tuple[WorkRequestV1, ...],
) -> tuple[WorkRequestV1, ...]:
    if type(requests) is not tuple or not requests:
        raise ValueError("execution backend requires a nonempty immutable request tuple")
    if any(type(item) is not WorkRequestV1 for item in requests):
        raise TypeError("execution backend requests must contain WorkRequestV1 values")
    ordered = tuple(
        sorted(
            requests,
            key=lambda item: item.logical_work_unit.logical_work_unit_id,
        )
    )
    logical_ids = tuple(
        item.logical_work_unit.logical_work_unit_id for item in ordered
    )
    request_ids = tuple(item.work_request_id for item in ordered)
    if len(logical_ids) != len(set(logical_ids)):
        raise ValueError("execution backend cannot receive duplicate logical work")
    if len(request_ids) != len(set(request_ids)):
        raise ValueError("execution backend cannot receive duplicate work requests")
    return ordered


def _canonical_results(
    results: tuple[WorkerResultV1, ...],
    requests: tuple[WorkRequestV1, ...],
) -> tuple[WorkerResultV1, ...]:
    if type(results) is not tuple or any(
        type(item) is not WorkerResultV1 for item in results
    ):
        raise TypeError("execution backend results must contain WorkerResultV1 values")
    by_request: dict[str, WorkerResultV1] = {}
    for result in results:
        request_id = result.request.work_request_id
        if request_id in by_request:
            raise ValueError("execution backend returned a duplicate request result")
        by_request[request_id] = result
    expected = tuple(item.work_request_id for item in requests)
    if frozenset(by_request) != frozenset(expected):
        raise ValueError("execution backend omitted or invented work-request results")
    return tuple(by_request[request_id] for request_id in expected)


def _worker_environment(source: Mapping[str, str]) -> dict[str, str]:
    if not isinstance(source, Mapping) or any(
        type(key) is not str or type(value) is not str
        for key, value in source.items()
    ):
        raise TypeError("worker environment must be a text mapping")
    result = dict(source)
    result["PYTHONHASHSEED"] = "0"
    result["PYTHONDONTWRITEBYTECODE"] = "1"
    return result


__all__ = [
    "LOCAL_BACKEND_MAX_WORKERS_V1",
    "LOCAL_WORKER_MODULE_V1",
    "ExecutionBackendV1",
    "LocalSubprocessBackendV1",
    "LocalWorkerProcessError",
    "SingleProcessBackendV1",
]
