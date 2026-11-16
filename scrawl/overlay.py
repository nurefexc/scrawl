# Copyright (c) 2026 nurefexc
# SPDX-License-Identifier: MIT
"""GTK3 override-redirect overlay window and X11 input handling."""

from __future__ import annotations

import math
import traceback
from typing import Callable

LogCallback = Callable[[str], None]


class OverlayWindow:
    """A full-screen ARGB window that is click-through unless a mode is active."""

    def __init__(
        self,
        *,
        gdk,
        gtk,
        cairo,
        display,
        screen,
        log: LogCallback,
        on_draw,
        on_key_press,
        on_button_press,
        on_button_release,
        on_motion,
        on_leave,
        on_destroy,
    ) -> None:
        self.Gdk = gdk
        self.Gtk = gtk
        self.cairo = cairo
        self.display = display
        self.screen = screen
        self._log = log

        self.width = int(screen.get_width())
        self.height = int(screen.get_height())
        if self.width <= 0 or self.height <= 0:
            raise RuntimeError("invalid screen size")

        self.grabbed = False
        self.prev_focus = None
        self.win = None
        self.gdkwin = None

        self._on_draw = on_draw
        self._on_key_press = on_key_press
        self._on_button_press = on_button_press
        self._on_button_release = on_button_release
        self._on_motion = on_motion
        self._on_leave = on_leave
        self._on_destroy = on_destroy

        self._build()

    # -- construction -----------------------------------------------------

    def _build(self) -> None:
        Gtk = self.Gtk
        Gdk = self.Gdk

        win = Gtk.Window(type=Gtk.WindowType.TOPLEVEL)
        win.set_title("scrawl-overlay")
        win.set_role("scrawl-overlay")
        win.set_wmclass("scrawl", "Scrawl")
        win.set_decorated(False)
        win.set_resizable(False)
        win.set_skip_taskbar_hint(True)
        win.set_skip_pager_hint(True)
        win.set_keep_above(True)
        win.set_accept_focus(False)
        win.set_can_focus(False)
        win.set_focus_on_map(False)
        win.set_type_hint(Gdk.WindowTypeHint.NOTIFICATION)
        win.set_app_paintable(True)

        visual = self.screen.get_rgba_visual()
        if visual is not None:
            win.set_visual(visual)

        win.add_events(
            Gdk.EventMask.BUTTON_PRESS_MASK
            | Gdk.EventMask.BUTTON_RELEASE_MASK
            | Gdk.EventMask.POINTER_MOTION_MASK
            | Gdk.EventMask.ENTER_NOTIFY_MASK
            | Gdk.EventMask.LEAVE_NOTIFY_MASK
            | Gdk.EventMask.KEY_PRESS_MASK
            | Gdk.EventMask.KEY_RELEASE_MASK
        )
        win.connect("draw", self._on_draw)
        win.connect("key-press-event", self._on_key_press)
        win.connect("button-press-event", self._on_button_press)
        win.connect("button-release-event", self._on_button_release)
        win.connect("motion-notify-event", self._on_motion)
        win.connect("leave-notify-event", self._on_leave)
        win.connect("destroy", self._on_destroy)

        win.set_default_size(self.width, self.height)
        win.move(0, 0)
        win.realize()

        gdkwin = win.get_window()
        self.win = win
        self.gdkwin = gdkwin

        if gdkwin is not None:
            # Bypass the window manager: never tiled, never decorated, always
            # above normal windows.
            gdkwin.set_override_redirect(True)
            gdkwin.set_keep_above(True)

        # Install the empty input shape before mapping, so the overlay is
        # click-through from its very first frame.
        self.set_clickthrough(True)
        win.show()
        if gdkwin is not None:
            gdkwin.raise_()

    # -- window state -----------------------------------------------------

    def set_clickthrough(self, enabled: bool) -> None:
        if self.gdkwin is None:
            return
        try:
            if enabled:
                region = self.cairo.Region()
            else:
                region = self.cairo.Region(
                    self.cairo.RectangleInt(0, 0, self.width, self.height)
                )
            self.gdkwin.input_shape_combine_region(region, 0, 0)
        except Exception:
            self._log(
                "warning: could not update the X11 input shape\n"
                + traceback.format_exc()
            )

    def show(self) -> None:
        self.win.show()

    def hide(self) -> None:
        self.win.hide()

    def raise_(self) -> None:
        if self.gdkwin is None:
            return
        try:
            self.gdkwin.raise_()
        except Exception:
            pass

    def queue_draw(self) -> None:
        self.win.queue_draw()

    def queue_draw_area(self, bbox: tuple[float, float, float, float]) -> None:
        x0, y0, x1, y1 = bbox
        x0 = max(0.0, min(float(self.width), x0))
        y0 = max(0.0, min(float(self.height), y0))
        x1 = max(0.0, min(float(self.width), x1))
        y1 = max(0.0, min(float(self.height), y1))
        if x1 <= x0 or y1 <= y0:
            return
        self.win.queue_draw_area(
            int(math.floor(x0)),
            int(math.floor(y0)),
            max(1, int(math.ceil(x1 - x0))),
            max(1, int(math.ceil(y1 - y0))),
        )

    def resize(self, width: int, height: int) -> None:
        self.width, self.height = width, height
        self.win.resize(width, height)
        self.win.move(0, 0)

    def destroy(self) -> None:
        try:
            if self.win is not None:
                self.win.destroy()
        except Exception:
            pass

    # -- interactive state ------------------------------------------------

    def _cursor(self, cursor_type: str):
        Gdk = self.Gdk
        ctype = getattr(Gdk.CursorType, cursor_type)
        try:
            return Gdk.Cursor.new_for_display(self.display, ctype)
        except Exception:
            return Gdk.Cursor.new_from_name(self.display, "default")

    def enter_interactive(self, cursor_type: str) -> None:
        Gdk = self.Gdk
        self.win.set_accept_focus(True)
        self.win.set_can_focus(True)
        self.set_clickthrough(False)
        try:
            self.display.flush()
        except Exception:
            pass

        cursor = self._cursor(cursor_type)
        if self.gdkwin is not None:
            self.gdkwin.set_cursor(cursor)
            self.gdkwin.raise_()

        self.grabbed = False
        self.prev_focus = None
        try:
            status = self.display.get_default_seat().grab(
                self.gdkwin,
                Gdk.SeatCapabilities.POINTER | Gdk.SeatCapabilities.KEYBOARD,
                True,
                cursor,
                None,
                None,
                None,
            )
            self.grabbed = status == Gdk.GrabStatus.SUCCESS
        except Exception:
            self._log(
                "warning: could not grab pointer/keyboard\n"
                + traceback.format_exc()
            )

        if not self.grabbed:
            # Fallback for environments where the grab is refused: at least
            # take keyboard focus so Esc still exits the mode.
            try:
                self.prev_focus = self.screen.get_active_window()
                if self.gdkwin is not None:
                    self.gdkwin.focus(Gdk.CURRENT_TIME)
            except Exception:
                self.prev_focus = None
        self.win.queue_draw()

    def leave_interactive(self) -> None:
        Gdk = self.Gdk
        if self.grabbed:
            try:
                self.display.get_default_seat().ungrab()
            except Exception:
                pass
            self.grabbed = False
        if self.gdkwin is not None:
            try:
                self.gdkwin.set_cursor(None)
            except Exception:
                pass
        self.set_clickthrough(True)
        self.win.set_accept_focus(False)
        self.win.set_can_focus(False)
        if self.prev_focus is not None:
            try:
                if self.prev_focus != self.gdkwin:
                    self.prev_focus.focus(Gdk.CURRENT_TIME)
            except Exception:
                pass
            self.prev_focus = None
