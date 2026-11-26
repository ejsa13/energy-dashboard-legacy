#!/usr/bin/env python3
"""Exercise local stack recovery. Stops only the energy-dashboard Compose project."""
import json
from pathlib import Path
import subprocess
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]


def compose(*args: str) -> str:
    return subprocess.check_output(["docker", "compose", *args], cwd=ROOT, text=True).strip()


def get(path: str, port: int = 8000):
    try:
        with urllib.request.urlopen(f"http://localhost:{port}{path}", timeout=12) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        try:
            payload = json.load(error)
        except json.JSONDecodeError:
            payload = {}
        return error.code, payload


def wait_for(predicate, description: str, timeout: int = 60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if predicate():
                print(f"PASS {description}", flush=True)
                return
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            pass
        time.sleep(2)
    raise AssertionError(description)


def heartbeat() -> str:
    return compose("exec", "-T", "db", "psql", "-U", "energy", "-d", "energy", "-Atc",
                   "SELECT last_heartbeat FROM collector_heartbeat WHERE name = 'primary'")


def check_sse():
    with urllib.request.urlopen("http://localhost:5173/api/events", timeout=12) as response:
        assert response.headers.get("X-Accel-Buffering") == "no"
        assert response.headers.get("Content-Type", "").startswith("text/event-stream")
        count = 0
        while count < 2:
            line = response.readline().decode()
            if line.startswith("data: "):
                data = json.loads(line[6:])
                assert data["providers"]["ecoflow"]["state"] == "not_connected"
                assert data["providers"]["octopus_mini"]["state"] == "not_connected"
                assert data["providers"]["octopus"]["state"] in {
                    "not_connected", "backfilling", "syncing", "healthy", "failed"
                }
                count += 1
    print("PASS two SSE snapshots through Vite", flush=True)


def main():
    try:
        wait_for(lambda: get("/api/health/ready")[0] == 200, "initial readiness")
        compose("exec", "-T", "api", "alembic", "upgrade", "head")
        compose("exec", "-T", "api", "alembic", "upgrade", "head")
        print("PASS repeated migrations", flush=True)
        check_sse()
        compose("stop", "db")
        assert get("/api/health/live")[0] == 200
        assert get("/api/health/ready")[0] == 503
        assert get("/api/status")[1]["collector"]["state"] == "unknown"
        print("PASS database outage: live=200, ready=503, collector unknown", flush=True)
        compose("start", "db")
        wait_for(lambda: get("/api/health/ready")[0] == 200, "database readiness recovery")
        wait_for(lambda: get("/api/status")[1]["collector"]["state"] == "healthy", "collector database recovery")
        compose("stop", "collector")
        before = heartbeat()
        assert before
        wait_for(lambda: get("/api/status")[1]["collector"]["state"] == "stale", "collector becomes stale", 45)
        assert heartbeat() == before
        compose("down")
        compose("up", "-d", "--wait", "api", "frontend")
        assert heartbeat() == before
        print("PASS database heartbeat persists across down/up", flush=True)
        compose("up", "-d", "collector")
        wait_for(lambda: get("/api/status")[1]["collector"]["state"] == "healthy", "collector restart recovery")
        compose("restart", "api")
        wait_for(lambda: get("/api/health/ready", 5173)[0] == 200, "API recovery through Vite")
        check_sse()
    finally:
        compose("up", "-d", "--wait")


if __name__ == "__main__":
    main()
