"""Typed response and request models for every Snapvisor API object.

0.1.0 returned ``dict[str, Any]`` everywhere, so nothing about a build, a
project, or a review could be autocompleted or type-checked. These models are
generated from the OpenAPI document, so they cannot drift from the API::

    from snapvisor import Snapvisor
    from snapvisor.models import Build, Project

    sv = Snapvisor()
    project: Project = sv.projects.get_project(owner="acme", project="web")

Every model is an ``attrs`` class with ``to_dict()``/``from_dict()`` and an
``additional_properties`` escape hatch for fields the SDK's vendored spec
predates.
"""

from __future__ import annotations

from snapvisor._generated import models as _models
from snapvisor._generated.models import *  # noqa: F403 - re-export the generated surface
from snapvisor._generated.types import UNSET, File, Response, Unset

__all__ = [*getattr(_models, "__all__", []), "UNSET", "Unset", "File", "Response"]
