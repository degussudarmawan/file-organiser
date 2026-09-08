"""The undo log and the first-run acknowledgement ledger.

Every real move is appended here as one JSON object per line, with enough
detail to reverse it by hand (or with `cli.py undo --dry-run`). Nothing is
ever deleted from this file by the app.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

from .paths import state_dir

LOG_NAME = "undo-log.jsonl"
ACK_NAME = "acknowledged-targets.json"


def undo_log_path() -> Path:
    return state_dir() / LOG_NAME


def _ack_path() -> Path:
    return state_dir() / ACK_NAME


def record_move(
    *,
    plan_id: str,
    item_id: str,
    source: str,
    destination: str,
    category: str,
    decided_by: str,
    confidence: str,
) -> dict:
    """Append one move. Returns the entry that was written."""
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "plan_id": plan_id,
        "item_id": item_id,
        "source": source,
        "destination": destination,
        "category": category,
        "decided_by": decided_by,
        "confidence": confidence,
    }
    with undo_log_path().open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry) + "\n")
    return entry


def read_log(limit: int = 200, plan_id: str | None = None) -> list[dict]:
    """Most recent entries first."""
    path = undo_log_path()
    if not path.exists():
        return []

    entries = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if plan_id and entry.get("plan_id") != plan_id:
                continue
            entries.append(entry)

    entries.reverse()
    return entries[:limit]


# --- first-run confirmation -------------------------------------------------


def _load_acks() -> list[str]:
    path = _ack_path()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return []
    return data if isinstance(data, list) else []


def is_first_run(target_dir: str) -> bool:
    """True until this folder has been explicitly confirmed once."""
    return str(target_dir) not in _load_acks()


def acknowledge_target(target_dir: str) -> None:
    """Record that the user has explicitly confirmed applying to this folder.
    Only called after a real confirmation, never implicitly."""
    acks = _load_acks()
    target = str(target_dir)
    if target not in acks:
        acks.append(target)
        _ack_path().write_text(json.dumps(acks, indent=2))


def acknowledged_targets() -> list[str]:
    return _load_acks()
