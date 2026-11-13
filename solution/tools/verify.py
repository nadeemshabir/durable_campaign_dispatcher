"""Check a run the way the graders do.

    python -m solution.tools.verify [--provider http://127.0.0.1:8099] [--csv final_state.csv]

Prints the provider's /admin/report summary and diffs final_state.csv against
/admin/leads. Exit code 0 only if there are no violations and every lead matches.
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

import httpx

FIELDS = ("final_state", "attempts", "last_status", "last_disposition")


def compare(ours: list[dict], provider_leads: list[dict]) -> list[str]:
    mine = {(r["campaign_id"], r["lead_id"]): r for r in ours}
    problems: list[str] = []
    for t in provider_leads:
        key = (t["campaign_id"], t["lead_id"])
        expected = {"final_state": t["expected_state"], "attempts": str(t["attempts"]),
                    "last_status": t["last_status"] or "", "last_disposition": t["last_disposition"] or ""}
        m = mine.pop(key, None)
        if m is None:
            problems.append(f"{key[0]}/{key[1]}: missing from CSV (provider expects {expected})")
            continue
        diffs = [f"{f}: ours={m.get(f)!r} provider={expected[f]!r}" for f in FIELDS if m.get(f) != expected[f]]
        if diffs:
            problems.append(f"{key[0]}/{key[1]}: " + "; ".join(diffs))
    problems += [f"{k[0]}/{k[1]}: in CSV but unknown to the provider" for k in mine]
    return problems


def verify(provider_url: str, csv_path: str | Path) -> tuple[bool, str]:
    with httpx.Client(base_url=provider_url, timeout=30) as http:
        report = http.get("/admin/report").json()
        provider_leads = http.get("/admin/leads").json()
    with open(csv_path, newline="") as f:
        rows = list(csv.DictReader(f))
    keys = [(r["campaign_id"], r["lead_id"]) for r in rows]
    problems = compare(rows, provider_leads)
    if keys != sorted(keys):
        problems.insert(0, "CSV is not sorted by campaign_id, lead_id")
    incomplete = sum(1 for t in provider_leads if t["expected_state"] == "incomplete")

    out = [
        f"sim_now               {report['sim_now']}",
        f"calls_total           {report['calls_total']}  (active {report['calls_active']})",
        f"calls_by_status       {report['calls_by_status']}",
        f"violation_count       {report['violation_count']}  {report['violations_by_type'] or ''}",
        f"max_concurrency_seen  {report['max_concurrency_seen']}",
        f"callback_lateness_min {report['callback_lateness_minutes']}",
        f"webhooks              {report['webhooks']}",
        f"leads                 {len(provider_leads)} at provider, {len(rows)} in CSV, {incomplete} incomplete",
        f"csv mismatches        {len(problems)}",
    ]
    out += [f"  - {p}" for p in problems[:20]]
    if len(problems) > 20:
        out.append(f"  ... and {len(problems) - 20} more")
    for v in report["violations"][:10]:
        out.append(f"  violation: {v}")
    ok = report["violation_count"] == 0 and not problems and incomplete == 0
    out.append("RESULT                " + ("PASS" if ok else "FAIL"))
    return ok, "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m solution.tools.verify")
    p.add_argument("--provider", default="http://127.0.0.1:8099")
    p.add_argument("--csv", default="final_state.csv")
    a = p.parse_args(argv)
    ok, text = verify(a.provider, a.csv)
    print(text)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
