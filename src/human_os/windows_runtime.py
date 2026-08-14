"""Supervise the four loopback Human OS read-only services on Windows."""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO


def _port_ready(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.3):
            return True
    except OSError:
        return False


def _wait_ready(processes: list[subprocess.Popen[bytes]], ports: tuple[int, ...]) -> None:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        failed = next((process for process in processes if process.poll() is not None), None)
        if failed is not None:
            raise RuntimeError(f"Human OS child process exited during startup: {failed.pid}")
        if all(_port_ready(port) for port in ports):
            return
        time.sleep(0.2)
    raise TimeoutError("Human OS services did not become ready within 20 seconds")


def _stop(processes: list[subprocess.Popen[bytes]]) -> None:
    for process in reversed(processes):
        if process.poll() is None:
            process.terminate()
    deadline = time.monotonic() + 5
    for process in reversed(processes):
        remaining = max(0.0, deadline - time.monotonic())
        try:
            process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)


def supervise(
    database: Path,
    bridge_env: Path,
    tool_env: Path,
    mobile_env: Path,
    private_dir: Path,
) -> None:
    for path, label in (
        (database, "database"), (bridge_env, "bridge env"), (tool_env, "tool env"),
        (mobile_env, "mobile env")
    ):
        if not path.is_file():
            raise FileNotFoundError(f"{label} does not exist: {path}")
    private_dir.mkdir(parents=True, exist_ok=True)
    state_path = private_dir / "windows-runtime-state.json"
    if state_path.exists():
        if any(_port_ready(port) for port in (8765, 8787, 8899, 8990)):
            raise RuntimeError(
                f"runtime state already exists: {state_path}; a prior runtime appears active"
            )
        state_path.unlink()
    commands = (
        (
            "local-api",
            [sys.executable, "-m", "human_os.search_api", "--db", str(database),
             "--host", "127.0.0.1", "--port", "8765"],
        ),
        (
            "bridge",
            [sys.executable, "-m", "human_os.bridge", "--env-file", str(bridge_env)],
        ),
        (
            "tool-api",
            [sys.executable, "-m", "human_os.tool_api", "--bridge-env-file",
             str(bridge_env), "--env-file", str(tool_env)],
        ),
        (
            "mobile-web",
            [sys.executable, "-m", "human_os.mobile_web", "--tool-env-file",
             str(tool_env), "--env-file", str(mobile_env)],
        ),
    )
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    processes: list[subprocess.Popen[bytes]] = []
    streams: list[BinaryIO] = []
    try:
        for name, command in commands:
            stdout = (private_dir / f"{name}.stdout.log").open("ab", buffering=0)
            stderr = (private_dir / f"{name}.stderr.log").open("ab", buffering=0)
            streams.extend((stdout, stderr))
            processes.append(subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                creationflags=flags,
                close_fds=True,
            ))
        state = {
            "started_at": datetime.now(timezone.utc).isoformat(),
            "supervisor_pid": os.getpid(),
            "services": {
                name: {"pid": process.pid, "bind": f"127.0.0.1:{port}"}
                for (name, _), process, port in zip(commands, processes, (8765, 8787, 8899, 8990))
            },
        }
        state_path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
        _wait_ready(processes, (8765, 8787, 8899, 8990))
        print("Human OS read-only runtime ready on loopback ports 8765, 8787, 8899, 8990")
        while True:
            failed = next((process for process in processes if process.poll() is not None), None)
            if failed is not None:
                raise RuntimeError(f"Human OS child process stopped unexpectedly: {failed.pid}")
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        _stop(processes)
        state_path.unlink(missing_ok=True)
        for stream in streams:
            stream.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("private/human_os.db"))
    parser.add_argument("--bridge-env", type=Path, default=Path("private/bridge.env"))
    parser.add_argument("--tool-env", type=Path, default=Path("private/tool.env"))
    parser.add_argument("--mobile-env", type=Path, default=Path("private/mobile.env"))
    parser.add_argument("--private-dir", type=Path, default=Path("private/runtime"))
    args = parser.parse_args()
    supervise(args.db, args.bridge_env, args.tool_env, args.mobile_env, args.private_dir)


if __name__ == "__main__":
    main()
