"""The undo log and the first-run acknowledgement ledger.

Every real move is appended here as one JSON object per line, with enough
detail to reverse it by hand (or with `cli.py undo --dry-run`). Nothing is
ever deleted from this file by the app.
"""

import json
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .paths import state_dir, unique_destination

LOG_NAME = "undo-log.jsonl"
ACK_NAME = "acknowledged-targets.json"

# High enough that "undo this plan" never silently misses part of a plan
# because the log has grown. Reading 100k JSON lines costs a fraction of a
# second; reversing half a plan because we stopped reading is unrecoverable.
SCAN_LIMIT = 100_000

# Reversals are themselves moves, so they are logged too — under this prefix,
# so that "the last plan" never resolves to an undo of an undo.
UNDO_PREFIX = "undo:"


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


# --- reversing a plan -------------------------------------------------------
#
# Kept here rather than in whichever interface asked for it. Undo is the one
# operation where a second, slightly-different implementation is genuinely
# dangerous: two copies of "work out where this file came back to" will drift,
# and the failure mode is a file restored on top of another one. The CLI and
# the MCP server both call these.


@dataclass(frozen=True)
class Reversal:
    """One logged move, and where it would be put back."""

    entry: dict
    source: Path  # where the file is now — the original move's destination
    destination: Path  # where it goes back to, renamed if that name is taken

    @property
    def renamed(self) -> bool:
        return self.destination.name != self.source.name


@dataclass
class UndoPlan:
    """What reversing a plan would do. Inert: building one moves nothing."""

    plan_id: str
    reversals: list[Reversal] = field(default_factory=list)
    blocked: list[tuple[dict, str]] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "plan_id": self.plan_id,
            "would_reverse": len(self.reversals),
            "blocked": len(self.blocked),
            "reversals": [
                {
                    "from": str(r.source),
                    "to": str(r.destination),
                    "renamed": r.renamed,
                }
                for r in self.reversals
            ],
            "blocked_detail": [
                {"file": str(e.get("destination") or e.get("source") or "?"),
                 "reason": reason}
                for e, reason in self.blocked
            ],
        }


def latest_plan_id() -> str | None:
    """The most recently applied plan, ignoring reversals.

    Excluding `undo:` entries is what stops "undo the last thing" from
    becoming a redo the second time you call it.
    """
    for entry in read_log(limit=SCAN_LIMIT):
        pid = str(entry.get("plan_id") or "")
        if pid and not pid.startswith(UNDO_PREFIX):
            return pid
    return None


def plan_undo(plan_id: str) -> UndoPlan:
    """Work out how to reverse `plan_id`. Touches nothing on disk."""
    undo = UndoPlan(plan_id=plan_id)
    reserved: set[Path] = set()

    # read_log is newest first, which is the right order to unwind in: a later
    # move can only have depended on an earlier one, never the other way round.
    for entry in read_log(limit=SCAN_LIMIT, plan_id=plan_id):
        raw_destination = str(entry.get("destination") or "")
        raw_source = str(entry.get("source") or "")

        if not raw_destination or not raw_source:
            undo.blocked.append((entry, "log entry is missing a source or destination"))
            continue

        destination = Path(raw_destination)
        source = Path(raw_source)

        if not destination.exists():
            undo.blocked.append((entry, f"{destination} no longer exists"))
            continue
        if source.exists():
            undo.blocked.append(
                (entry, f"{source} is occupied again — refusing to overwrite")
            )
            continue

        # Two logged moves can name the same original path (a file moved,
        # restored by hand, then moved again). Never let the second one land on
        # top of the first.
        final = unique_destination(source, reserved)
        reserved.add(final)
        undo.reversals.append(Reversal(entry=entry, source=destination, destination=final))

    return undo


def execute_undo(undo: UndoPlan) -> dict:
    """Carry out a reversal. The only function here that writes to disk.

    Each reversal is itself logged, so undoing is as auditable as doing.
    """
    reversed_count = 0
    failures: list[dict] = []

    for reversal in undo.reversals:
        try:
            reversal.destination.parent.mkdir(parents=True, exist_ok=True)
            # `destination` was proven free in plan_undo, so this never clobbers.
            shutil.move(str(reversal.source), str(reversal.destination))
        except OSError as exc:
            failures.append({"file": str(reversal.source), "error": str(exc)})
            continue

        reversed_count += 1
        entry = reversal.entry
        record_move(
            plan_id=f"{UNDO_PREFIX}{undo.plan_id}",
            item_id=str(entry.get("item_id", "")),
            source=str(reversal.source),
            destination=str(reversal.destination),
            category=str(entry.get("category", "")),
            decided_by="undo",
            confidence="n/a",
        )

    return {
        "plan_id": undo.plan_id,
        "reversed": reversed_count,
        "blocked": len(undo.blocked),
        "failures": failures,
        "logged_under": f"{UNDO_PREFIX}{undo.plan_id}",
    }


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
