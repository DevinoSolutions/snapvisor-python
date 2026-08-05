"""Coverage is asserted against the *spec*, never against a hand-written list.

The list is the thing that drifts: 0.1.0's two hand-written call sites were both
already partial against their schemas. Here every operation is discovered by
parsing the vendored ``openapi.yaml``, so an operation the backend adds fails
this suite until the SDK is regenerated.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest

from snapvisor import AsyncSnapvisor, Snapvisor
from snapvisor._operations import OPERATIONS
from snapvisor.operations import operation_module

SPEC = Path(__file__).resolve().parent.parent / "openapi.yaml"
HTTP_METHODS = ("get", "post", "put", "patch", "delete")


def _spec_operations() -> list[tuple[str, str, str]]:
    """``(operationId, method, path)`` for every operation in the vendored spec.

    Parsed with a small reader rather than PyYAML so the test suite does not need
    the codegen extra installed.
    """
    operations: list[tuple[str, str, str]] = []
    path: str | None = None
    method: str | None = None
    in_paths = False
    for line in SPEC.read_text(encoding="utf-8").splitlines():
        if re.match(r"^paths:\s*$", line):
            in_paths = True
            continue
        if in_paths and re.match(r"^\S", line):
            break
        if not in_paths:
            continue
        path_match = re.match(r"^  (/\S*):\s*$", line)
        if path_match:
            path, method = path_match.group(1), None
            continue
        method_match = re.match(r"^    ([a-z]+):\s*$", line)
        if method_match and method_match.group(1) in HTTP_METHODS:
            method = method_match.group(1)
            continue
        id_match = re.match(r"^      operationId:\s*(\S+)\s*$", line)
        if id_match and path and method:
            operations.append((id_match.group(1), method, path))
    return operations


SPEC_OPERATIONS = _spec_operations()


def test_the_spec_reader_found_the_whole_document():
    assert len(SPEC_OPERATIONS) == 37, (
        "The vendored spec should publish 37 operations; if the API grew, "
        "run `python scripts/regen.py` and update this number deliberately."
    )


@pytest.mark.parametrize(
    ("operation_id", "method", "path"),
    SPEC_OPERATIONS,
    ids=[operation_id for operation_id, _, _ in SPEC_OPERATIONS],
)
def test_every_spec_operation_is_registered(operation_id: str, method: str, path: str):
    assert operation_id in OPERATIONS, f"{operation_id} is in the spec but not in the SDK"
    info = OPERATIONS[operation_id]
    assert info.method == method.upper()
    assert info.path == path


@pytest.mark.parametrize("operation_id", sorted(OPERATIONS), ids=sorted(OPERATIONS))
def test_every_operation_has_sync_and_async_callables(operation_id: str):
    module = operation_module(operation_id)
    for attribute in ("sync", "sync_detailed", "asyncio", "asyncio_detailed"):
        function = getattr(module, attribute, None)
        assert callable(function), f"{operation_id} is missing {attribute}"
    assert inspect.iscoroutinefunction(module.asyncio_detailed)


# Building a client constructs an httpx transport (and with it an SSL context),
# which is far too slow to repeat 37 times per parametrized test.
@pytest.fixture(scope="module")
def sync_client():
    client = Snapvisor(token="tok")
    yield client
    client.close()


@pytest.fixture(scope="module")
def async_client():
    return AsyncSnapvisor(token="tok")


@pytest.mark.parametrize("operation_id", sorted(OPERATIONS), ids=sorted(OPERATIONS))
def test_every_operation_is_reachable_from_the_sync_facade(operation_id: str, sync_client):
    info = OPERATIONS[operation_id]
    namespace = getattr(sync_client, info.namespace)
    assert callable(getattr(namespace, info.python_name))
    assert callable(getattr(namespace, operation_id)), "operationId must resolve too"


@pytest.mark.parametrize("operation_id", sorted(OPERATIONS), ids=sorted(OPERATIONS))
def test_every_operation_is_reachable_from_the_async_facade(operation_id: str, async_client):
    info = OPERATIONS[operation_id]
    bound = getattr(getattr(async_client, info.namespace), info.python_name)
    assert inspect.iscoroutinefunction(bound)


def test_namespaces_partition_the_whole_surface(sync_client):
    reachable = {
        info.operation_id
        for namespace in sync_client.namespaces
        for info in getattr(sync_client, namespace).operations.values()
    }
    assert reachable == set(OPERATIONS)


def test_unknown_operation_names_fail_loudly(sync_client):
    with pytest.raises(AttributeError, match="has no operation"):
        sync_client.builds.definitely_not_an_operation  # noqa: B018
    with pytest.raises(AttributeError, match="Namespaces"):
        sync_client.definitely_not_a_namespace  # noqa: B018
