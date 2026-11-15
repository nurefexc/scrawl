# scrawl

A command-line screen annotation overlay for X11.

scrawl keeps a transparent, always-on-top canvas over the X screen. The window
is click-through while idle, so pointer and keyboard input reaches the windows
below it. Pen, eraser and text modes take over input only while they are
active, then return the overlay to click-through mode.

There is no toolbar, tray icon or settings window. Every action is a shell
command that talks to a small background daemon over a Unix socket.

- Author: nurefexc
- License: MIT
- Version: 1.2.0

## Features

- Freehand pen with configurable colour and width
- Eraser with configurable diameter and a visible size ring
- Text placement and direct on-screen typing, including multi-line input
- Click-through by default; input is captured only during a mode
- Colour and size aliases loaded from JSON files
- Raw hex/RGB colours and pixel sizes still accepted on the command line
- Persistent current tool settings, so colour and size are switched once
- Workspace-aware overlay: annotations stay on the workspace where they started
- `clear`, `undo`, `hide`, `show` and `toggle`
- Persistent daemon; annotations survive between commands
- JSON status output for scripts, keybindings and status bars
- Straightforward CLI that is easy to wrap with fzf or window manager bindings

## Requirements

- X11. Wayland is not supported.
- Python 3.10 or newer.
- GTK3 with Python bindings (`python-gobject`).
- `pycairo` (installed into the project virtual environment by `install.sh`).
- A compositor for real transparency. picom works; without a compositor the
  overlay may be drawn as an opaque or black window.
- `xdotool` or `xprop` for workspace tracking. Both are usually present on an
  X11 desktop. Without either, the overlay stays visible on every workspace.

On Arch Linux:

```sh
sudo pacman -S gtk3 python-gobject
```

## Installation

Place the project directory anywhere and run:

```sh
cd /path/to/scrawl
./install.sh
```

The installer creates a project-local virtual environment with access to the
system GTK bindings, installs `pycairo`, writes the `scrawl` launcher to
`~/.local/bin`, and creates the split configuration files if they are missing.

Make sure `~/.local/bin` is in `PATH`. If the project directory moves, run
`./install.sh` again so the launcher points at the new location.

Running from the source tree is also supported:

```sh
./scrawl.py --help
```

The script re-executes itself with `.venv/bin/python` when `pycairo` is not
available in the current interpreter.

## Quick start

```sh
scrawl pen
```

Drag with the left mouse button to draw. Press `Esc`, `Enter`, or right-click
to stop drawing. The drawing stays on screen and the overlay becomes
click-through again.

Switch the pen colour and width, then keep drawing:

```sh
scrawl set pen-color neon
scrawl set pen-size bold
scrawl pen
```

Use raw values for a one-off change. The value also becomes the current
setting:

```sh
scrawl pen --color '#00ff9d' --size 12
```

Text can be placed immediately:

```sh
scrawl text --at 100,100 --size text-large --color white 'Hello world'
```

Or typed directly on the overlay:

```sh
scrawl text
```

- `Enter` commits the text and lets you click another position.
- `Shift+Enter` inserts a new line.
- `Backspace` deletes the previous character.
- `Esc` or right-click exits text mode.

Erase with the current diameter:

```sh
scrawl eraser
```

The ring around the cursor shows the eraser size. Erasing is stored as an
eraser stroke, so `undo` can restore what was removed.

```sh
scrawl clear       # remove everything
scrawl undo        # remove the most recent stroke or text
scrawl current     # show the current pen, eraser and text settings
scrawl quit        # stop the daemon; annotations are lost
```

## Command reference

| Command | Description |
| --- | --- |
| `scrawl pen` / `scrawl draw` | Interactive pen mode. Optional `--color`, `--size`. |
| `scrawl eraser` / `scrawl erase` | Interactive eraser mode. Optional `--size`. |
| `scrawl text` / `scrawl write` | Place text or type on screen. Optional `--at X,Y`, `--color`, `--size`, `--font`, `--bold`, `--italic`, and the text as arguments. |
| `scrawl current [--json\|--tsv]` | Show the current pen, eraser and text settings. |
| `scrawl set WHAT VALUE` | Switch a setting and persist it in `settings.json`. |
| `scrawl colors [--json\|--tsv]` | List colour aliases. |
| `scrawl sizes [GROUP] [--json\|--tsv]` | List size aliases, optionally for `pen`, `eraser` or `text`. |
| `scrawl config [--json\|--init]` | Show the config files, or create missing files and migrate a legacy config. |
| `scrawl clear` / `scrawl all-clear` | Remove every annotation. |
| `scrawl undo` | Remove the most recent item. |
| `scrawl hide` | Hide the overlay; annotations are kept. |
| `scrawl show` | Show the overlay again. |
| `scrawl toggle` | Toggle overlay visibility. |
| `scrawl status [--json]` | Daemon status, workspace state and current settings. |
| `scrawl daemon` | Run the daemon in the foreground for debugging. |
| `scrawl quit` | Stop the daemon and clear the annotations. |

The daemon starts automatically on the first drawing command and keeps running
until `scrawl quit`, logout, or a signal.

### `scrawl set`

`WHAT` accepts the following names:

| Name | Meaning |
| --- | --- |
| `pen-color`, `color` | Pen colour |
| `pen-size`, `size` | Pen width |
| `eraser-size` | Eraser diameter |
| `text-color` | Text colour |
| `text-size` | Font size |
| `text-font` | Font family |
| `text-bold` | Bold on/off |
| `text-italic` | Italic on/off |

Boolean values accept `on`/`off`, `true`/`false`, `yes`/`no`, and `1`/`0`.

## Configuration

Configuration is split by purpose. The files live in
`${XDG_CONFIG_HOME:-~/.config}/scrawl/`:

| File | Contents |
| --- | --- |
| `colors.json` | Colour aliases: alias to any GTK/CSS colour string. |
| `sizes.json` | Size aliases: alias to a positive pixel value. |
| `groups.json` | Ordered alias groups for the pen, eraser and text pickers. |

Current selections are stored separately in
`${XDG_STATE_HOME:-~/.local/state}/scrawl/settings.json`. That file is written
by `scrawl set` and by explicit `--color`, `--size` and `--font` arguments.

Starting values are built into the program and are not read from the config
files:

| Tool | Default colour | Default size |
| --- | --- | --- |
| Pen | `red` (`#ff3b30`) | `thin` (4 px) |
| Eraser | n/a | `eraser` (30 px) |
| Text | `red` (`#ff3b30`) | `text` (32 px), `sans` |

To change the current values, use `scrawl set`; this writes
`settings.json`. To change the compiled-in starting values, edit the constants
in `scrawl.py` before installing.

### `colors.json`

The file contains a single object mapping aliases to colours:

```json
{
  "red": "#ff3b30",
  "orange": "#ff9500",
  "yellow": "#ffcc00",
  "green": "#34c759",
  "neon": "#00ff9d",
  "blue": "#0a84ff",
  "white": "#ffffff",
  "black": "#000000",
  "brand": "#1f6feb",
  "brand-muted": "rgba(31, 111, 235, 0.45)"
}
```

Aliases may point at other aliases:

```json
{
  "brand": "#1f6feb",
  "accent": "brand"
}
```

Any value accepted by GTK's `Gdk.RGBA.parse()` works. That includes named
colours, `#rgb`, `#rrggbb`, `#rrggbbaa`, `rgb(...)` and `rgba(...)`.

### `sizes.json`

The file maps aliases to positive pixel values:

```json
{
  "hairline": 2,
  "thin": 4,
  "normal": 8,
  "bold": 16,
  "marker": 24,
  "eraser": 30,
  "eraser-large": 60,
  "eraser-xl": 100,
  "text-small": 20,
  "text": 32,
  "text-large": 48,
  "text-xl": 72
}
```

### `groups.json`

Groups control the order in which aliases appear in pickers. Each group is a
list of size aliases:

```json
{
  "pen": ["hairline", "thin", "normal", "bold", "marker"],
  "eraser": ["eraser", "eraser-large", "eraser-xl"],
  "text": ["text-small", "text", "text-large", "text-xl"]
}
```

A group alias that is not present in `sizes.json` is ignored by
`scrawl sizes GROUP`.

### Creating and migrating config files

```sh
scrawl config --init
```

This creates any missing split file from the built-in aliases. If an older
single-file `config.json` exists, its colour, size and group entries are
merged with any existing split files and written there, and the old file is
renamed to `config.json.legacy`. Without a legacy file, existing split files
are left untouched.

Inspect the result:

```sh
scrawl config             # paths and alias counts
scrawl config --json      # effective config, including built-in defaults
scrawl colors --tsv       # alias<TAB>#rrggbb
scrawl sizes pen --tsv    # alias<TAB>pixels
```

Invalid files are reported with the file name and the offending entry. A
missing file means the built-in aliases are used for that section.

### Settings file

`settings.json` is managed by scrawl:

```json
{
  "pen": {
    "color": "neon",
    "size": "bold"
  },
  "eraser": {
    "size": "eraser"
  },
  "text": {
    "color": "red",
    "size": "text",
    "font": "sans",
    "bold": false,
    "italic": false
  }
}
```

You normally do not need to edit it by hand. Delete it to return to the
built-in starting values.

## Workspaces

The overlay is an override-redirect window, so the window manager does not
move it between workspaces. scrawl polls `_NET_CURRENT_DESKTOP` and binds the
overlay to the workspace where drawing starts:

- starting a mode, or running `scrawl show`, adopts the current workspace
- switching away hides the overlay and keeps the annotations
- switching back shows the same annotations again, still click-through
- switching away during a mode ends the mode before hiding the overlay

`scrawl status` reports the bound desktop, the current desktop, and whether the
overlay is hidden by a workspace switch. If neither `xdotool` nor `xprop` is
available, workspace tracking is disabled and the overlay stays visible
everywhere.

## Window manager integration

Any X11 window manager or hotkey daemon can launch scrawl. A common setup is a
submap or a set of keybindings that call the CLI directly.

For xmonad, an `M-x` submap can look like this:

```haskell
scrawlSubmap :: XConfig a -> M.Map (KeyMask, KeySym) (X ())
scrawlSubmap c = mkKeymap c
  [ ("p", spawn "scrawl pen")
  , ("e", spawn "scrawl eraser")
  , ("t", spawn "scrawl text")
  , ("c", spawn "scrawl clear")
  , ("u", spawn "scrawl undo")
  , ("h", spawn "scrawl hide")
  , ("s", spawn "scrawl show")
  , ("v", spawn "scrawl toggle")
  , ("q", spawn "scrawl quit")
  ]
```

and in the key list:

```haskell
  , ("M-x", submap $ scrawlSubmap def)
```

A separate fzf wrapper can replace the direct CLI calls with interactive
colour and size selection. It is optional; the daemon and the CLI do not
depend on fzf or on any particular desktop setup.

### Status bars and scripts

`scrawl status --json` prints a single JSON object. It can be read by a status
bar, a shell prompt or a custom script. scrawl does not require a status bar
and does not depend on one.

Example fields:

```json
{
  "running": true,
  "visible": true,
  "effective_visible": true,
  "workspace_hidden": false,
  "clickthrough": true,
  "desktop": 1,
  "current_desktop": 1,
  "mode": null,
  "items": 3
}
```

The same object also contains the current pen, eraser and text settings.

## Click-through and window handling

The overlay is a full-screen, override-redirect, ARGB GTK3 window. While idle
it has an empty X11 input shape, so all pointer and keyboard events pass to the
windows below it.

Entering a mode installs a full-screen input shape and grabs the pointer and
keyboard. Leaving the mode restores the empty input shape and releases the
grab. If the grab is refused, scrawl falls back to keyboard focus and still
restores click-through when the mode ends.

The daemon also watches the client connection. If the client process dies
during a mode, the mode is closed instead of leaving input captured.

## Architecture

scrawl consists of two parts:

- `scrawl.py daemon` keeps the GTK window and the drawing model alive.
- `scrawl` (the same script with a different subcommand) is a thin client that
  sends one JSON command over a Unix socket.

The daemon is single-instance per X display, protected by a lock file. The
socket is created with mode `0600` under `$XDG_RUNTIME_DIR/scrawl/` or a
per-user temporary directory. Commands and replies are newline-delimited JSON.

Annotations are stored as an ordered list of pen strokes, eraser strokes and
text items. The backing surface is a full-screen cairo image surface. Live
drawing repaints only the dirty rectangle and replays intersecting items with
a clip, which keeps translucent strokes uniform and makes undo
straightforward. Eraser strokes use cairo's `OPERATOR_CLEAR`, so they remove
alpha from the surface rather than painting over it.

## Files

Project files:

```text
scrawl.py        CLI, daemon and drawing model
install.sh       virtual environment, launcher and config installer
LICENSE          MIT license
AUTHORS          project author
README.md        this file
.venv/           project-local Python environment (created by install.sh)
```

Runtime files:

```text
~/.config/scrawl/colors.json          colour aliases
~/.config/scrawl/sizes.json           size aliases
~/.config/scrawl/groups.json          picker groups
~/.local/state/scrawl/settings.json   current tool settings
~/.local/state/scrawl/daemon.log      daemon log
$XDG_RUNTIME_DIR/scrawl/daemon_*.sock Unix socket
```

## Troubleshooting

- `scrawl: not running` means there is no active daemon. Drawing commands
  start it automatically; `scrawl status` only reports.
- Read `~/.local/state/scrawl/daemon.log` if the daemon fails to start.
- If `scrawl status` shows items but nothing is visible, check that a
  compositor is running and that the chosen colour is not too close to the
  background.
- If the overlay appears black, exclude it from compositor unredirecting:

  ```conf
  unredir-if-possible-exclude = ["class_g = 'Scrawl'"];
  ```

- If text input ignores dead keys or an IME, use a precomposed string with
  `scrawl text --at X,Y '...'`.
- If the overlay keeps appearing on every workspace, install `xdotool` or
  `xprop`, or check that `_NET_CURRENT_DESKTOP` is set by the window manager.
- Kill a stuck instance with `scrawl quit`, or `pkill -f scrawl.py` as a last
  resort.

## Limitations

- X11 only. Wayland's security model does not allow this kind of global
  overlay and input-shape tool without compositor-specific protocols.
- The overlay spans the whole X screen, not a single monitor.
- HiDPI scaling is handled in logical pixels. Drawing can be slightly soft at
  fractional scales.
- IME pre-edit and compose text are not supported during direct on-screen
  typing.
- Transparency depends on a compositor.

## Development

Run the source tree directly:

```sh
./scrawl.py --help
./scrawl.py pen
```

After changing `scrawl.py`, check that it compiles and exercise the main
commands:

```sh
.venv/bin/python -m py_compile scrawl.py
scrawl config --init
scrawl current --tsv
scrawl pen
```

The daemon can be run in the foreground to see startup errors:

```sh
./scrawl.py daemon
```

## License

MIT. See [LICENSE](LICENSE).

Copyright (c) 2026 nurefexc.
