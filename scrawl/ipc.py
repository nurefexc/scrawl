# Copyright (c) 2026 nurefexc
# SPDX-License-Identifier: MIT
"""Unix-socket transport between the CLI and the daemon."""

from __future__ import annotations

import fcntl
import json
import os
import socket
import subprocess
import threading
import time
from typing import Any, Callable, Optional

from . import APP_NAME, MAX_COMMAND_BYTES, SOCKET_START_TIMEOUT, VERSION
from .paths import RuntimePaths, project_root
from .runtime import daemon_command

CommandCallback = Callable[[dict[str, Any], socket.socket], bool]
LogCallback = Callable[[str], None]


class ScrawlClient:
    """Thin client for one JSON command per connection."""

    def __init__(self, paths: Optional[RuntimePaths] = None) -> None:
        self.paths = paths or RuntimePaths()

    def can_connect(self, timeout: float = 0.2) -> bool:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        try:
            sock.connect(self.paths.socket_path())
            return True
        except OSError:
            return False
        finally:
            sock.close()

    def exchange(
        self, command: dict[str, Any], start_daemon: bool = True
    ) -> dict[str, Any]:
        """Send one command to the daemon and wait for its final reply."""
        if not self.can_connect():
            if not start_daemon:
                raise RuntimeError("the scrawl daemon is not running")
            self.spawn_daemon()

        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(None)
        try:
            sock.connect(self.paths.socket_path())
            payload = (
                json.dumps(command, ensure_ascii=False).encode("utf-8") + b"\n"
            )
            sock.sendall(payload)
            reader = sock.makefile("rb")
            line = reader.readline(MAX_COMMAND_BYTES + 1)
            if not line:
                raise RuntimeError("the daemon closed the connection")
            if not line.endswith(b"\n"):
                raise RuntimeError("invalid reply from the daemon")
            response = json.loads(line.decode("utf-8"))
            if not isinstance(response, dict):
                raise RuntimeError("invalid reply from the daemon")
            if not response.get("ok", False):
                raise RuntimeError(
                    str(response.get("error", "unknown daemon error"))
                )
            return response
        finally:
            sock.close()

    def spawn_daemon(self) -> None:
        """Start the daemon in the background and wait until its socket is ready."""
        self.paths.ensure_runtime_dirs()
        log = self.paths.log_path()
        self.paths.rotate_log_if_needed(log)

        with open(log, "ab") as logfile:
            logfile.write(
                f"\n--- {APP_NAME} {VERSION} starting "
                f"{time.strftime('%Y-%m-%d %H:%M:%S')} "
                f"DISPLAY={os.environ.get('DISPLAY', '-')} ---\n".encode("utf-8")
            )
            logfile.flush()
            subprocess.Popen(
                daemon_command(),
                stdin=subprocess.DEVNULL,
                stdout=logfile,
                stderr=logfile,
                start_new_session=True,
                close_fds=True,
                cwd=project_root() or None,
            )

        deadline = time.monotonic() + SOCKET_START_TIMEOUT
        while time.monotonic() < deadline:
            if self.can_connect(timeout=0.2):
                return
            time.sleep(0.05)

        tail = self.paths.log_tail(log)
        message = "failed to start the scrawl daemon"
        if tail:
            message += "\n\nLast lines of the daemon log:\n" + tail
        message += f"\nLog file: {log}"
        raise RuntimeError(message)


class DaemonServer:
    """Single-instance Unix socket server used by the daemon."""

    def __init__(
        self,
        paths: RuntimePaths,
        glib,
        on_command: CommandCallback,
        log: LogCallback,
    ) -> None:
        self.paths = paths
        self.GLib = glib
        self.on_command = on_command
        self._log = log

        self.server: Optional[socket.socket] = None
        self.thread: Optional[threading.Thread] = None
        self.lock_fd: Optional[int] = None
        self.running = False

    def start(self) -> None:
        self.paths.ensure_runtime_dirs()
        path = self.paths.socket_path()

        self.lock_fd = os.open(
            self.paths.lock_path(), os.O_CREAT | os.O_RDWR, 0o600
        )
        try:
            fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(self.lock_fd)
            self.lock_fd = None
            self._log("another scrawl daemon is already running on this display")
            raise SystemExit(0)

        try:
            os.unlink(path)
        except OSError:
            pass

        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            server.bind(path)
            os.chmod(path, 0o600)
            server.listen(8)
        except Exception:
            server.close()
            if self.lock_fd is not None:
                os.close(self.lock_fd)
                self.lock_fd = None
            raise

        self.server = server
        self.running = True
        self.thread = threading.Thread(
            target=self._accept_loop, name="scrawl-socket", daemon=True
        )
        self.thread.start()

        try:
            with open(path + ".pid", "w", encoding="ascii") as handle:
                handle.write(str(os.getpid()))
        except OSError:
            pass

    def _accept_loop(self) -> None:
        while self.running:
            try:
                if self.server is None:
                    break
                client, _addr = self.server.accept()
            except OSError:
                break
            try:
                client.settimeout(5.0)
                chunks = bytearray()
                while b"\n" not in chunks:
                    chunk = client.recv(65536)
                    if not chunk:
                        raise ValueError("empty request")
                    chunks.extend(chunk)
                    if len(chunks) > MAX_COMMAND_BYTES:
                        raise ValueError("request too large")
                line = bytes(chunks).split(b"\n", 1)[0]
                cmd = json.loads(line.decode("utf-8"))
                if not isinstance(cmd, dict):
                    raise ValueError("request is not a JSON object")
                client.settimeout(None)
            except Exception as exc:
                try:
                    self.reply(client, {"ok": False, "error": str(exc)})
                except Exception:
                    pass
                continue
            self.GLib.idle_add(self.on_command, cmd, client)

    @staticmethod
    def reply(client: socket.socket, payload: dict[str, Any]) -> None:
        try:
            client.sendall(
                json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n"
            )
            try:
                client.shutdown(socket.SHUT_WR)
            except OSError:
                pass
        except OSError:
            pass
        finally:
            try:
                client.close()
            except OSError:
                pass

    def stop(self) -> None:
        self.running = False
        if self.server is not None:
            try:
                self.server.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                self.server.close()
            except OSError:
                pass
            self.server = None
        self.cleanup()

    def cleanup(self) -> None:
        for path in (
            self.paths.socket_path(),
            self.paths.socket_path() + ".pid",
        ):
            try:
                os.unlink(path)
            except OSError:
                pass
        if self.lock_fd is not None:
            try:
                os.close(self.lock_fd)
            except OSError:
                pass
            self.lock_fd = None
