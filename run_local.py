from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env", override=False)


def start(cmd: list[str], env: dict | None = None) -> subprocess.Popen:
    return subprocess.Popen(cmd, cwd=ROOT, env=env)


def port_from_url(url: str, default: int = 8001) -> int:
    return urlparse(url).port or default


def wait_for_api(url: str, proc: subprocess.Popen, timeout_seconds: float = 10.0, name: str = "Vendor-risk API") -> None:
    """Wait until a local service is reachable or fail with a useful message."""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(
                f"{name} exited during startup with code {proc.returncode}. "
                "Check the terminal output above (a port conflict is a common cause)."
            )
        try:
            response = requests.get(url, timeout=0.5)
            if response.ok:
                return
        except requests.RequestException:
            pass
        time.sleep(0.25)
    raise RuntimeError(f"{name} did not become ready within {timeout_seconds:.0f}s: {url}")


def start_mock_api(port: int, env: dict | None = None, quiet: bool = False) -> subprocess.Popen:
    """Start the mock vendor-risk API on 127.0.0.1:<port> and wait for /health."""
    cmd = [sys.executable, "-m", "uvicorn", "mock_api.app:app", "--host", "127.0.0.1", "--port", str(port)]
    if quiet:
        cmd += ["--log-level", "warning"]
    proc = start(cmd, env=env)
    wait_for_api(f"http://127.0.0.1:{port}/health", proc)
    return proc


def stop_processes(procs: list[subprocess.Popen]) -> None:
    for proc in procs:
        if proc.poll() is None:
            proc.terminate()
    for proc in procs:
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def _handle_termination(signum: int, frame: object) -> None:
    """Route SIGTERM through normal cleanup (useful for IDE/terminal stop actions)."""
    raise KeyboardInterrupt


def main() -> None:
    signal.signal(signal.SIGTERM, _handle_termination)
    api_url = os.getenv("VENDOR_RISK_BASE_URL", "http://127.0.0.1:8001").rstrip("/")
    api_port = port_from_url(api_url)
    app_port = int(os.getenv("APP_PORT", "8000") or 8000)
    procs: list[subprocess.Popen] = []
    try:
        print(f"Starting vendor-risk API on http://127.0.0.1:{api_port} ...")
        procs.append(start_mock_api(api_port))
        print("Vendor-risk API is ready.")

        print(f"Starting web app on http://127.0.0.1:{app_port} ...")
        app_proc = start(
            [sys.executable, "-m", "uvicorn", "webapp.main:app", "--host", "127.0.0.1", "--port", str(app_port)]
        )
        procs.append(app_proc)
        wait_for_api(f"http://127.0.0.1:{app_port}/api/health", app_proc, timeout_seconds=15, name="Web app")

        print("\nReady:")
        print(f"  Web app:         http://127.0.0.1:{app_port}")
        print(f"  Vendor-risk API: http://127.0.0.1:{api_port}")
        print("Press Ctrl+C to stop.")

        while True:
            time.sleep(1)
            for proc in procs:
                if proc.poll() is not None:
                    raise RuntimeError(f"A local process exited with code {proc.returncode}")
    except KeyboardInterrupt:
        print("\nStopping local services ...")
    finally:
        stop_processes(procs)


if __name__ == "__main__":
    main()
