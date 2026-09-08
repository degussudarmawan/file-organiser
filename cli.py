#!/usr/bin/env python3
"""Command-line front end for the file-organising agent system.

Same ergonomics as the original file_organizer.py: it is a dry run unless you
ask for `--apply`. Building a plan never touches the target folder; applying
one is always a separate, explicit step against a plan id.

This module is a view, not a brain. Every decision and every filesystem write
goes through `organiser.manager.Manager` — the CLI only renders what the
manager returns, exactly like the web UI only renders what the HTTP layer
returns. The one exception is `undo`, which reverses moves the manager already
recorded, and which is held to the same rules: never delete, never overwrite.
"""

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

from organiser.categories import FALLBACK_CATEGORY
from organiser.config import (
    DEFAULT_EFFORT,
    ConfigError,
    config_path,
    load_settings,
)
from organiser.delegates import get as get_delegate
from organiser.manager import FirstRunConfirmationRequired, Manager
from organiser.paths import UnsafePath, unique_destination
from organiser.plan import Plan, PlanItem
from organiser.undo_log import read_log, record_move, undo_log_path

# Importing the two delegate modules is what registers them with the delegate
# registry: the classifier (per-file) and the scheme proposer (per-run
# vocabulary). Without them the manager would find no delegate and run
# rules-only. Guarded so a missing dependency degrades the CLI instead of
# killing it — `log`, `undo` and `config` have no business failing over an
# import. (Neither module imports a provider SDK any more: that happens lazily
# in `organiser/providers/`, so having only the SDK you use is not an error.)
DELEGATE_IMPORT_ERROR: str | None = None
try:
    import organiser.classifier  # noqa: F401  (imported for the register() side effect)
    import organiser.scheme  # noqa: F401  (same — registers the scheme proposer)
except Exception as exc:  # noqa: BLE001 - reported, not swallowed
    DELEGATE_IMPORT_ERROR = str(exc)

UNDO_LOG_SCAN_LIMIT = 100_000


# --- output -----------------------------------------------------------------


class Style:
    """Plain ANSI, applied only when stdout is a terminal."""

    def __init__(self, enabled: bool):
        self.enabled = enabled

    def _wrap(self, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.enabled else text

    def bold(self, text: str) -> str:
        return self._wrap("1", text)

    def dim(self, text: str) -> str:
        return self._wrap("2", text)

    def red(self, text: str) -> str:
        return self._wrap("31", text)

    def green(self, text: str) -> str:
        return self._wrap("32", text)

    def yellow(self, text: str) -> str:
        return self._wrap("33", text)

    def blue(self, text: str) -> str:
        return self._wrap("34", text)

    def cyan(self, text: str) -> str:
        return self._wrap("36", text)


STYLE = Style(sys.stdout.isatty())


def out(text: str = "") -> None:
    print(text)


def err(text: str) -> None:
    # Flush first: when stdout is a pipe it is block-buffered while stderr is
    # not, and an error that jumps ahead of the output explaining it is worse
    # than useless.
    sys.stdout.flush()
    print(STYLE.red(f"error: {text}"), file=sys.stderr)


def progress(text: str) -> None:
    """A note about work in flight, never part of the output.

    A scan now makes real model calls — one to the scheme proposer plus one per
    delegated file — so it can sit silent for a while. These lines go to stderr
    so they cannot contaminate `--json` or a piped plan, and they are suppressed
    entirely in `--json` mode.
    """
    print(STYLE.dim(text), file=sys.stderr, flush=True)


def heading(text: str) -> None:
    out()
    out(STYLE.bold(text))
    out(STYLE.dim("-" * len(text)))


def _invocation() -> str:
    """How this script was invoked, shortened to a relative path where that
    still names the same thing — so the printed apply command is copy-pastable."""
    parts = []
    for raw in (Path(sys.executable), Path(sys.argv[0]).resolve()):
        try:
            parts.append(str(raw.relative_to(Path.cwd())))
        except ValueError:
            parts.append(str(raw))
    return " ".join(parts)


# --- plan rendering ---------------------------------------------------------


def _agent_label(item: PlanItem) -> str:
    """The uncoloured tag: `[rules]` or `[classifier medium]`.

    Kept separate from the coloured form because ANSI escapes have width on the
    screen but not in `len()`, and these are printed as an aligned column.
    """
    if item.decided_by == "rules":
        return "[rules]"
    if item.decided_by in ("", "none"):
        return ""
    return f"[{item.decided_by} {item.confidence}]"


def _agent_tag(item: PlanItem) -> str:
    """`[rules]` or `[classifier medium]`, coloured by how much trust it earns.

    The two agents must never read alike: a rule decision is a local table hit
    with nothing to second-guess, so it is dimmed to the background. A
    classifier decision is a model's judgement inside the proposed scheme, so
    it is coloured by its own confidence and always carries that word.
    """
    label = _agent_label(item)
    if not label:
        return ""
    if item.decided_by == "rules":
        return STYLE.dim(label)
    if item.confidence == "high":
        return STYLE.green(label)
    if item.confidence == "medium":
        return STYLE.yellow(label)
    return STYLE.red(label)


def _tag_width(items: list[PlanItem]) -> int:
    """Width of the tag column for a block of items."""
    return max((len(_agent_label(i)) for i in items), default=0)


def _tagged_line(item: PlanItem, width: int, indent: int = 4) -> str:
    """`[rules]            report.pdf -> Documents/report.pdf`

    The tag leads the line rather than trailing it. Which agent decided a move
    is the thing you scan a plan for, and in a left-hand column the rule hits
    and the delegated picks separate at a glance instead of having to be read
    filename by filename.
    """
    padding = " " * (width - len(_agent_label(item)))
    return (
        f"{' ' * indent}{_agent_tag(item)}{padding}  "
        f"{item.filename} -> {_destination_label(item)}"
    )


def _destination_label(item: PlanItem) -> str:
    """`Category/final_name` — the final name, so a collision rename shows up."""
    category = item.category or FALLBACK_CATEGORY
    name = Path(item.destination).name if item.destination else item.filename
    return f"{category}/{name}"


def _no_scheme_reason(plan: Plan) -> str | None:
    """Why no scheme exists, taken from the files that were left alone.

    A plan with no proposed categories and no skipped files simply had nothing
    to delegate — the rules placed everything — and needs no explanation. A
    plan with skips does: the manager already recorded the cause on each item
    it refused to place, so it is reported rather than re-derived here.
    """
    if plan.proposed_categories:
        return None
    reasons = [i.reason for i in plan.skips if i.decided_by == "none" and i.reason]
    if not reasons:
        return None
    return max(set(reasons), key=reasons.count)


def print_scheme(plan: Plan) -> None:
    """The vocabulary this run invented, before the moves that use it.

    Printed ahead of PLANNED MOVES on purpose: the category names in that list
    are not a fixed vocabulary any more, and reading them without knowing where
    they came from is how you would mistake them for one.
    """
    categories = plan.proposed_categories
    if not categories:
        return

    heading(f"PROPOSED CATEGORIES FOR THIS FOLDER ({len(categories)})")
    if plan.scheme_reason:
        out(f"  {plan.scheme_reason}")
    out(
        STYLE.dim(
            "  Proposed for this run by the scheme proposer agent, from the contents"
        )
    )
    out(
        STYLE.dim(
            "  of this folder. Not a fixed list: nothing is remembered between runs,"
        )
    )
    out(
        STYLE.dim(
            "  so another run may name the same idea differently. Names marked NEW do"
        )
    )
    out(STYLE.dim("  not exist in the target yet; the rest reuse what is already there."))
    out()

    name_width = max(len(str(c.get("name", ""))) for c in categories)
    for entry in categories:
        name = str(entry.get("name", ""))
        marker = "NEW" if entry.get("is_new") else "reused"
        marker_text = (
            STYLE.yellow(f"{marker:<6}") if entry.get("is_new") else STYLE.dim(f"{marker:<6}")
        )
        description = str(entry.get("description", "")).strip()
        line = f"  {STYLE.cyan(name.ljust(name_width))}  {marker_text}"
        if description:
            line = f"{line}  {STYLE.dim(description)}"
        out(line.rstrip())


def print_plan(plan: Plan) -> None:
    summary = plan.summary()

    out(STYLE.bold(f"Target:     {plan.target_dir}"))
    if not plan.used_rules:
        # Printed only in the non-default mode. Without this line a plan where
        # an ordinary .jpg was decided by the classifier looks like a bug in
        # the rule table rather than a rule table that was never consulted.
        out(
            f"Rules:      {STYLE.yellow('skipped')}"
            f"{STYLE.dim(' — every file sent to the agents (--no-rules)')}"
        )
    if plan.classifier_available:
        status = STYLE.green("available")
    else:
        # With rules off there is no cheap pass to fall back on, so "unmatched"
        # means everything. Say which of the two it is.
        left_alone = (
            "unmatched files are left alone"
            if plan.used_rules
            else "rules are off too, so every file is left alone"
        )
        status = STYLE.yellow(f"unavailable — {left_alone}")
    detail = f" ({plan.classifier_detail})" if plan.classifier_detail else ""
    out(f"Classifier: {status}{STYLE.dim(detail)}")
    missing_scheme = _no_scheme_reason(plan)
    if missing_scheme:
        out(
            f"Scheme:     {STYLE.yellow('no categories were proposed for this run')}"
            f"{STYLE.dim(' — ' + missing_scheme)}"
        )
    if plan.is_first_run:
        out(STYLE.yellow("First run:  this folder has never been applied to before"))

    print_scheme(plan)

    grouped = plan.grouped_by_category()
    if grouped:
        heading(f"PLANNED MOVES ({summary['to_move']})")
        # Only explain the tags that actually appear — a rules-only plan has no
        # business carrying three lines about an agent that never ran.
        deciders = {i.decided_by for i in plan.moves}
        if "rules" in deciders:
            out(
                STYLE.dim(
                    "  [rules]      decided locally by the extension/filename table."
                )
            )
        if deciders - {"rules"}:
            out(
                STYLE.dim(
                    "  [classifier] decided by the classifier agent, from the proposed"
                )
            )
            out(STYLE.dim("               categories above, at the confidence shown."))
        if not plan.used_rules:
            # The absence of [rules] tags is a choice this run made, not a
            # table that failed to match. A column of identical tags reads as
            # noise unless you know why nothing else could appear in it.
            out(
                STYLE.dim(
                    "  No [rules] tags: --no-rules sent every file to the agents."
                )
            )
        width = _tag_width(plan.moves)
        for category, items in grouped.items():
            out(STYLE.cyan(f"  {category}"))
            for item in items:
                out(_tagged_line(item, width))
    else:
        heading("PLANNED MOVES (0)")
        out(STYLE.dim("  nothing to move"))

    review = plan.review_items
    if review:
        heading(f"NEEDS REVIEW ({len(review)})")
        out(STYLE.dim("  Delegated picks the classifier was not confident about."))
        out(STYLE.dim("  Eyeball these before approving them."))
        width = _tag_width(review)
        for item in review:
            out(_tagged_line(item, width, indent=2))
            out(STYLE.dim(f"      {item.reason}"))

    skips = plan.skips
    if skips:
        heading(f"SKIPPED ({len(skips)})")
        for item in skips:
            out(f"  {item.filename}")
            out(STYLE.dim(f"      {item.reason}"))

    by_agent = ", ".join(f"{name} {count}" for name, count in sorted(summary["by_agent"].items()))
    heading("SUMMARY")
    out(
        f"  {summary['to_move']} to move across {summary['categories']} categories, "
        f"{summary['to_skip']} skipped, {summary['needs_review']} need review "
        f"(of {summary['total_files']} files)"
    )
    if by_agent:
        out(STYLE.dim(f"  decided by: {by_agent}"))
    out(f"  plan id: {STYLE.bold(plan.plan_id)}")


def print_apply_hint(plan: Plan) -> None:
    out()
    out(STYLE.dim("Nothing has been moved. Apply this plan with:"))
    out(f"  {_invocation()} apply {plan.plan_id}")


# --- apply result rendering -------------------------------------------------


def _entry_name(entry: dict) -> str:
    name = entry.get("filename")
    if name:
        return str(name)
    source = entry.get("source")
    return Path(str(source)).name if source else str(entry.get("item_id", "?"))


def _entry_destination(entry: dict) -> str:
    destination = entry.get("destination")
    if not destination:
        return ""
    category = entry.get("category") or Path(str(destination)).parent.name
    return f"{category}/{Path(str(destination)).name}"


def _entry_reason(entry: dict) -> str:
    return str(entry.get("reason") or entry.get("error") or "")


def print_apply_result(result) -> None:
    moved = list(result.moved)
    skipped = list(result.skipped)
    failed = list(result.failed)

    heading(f"MOVED ({len(moved)})")
    if moved:
        for entry in moved:
            out(f"  {STYLE.green('ok')} {_entry_name(entry)} -> {_entry_destination(entry)}")
    else:
        out(STYLE.dim("  nothing moved"))

    if skipped:
        heading(f"SKIPPED ({len(skipped)})")
        for entry in skipped:
            out(f"  {_entry_name(entry)}")
            reason = _entry_reason(entry)
            if reason:
                out(STYLE.dim(f"      {reason}"))

    if failed:
        heading(f"FAILED ({len(failed)})")
        for entry in failed:
            out(f"  {STYLE.red('!!')} {_entry_name(entry)}")
            reason = _entry_reason(entry)
            if reason:
                out(STYLE.dim(f"      {reason}"))

    out()
    out(STYLE.dim(f"Every move above is logged in {undo_log_path()}"))


# --- config rendering -------------------------------------------------------

# Which env var holds the credential for each provider. Only the *name* is ever
# used: this command reports set/not set and never reads the value, because a
# credential printed into a terminal ends up in scrollback, in a screenshot, and
# in whatever the shell logs.
API_KEY_ENV = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    # A local OpenAI-protocol server usually ignores the key entirely; the
    # provider supplies a placeholder so nobody has to invent one for Ollama.
    "compatible": "OPENAI_API_KEY",
}

NOT_SET = "—"

CONFIG_AGENTS = ("scheme", "classifier")


def _tilde(path) -> str:
    """`~/.file-organiser-agent/config.json` — shorter, and still pasteable."""
    text = str(path)
    home = str(Path.home())
    if text == home or text.startswith(home + os.sep):
        return "~" + text[len(home) :]
    return text


def _agent_statuses() -> dict[str, tuple[bool, str]]:
    """Ask each delegate whether it could actually run, without a network call.

    Every failure is caught: `config` is the command you reach for *because*
    something is broken, so it must report the breakage rather than become it.
    """
    statuses: dict[str, tuple[bool, str]] = {}
    for name in CONFIG_AGENTS:
        delegate = get_delegate(name)
        if delegate is None:
            statuses[name] = (
                False,
                DELEGATE_IMPORT_ERROR or f"no delegate named {name!r} is registered",
            )
            continue
        try:
            ok, detail = delegate.available()
        except Exception as exc:  # noqa: BLE001 - reported, not swallowed
            ok, detail = False, str(exc)
        statuses[name] = (bool(ok), str(detail))
    return statuses


def _starter_config(settings) -> str:
    """A config file that would reproduce the settings currently in force.

    Generated from the resolved settings rather than hardcoded, so it is a
    starting point for *this* setup. It is printed, never written: creating a
    file on disk is a thing you should have asked for.
    """
    data: dict = {"provider": settings.provider}
    if settings.base_url or settings.provider == "compatible":
        data["base_url"] = settings.base_url or "http://localhost:11434/v1"
    for agent in CONFIG_AGENTS:
        per_agent = settings.for_agent(agent)
        data[agent] = {
            # A placeholder, deliberately not a plausible model id: there is no
            # built-in default outside Anthropic, and inventing one here would
            # just move the failure to request time.
            "model": per_agent.model or "<model id>",
            "effort": per_agent.effort or DEFAULT_EFFORT[agent],
        }
    return json.dumps(data, indent=2)


def print_config(settings, statuses: dict[str, tuple[bool, str]]) -> None:
    """The effective settings, and — the actual point — which layer set each.

    Knowing that the classifier ran on `gpt-5-mini` is half an answer. The half
    that resolves an argument with yourself is knowing it came from an
    environment variable rather than the config file you have been editing.
    """
    key_env = API_KEY_ENV.get(settings.provider, "")
    key_set = bool(os.environ.get(key_env))
    path = config_path()
    exists = path.exists()

    rows = [
        ("provider", settings.provider, settings.sources.get("provider", "default"), None),
        (
            "base_url",
            settings.base_url or NOT_SET,
            settings.sources.get("base_url", "default"),
            None,
        ),
    ]
    for agent in CONFIG_AGENTS:
        per_agent = settings.for_agent(agent)
        model_source = settings.sources.get(f"{agent}.model", "default")
        if per_agent.model is None:
            # "(default)" would be read as "some built-in model" when what it
            # actually means is that this provider has none and nothing named
            # one. Say that, since it is the thing you have to go fix.
            model_source = "no default for this provider"
        rows.append(
            (
                agent,
                per_agent.model or NOT_SET,
                model_source,
                (
                    per_agent.effort or NOT_SET,
                    settings.sources.get(f"{agent}.effort", "default"),
                ),
            )
        )

    # Pad before colouring: ANSI escapes have width on screen but not in len().
    label_width = len("config file")
    value_width = max(len(str(r[1])) for r in rows)
    source_width = max(len(f"({r[2]})") for r in rows)
    effort_width = max(len(str(r[3][0])) for r in rows if r[3])

    heading("EFFECTIVE SETTINGS")
    for label, value, source, effort in rows:
        # Only pad the source cell when something follows it, so the rows
        # without an effort column do not trail invisible whitespace.
        source_cell = f"({source})".ljust(source_width) if effort else f"({source})"
        line = (
            f"  {label.ljust(label_width)}  {STYLE.cyan(str(value).ljust(value_width))}"
            f"  {STYLE.dim(source_cell)}"
        )
        if effort:
            effort_value, effort_source = effort
            line = (
                f"{line}  effort {str(effort_value).ljust(effort_width)}"
                f"  {STYLE.dim(f'({effort_source})')}"
            )
        out(line.rstrip())

    out()
    if key_set:
        key_text = STYLE.green("set".ljust(value_width))
    elif settings.provider == "compatible":
        key_text = STYLE.dim("not set".ljust(value_width))
    else:
        key_text = STYLE.yellow("not set".ljust(value_width))
    key_note = f"({key_env})"
    if settings.provider == "compatible" and not key_set:
        key_note = f"({key_env} — usually not needed for a local endpoint)"
    out(f"  {'api key'.ljust(label_width)}  {key_text}  {STYLE.dim(key_note)}")
    out(
        f"  {'config file'.ljust(label_width)}  {_tilde(path)}"
        f"{'' if exists else STYLE.dim('  (does not exist)')}"
    )
    out()
    out(STYLE.dim("  The API key is never read or stored by this tool's config —"))
    out(STYLE.dim("  it stays in the environment, so the state dir holds no secret."))

    heading("AGENT STATUS")
    status_width = len("unavailable")
    for agent in CONFIG_AGENTS:
        ok, detail = statuses[agent]
        text = (
            STYLE.green("available".ljust(status_width))
            if ok
            else STYLE.yellow("unavailable".ljust(status_width))
        )
        out(f"  {agent.ljust(label_width)}  {text}  {STYLE.dim(detail)}")

    if not exists:
        heading("NO CONFIG FILE YET")
        out("  There is no config file at")
        out(f"    {STYLE.cyan(_tilde(path))}")
        out("  so every value above came from an environment variable or a built-in")
        out("  default. Nothing has been created for you — to save these settings,")
        out("  write that file yourself:")
        out()
        for line in _starter_config(settings).splitlines():
            out(STYLE.dim(f"    {line}"))
        out()
        out(STYLE.dim(f"  Edit it with:  $EDITOR $({_invocation()} config --path)"))


# --- confirmation -----------------------------------------------------------


def confirm_first_run(target_dir: str, assume_yes: bool) -> bool:
    """The one hard gate: the first apply to a folder must be said out loud."""
    if assume_yes:
        return True

    out()
    out(STYLE.yellow(STYLE.bold("!!  FIRST APPLY TO THIS FOLDER  !!")))
    out(STYLE.yellow(f"    {target_dir}"))
    out()
    out("This folder has never been organised by this tool before. Applying will")
    out("move its top-level files into category subfolders. Files are only ever")
    out("moved, never deleted or overwritten, and every move is logged so it can")
    out("be reversed — but this is the point to check you named the right folder.")
    out()

    if not sys.stdin.isatty():
        err("first apply needs confirmation; re-run with --yes (no terminal to prompt on)")
        return False

    try:
        answer = input("Type 'yes' to confirm: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        out()
        return False
    return answer == "yes"


def confirm(prompt: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        err("this needs confirmation; re-run with --yes (no terminal to prompt on)")
        return False
    try:
        return input(f"{prompt} ").strip().lower() == "yes"
    except (EOFError, KeyboardInterrupt):
        out()
        return False


# --- shared apply path ------------------------------------------------------


def run_apply(
    manager: Manager,
    plan_id: str,
    approved_ids: list[str] | None,
    assume_yes: bool,
) -> int:
    """Apply a plan, handling the first-run gate. Returns an exit code."""
    try:
        result = manager.apply(
            plan_id,
            approved_ids=approved_ids,
            confirm_first_apply=assume_yes,
        )
    except FirstRunConfirmationRequired as exc:
        if not confirm_first_run(str(exc.target_dir), assume_yes):
            out(STYLE.dim("Not confirmed. Nothing was moved."))
            return 1
        result = manager.apply(
            plan_id,
            approved_ids=approved_ids,
            confirm_first_apply=True,
        )

    print_apply_result(result)
    return 1 if result.failed else 0


# --- commands ---------------------------------------------------------------


def announce_scan(folder: str, quiet: bool, use_rules: bool = True) -> None:
    """Say what the scan is about to do before it goes quiet on the network."""
    if quiet:
        return
    progress(f"scanning {folder} (nothing will be moved)…")
    if use_rules:
        progress(
            "  running the rule table, then asking the scheme proposer agent to propose"
        )
        progress(
            "  this folder's categories, then classifying what is left — this makes real"
        )
        progress("  model calls and can take a while.")
    else:
        progress(
            "  skipping the rule table: the scheme proposer agent sees every file, then"
        )
        progress(
            "  the classifier is asked about each one in turn — this makes real model"
        )
        progress("  calls and can take a while.")


def warn(text: str) -> None:
    """A warning about how the run is configured, never part of the output.

    stderr, like `progress`, and for the same reason: `--json` is a machine
    interface and a warning printed into it corrupts the plan. Unlike
    `progress` it is not suppressed in JSON mode — a cost this large should be
    said whether or not a human is watching the scan happen.
    """
    print(STYLE.yellow(text), file=sys.stderr, flush=True)


def delegates_offline(manager: Manager) -> str | None:
    """Why nothing could be delegated this run, or None if the agents can run.

    A delegated decision needs *both* agents: the scheme proposer invents the
    run's vocabulary and the classifier assigns files within it, so either one
    missing leaves every delegated file exactly where it is. Both probes are
    local config checks — no network call — which is what makes it reasonable
    to ask before the scan rather than infer it from the plan afterwards.
    """
    ok, detail = manager.delegate_status()
    if not ok:
        return f"classifier unavailable ({detail})"

    proposer = get_delegate("scheme")
    if proposer is None:
        return DELEGATE_IMPORT_ERROR or "no scheme proposer agent is registered"
    try:
        ok, detail = proposer.available()
    except Exception as exc:  # noqa: BLE001 - a broken probe is unavailable
        return f"scheme proposer failed its check ({exc})"
    if not ok:
        return f"scheme proposer unavailable ({detail})"
    return None


def warn_no_rules(manager: Manager) -> None:
    """Say what `--no-rules` costs, before the scan spends it.

    Two different warnings, because they are two different situations. The
    ordinary one is about price: the rule table is free and instant, the
    agents are neither, and with rules off there is one model call per file
    rather than one per leftover.

    The second is the case worth catching. With rules off *and* the agents
    unavailable there is nothing left to decide anything — every file is
    skipped and the run cannot accomplish anything at all. With rules on the
    same broken setup still files everything the table recognises, so this is
    specific to the combination, and it is worth saying out loud instead of
    printing an empty plan and letting someone work it out from the skip
    reasons.
    """
    offline = delegates_offline(manager)
    if offline is None:
        warn(
            "--no-rules: the rule table is off, so every file goes to the agents — "
            "one model call per file."
        )
        warn(
            "  Cost and time scale with the size of the folder; --max-delegations N "
            "caps how many files are sent."
        )
        return

    warn("--no-rules, but the delegate agents cannot run:")
    warn(f"  {offline}")
    warn(
        "  Rules are off, so there is nothing else to classify with: every file will "
        "be skipped"
    )
    warn(
        "  and this run will organise nothing. Drop --no-rules to still file what the "
        "rule table"
    )
    warn(f"  recognises, or fix the configuration: {_invocation()} config")


def cmd_scan(args: argparse.Namespace) -> int:
    use_rules = not args.no_rules
    manager = Manager(delegate_name="classifier")
    if not use_rules:
        warn_no_rules(manager)
    announce_scan(args.folder, quiet=args.json, use_rules=use_rules)
    plan = manager.scan(
        args.folder, max_delegations=args.max_delegations, use_rules=use_rules
    )

    if args.json:
        # plan.to_dict() carries proposed_categories, scheme_reason and
        # used_rules along with the items, so the JSON view shows the run's
        # vocabulary and whether the rule table was consulted at all.
        out(json.dumps(plan.to_dict(), indent=2))
        return 0

    print_plan(plan)
    print_apply_hint(plan)
    return 0


def cmd_apply(args: argparse.Namespace) -> int:
    approved_ids = None
    # `is not None`, not truthiness: `--only ""` must be an error, never a
    # silent "then apply everything".
    if args.only is not None:
        approved_ids = [i.strip() for i in args.only.split(",") if i.strip()]
        if not approved_ids:
            err("--only was given but listed no item ids")
            return 1

    manager = Manager(delegate_name="classifier")
    return run_apply(manager, args.plan_id, approved_ids, args.yes)


def cmd_run(args: argparse.Namespace) -> int:
    use_rules = not args.no_rules
    manager = Manager(delegate_name="classifier")
    if not use_rules:
        warn_no_rules(manager)
    announce_scan(args.folder, quiet=False, use_rules=use_rules)
    plan = manager.scan(
        args.folder, max_delegations=args.max_delegations, use_rules=use_rules
    )
    print_plan(plan)

    if not args.apply:
        out()
        out(STYLE.yellow(STYLE.bold("DRY RUN — nothing was moved.")))
        out(STYLE.dim("Re-run with --apply to carry this plan out, or apply it later:"))
        out(f"  {_invocation()} apply {plan.plan_id}")
        return 0

    if not plan.moves:
        out()
        out(STYLE.dim("Nothing to apply."))
        return 0

    return run_apply(manager, plan.plan_id, None, args.yes)


def cmd_log(args: argparse.Namespace) -> int:
    entries = read_log(limit=args.limit, plan_id=args.plan_id)

    label = f"UNDO LOG ({len(entries)} entries, newest first)"
    if args.plan_id:
        label = f"UNDO LOG for plan {args.plan_id} ({len(entries)} entries, newest first)"
    heading(label)

    if not entries:
        out(STYLE.dim("  no moves recorded yet"))
    for entry in entries:
        source = Path(str(entry.get("source", "")))
        destination = Path(str(entry.get("destination", "")))
        tag = f"{entry.get('decided_by', '?')} {entry.get('confidence', '')}".strip()
        out(f"  {STYLE.dim(str(entry.get('timestamp', '')))}  {source.name}")
        out(f"      {source} -> {destination}")
        out(STYLE.dim(f"      plan {entry.get('plan_id', '?')} · {tag}"))

    out()
    out(STYLE.dim(f"Log file: {undo_log_path()}"))
    return 0


def cmd_undo(args: argparse.Namespace) -> int:
    entries = read_log(limit=UNDO_LOG_SCAN_LIMIT, plan_id=args.plan_id)
    if not entries:
        err(f"no logged moves for plan {args.plan_id}")
        return 1

    # read_log is newest first, which is the right order to unwind in: a later
    # move can only have depended on an earlier one, never the other way round.
    reversals: list[tuple[dict, Path, Path]] = []
    blocked: list[tuple[dict, str]] = []
    reserved: set[Path] = set()

    for entry in entries:
        raw_destination = str(entry.get("destination") or "")
        raw_source = str(entry.get("source") or "")

        if not raw_destination or not raw_source:
            blocked.append((entry, "log entry is missing a source or destination"))
            continue

        destination = Path(raw_destination)
        source = Path(raw_source)

        if not destination.exists():
            blocked.append((entry, f"{destination} no longer exists"))
            continue
        if source.exists():
            blocked.append((entry, f"{source} is occupied again — refusing to overwrite"))
            continue

        # Two logged moves can name the same original path (a file moved,
        # restored by hand, then moved again). Never let the second one land on
        # top of the first.
        final = unique_destination(source, reserved)
        reserved.add(final)
        reversals.append((entry, destination, final))

    heading(f"UNDO plan {args.plan_id}")
    out(f"  {len(reversals)} moves to reverse, {len(blocked)} skipped")

    if reversals:
        heading("WOULD REVERSE" if not args.apply else "REVERSING")
        for _, destination, final in reversals:
            note = ""
            if final.name != destination.name:
                note = STYLE.dim("  (renamed — original name was taken)")
            out(f"  {destination}")
            out(f"      -> {final}{note}")

    if blocked:
        heading(f"SKIPPED ({len(blocked)})")
        for entry, reason in blocked:
            out(f"  {_entry_name(entry)}")
            out(STYLE.dim(f"      {reason}"))

    if not args.apply:
        out()
        out(STYLE.yellow(STYLE.bold("DRY RUN — nothing was moved.")))
        out(STYLE.dim("Re-run with --apply to carry the reversal out:"))
        out(f"  {_invocation()} undo {args.plan_id} --apply")
        return 0

    if not reversals:
        out()
        out(STYLE.dim("Nothing to reverse."))
        return 0

    if not confirm(f"Reverse {len(reversals)} moves? Type 'yes' to confirm:", args.yes):
        out(STYLE.dim("Not confirmed. Nothing was moved."))
        return 1

    reversed_count = 0
    failures: list[tuple[Path, str]] = []
    for entry, destination, final in reversals:
        try:
            final.parent.mkdir(parents=True, exist_ok=True)
            # `final` was proven free above, so this never clobbers anything.
            shutil.move(str(destination), str(final))
        except OSError as exc:
            failures.append((destination, str(exc)))
            continue

        reversed_count += 1
        # The reversal is itself a move, so it goes in the log too. Source and
        # destination are swapped relative to the entry being undone.
        record_move(
            plan_id=f"undo:{args.plan_id}",
            item_id=str(entry.get("item_id", "")),
            source=str(destination),
            destination=str(final),
            category=str(entry.get("category", "")),
            decided_by="undo",
            confidence="n/a",
        )

    heading("RESULT")
    out(f"  {STYLE.green(str(reversed_count))} moves reversed, {len(blocked)} skipped")
    if failures:
        out(f"  {STYLE.red(str(len(failures)))} failed:")
        for path, reason in failures:
            out(f"      {path}")
            out(STYLE.dim(f"          {reason}"))
    out(STYLE.dim(f"Reversals logged under plan id undo:{args.plan_id}"))
    return 1 if failures else 0


def cmd_config(args: argparse.Namespace) -> int:
    """Show which provider and model a run would actually use, and why.

    Doubles as the "why isn't it working" command: it reports each delegate's
    own availability probe, so an unset key, an unnamed model and a missing SDK
    all surface here instead of as an empty scan.
    """
    # `--path` deliberately does not load settings: the moment you most want
    # the path is when the file in it is broken.
    if args.path:
        out(str(config_path()))
        return 0

    try:
        settings = load_settings()
    except ConfigError as exc:
        err(str(exc))
        if args.json:
            # Still emit valid JSON, so a `| jq` pipeline sees the failure
            # rather than a parse error.
            out(json.dumps({"error": str(exc), "config_path": str(config_path())}, indent=2))
        return 1

    statuses = _agent_statuses()

    if args.json:
        payload = settings.to_dict()
        payload["config_file_exists"] = config_path().exists()
        # The name of the variable, never its contents.
        payload["api_key"] = {
            "env_var": API_KEY_ENV.get(settings.provider),
            "set": bool(os.environ.get(API_KEY_ENV.get(settings.provider, ""))),
        }
        payload["agents"] = {
            name: {"available": ok, "detail": detail}
            for name, (ok, detail) in statuses.items()
        }
        out(json.dumps(payload, indent=2))
        return 0

    print_config(settings, statuses)
    return 0


# --- argument parsing -------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cli.py",
        description=(
            "Organise a folder's top-level files with a manager agent and two "
            "delegates: a scheme proposer that decides this run's categories "
            "from the folder's contents, and a classifier that assigns files "
            "within them. Dry run by default — nothing moves without an "
            "explicit apply."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  cli.py scan ~/Downloads\n"
            "  cli.py scan ~/Documents --no-rules --max-delegations 10\n"
            "  cli.py apply 3f9c1a2b4d5e\n"
            "  cli.py run ~/Downloads --apply\n"
            "  cli.py log --limit 20\n"
            "  cli.py undo 3f9c1a2b4d5e --apply\n"
            "  cli.py config\n"
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    scan = subparsers.add_parser(
        "scan", help="build and print a move plan; moves nothing"
    )
    scan.add_argument("folder", help="folder to scan (top-level files only)")
    scan.add_argument(
        "--max-delegations",
        type=int,
        default=None,
        metavar="N",
        help="cap how many files are sent to the classifier agent",
    )
    scan.add_argument(
        "--no-rules",
        action="store_true",
        help=(
            "skip the extension/filename rule table and send every file to the "
            "agents — better grouping, but one model call per file"
        ),
    )
    scan.add_argument("--json", action="store_true", help="print the raw plan as JSON")
    scan.set_defaults(func=cmd_scan)

    apply_cmd = subparsers.add_parser(
        "apply", help="execute a previously built plan by id"
    )
    apply_cmd.add_argument("plan_id", help="plan id printed by scan or run")
    apply_cmd.add_argument(
        "--only",
        metavar="ID1,ID2",
        help="apply only these item ids (comma-separated); everything else is skipped",
    )
    apply_cmd.add_argument(
        "--yes",
        action="store_true",
        help="skip the interactive first-apply confirmation for a new folder",
    )
    apply_cmd.set_defaults(func=cmd_apply)

    run_cmd = subparsers.add_parser(
        "run", help="scan then optionally apply in one shot (dry run without --apply)"
    )
    run_cmd.add_argument("folder", help="folder to organise (top-level files only)")
    run_cmd.add_argument(
        "--apply", action="store_true", help="actually move the files"
    )
    run_cmd.add_argument(
        "--max-delegations",
        type=int,
        default=None,
        metavar="N",
        help="cap how many files are sent to the classifier agent",
    )
    run_cmd.add_argument(
        "--no-rules",
        action="store_true",
        help=(
            "skip the extension/filename rule table and send every file to the "
            "agents — better grouping, but one model call per file"
        ),
    )
    run_cmd.add_argument(
        "--yes",
        action="store_true",
        help="skip the interactive first-apply confirmation for a new folder",
    )
    run_cmd.set_defaults(func=cmd_run)

    log_cmd = subparsers.add_parser("log", help="print the undo log, newest first")
    log_cmd.add_argument(
        "--limit", type=int, default=50, metavar="N", help="how many entries to show"
    )
    log_cmd.add_argument("--plan-id", metavar="ID", help="only entries from this plan")
    log_cmd.set_defaults(func=cmd_log)

    undo_cmd = subparsers.add_parser(
        "undo", help="reverse a plan's logged moves (dry run without --apply)"
    )
    undo_cmd.add_argument("plan_id", help="plan id to reverse")
    undo_cmd.add_argument(
        "--apply", action="store_true", help="actually move the files back"
    )
    undo_cmd.add_argument(
        "--yes", action="store_true", help="skip the interactive confirmation"
    )
    undo_cmd.set_defaults(func=cmd_undo)

    config_cmd = subparsers.add_parser(
        "config",
        help="show the effective provider/model settings and where each came from",
        description=(
            "Print the provider, per-agent model and effort a run would use, "
            "which layer set each value (environment variable, config file, or "
            "built-in default), whether the provider's API key variable is set, "
            "and whether each delegate agent could actually run. Reads only — "
            "it never writes a config file and never prints a credential."
        ),
    )
    config_cmd.add_argument(
        "--json", action="store_true", help="print the settings as JSON"
    )
    config_cmd.add_argument(
        "--path",
        action="store_true",
        help="print the config file path and nothing else (for $EDITOR)",
    )
    config_cmd.set_defaults(func=cmd_config)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if DELEGATE_IMPORT_ERROR:
        # stderr, not stdout: a warning must not end up inside `--json`.
        print(
            STYLE.yellow(
                "warning: the delegate agents (classifier, scheme proposer) could not "
                f"be loaded ({DELEGATE_IMPORT_ERROR}); running rules-only"
            ),
            file=sys.stderr,
            flush=True,
        )

    try:
        return args.func(args)
    except UnsafePath as exc:
        err(str(exc))
        return 1
    except FileNotFoundError as exc:
        err(str(exc))
        return 1
    except ValueError as exc:  # unknown or malformed plan id
        err(str(exc))
        return 1
    except KeyboardInterrupt:
        out()
        err("interrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
