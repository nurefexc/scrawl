# Copyright (c) 2026 nurefexc
# SPDX-License-Identifier: MIT
"""Per-desktop drawing storage."""

from __future__ import annotations

from typing import Optional

from .model import Drawing

Desktop = Optional[int]


class DesktopDrawings:
    """Keep one ordered drawing for each desktop.

    Drawings are created when a desktop is first used. ``None`` is the fallback
    key for systems where the current desktop cannot be queried.
    """

    def __init__(self) -> None:
        self._drawings: dict[Desktop, Drawing] = {}
        self._desktop: Desktop = None

    def bind(self, desktop: Desktop) -> None:
        """Select the desktop that commands and input events act on."""
        self._desktop = desktop

    @property
    def current(self) -> Drawing:
        """Return the selected drawing, creating it on first use."""
        return self.for_desktop(self._desktop)

    def for_desktop(self, desktop: Desktop) -> Drawing:
        """Return the drawing for ``desktop``, creating it on first use."""
        drawing = self._drawings.get(desktop)
        if drawing is None:
            drawing = Drawing()
            self._drawings[desktop] = drawing
        return drawing
