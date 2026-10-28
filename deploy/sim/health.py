#!/usr/bin/env python3
"""Report this deployment's real container and HTTP health, without secrets."""

import json
from datetime import datetime, timezone
from pathlib import Path
import subprocess
import urllib.request


def database_write_check():
    # Existing migrated table, zero rows, and rollback: no business data changes.
    # This checks transaction admission, not free disk space or future durability.
    sql = ("BEGIN; SET LOCAL statement_timeout = '3s'; "
           "SET LOCAL lock_timeout = '1s'; "
           "UPDATE public.workflow SET id = id WHERE false; ROLLBACK;")
    try:
        probe = subprocess.run(
            ['docker', 'exec', '-e', 'PGCONNECT_TIMEOUT=3', 'agent-line-db-1',
             'psql', '-X', '-w', '-U', 'agent_line', '-d', 'agent_line_sim',
             '-v', 'ON_ERROR_STOP=1', '-At', '-c', sql],
            capture_output=True, text=True, timeout=10,
        )
        passed = probe.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        passed = False
    return {'service': 'database_write', 'expected': 'transaction_admitted',
            'actual': 'transaction_admitted' if passed else 'unavailable', 'passed': passed}


def main():
    result = {"checked_at": datetime.now(timezone.utc).isoformat(), "checks": [], "healthy": False}
    expected = {
        "db": "healthy", "redis": "healthy", "realtime": "healthy",
        "simstudio": "healthy", "cron": "running",
        "migrations": "completed", "uploads-init": "completed",
    }
    for service, target in expected.items():
        name = f"agent-line-{service}-1"
        process = subprocess.run(
            ["docker", "inspect", "--format", "{{json .State}}", name],
            capture_output=True, text=True,
        )
        state = json.loads(process.stdout) if process.returncode == 0 else {}
        status = state.get("Health", {}).get("Status", state.get("Status", "missing"))
        if status == "exited" and state.get("ExitCode") == 0:
            status = "completed"
        result["checks"].append({"service": service, "expected": target, "actual": status, "passed": status == target})
    result['checks'].append(database_write_check())
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for url in ("http://127.0.0.1:3190", "http://127.0.0.1:3192/health"):
        try:
            with opener.open(url, timeout=10) as response:
                result["checks"].append({"url": url, "http_status": response.status, "passed": response.status == 200})
        except Exception as error:
            result["checks"].append({"url": url, "error": str(error), "passed": False})
    result["healthy"] = all(check["passed"] for check in result["checks"])
    destination = Path(__file__).resolve().parents[2] / "var/sim/health.json"
    destination.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result["healthy"] else 1)


if __name__ == "__main__":
    main()
