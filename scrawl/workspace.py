# Copyright (c) 2026 nurefexc
# SPDX-License-Identifier: MIT
"""Workspace tracking for override-redirect windows.

Override-redirect windows are visible on every X11 workspace. The watcher binds
the overlay to the workspace where drawing starts and hides it when the user
switches away. A persistent ``xprop -spy`` process is used when available;
otherwise it falls back to polling.
"""

from __future__ import annotations

import re
import subprocess
from typing import Callable, Optional

HideCallback = Callable[[int, int], None]
ShowCallback = Callable[[int, int], None]
LogCallback = Callable[[str], None]


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
    """Keep the overlay bound to one workspace."""

    def __init__(
        self,
        glib,
        *,
        on_hide: HideCallback,
        on_show: ShowCallback,
        log: LogCallback,
    ) -> None:
        self.GLib = glib
        self.on_hide = on_hide
        self.on_show = on_show
        self._log = log

        self.bound = query_current_desktop()
        self.current = self.bound
        self.hidden = False

        self._timer: Optional[int] = None
        self._proc: Optional[subprocess.Popen[str]] = None
        self._watch: Optional[int] = None
        self._stopping = False

    # -- public state -----------------------------------------------------

    def adopt(self) -> None:
        """Bind the overlay to the workspace the user is looking at now."""
        desktop = self.current
        if desktop is None:
            desktop = query_current_desktop()
        if desktop is None:
            return
        self.bound = desktop
        self.current = desktop
        self.hidden = False

    def clear_hidden(self) -> None:
        self.hidden = False

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
        current = query_current_desktop()
        if current is None:
            return True
        self._apply(current)
        return True

    def _apply(self, current: int) -> None:
        if self._stopping:
            return
        self.current = current

        if self.bound is None:
            self.bound = current
            return

        if current != self.bound:
            if not self.hidden:
                self.hidden = True
                self.on_hide(self.bound, current)
        elif self.hidden:
            self.hidden = False
            self.on_show(self.bound, current)
