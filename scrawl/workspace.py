# Copyright (c) 2026 nurefexc
# SPDX-License-Identifier: MIT
"""Current-workspace tracking for X11.

The watcher only reports which workspace is active. A persistent
``xprop -spy`` process is used when available; otherwise it falls back to
polling.
"""

from __future__ import annotations

import re
import subprocess
from typing import Callable, Optional

Desktop = Optional[int]
ChangeCallback = Callable[[Desktop, Desktop], None]


def query_current_desktop() -> Optional[int]:
    """Return the EWMH current desktop, or None when it cannot be queried."""
    commands = (
        (["xdotool", "get_desktop"], "plain"),
        (["xprop", "-root", "-notype", "_NET_CURRENT_DESKTOP"], "xprop"),
    )
    for command, mode in commands:
        try:
            result = subprocess.run(
                command, capture_output=True, text=True, timeout=0.4
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if result.returncode != 0:
            continue
        output = result.stdout.strip()
        if mode == "xprop":
            match = re.search(r"=\s*(\d+)", output)
            if match:
                return int(match.group(1))
        else:
            try:
                return int(output)
            except ValueError:
                continue
    return None


class WorkspaceWatcher:
    """Report changes of the current workspace."""

    def __init__(
        self,
        glib,
        *,
        on_change: ChangeCallback,
    ) -> None:
        self.GLib = glib
        self.on_change = on_change

        self.current: Desktop = query_current_desktop()
        self._timer: Optional[int] = None
        self._proc: Optional[subprocess.Popen[str]] = None
        self._watch: Optional[int] = None
        self._stopping = False

    # -- public state -----------------------------------------------------

    def refresh(self) -> Desktop:
        """Query the current workspace once and report a change immediately."""
        current = query_current_desktop()
        if current is not None:
            self._apply(current)
        return self.current

    # -- lifecycle --------------------------------------------------------

    def start(self) -> None:
        """Start watching without blocking the GTK main loop."""
        self._stopping = False
        try:
            proc = subprocess.Popen(
                ["xprop", "-root", "-spy", "_NET_CURRENT_DESKTOP"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
            )
        except OSError:
            proc = None

        if proc is not None and proc.stdout is not None:
            self._proc = proc
            try:
                self._watch = self.GLib.io_add_watch(
                    proc.stdout.fileno(),
                    self.GLib.PRIORITY_DEFAULT,
                    self.GLib.IOCondition.IN | self.GLib.IOCondition.HUP,
                    self._on_output,
                )
                return
            except Exception:
                self._stop_proc()

        self._timer = self.GLib.timeout_add(600, self._check)

    def stop(self) -> None:
        self._stopping = True
        if self._timer is not None:
            try:
                self.GLib.source_remove(self._timer)
            except Exception:
                pass
            self._timer = None
        self._stop_proc()

    def _stop_proc(self) -> None:
        if self._watch is not None:
            try:
                self.GLib.source_remove(self._watch)
            except Exception:
                pass
            self._watch = None
        if self._proc is not None:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=1.0)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
            self._proc = None

    # -- change detection -------------------------------------------------

    def _on_output(self, _fd: int, _condition) -> bool:
        proc = self._proc
        if proc is None or proc.stdout is None:
            self._watch = None
            return False
        line = proc.stdout.readline()
        if not line:
            # xprop exited; the source is removed by returning False. Fall
            # back to polling so workspace tracking keeps working.
            self._watch = None
            if not self._stopping and self._timer is None:
                self._timer = self.GLib.timeout_add(600, self._check)
            return False
        match = re.search(r"=\s*(\d+)", line)
        if match:
            self._apply(int(match.group(1)))
        return True

    def _check(self) -> bool:
        if self._stopping:
            return False
        self.refresh()
        return True

    def _apply(self, current: int) -> None:
        if self._stopping or current == self.current:
            return
        previous = self.current
        self.current = current
        self.on_change(current, previous)
