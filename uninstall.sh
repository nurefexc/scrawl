#!/usr/bin/env bash
# Copyright (c) 2026 nurefexc
# SPDX-License-Identifier: MIT
#
# Remove the generated parts of a scrawl installation: the launcher, the
# project virtual environment, Python caches and this display's runtime files.
# The project source tree is never removed. Configuration and saved settings
# are kept unless --purge is given.
set -euo pipefail

HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
LAUNCHER="${HOME}/.local/bin/scrawl"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/scrawl"
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/scrawl"

# Mirror scrawl/paths.py without importing the package, so uninstall works
# after the virtual environment or the package files are gone.
if [ -n "${XDG_RUNTIME_DIR:-}" ] &&
    [ -d "${XDG_RUNTIME_DIR}" ] &&
    [ -w "${XDG_RUNTIME_DIR}" ]; then
    RUNTIME_DIR="${XDG_RUNTIME_DIR}/scrawl"
else
    RUNTIME_DIR="/tmp/scrawl-$(id -u)"
fi
DISPLAY_TAG="$(
    printf '%s' "${DISPLAY:-nodisplay}" |
        sed 's/[^A-Za-z0-9_.-][^A-Za-z0-9_.-]*/_/g'
)"
SOCKET="${RUNTIME_DIR}/daemon${DISPLAY_TAG}.sock"

PURGE=0
DRY_RUN=0

usage() {
    cat <<'EOF'
Usage: ./uninstall.sh [--dry-run] [--purge]

Stop the running scrawl daemon and remove:
  - the ~/.local/bin/scrawl launcher, if it belongs to this checkout
  - the project virtual environment (.venv)
  - Python __pycache__ directories inside the project
  - this display's socket, lock and pid files

The project source tree is left untouched. Configuration files in
~/.config/scrawl and saved settings/state in ~/.local/state/scrawl are kept
unless --purge is given.

Options:
  -n, --dry-run  show what would be removed without changing anything
  -p, --purge    also remove configuration and saved state
  -h, --help     show this help

Examples:
  ./uninstall.sh
  ./uninstall.sh --dry-run
  ./uninstall.sh --purge
EOF
}

say() {
    printf '%s\n' "$*"
}

warn() {
    printf 'warning: %s\n' "$*" >&2
}

remove_path() {
    local path="$1"
    if [ -e "$path" ] || [ -L "$path" ]; then
        if [ "$DRY_RUN" -eq 1 ]; then
            say "would remove $path"
        else
            rm -rf -- "$path"
            say "removed $path"
        fi
    fi
}

launcher_belongs() {
    local target=""
    if [ -L "$LAUNCHER" ]; then
        target="$(readlink -f -- "$LAUNCHER" 2>/dev/null || true)"
        case "$target" in
            "$HERE"/*) return 0 ;;
        esac
        return 1
    fi
    [ -f "$LAUNCHER" ] || return 1
    grep -qF -- "$HERE/scrawl.py" "$LAUNCHER" 2>/dev/null || return 1
    grep -qF -- "$HERE/.venv/bin/python" "$LAUNCHER" 2>/dev/null || return 1
    return 0
}

read_pid() {
    local value=""
    if [ -f "${SOCKET}.pid" ]; then
        value="$(tr -cd '0-9' <"${SOCKET}.pid" 2>/dev/null || true)"
    fi
    printf '%s' "$value"
}

pid_matches_checkout() {
    local pid="$1"
    local cmdline=""
    case "$pid" in
        "" | *[!0-9]*) return 1 ;;
    esac
    [ -r "/proc/${pid}/cmdline" ] || return 1
    cmdline="$(tr '\0' ' ' <"/proc/${pid}/cmdline" 2>/dev/null || true)"
    case "$cmdline" in
        *"$HERE/scrawl.py"*) return 0 ;;
    esac
    return 1
}

wait_pid() {
    local pid="$1"
    local limit="${2:-30}"
    local i=0
    while [ "$i" -lt "$limit" ]; do
        kill -0 "$pid" 2>/dev/null || return 0
        sleep 0.1
        i=$((i + 1))
    done
    kill -0 "$pid" 2>/dev/null && return 1
    return 0
}

stop_daemon() {
    local pid=""
    local running=0

    pid="$(read_pid)"
    if [ -n "$pid" ] && pid_matches_checkout "$pid"; then
        running=1
    fi

    if [ "$DRY_RUN" -eq 1 ]; then
        if [ "$running" -eq 1 ]; then
            say "would stop the scrawl daemon (pid ${pid})"
        fi
        return 0
    fi

    if [ "$running" -eq 0 ] && [ ! -S "$SOCKET" ]; then
        say "scrawl daemon is not running"
        return 0
    fi

    if [ "$running" -eq 1 ]; then
        say "stopping the scrawl daemon (pid ${pid}) ..."
    else
        say "removing a stale scrawl socket ..."
    fi

    # Prefer the normal client shutdown. This also handles a daemon started
    # by an older installation of the same checkout.
    if [ -x "$HERE/.venv/bin/python" ] && [ -f "$HERE/scrawl.py" ]; then
        "$HERE/.venv/bin/python" "$HERE/scrawl.py" quit >/dev/null 2>&1 || true
    elif [ -x "$LAUNCHER" ] && launcher_belongs; then
        "$LAUNCHER" quit >/dev/null 2>&1 || true
    fi

    # Fallback for a wedged daemon. Only signal a process whose command line
    # points into this checkout, so a stale pid file cannot hit another
    # program that reused the pid.
    if [ -n "$pid" ] &&
        kill -0 "$pid" 2>/dev/null &&
        pid_matches_checkout "$pid"; then
        kill -TERM "$pid" 2>/dev/null || true
        if ! wait_pid "$pid" 50; then
            if pid_matches_checkout "$pid"; then
                warn "daemon did not stop; sending SIGKILL to pid ${pid}"
                kill -KILL "$pid" 2>/dev/null || true
                wait_pid "$pid" 20 || true
            fi
        fi
    fi
}

remove_launcher() {
    if [ ! -e "$LAUNCHER" ] && [ ! -L "$LAUNCHER" ]; then
        return 0
    fi
    if launcher_belongs; then
        remove_path "$LAUNCHER"
    else
        warn "leaving ${LAUNCHER}: it does not belong to ${HERE}"
        warn "remove it manually if it is an old scrawl launcher"
    fi
}

remove_pycache() {
    local cache=""
    while IFS= read -r -d '' cache; do
        remove_path "$cache"
    done < <(
        find "$HERE" \
            \( -path "$HERE/.git" -o -path "$HERE/.venv" \) -prune -o \
            -type d -name __pycache__ -print0 2>/dev/null
    )
}

remove_runtime_files() {
    remove_path "${SOCKET}"
    remove_path "${SOCKET}.pid"
    remove_path "${SOCKET}.lock"

    if [ -d "$RUNTIME_DIR" ]; then
        if [ "$DRY_RUN" -eq 1 ]; then
            if [ -z "$(ls -A -- "$RUNTIME_DIR" 2>/dev/null)" ]; then
                say "would remove empty runtime directory $RUNTIME_DIR"
            fi
        else
            rmdir -- "$RUNTIME_DIR" 2>/dev/null || true
        fi
    fi
}

remove_user_data() {
    remove_path "$CONFIG_DIR"
    remove_path "$STATE_DIR"
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        -n | --dry-run)
            DRY_RUN=1
            ;;
        -p | --purge)
            PURGE=1
            ;;
        -h | --help)
            usage
            exit 0
            ;;
        *)
            printf 'error: unknown option: %s\n\n' "$1" >&2
            usage >&2
            exit 2
            ;;
    esac
    shift
done

stop_daemon
remove_launcher
remove_path "$HERE/.venv"
remove_pycache
remove_runtime_files
if [ "$PURGE" -eq 1 ]; then
    remove_user_data
fi

say
if [ "$DRY_RUN" -eq 1 ]; then
    say "Dry run: nothing was changed."
else
    say "Generated files removed; the project source in ${HERE} is untouched."
fi

if [ "$PURGE" -eq 1 ]; then
    if [ "$DRY_RUN" -eq 1 ]; then
        say "Configuration and saved state would also be removed."
    else
        say "Configuration and saved state were removed."
    fi
else
    say "Configuration and saved state were kept:"
    say "  ${CONFIG_DIR}"
    say "  ${STATE_DIR}"
    say "Run ./uninstall.sh --purge to remove them as well."
fi
