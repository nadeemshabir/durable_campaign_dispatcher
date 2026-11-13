"""final_state.csv, written atomically so a crash mid-write never leaves half a file."""
from __future__ import annotations

import csv
import os
from pathlib import Path
from typing import Iterable

HEADER = ("campaign_id", "lead_id", "final_state", "attempts", "last_status", "last_disposition")


def write_final_state(rows: Iterable[tuple], path: str | Path) -> Path:
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", newline="") as f:
        writer = csv.writer(f, lineterminator="\n")
        writer.writerow(HEADER)
        writer.writerows(rows)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    return path
