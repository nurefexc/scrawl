# Copyright (c) 2026 nurefexc
# SPDX-License-Identifier: MIT
"""Allow ``python -m scrawl`` to behave like the executable entry point."""

from __future__ import annotations

import sys

from .runtime import ensure_runtime

if not ensure_runtime(sys.argv[1:]):
    raise SystemExit(1)

from .cli import main  # noqa: E402  (import after the venv check)

raise SystemExit(main())
