"""CLI: `lerobot-monitor --config config.yaml`."""

from __future__ import annotations

import argparse
import socket
from pathlib import Path

import uvicorn

from .config import MonitorConfig
from .pathutil import ensure_lerobot_on_path


def _local_ip() -> str:
    try:
        return socket.gethostbyname(socket.gethostname())
    except Exception:
        return "127.0.0.1"


def main() -> None:
    parser = argparse.ArgumentParser(description="LeRobot standalone monitor")
    parser.add_argument(
        "--config",
        default=str(Path(__file__).resolve().parents[2] / "config.yaml"),
        help="YAML config path",
    )
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args()

    ensure_lerobot_on_path()
    config_path = Path(args.config)
    config = MonitorConfig.load(config_path if config_path.is_file() else None)
    host = args.host or config.server.host
    port = args.port or config.server.port

    print(f"[lerobot-monitor] config: {config_path if config_path.is_file() else '(defaults)'}")
    print(f"[lerobot-monitor] http://{host}:{port}")
    print(f"[lerobot-monitor] LAN:    http://{_local_ip()}:{port}")

    from .app import create_app

    uvicorn.run(create_app(config), host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
