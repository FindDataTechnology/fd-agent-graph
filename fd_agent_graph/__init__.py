"""fd-agent-graph: agent-graph engineering toolkit.

Runtime-agnostic: definitions, static checks, topology analysis and a CI gate.
Nodes are never executed except by the reference executor used in tests.
"""

from __future__ import annotations

import sys

__version__ = "0.1.0"

#: Declared floor. `pyproject.toml` says `requires-python = ">=3.11"`, but a
#: metadata field is not a check: an older interpreter imports this package and
#: fails eleven lines later with a TypeError from inside `dataclasses`, which
#: names neither the package nor the version. Measured on macOS system Python
#: 3.9.6: 19 unrelated ImportErrors across the suite, zero mentions of 3.11.
MINIMUM_PYTHON = (3, 11)


def require_python(version_info=sys.version_info) -> None:
    """Refuse to load under an interpreter older than the declared floor.

    Takes the version as an argument so the refusal is testable on any machine:
    the suite must be able to prove this fires without shelling out to an
    interpreter it does not have.
    """
    if tuple(version_info[:2]) < MINIMUM_PYTHON:
        have = ".".join(str(part) for part in tuple(version_info)[:3])
        want = ".".join(str(part) for part in MINIMUM_PYTHON)
        raise RuntimeError(
            f"fd-agent-graph requires Python {want} or newer; this interpreter is "
            f"{have}. The package uses `dataclass(slots=True)` and PEP 604 unions "
            f"evaluated at import time, so an older interpreter fails later with a "
            f"`TypeError` inside the standard library that names neither this "
            f"package nor the version."
        )


require_python()
