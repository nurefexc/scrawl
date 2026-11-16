#!/usr/bin/env python3
# Copyright (c) 2026 nurefexc
# SPDX-License-Identifier: MIT
"""Executable entry point for scrawl.

All behaviour lives in the ``scrawl`` package. This file only prepares the
runtime and hands control to the command-line client.
"""

from __future__ import annotations

import sys

from scrawl.runtime import ensure_runtime


def main() -> int:
    if not ensure_runtime(sys.argv[1:]):
        return 1
    from scrawl.cli import main as cli_main

    return cli_main()


if __name__ == "__main__":
    raise SystemExit(main())
