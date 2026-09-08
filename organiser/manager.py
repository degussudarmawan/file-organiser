"""The manager agent: the only thing in the system allowed to move a file.

The manager owns the workflow — scan, decide, plan, apply — and owns every
filesystem write. It runs the cheap rule pass itself and delegates only the
files the rules cannot place, talking to the delegate through the registry and
the `Delegate` protocol so it never depends on a concrete agent.

Two hard splits are load-bearing here:

* decision vs. action — a delegate returns a category and never sees a path it
  could write to; the manager extracts the content snippet and performs the
  move.
* scan vs. apply — `scan()` is inert and produces a plan, `apply()` is the
  explicit second step that executes one. Nothing moves without a plan_id.

When the delegate cannot run, unmatched files are left exactly where they are.
The manager never guesses a category and never deletes anything.
"""

import shutil
from dataclasses import dataclass, field
from pathlib import Path

from . import delegates, undo_log
from .categories import FALLBACK_CATEGORY, dedupe_categories, normalise_category
from .delegates import (
    DelegateRequest,
    DelegateUnavailable,
    FileSummary,
    SchemeRequest,
)
from .paths import (
    UnsafePath,
    is_direct_child,
    should_skip,
    unique_destination,
    validate_target_dir,
)
from .plan import Plan, PlanItem, PlanStore
from .rules import classify_by_rules

# Importing the classifier module is what puts it in the delegate registry.
# It is imported for that side effect only — the manager resolves it by name
# and talks to it through the `Delegate` protocol, never through this symbol.
# A failure here (missing SDK, bad install) must degrade to "no delegate",
# not take the whole manager down with it.
_DELEGATE_IMPORT_ERROR: str | None = None
try:
    from . import classifier as _classifier  # noqa: F401
    from . import scheme as _scheme  # noqa: F401
except Exception as exc:  # noqa: BLE001 - reported via delegate_status()
    _DELEGATE_IMPORT_ERROR = f"delegate module could not be imported: {exc}"

SNIPPET_BYTES = 4000


class FirstRunConfirmationRequired(Exception):
    """Raised by apply() the first time a target folder is used.

    Carries `.target_dir` so the caller can name the folder in the
    confirmation it puts in front of the user.
    """

    def __init__(self, target_dir: str):
        super().__init__(
            f"first apply to {target_dir} needs an explicit confirmation"
        )
        self.target_dir = str(target_dir)


@dataclass
class ApplyResult:
    """The outcome of one apply(): what moved, what was left, what broke."""

    plan_id: str
    target_dir: str
    moved: list[dict] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)
    failed: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "plan_id": self.plan_id,
            "target_dir": self.target_dir,
            "moved": self.moved,
            "skipped": self.skipped,
            "failed": self.failed,
            "counts": {
                "moved": len(self.moved),
                "skipped": len(self.skipped),
                "failed": len(self.failed),
            },
        }


class Manager:
    """Orchestrates a run. Stateless between calls apart from its store."""

    def __init__(
        self,
        delegate_name: str = "classifier",
        store: PlanStore | None = None,
        scheme_name: str = "scheme",
    ) -> None:
        self._delegate_name = delegate_name
        self._scheme_name = scheme_name
        self._store = store or PlanStore()

    # --- delegate plumbing --------------------------------------------------

    def delegate_status(self) -> tuple[bool, str]:
        """(available, detail) for the delegate this manager would use."""
        delegate = delegates.get(self._delegate_name)
        if delegate is None:
            if _DELEGATE_IMPORT_ERROR:
                return False, _DELEGATE_IMPORT_ERROR
            known = ", ".join(delegates.registered_names()) or "none"
            return (
                False,
                f"no delegate registered as {self._delegate_name!r} (registered: {known})",
            )
        try:
            return delegate.available()
        except Exception as exc:  # noqa: BLE001 - a broken probe is unavailable
            return False, f"delegate {self._delegate_name!r} failed its check: {exc}"

    @staticmethod
    def _read_snippet(path: Path) -> tuple[str | None, bool]:
        """Return (snippet, is_binary) for the head of `path`.

        The manager reads the bytes so the delegate never has to touch the
        filesystem. Anything with a NUL byte or that won't decode as UTF-8 is
        reported as binary rather than shipped as mojibake.
        """
        try:
            with path.open("rb") as handle:
                raw = handle.read(SNIPPET_BYTES)
        except OSError:
            return None, False

        if b"\x00" in raw:
            return None, True
        try:
            return raw.decode("utf-8"), False
        except UnicodeDecodeError:
            return None, True

    @staticmethod
    def _size_of(path: Path) -> int:
        try:
            return path.stat().st_size
        except OSError:
            return 0

    # --- scan ---------------------------------------------------------------

    def scan(
        self,
        target_dir: str,
        max_delegations: int | None = None,
        use_rules: bool = True,
    ) -> Plan:
        """Build a move plan for `target_dir`. Moves nothing, ever.

        Three passes, in order of cost:
        1. the rule table, run here in-process for the easy cases;
        2. one corpus-level call to the scheme proposer, which reads every
           file the rules could not place and proposes this run's categories;
        3. the classifier, per file, restricted to the categories from (2).

        Splitting (2) from (3) is what keeps the vocabulary coherent. Asking
        the classifier to name a category per file in isolation is what
        produces Invoices/, Bills/ and Receipts/ for one concept.

        `use_rules=False` skips pass 1 entirely and sends every file to the
        agents. The rule table groups by file *type* (a folder of 40 PDFs is
        one undifferentiated Documents/), while the agents group by what the
        files are *about*. That is worth paying for sometimes — but it is one
        model call per file instead of only the leftovers, so it is opt-in.
        """
        target = validate_target_dir(target_dir)

        plan = Plan.create(str(target))
        available, detail = self.delegate_status()
        plan.classifier_available = available
        plan.classifier_detail = detail
        delegate = delegates.get(self._delegate_name) if available else None

        entries = [
            entry
            for entry in sorted(target.iterdir(), key=lambda p: p.name.lower())
            # Entries we don't manage (dotfiles, folders, symlinks) are simply
            # not part of the plan.
            if should_skip(entry) is None
        ]

        # --- pass 1: the rule table ----------------------------------------
        plan.used_rules = use_rules
        items: dict[str, PlanItem] = {}
        unplaced: list[Path] = []
        for entry in entries:
            # With rules off, every file counts as unplaced and goes to the
            # agents — the rule table is never consulted at all.
            match = classify_by_rules(entry.name) if use_rules else None
            if match is None:
                unplaced.append(entry)
                continue
            items[entry.name] = PlanItem(
                item_id=PlanItem.new_id(),
                filename=entry.name,
                source=str(entry),
                size_bytes=self._size_of(entry),
                action="move",
                category=match.category,
                destination=None,
                decided_by="rules",
                confidence="high",
                reason=match.reason,
                needs_review=False,
            )

        # Snippets are read once here and reused for both remaining passes, so
        # a file is never opened twice in a run.
        summaries: dict[str, FileSummary] = {}
        for entry in unplaced:
            snippet, is_binary = self._read_snippet(entry)
            summaries[entry.name] = FileSummary(
                filename=entry.name,
                snippet=snippet,
                snippet_is_binary=is_binary,
                size_bytes=self._size_of(entry),
            )

        # --- pass 2: propose this run's vocabulary --------------------------
        allowed: list[str] = []
        blocked_reason: str | None = None
        if unplaced:
            if delegate is None:
                blocked_reason = f"classifier unavailable ({detail}); left in place"
            else:
                allowed, proposal, blocked_reason = self._propose_scheme(
                    target,
                    [summaries[e.name] for e in unplaced],
                    {i.category for i in items.values() if i.category},
                )
                if proposal is not None:
                    plan.scheme_reason = proposal.reason
                    # Names are normalised here too, not just in the proposer:
                    # the manager does not trust a delegate's output, and what
                    # the plan displays must match the folder that gets made.
                    plan.proposed_categories = [
                        {
                            "name": safe,
                            "description": c.description,
                            "is_new": c.is_new,
                        }
                        for c, safe in (
                            (c, normalise_category(c.name)) for c in proposal.categories
                        )
                        if safe is not None
                    ]

        # --- pass 3: assign the unplaced files ------------------------------
        delegations_used = 0
        # Set once a delegate fails mid-run: the same auth/network fault will
        # hit every remaining call, so we stop asking and skip the rest.
        halted_reason: str | None = None

        for entry in unplaced:
            size = summaries[entry.name].size_bytes

            if blocked_reason is not None:
                items[entry.name] = self._left_alone(entry, size, blocked_reason)
                continue
            if halted_reason is not None:
                items[entry.name] = self._left_alone(entry, size, halted_reason)
                continue
            if max_delegations is not None and delegations_used >= max_delegations:
                items[entry.name] = self._left_alone(
                    entry,
                    size,
                    f"delegation cap of {max_delegations} reached this run; left in place",
                )
                continue

            summary = summaries[entry.name]
            request = DelegateRequest(
                filename=summary.filename,
                snippet=summary.snippet,
                snippet_is_binary=summary.snippet_is_binary,
                size_bytes=summary.size_bytes,
                allowed_categories=list(allowed),
            )
            delegations_used += 1
            try:
                decision = delegate.decide(request)
            except DelegateUnavailable as exc:
                halted_reason = (
                    f"classifier became unavailable ({exc}); left in place and "
                    "no further files were delegated this run"
                )
                items[entry.name] = self._left_alone(entry, size, halted_reason)
            except Exception as exc:  # noqa: BLE001 - one bad file, not a bad run
                items[entry.name] = self._left_alone(
                    entry, size, f"classifier errored on this file ({exc}); left in place"
                )
            else:
                if decision.category is None:
                    # An explicit "I don't know". Distinct from the delegate
                    # being broken, and it must not be turned into a guess: a
                    # delegate declining to place a file is the system working,
                    # so the file stays put and the delegate's own words say
                    # why.
                    items[entry.name] = self._left_alone(
                        entry,
                        size,
                        decision.reason
                        or f"{decision.decided_by or self._delegate_name} had no "
                        "opinion; left in place",
                    )
                    continue

                category = decision.category
                confidence = decision.confidence
                reason = decision.reason
                # The manager handed out the vocabulary, so the manager is the
                # authority on it — not the delegate. A category outside
                # `allowed` becomes the fallback and is flagged for review,
                # rather than quietly creating a folder nobody proposed. The
                # real classifier coerces this too; enforcing it here as well
                # means a third-party delegate cannot widen the scheme.
                if category not in allowed:
                    category = FALLBACK_CATEGORY
                    confidence = "low"
                    reason = (
                        f"{decision.decided_by or self._delegate_name} returned "
                        f"{decision.category!r}, which was not one of this run's "
                        f"categories; filed as {FALLBACK_CATEGORY}"
                    )
                items[entry.name] = PlanItem(
                    item_id=PlanItem.new_id(),
                    filename=entry.name,
                    source=str(entry),
                    size_bytes=size,
                    action="move",
                    category=category,
                    destination=None,
                    decided_by=decision.decided_by or self._delegate_name,
                    confidence=confidence,
                    reason=reason,
                    needs_review=confidence != "high",
                )

        # --- destinations, in the folder's own order ------------------------
        reserved: set[Path] = set()
        for entry in entries:
            item = items[entry.name]
            if item.action == "move" and item.category:
                # Category names now come from a model, so the name is
                # sanitised again here — this is the last point before it
                # becomes a real path.
                safe = normalise_category(item.category)
                if safe is None:
                    item = self._left_alone(
                        entry, item.size_bytes, f"unusable category name {item.category!r}"
                    )
                    items[entry.name] = item
                else:
                    item.category = safe
                    destination = unique_destination(
                        target / safe / item.filename, reserved
                    )
                    reserved.add(destination)
                    item.destination = str(destination)
            plan.items.append(item)

        plan.is_first_run = undo_log.is_first_run(str(target))
        self._store.save(plan)
        return plan

    def _propose_scheme(
        self,
        target: Path,
        summaries: list[FileSummary],
        rule_categories_used: set[str],
    ) -> tuple[list[str], "object | None", str | None]:
        """Ask the scheme proposer for this run's categories.

        Returns (allowed_categories, proposal, blocked_reason). A blocked
        reason means no vocabulary could be produced, so the unplaced files
        are left alone — the manager never falls back to inventing one.
        """
        proposer = delegates.get(self._scheme_name)
        if proposer is None:
            return [], None, (
                f"no scheme proposer registered as {self._scheme_name!r}; "
                "unplaceable files left in place"
            )

        try:
            ok, why = proposer.available()
        except Exception as exc:  # noqa: BLE001 - a broken probe is unavailable
            return [], None, f"scheme proposer failed its check ({exc}); left in place"
        if not ok:
            return [], None, f"scheme proposer unavailable ({why}); left in place"

        # Folders that already exist are the strongest hint about how this
        # person names things, so they are offered for reuse alongside the
        # categories the rule pass is already filling this run.
        existing = set(rule_categories_used)
        try:
            for child in target.iterdir():
                if child.is_dir() and not child.name.startswith("."):
                    existing.add(child.name)
        except OSError:
            pass

        try:
            proposal = proposer.propose(
                SchemeRequest(files=summaries, existing_categories=sorted(existing))
            )
        except DelegateUnavailable as exc:
            return [], None, f"could not propose categories ({exc}); left in place"
        except Exception as exc:  # noqa: BLE001 - a bad proposal is not a crash
            return [], None, f"scheme proposal failed ({exc}); left in place"

        # Sanitised at this boundary rather than trusting the delegate: the
        # allowed list handed to the classifier must only contain names that
        # can legally become folders.
        names = dedupe_categories(proposal.names())
        if not names:
            return [], proposal, "the proposed scheme was empty; left in place"

        # The fallback is appended, never proposed, so the classifier always
        # has somewhere honest to put a file that fits nothing.
        return names + [FALLBACK_CATEGORY], proposal, None

    @staticmethod
    def _left_alone(entry: Path, size: int, reason: str) -> PlanItem:
        """A file we refuse to place. No category is ever guessed here."""
        return PlanItem(
            item_id=PlanItem.new_id(),
            filename=entry.name,
            source=str(entry),
            size_bytes=size,
            action="skip",
            category=None,
            destination=None,
            decided_by="none",
            confidence="n/a",
            reason=reason,
            needs_review=False,
        )

    # --- apply --------------------------------------------------------------

    def apply(
        self,
        plan_id: str,
        approved_ids: list[str] | None = None,
        confirm_first_apply: bool = False,
    ) -> ApplyResult:
        """Execute a saved plan. The only code path that writes to disk."""
        plan = self._store.load(plan_id)
        if plan is None:
            raise ValueError(f"unknown plan id: {plan_id!r}")

        # The disk may have changed since the scan, so nothing from the plan is
        # trusted: the target is re-validated and so is every single item.
        target = validate_target_dir(plan.target_dir)

        first_run = undo_log.is_first_run(plan.target_dir)
        if first_run and not confirm_first_apply:
            raise FirstRunConfirmationRequired(plan.target_dir)

        if approved_ids is None:
            approved = {item.item_id for item in plan.moves}
        else:
            approved = set(approved_ids)

        result = ApplyResult(plan_id=plan.plan_id, target_dir=str(target))
        reserved: set[Path] = set()

        for item in plan.items:
            if item.action != "move" or item.item_id not in approved:
                reason = "not approved" if item.action == "move" else item.reason
                result.skipped.append(
                    {
                        "item_id": item.item_id,
                        "filename": item.filename,
                        "reason": reason or "not approved",
                    }
                )
                continue

            error = self._validate_item(item, target)
            if error is not None:
                result.failed.append(
                    {"item_id": item.item_id, "filename": item.filename, "error": error}
                )
                continue

            source = Path(item.source)
            destination = Path(str(item.destination))
            try:
                destination.parent.mkdir(parents=True, exist_ok=True)
                # Recomputed here as well as at scan time: another process (or
                # an earlier item in this run) may have taken the name since.
                final = unique_destination(destination, reserved)
                reserved.add(final)
                shutil.move(str(source), str(final))
                # Logged immediately so the undo log can never miss a move.
                undo_log.record_move(
                    plan_id=plan.plan_id,
                    item_id=item.item_id,
                    source=str(source),
                    destination=str(final),
                    category=item.category or "",
                    decided_by=item.decided_by,
                    confidence=item.confidence,
                )
            except (OSError, UnsafePath) as exc:
                result.failed.append(
                    {
                        "item_id": item.item_id,
                        "filename": item.filename,
                        "error": str(exc),
                    }
                )
                continue

            result.moved.append(
                {
                    "item_id": item.item_id,
                    "filename": item.filename,
                    "source": str(source),
                    "destination": str(final),
                    "category": item.category,
                }
            )

        # Acknowledged once, and only after the run was actually attempted, so
        # a folder is never marked "confirmed" by a call that did nothing.
        if first_run:
            undo_log.acknowledge_target(plan.target_dir)

        self._store.mark_applied(plan)
        return result

    @staticmethod
    def _validate_item(item: PlanItem, target: Path) -> str | None:
        """Re-check one item against the disk. Returns an error, or None.

        Every safety invariant that could have been invalidated between scan
        and apply is re-tested here rather than trusted from the plan.
        """
        if not item.category or not item.destination:
            return "plan item has no destination"

        source = Path(item.source)
        if source.is_symlink():
            return f"{source} is a symlink; symlinks are never moved"
        if not source.exists():
            return f"{source} no longer exists"
        if not source.is_file():
            return f"{source} is no longer a regular file"
        if not is_direct_child(source, target):
            return f"{source} is not directly inside {target}"

        # target/<category>/<name>: the category folder must sit directly in
        # the target, which keeps a doctored plan from writing anywhere else.
        destination = Path(item.destination)
        if not is_direct_child(destination.parent, target):
            return f"{destination} is not inside {target}"

        return None
