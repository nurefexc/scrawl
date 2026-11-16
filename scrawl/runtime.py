# Copyright (c) 2026 nurefexc
# SPDX-License-Identifier: MIT
"""Process bootstrap helpers.

These functions keep the executable entry point thin: they locate the project
virtual environment when pycairo is missing and describe how to spawn the
daemon from the package.
"""

from __future__ import annotations

import os
import sys

from . import APP_NAME
from .paths import project_root


def entry_script() -> str | None:
    """Return ``scrawl.py`` when it exists next to the package."""
    candidate = os.path.join(project_root(), "scrawl.py")
    return candidate if os.path.isfile(candidate) else None


def daemon_command() -> list[str]:
    """Build the command that starts the daemon in a new process."""
    entry = entry_script()
    if entry is not None:
        return [sys.executable, entry, "daemon"]
    return [sys.executable, "-m", APP_NAME, "daemon"]


def ensure_runtime(argv: list[str]) -> bool:
    """Re-exec with the project venv when pycairo is missing."""
    try:
        import cairo  # noqa: F401
    except ImportError:
        root = project_root()
        venv_dir = os.path.join(root, ".venv")
        venv_python = os.path.join(venv_dir, "bin", "python")
        if (
            os.path.isfile(venv_python)
            and os.environ.get("SCRAWL_REEXEC") != "1"
            and not _uses_prefix(venv_dir)
        ):
            env = dict(os.environ)
            env["SCRAWL_REEXEC"] = "1"
            entry = entry_script()
            if entry is not None:
                target = [venv_python, entry, *argv]
            else:
                target = [venv_python, "-m", APP_NAME, *argv]
            os.execve(venv_python, target, env)
        print(
            f"{APP_NAME}: pycairo is required. Run ./install.sh or install it "
            "with: python -m pip install pycairo",
            file=sys.stderr,
        )
        return False
    return True


def _uses_prefix(path: str) -> bool:
    """Return whether the interpreter is already using the given prefix."""
    try:
        return os.path.samefile(sys.prefix, path)
    except OSError:
        return os.path.realpath(sys.prefix) == os.path.realpath(path)
