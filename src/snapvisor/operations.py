"""Direct access to every generated operation module, by its Python name.

The :class:`~snapvisor.api.Snapvisor` facade dispatches operations dynamically,
which is what keeps coverage structural — but it also means a type checker
cannot see each operation's individual signature. When you want that, import the
operation module itself and pass the facade's ``raw`` client::

    from snapvisor import Snapvisor
    from snapvisor.operations import list_builds

    sv = Snapvisor()
    response = list_builds.sync_detailed(owner="acme", project="web", client=sv.raw)

Each module exposes ``sync``, ``sync_detailed``, ``asyncio``, and
``asyncio_detailed``. ``OPERATIONS`` maps every OpenAPI ``operationId`` to where
its module lives.
"""

from __future__ import annotations

import importlib
from types import ModuleType

from snapvisor._operations import OPERATIONS, OperationInfo

__all__ = [
    "OPERATIONS",
    "OperationInfo",
    "body_model",
    "operation_module",
    "operation_names",
]

_BY_PYTHON_NAME: dict[str, OperationInfo] = {info.python_name: info for info in OPERATIONS.values()}


def operation_names() -> list[str]:
    """Every operation's Python name, sorted."""
    return sorted(_BY_PYTHON_NAME)


def operation_module(name: str) -> ModuleType:
    """Import the generated module for an ``operationId`` or Python name.

    Raises:
        KeyError: If no operation goes by that name.
    """
    info = OPERATIONS.get(name) or _BY_PYTHON_NAME.get(name)
    if info is None:
        raise KeyError(f"Unknown Snapvisor operation {name!r}")
    return importlib.import_module(info.module)


def body_model(name: str) -> type | None:
    """The generated request-body class for an operation, or ``None`` if it takes no body.

    Lets a caller build a request body from a plain mapping without knowing the
    generated class name::

        model = body_model("createProject")
        body = model.from_dict({"name": "web", "accountSlug": "acme"})

    Raises:
        KeyError: If no operation goes by that name.
    """
    import inspect

    parameter = inspect.signature(operation_module(name).sync_detailed).parameters.get("body")
    if parameter is None:
        return None
    annotation = parameter.annotation
    return annotation if isinstance(annotation, type) else None


def __getattr__(name: str) -> ModuleType:
    """Expose each operation module as an attribute of this package (PEP 562)."""
    try:
        return operation_module(name)
    except KeyError as error:
        raise AttributeError(f"module 'snapvisor.operations' has no attribute {name!r}") from error


def __dir__() -> list[str]:
    return sorted({*__all__, *_BY_PYTHON_NAME})
