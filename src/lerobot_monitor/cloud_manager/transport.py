"""Bounded OpenSSH transport with no credentials in process arguments."""

from __future__ import annotations

import os
import shlex
import socket
import subprocess
import time
from pathlib import Path
from typing import BinaryIO


class TransportError(RuntimeError):
    """An SSH operation failed; command output is intentionally kept private."""


class SSHTransport:
    def __init__(self, executable: str = "ssh") -> None:
        self.executable = executable

    def arguments(self, alias: str) -> list[str]:
        return [self.executable, "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
                "-o", "ConnectTimeout=15", "-o", "ServerAliveInterval=15",
                "-o", "ServerAliveCountMax=2", "-T", alias]

    def run(self, alias: str, arguments: list[str], *, data: bytes | BinaryIO | None = None,
            timeout: float = 30) -> str:
        command = self.arguments(alias) + [shlex.join(arguments)]
        options: dict[str, object] = {"stdout": subprocess.PIPE, "stderr": subprocess.PIPE,
                                      "timeout": timeout, "check": False}
        if os.name == "nt":
            options["creationflags"] = subprocess.CREATE_NO_WINDOW
        if hasattr(data, "read"):
            options["stdin"] = data
        else:
            options["input"] = data
        try:
            result = subprocess.run(command, **options)
        except subprocess.TimeoutExpired as exc:
            raise TransportError(f"SSH operation timed out after {timeout:g}s") from exc
        except OSError as exc:
            raise TransportError("Cannot launch OpenSSH; check installation and SSH config") from exc
        if result.returncode:
            # stderr can contain remote file paths or credentials; never return it to browsers.
            raise TransportError(f"SSH operation failed (exit {result.returncode}); check host access and service logs")
        return result.stdout.decode("utf-8", errors="replace")

    def tunnel(self, alias: str, remote_port: int) -> tuple[subprocess.Popen[bytes], int]:
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = int(listener.getsockname()[1])
        command = self.arguments(alias)
        command[-1:-1] = ["-N", "-o", "ExitOnForwardFailure=yes", "-L",
                          f"127.0.0.1:{port}:127.0.0.1:{remote_port}"]
        options: dict[str, object] = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
                                     "stderr": subprocess.DEVNULL}
        if os.name == "nt":
            options["creationflags"] = subprocess.CREATE_NO_WINDOW
        process = subprocess.Popen(command, **options)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise TransportError("SSH tunnel exited; check alias, keys and known_hosts")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    return process, port
            except OSError:
                time.sleep(0.1)
        self.stop(process)
        raise TransportError("SSH tunnel startup timed out")

    @staticmethod
    def stop(process: subprocess.Popen[bytes]) -> None:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
