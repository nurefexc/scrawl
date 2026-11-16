# Copyright (c) 2026 nurefexc
# SPDX-License-Identifier: MIT
"""scrawl - command-line screen annotation overlay for X11.

The package is split by responsibility: paths, configuration, drawing model,
rendering, the GTK overlay window, workspace tracking, IPC, the daemon and the
command-line client. The executable ``scrawl.py`` at the project root is only a
thin entry point.

Author: nurefexc
License: MIT
"""

from __future__ import annotations

__author__ = "nurefexc"
__copyright__ = "Copyright (c) 2026 nurefexc"
__license__ = "MIT"

APP_NAME = "scrawl"
VERSION = "1.3.0"

MAX_COMMAND_BYTES = 1024 * 1024
SOCKET_START_TIMEOUT = 4.0
