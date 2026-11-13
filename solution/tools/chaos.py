"""Run the dispatcher under kill -9, the way the graders will, then verify the result.

    # against a mock you already started:
    python -m solution.tools.chaos --kills 5

    # or let this script start (and stop) its own mock, on its own ports:
    python -m solution.tools.chaos --start-mock --mock-seed 4242 --mock-port 8199 --port 8299 --kills 6

The dispatcher is started with a fresh state file. After a random 0.5-12 real seconds it is
SIGKILLed (no cleanup handlers run) and started again with the identical command, until
--kills is reached; the last run is left to finish on its own. Then the verifier compares
against the provider.
"""
from __future__ import annotations

import argparse
import random
import subprocess
import sys
import time
from pathlib import Path

import httpx

from .verify import verify

ROOT = Path(__file__).resolve().parents[2]


def provider_answers(url: str) -> bool:
    try:
        return httpx.get(f"{url}/v1/clock", timeout=1).is_success
    except httpx.HTTPError:
        return False


def wait_for_provider(url: str, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if provider_answers(url):
            return
        time.sleep(0.2)
    raise SystemExit(f"provider at {url} did not come up")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m solution.tools.chaos")
    p.add_argument("--kills", type=int, default=5, help="how many times to kill -9 the dispatcher")
    p.add_argument("--min-gap", type=float, default=0.5,
                   help="min real seconds between start and kill (low enough to hit startup too)")
    p.add_argument("--max-gap", type=float, default=12.0, help="max real seconds between start and kill")
    p.add_argument("--rng-seed", type=int, default=None, help="seed for kill timing (default: random)")
    p.add_argument("--start-mock", action="store_true", help="start a mock provider for this run")
    p.add_argument("--mock-seed", type=int, default=7)
    p.add_argument("--speed", type=float, default=600.0)
    p.add_argument("--failure-scale", type=float, default=1.0)
    p.add_argument("--mock-port", type=int, default=8099)
    p.add_argument("--port", type=int, default=8200, help="dispatcher webhook port")
    p.add_argument("--workdir", type=Path, default=ROOT / "solution" / "runs",
                   help="where DB, CSV and logs go (gitignored)")
    p.add_argument("--timeout", type=float, default=1800.0, help="give up after this many real seconds")
    a = p.parse_args(argv)

    rng = random.Random(a.rng_seed)
    provider = f"http://127.0.0.1:{a.mock_port}"
    if a.start_mock and provider_answers(provider):
        # Our mock could not bind the port, and we would silently drive someone else's mock
        # (dialling leads it has already finished: a pile of violations on THEIR report).
        raise SystemExit(f"a provider is already running at {provider}; stop it or choose another --mock-port")
    tag = f"seed{a.mock_seed}-port{a.port}" if a.start_mock else f"port{a.port}"
    a.workdir.mkdir(parents=True, exist_ok=True)
    db, out, log_path = (a.workdir / f"{tag}.db", a.workdir / f"{tag}.csv", a.workdir / f"{tag}.log")
    log = open(log_path, "w")
    mock = None
    if a.start_mock:
        mock = subprocess.Popen(
            [sys.executable, "-m", "mock_telephony", "--seed", str(a.mock_seed), "--speed", str(a.speed),
             "--failure-scale", str(a.failure_scale), "--port", str(a.mock_port)],
            cwd=ROOT / "assignment-b-workflow", stdout=log, stderr=subprocess.STDOUT)
    try:
        wait_for_provider(provider)
        # Every start and restart uses the exact same command, as a grader would.
        cmd = [sys.executable, "-m", "solution.dispatcher", "--provider", provider,
               "--port", str(a.port), "--db", str(db), "--out", str(out)]
        for path in (out, db, Path(f"{db}-wal"), Path(f"{db}-shm")):
            path.unlink(missing_ok=True)  # a new experiment
        started, kills = time.monotonic(), 0
        while True:
            proc = subprocess.Popen(cmd, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
            if kills < a.kills:
                gap = rng.uniform(a.min_gap, a.max_gap)
                try:
                    code = proc.wait(timeout=gap)
                    print(f"dispatcher finished on its own (exit {code}) before kill #{kills + 1}")
                    break
                except subprocess.TimeoutExpired:
                    proc.kill()  # SIGKILL on POSIX: no cleanup, exactly like kill -9
                    proc.wait()
                    kills += 1
                    print(f"kill -9 #{kills} after {gap:.1f}s")
                    log.write(f"\n===== chaos: kill -9 #{kills} =====\n")
                    log.flush()
                    continue
            remaining = a.timeout - (time.monotonic() - started)
            try:
                code = proc.wait(timeout=max(1.0, remaining))
            except subprocess.TimeoutExpired:
                proc.kill()
                print(f"timed out after {a.timeout:.0f}s")
                return 2
            print(f"dispatcher exited with {code}")
            break
        print(f"wall time {time.monotonic() - started:.0f}s, kills {kills}, log {log_path}")
        ok, text = verify(provider, out)
        print(text)
        return 0 if ok else 1
    finally:
        if mock is not None:
            mock.terminate()
            mock.wait()
        log.close()


if __name__ == "__main__":
    sys.exit(main())
