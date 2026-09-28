"""Run the independent loopback cloud manager."""

from __future__ import annotations

import argparse
from pathlib import Path

import uvicorn

from .app import create_app


def main() -> None:
    parser = argparse.ArgumentParser(description="Independent LeRobot cloud model manager")
    parser.add_argument("--port", type=int, default=8095)
    parser.add_argument("--state-dir", type=Path, default=Path.home() / ".lerobot-cloud-manager")
    parser.add_argument("--project", type=Path, help="Source checkout to package when bootstrapping")
    arguments = parser.parse_args()
    uvicorn.run(create_app(arguments.state_dir, project=arguments.project), host="127.0.0.1", port=arguments.port)


if __name__ == "__main__":
    main()
