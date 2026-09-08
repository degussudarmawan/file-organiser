"""The move plan: the single object the manager produces and the UI renders.

A plan is built by a scan and is inert — building one never touches the target
folder. Applying one is a separate, explicit step that takes a plan_id.
"""

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .paths import plans_dir


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class PlanItem:
    """One file and what the system decided to do with it."""

    item_id: str
    filename: str
    source: str
    size_bytes: int
    action: str  # "move" | "skip"
    category: str | None
    destination: str | None
    decided_by: str  # "rules" | "classifier" | "none"
    confidence: str  # "high" | "medium" | "low" | "n/a"
    reason: str
    needs_review: bool = False

    @staticmethod
    def new_id() -> str:
        return uuid.uuid4().hex[:12]

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "PlanItem":
        return cls(**data)


@dataclass
class Plan:
    plan_id: str
    target_dir: str
    created_at: str
    items: list[PlanItem] = field(default_factory=list)
    classifier_available: bool = False
    classifier_detail: str = ""
    is_first_run: bool = True
    applied_at: str | None = None
    # The vocabulary the scheme proposer invented for this run, as
    # [{"name", "description", "is_new"}], plus its one-line rationale. Empty
    # when nothing needed delegating or no scheme could be proposed.
    proposed_categories: list[dict] = field(default_factory=list)
    scheme_reason: str = ""
    # False when the run skipped the rule table and sent everything to the
    # agents. Recorded so the plan explains why an ordinary .jpg was decided
    # by the classifier rather than by a table lookup.
    used_rules: bool = True

    @classmethod
    def create(cls, target_dir: str) -> "Plan":
        return cls(
            plan_id=uuid.uuid4().hex[:12],
            target_dir=str(target_dir),
            created_at=_now(),
        )

    # --- views the UI and CLI both want -------------------------------------

    @property
    def moves(self) -> list[PlanItem]:
        return [i for i in self.items if i.action == "move"]

    @property
    def skips(self) -> list[PlanItem]:
        return [i for i in self.items if i.action == "skip"]

    @property
    def review_items(self) -> list[PlanItem]:
        """Delegated decisions the classifier wasn't confident about. These are
        surfaced separately so they can be eyeballed before approval."""
        return [i for i in self.moves if i.needs_review]

    def grouped_by_category(self) -> dict[str, list[PlanItem]]:
        grouped: dict[str, list[PlanItem]] = {}
        for item in self.moves:
            grouped.setdefault(item.category or "Other", []).append(item)
        return dict(sorted(grouped.items()))

    def item(self, item_id: str) -> PlanItem | None:
        return next((i for i in self.items if i.item_id == item_id), None)

    def summary(self) -> dict:
        by_agent: dict[str, int] = {}
        for item in self.moves:
            by_agent[item.decided_by] = by_agent.get(item.decided_by, 0) + 1
        return {
            "total_files": len(self.items),
            "to_move": len(self.moves),
            "to_skip": len(self.skips),
            "needs_review": len(self.review_items),
            "by_agent": by_agent,
            "categories": len(self.grouped_by_category()),
        }

    # --- serialisation ------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "plan_id": self.plan_id,
            "target_dir": self.target_dir,
            "created_at": self.created_at,
            "classifier_available": self.classifier_available,
            "classifier_detail": self.classifier_detail,
            "is_first_run": self.is_first_run,
            "applied_at": self.applied_at,
            "proposed_categories": self.proposed_categories,
            "scheme_reason": self.scheme_reason,
            "used_rules": self.used_rules,
            "summary": self.summary(),
            "items": [i.to_dict() for i in self.items],
            "grouped": {
                category: [i.item_id for i in items]
                for category, items in self.grouped_by_category().items()
            },
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Plan":
        return cls(
            plan_id=data["plan_id"],
            target_dir=data["target_dir"],
            created_at=data["created_at"],
            items=[PlanItem.from_dict(i) for i in data.get("items", [])],
            classifier_available=data.get("classifier_available", False),
            classifier_detail=data.get("classifier_detail", ""),
            is_first_run=data.get("is_first_run", True),
            applied_at=data.get("applied_at"),
            proposed_categories=data.get("proposed_categories", []),
            scheme_reason=data.get("scheme_reason", ""),
            used_rules=data.get("used_rules", True),
        )


class PlanStore:
    """Plans persist to disk so /apply can execute a plan built by an earlier
    /scan — including across a server restart."""

    def __init__(self, directory: Path | None = None):
        self._dir = Path(directory) if directory else plans_dir()
        self._dir.mkdir(parents=True, exist_ok=True)

    def _path(self, plan_id: str) -> Path:
        if not plan_id.isalnum():
            raise ValueError(f"bad plan id: {plan_id!r}")
        return self._dir / f"{plan_id}.json"

    def save(self, plan: Plan) -> None:
        payload = plan.to_dict()
        self._path(plan.plan_id).write_text(json.dumps(payload, indent=2))

    def load(self, plan_id: str) -> Plan | None:
        path = self._path(plan_id)
        if not path.exists():
            return None
        return Plan.from_dict(json.loads(path.read_text()))

    def list_recent(self, limit: int = 20) -> list[dict]:
        files = sorted(
            self._dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True
        )
        out = []
        for path in files[:limit]:
            try:
                data = json.loads(path.read_text())
            except (json.JSONDecodeError, OSError):
                continue
            out.append(
                {
                    "plan_id": data.get("plan_id"),
                    "target_dir": data.get("target_dir"),
                    "created_at": data.get("created_at"),
                    "applied_at": data.get("applied_at"),
                    "summary": data.get("summary", {}),
                }
            )
        return out

    def mark_applied(self, plan: Plan) -> None:
        plan.applied_at = _now()
        self.save(plan)
