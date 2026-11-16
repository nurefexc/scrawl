# Copyright (c) 2026 nurefexc
# SPDX-License-Identifier: MIT
"""Runtime, state and configuration paths.

Every method reads the environment when it is called. That keeps the daemon,
the client and the tests independent of when ``XDG_*`` variables change.
"""

from __future__ import annotations

import os
import re

from . import APP_NAME
from .defaults import CONFIG_FILENAMES, LEGACY_CONFIG_FILENAME


def project_root() -> str:
    """Return the directory that contains the ``scrawl`` package."""
    return os.path.dirname(os.path.dirname(os.path.realpath(__file__)))


def bundled_config_dir() -> str:
    """Return the tracked ``config/`` directory shipped with the project."""
    return os.path.join(project_root(), "config")


class RuntimePaths:
    """Resolve every path scrawl uses outside its own source tree."""

    def runtime_dir(self) -> str:
        base = os.environ.get("XDG_RUNTIME_DIR")
        if base and os.path.isdir(base) and os.access(base, os.W_OK):
            return os.path.join(base, APP_NAME)
        return os.path.join("/tmp", f"{APP_NAME}-{os.getuid()}")

    def socket_path(self) -> str:
        display = re.sub(
            r"[^A-Za-z0-9_.-]+", "_", os.environ.get("DISPLAY", "nodisplay")
        )
        return os.path.join(self.runtime_dir(), f"daemon{display}.sock")

    def lock_path(self) -> str:
        return self.socket_path() + ".lock"

    def state_dir(self) -> str:
        base = os.environ.get("XDG_STATE_HOME")
        if not base:
            base = os.path.join(os.path.expanduser("~"), ".local", "state")
        return os.path.join(base, APP_NAME)

    def log_path(self) -> str:
        return os.path.join(self.state_dir(), "daemon.log")

    def config_dir(self) -> str:
        base = os.environ.get("XDG_CONFIG_HOME")
        if not base:
            base = os.path.join(os.path.expanduser("~"), ".config")
        return os.path.join(base, APP_NAME)

    def config_path(self, section: str) -> str:
        try:
            filename = CONFIG_FILENAMES[section]
        except KeyError:
            raise ValueError(f"unknown config section: {section!r}")
        return os.path.join(self.config_dir(), filename)

    def legacy_config_path(self) -> str:
        return os.path.join(self.config_dir(), LEGACY_CONFIG_FILENAME)

    def settings_path(self) -> str:
        return os.path.join(self.state_dir(), "settings.json")

    def ensure_runtime_dirs(self) -> None:
        self.ensure_private_dir(self.runtime_dir())
        self.ensure_private_dir(self.state_dir())

    @staticmethod
    def ensure_private_dir(path: str) -> None:
        os.makedirs(path, mode=0o700, exist_ok=True)
        try:
            os.chmod(path, 0o700)
        except OSError:
            pass

    @staticmethod
    def rotate_log_if_needed(path: str) -> None:
        try:
            if os.path.getsize(path) > 1024 * 1024:
                os.replace(path, path + ".old")
        except OSError:
            pass

    @staticmethod
    def log_tail(path: str, lines: int = 25) -> str:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                content = handle.readlines()
            return "".join(content[-lines:])
        except OSError:
            return ""


PATHS = RuntimePaths()
