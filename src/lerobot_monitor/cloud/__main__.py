"""Run the standalone service or manage its own background daemon."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

from . import code_hash
from .runtime import read_token


def request_service(root: Path, port: int, path: str, *, method: str = "GET") -> dict[str, Any]:
    token = (root / "token").read_text(encoding="utf-8").strip()
    request = urllib.request.Request(f"http://127.0.0.1:{port}/api/v1/{path}", method=method,
        headers={"Authorization": f"Bearer {token}"})
    # SSH-managed services must not be routed through shell proxy settings.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=3) as response:
        return json.load(response)


def daemon_status(root: Path, port: int) -> dict[str, Any]:
    record = root / "daemon.json"
    if not record.exists():
        return {"running": False, "root": str(root), "port": port}
    try:
        metadata = json.loads(record.read_text(encoding="utf-8"))
        health = request_service(root, metadata["port"], "health")
        if health.get("instance_id") != metadata["instance_id"]:
            return {"running": False, "error": "Daemon instance identity mismatch"}
        return {"running": True, **metadata, "active_sessions": health["active_sessions"],
                "code_hash": health.get("code_hash")}
    except (OSError, ValueError, KeyError, urllib.error.URLError):
        return {"running": False, "root": str(root), "port": port}


def daemon(action: str, root: Path, port: int) -> dict[str, Any]:
    status = daemon_status(root, port)
    if action == "status":
        return status
    if action == "stop":
        if not status["running"]:
            return status
        try:
            request_service(root, status["port"], "shutdown", method="POST")
        except urllib.error.HTTPError as exc:
            body = json.load(exc)
            raise RuntimeError(body.get("detail", "Daemon refused to stop")) from exc
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if not daemon_status(root, port)["running"]:
                try:
                    lock = RootLock(root)
                except RuntimeError:
                    pass  # HTTP can close before workers and SQLite finish shutting down.
                else:
                    lock.close()
                    return {"running": False, "root": str(root), "port": port}
            time.sleep(0.2)
        raise RuntimeError("Daemon did not stop within 15 seconds")
    if status["running"]:
        if status["code_hash"] != code_hash():
            raise RuntimeError("A different service build is running; stop it explicitly before upgrading")
        if status["port"] != port:
            raise RuntimeError("Service already runs on a different port")
        return status
    read_token(root)
    instance_id = uuid.uuid4().hex
    command = [sys.executable, "-m", "lerobot_monitor.cloud", "--root", str(root), "--port", str(port),
               "--instance-id", instance_id]
    log_path = root / "service.log"
    with log_path.open("ab") as log:
        kwargs: dict[str, Any] = {"stdin": subprocess.DEVNULL, "stdout": log, "stderr": log,
                                  "start_new_session": os.name != "nt"}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        process = subprocess.Popen(command, **kwargs)
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"Service exited during startup; inspect {log_path}")
        status = daemon_status(root, port)
        if status["running"] and status.get("instance_id") == instance_id:
            return status
        time.sleep(0.2)
    process.terminate()
    process.wait(timeout=5)
    raise RuntimeError(f"Service startup timed out; inspect {log_path}")


class RootLock:
    """Prevent separate foreground/daemon processes from owning the same root."""

    def __init__(self, root: Path) -> None:
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.stream = (root / "service.lock").open("a+b")
        try:
            if os.name == "nt":
                import msvcrt
                self.stream.seek(0)
                self.stream.write(b"0")
                self.stream.flush()
                self.stream.seek(0)
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError) as exc:
            self.stream.close()
            raise RuntimeError("Another service owns this root") from exc

    def close(self) -> None:
        self.stream.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default="~/.lerobot-monitor-cloud")
    parser.add_argument("--host", default="127.0.0.1", choices=["127.0.0.1", "::1"])
    parser.add_argument("--port", type=int, default=8091)
    parser.add_argument("--instance-id", help=argparse.SUPPRESS)
    subparsers = parser.add_subparsers(dest="command")
    daemon_parser = subparsers.add_parser("daemon")
    daemon_parser.add_argument("action", choices=["start", "status", "stop"])
    daemon_parser.add_argument("--root", default=argparse.SUPPRESS)
    daemon_parser.add_argument("--port", type=int, default=argparse.SUPPRESS)
    args = parser.parse_args()
    root = Path(args.root).expanduser().resolve()
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    try:
        if args.command == "daemon":
            print(json.dumps(daemon(args.action, root, args.port)))
            return
        import uvicorn
        from .app import create_app

        lock = RootLock(root)
        instance_id = args.instance_id or uuid.uuid4().hex
        fingerprint = code_hash()
        server: Any = None

        def shutdown() -> None:
            server.should_exit = True

        app = create_app(root, shutdown=shutdown, instance_id=instance_id, code_hash=fingerprint)
        server = uvicorn.Server(uvicorn.Config(app, host=args.host, port=args.port, access_log=False))
        record = root / "daemon.json"
        temporary = root / f"daemon.{instance_id}.tmp"
        temporary.write_text(json.dumps({"pid": os.getpid(), "port": args.port, "root": str(root),
                                        "instance_id": instance_id, "code_hash": fingerprint}), encoding="utf-8")
        temporary.replace(record)
        try:
            server.run()
        finally:
            app.state.runtime.close()
            record.unlink(missing_ok=True)
            lock.close()
    except (OSError, ValueError, RuntimeError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
