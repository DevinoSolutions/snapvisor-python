"""The prod e2e deliberately runs with the real environment.

The unit-test conftest strips every ``SNAPVISOR_*``/``ARGOS_*`` and CI variable so
unit tests behave identically everywhere. The e2e wants the opposite: it runs
against prod using the CI runner's real environment, including the CI-provider
variables the SDK is supposed to detect. Redefining the fixture here overrides
the parent's autouse one for this directory only.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolated_environment() -> None:
    """No-op override: keep the real environment for prod end-to-end runs."""
    return None
