"""The MCP server: this tool's capabilities, exposed to a host LLM.

WHAT MAKES THIS DIFFERENT FROM THE OTHER TWO INTERFACES
-------------------------------------------------------
The CLI and the web UI are driven by a person who can see what they are about
to do. This one is driven by a model. So the interesting question is not "can
an LLM call these functions" — it obviously can — but "what is the worst thing
that happens when it calls them wrongly, or is talked into calling them by
something it read in a file".

The answer is bounded by four things, none of which the model can reach:

  1. The allowlist (organiser/paths.py). Folders outside it do not exist as
     far as this server is concerned. The model can ask; the answer is no.
  2. Move-only. There is no delete in this codebase, so no argument to any of
     these tools can produce one.
  3. Proposing and applying are separate calls. Nothing this server does can
     move a file without a plan id from a previous call.
  4. Every move is logged and reversible, by the same code the CLI uses.

WHERE THE JUDGEMENT COMES FROM
------------------------------
Either side of the connection can decide where files go, and this is the
reason the delegation seam exists:

  - `propose_plan(path)` alone uses the configured provider agents. That is
    the CLI's behaviour, unchanged.
  - `propose_plan(path, assignments=...)` takes the HOST model's decisions
    instead, by registering them as a delegate. The manager cannot tell the
    difference, and needed no change to support this.

The second is the better shape for MCP: the model on the other end of the
connection has already read the file list, so paying a second model to read it
again is cost and latency for nothing, and it puts a model in the loop that
the person watching cannot see.

Run it with:  python -m organiser.mcp_server
"""

from __future__ import annotations

from pathlib import Path

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

from . import delegates
from .delegates import (
    DelegateDecision,
    DelegateRequest,
    ProposedCategory,
    SchemeProposal,
    SchemeRequest,
)
from .manager import FirstRunConfirmationRequired, Manager
from .paths import UnsafePath, root_policy, should_skip, validate_target_dir
from .plan import PlanStore
from .rules import classify_by_rules
from .undo_log import execute_undo, latest_plan_id, plan_undo, read_log

mcp = FastMCP("file-organiser")

# A directory listing goes straight into the host model's context, so it has to
# be bounded. Truncation is reported rather than silent — a model that thinks
# it has seen every file will confidently assign the ones it never saw.
MAX_LISTED = 300

# Content snippets are OFF unless asked for. The filename is metadata; the
# contents are the document. Sending every file's first few hundred characters
# to a model by default is not a decision this server should make quietly on
# someone's behalf — particularly the someone whose folder holds client
# records.
SNIPPET_CHARS = 400

_STORE = PlanStore()

HOST_DELEGATE = "mcp-host"
HOST_SCHEME = "mcp-host-scheme"


class _HostDelegate:
    """A delegate whose judgement is a dict handed in by the host model.

    This is the whole delegation seam paying off: the manager asks "what
    category for this file?", and the answer happens to have come from the
    model on the other end of the MCP connection rather than from an API call.
    manager.py did not change to allow this and cannot tell.
    """

    name = HOST_DELEGATE

    def __init__(self, assignments: dict[str, str]):
        self._assignments = assignments

    def available(self) -> tuple[bool, str]:
        return True, f"{len(self._assignments)} assignments supplied by the host"

    def decide(self, request: DelegateRequest) -> DelegateDecision:
        category = self._assignments.get(request.filename)
        if category is None:
            # Not an error — the host simply didn't assign this one, and the
            # file staying put is the correct outcome.
            return DelegateDecision(
                category=None,
                confidence="n/a",
                reason="the host model did not assign this file; left in place",
                decided_by=self.name,
            )
        return DelegateDecision(
            category=category,
            confidence="high",
            reason="assigned by the host model",
            decided_by=self.name,
        )


class _HostSchemeProposer:
    """The vocabulary the host chose, as a scheme proposal."""

    name = HOST_SCHEME

    def __init__(self, assignments: dict[str, str]):
        self._categories = list(dict.fromkeys(assignments.values()))

    def available(self) -> tuple[bool, str]:
        return True, f"{len(self._categories)} categories supplied by the host"

    def propose(self, request: SchemeRequest) -> SchemeProposal:
        return SchemeProposal(
            categories=[ProposedCategory(name=c, description="") for c in self._categories],
            reason="categories chosen by the host model",
            proposed_by=self.name,
        )


def _guard(fn, *args, **kwargs):
    """Turn this codebase's refusals into MCP tool errors that say why.

    An UnsafePath already carries the allowlist in its message, so a model that
    picks the wrong folder is told what it may have instead of just "no".
    """
    try:
        return fn(*args, **kwargs)
    except UnsafePath as exc:
        raise ToolError(str(exc)) from exc
    except ValueError as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool(
    annotations=ToolAnnotations(
        title="List reachable folders",
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=False,
    )
)
def list_allowed_roots() -> dict:
    """The folders this server may touch. Everything else is refused.

    Call this first. It is cheaper than guessing a path and being refused, and
    it is the actual security boundary rather than a suggestion.
    """
    try:
        policy = root_policy()
    except UnsafePath as exc:
        raise ToolError(str(exc)) from exc
    return {
        "roots": [str(r) for r in policy.roots],
        "source": policy.source,
        "note": (
            "Folders outside these are refused before anything is read. "
            "Subfolders of a root are reachable; the root itself is too."
        ),
    }


@mcp.tool(
    annotations=ToolAnnotations(
        title="Scan a folder",
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=False,
    )
)
def scan_directory(path: str, include_snippets: bool = False) -> dict:
    """Inventory one folder. Reads nothing outside it and moves nothing.

    Returns each top-level file, whether the built-in rule table already knows
    where it goes, and which files are left needing judgement — those are the
    ones to assign in `propose_plan`.

    Subfolders, hidden files and symlinks are listed as skipped, not entered.

    `include_snippets` adds the first few hundred characters of each
    undecided file. Off by default: the filename is metadata, the contents are
    the document, and sending documents to a model should be a decision
    somebody makes rather than a default.
    """
    target = _guard(validate_target_dir, path)

    decided: list[dict] = []
    undecided: list[dict] = []
    skipped: list[dict] = []

    for entry in sorted(target.iterdir(), key=lambda p: p.name.lower()):
        reason = should_skip(entry)
        if reason:
            skipped.append({"name": entry.name, "reason": reason})
            continue

        try:
            size = entry.stat().st_size
        except OSError:
            size = 0

        match = classify_by_rules(entry.name)
        if match:
            decided.append(
                {"name": entry.name, "size_bytes": size, "category": match.category}
            )
            continue

        item: dict = {"name": entry.name, "size_bytes": size}
        if include_snippets:
            item["snippet"] = _snippet(entry)
        undecided.append(item)

    return {
        "path": str(target),
        "counts": {
            "decided_by_rules": len(decided),
            "needs_judgement": len(undecided),
            "skipped": len(skipped),
        },
        "decided_by_rules": decided[:MAX_LISTED],
        "needs_judgement": undecided[:MAX_LISTED],
        "skipped": skipped[:MAX_LISTED],
        "truncated": max(
            0,
            len(decided) - MAX_LISTED,
        )
        + max(0, len(undecided) - MAX_LISTED)
        + max(0, len(skipped) - MAX_LISTED),
        "next_step": (
            "Assign a category to each entry in needs_judgement, then call "
            "propose_plan(path, assignments). Nothing moves until apply_plan."
        ),
    }


def _snippet(entry: Path) -> str | None:
    """A short text preview, or None if the file isn't readable as text."""
    try:
        with entry.open("r", encoding="utf-8", errors="strict") as handle:
            return handle.read(SNIPPET_CHARS)
    except (OSError, UnicodeDecodeError):
        return None


@mcp.tool(
    annotations=ToolAnnotations(
        title="Propose a move plan",
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=False,
        open_world_hint=False,
    )
)
def propose_plan(
    path: str,
    assignments: dict[str, str] | None = None,
    use_rules: bool | None = None,
) -> dict:
    """Build a plan of moves. Touches nothing on disk.

    `assignments` maps filename to category — your decisions. Files you leave
    out are left where they are; this tool never guesses on your behalf.

    Omit `assignments` entirely to let the server's own configured agents
    decide instead. That needs an API key and costs a call per file.

    `use_rules` controls the built-in extension table. It defaults to **off
    when you supply assignments** and on when you don't, because the rule pass
    runs BEFORE any delegate: with it on, a file the rules recognise never
    reaches your decision at all, and asking for `notes.txt -> Meetings` would
    quietly produce `Documents/notes.txt`. Turn it on deliberately if you want
    the rules to handle everything you didn't name — anything of yours they
    overrule is reported in `overruled_by_rules` rather than lost silently.

    Returns a plan id. Nothing moves until you pass it to `apply_plan`.
    """
    # Assignments are authoritative by default: you have already seen the
    # inventory, including what the rules would have said.
    if use_rules is None:
        use_rules = assignments is None

    if assignments is not None:
        if not isinstance(assignments, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in assignments.items()
        ):
            raise ToolError("assignments must be a mapping of filename to category")
        # Registering under fixed names replaces the previous host delegate.
        # Fine for a stdio server, which has one client and one call in flight;
        # it would need per-call names under real concurrency.
        delegates.register(_HostDelegate(assignments))
        delegates.register(_HostSchemeProposer(assignments))
        manager = Manager(
            delegate_name=HOST_DELEGATE, scheme_name=HOST_SCHEME, store=_STORE
        )
    else:
        manager = Manager(store=_STORE)

    plan = _guard(manager.scan, path, use_rules=use_rules)
    data = plan.to_dict()

    # An assignment the rule pass claimed first. Silently dropping these is
    # the failure mode this reports its way out of: you asked for one category
    # and got another, with nothing to say so.
    overruled = []
    if assignments:
        by_name = {item.filename: item for item in plan.items}
        for name, wanted in assignments.items():
            item = by_name.get(name)
            if item is None:
                overruled.append({"name": name, "wanted": wanted, "got": "not in folder"})
            elif item.decided_by == "rules" and item.category != wanted:
                overruled.append({"name": name, "wanted": wanted, "got": item.category})

    return {
        "plan_id": plan.plan_id,
        "path": plan.target_dir,
        "summary": data["summary"],
        "is_first_run": plan.is_first_run,
        "used_rules": use_rules,
        "overruled_by_rules": overruled,
        "moves": [
            {
                "name": item.filename,
                "category": item.category,
                "destination": item.destination,
                "decided_by": item.decided_by,
                "confidence": item.confidence,
                "needs_review": item.needs_review,
            }
            for item in plan.moves[:MAX_LISTED]
        ],
        "staying_put": [
            {"name": item.filename, "reason": item.reason}
            for item in plan.skips[:MAX_LISTED]
        ],
        "next_step": (
            f"Nothing has moved. Call apply_plan('{plan.plan_id}') to carry it out"
            + (
                ", passing confirm=True — this is the first time this folder "
                "would be touched."
                if plan.is_first_run
                else "."
            )
        ),
    }


@mcp.tool(
    annotations=ToolAnnotations(
        title="Apply a move plan",
        read_only_hint=False,
        # Move-only: nothing is deleted or overwritten, and every move is
        # logged and reversible. Not "destructive" in the sense this hint
        # means, and saying otherwise would cry wolf.
        destructive_hint=False,
        idempotent_hint=False,
        open_world_hint=False,
    )
)
def apply_plan(plan_id: str, confirm: bool = False) -> dict:
    """Carry out a plan built by `propose_plan`. This moves files.

    Files are moved, never deleted or overwritten: a name collision gets a
    `_1` suffix. Every move is written to the undo log and can be reversed
    with `undo_plan`.

    The first apply to any folder needs `confirm=True`, so that reaching a new
    folder is always a deliberate second step.
    """
    try:
        result = Manager(store=_STORE).apply(plan_id, confirm_first_apply=confirm)
    except FirstRunConfirmationRequired as exc:
        raise ToolError(
            f"{exc.target_dir} has not been organised before. Re-send with "
            "confirm=True if that is what you intend."
        ) from exc
    except UnsafePath as exc:
        raise ToolError(str(exc)) from exc
    except ValueError as exc:
        raise ToolError(str(exc)) from exc

    data = result.to_dict()
    data["next_step"] = (
        f"Reversible with undo_plan('{plan_id}'), or undo_plan() for the most "
        "recent."
    )
    return data


@mcp.tool(
    annotations=ToolAnnotations(
        title="Undo a plan",
        read_only_hint=False,
        destructive_hint=False,
        idempotent_hint=False,
        open_world_hint=False,
    )
)
def undo_plan(plan_id: str | None = None, confirm: bool = False) -> dict:
    """Put files back where they were.

    `plan_id` defaults to the most recently applied plan. Reversals that would
    overwrite something are refused and reported rather than forced.

    Called without `confirm=True` this is a dry run: it reports what it would
    reverse and changes nothing.
    """
    target_plan = plan_id or latest_plan_id()
    if not target_plan:
        raise ToolError("nothing has been applied yet, so there is nothing to undo")

    if not read_log(plan_id=target_plan, limit=1):
        raise ToolError(f"no logged moves for plan {target_plan}")

    undo = plan_undo(target_plan)

    if not confirm:
        preview = undo.to_dict()
        preview["dry_run"] = True
        preview["next_step"] = (
            f"Nothing was moved. Re-send with confirm=True to reverse "
            f"{len(undo.reversals)} moves."
        )
        return preview

    result = execute_undo(undo)
    result["dry_run"] = False
    return result


def main() -> None:
    # No banner: this process talks JSON-RPC on stdout and is launched by a
    # client, not read by a person. Decoration on startup is noise in a log
    # somebody will one day be reading to work out what went wrong.
    mcp.run(show_banner=False)


if __name__ == "__main__":
    main()
