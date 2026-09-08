"""The scheme proposer: a second delegate agent that decides the vocabulary.

Where the classifier answers "which of these categories does this file belong
to", this agent answers "what categories does this folder actually need". It
sees every rule-unplaceable file in one request, so the grouping it proposes
fits the corpus rather than being invented one file at a time.

Same rules as every delegate: stateless, read-only, decision-only. It receives
filenames and snippets the manager already read, never a path, and it never
touches the filesystem.
"""

from pydantic import BaseModel, Field

from .categories import FALLBACK_CATEGORY, dedupe_categories, normalise_category
from .config import ConfigError, load_settings
from .delegates import (
    DelegateUnavailable,
    ProposedCategory,
    SchemeProposal,
    SchemeRequest,
    register,
)
from .providers import ProviderError, get_provider

# A folder of 300 loose files shouldn't become a 300-category scheme, and the
# proposal has to stay small enough to be worth reviewing.
MAX_CATEGORIES = 12
# Cap what one request carries; snippets are trimmed hard because the proposer
# needs the gist of many files, not the detail of a few.
MAX_FILES_IN_REQUEST = 120
SNIPPET_CHARS = 400

SYSTEM_PROMPT = """You design the folder scheme for a local file organiser.

You are given every file in one folder that a rule-based pass could not \
classify, with a short content snippet where the file is readable text. \
Propose the set of categories this specific folder actually needs.

Rules:
- Propose between 1 and {max_categories} categories. Fewer, broader, \
well-populated categories beat many thin ones.
- Every category must earn its place: at least two files should plausibly \
belong to it, unless a single file is genuinely unlike everything else.
- Reuse any category in "already in use" verbatim when it fits. Those are \
folders that already exist or that the rule pass is already filling — \
inventing a synonym for one of them (Invoices when Bills exists) is the main \
failure mode here, so prefer the existing name every time.
- Name categories the way a person names folders: short noun phrases, \
Title Case, no punctuation beyond & - _ (e.g. "Tenancy", "Lecture Notes", \
"Tax Returns").
- Do not propose a catch-all such as "Other" or "Misc" — the organiser adds \
its own fallback for files that fit nothing.
- Base categories on what the files *are*, using the content snippets, not \
merely on their file extension.
- Give each category a one-line description of what belongs in it."""


class _Category(BaseModel):
    name: str = Field(description="Short Title Case folder name.")
    description: str = Field(description="One line: what belongs in this category.")


class _Scheme(BaseModel):
    categories: list[_Category]
    reason: str = Field(description="One line on how you grouped this folder.")


def _build_prompt(request: SchemeRequest) -> str:
    lines: list[str] = []
    if request.existing_categories:
        lines.append("Already in use (reuse verbatim where they fit):")
        lines.append(", ".join(request.existing_categories))
        lines.append("")

    shown = request.files[:MAX_FILES_IN_REQUEST]
    lines.append(f"Files needing a category ({len(shown)}):")
    for summary in shown:
        lines.append(f"\n- {summary.filename} ({summary.size_bytes} bytes)")
        if summary.snippet_is_binary:
            lines.append("  content: binary, judge from the name/extension")
        elif summary.snippet:
            snippet = " ".join(summary.snippet[:SNIPPET_CHARS].split())
            lines.append(f"  snippet: {snippet}")
        else:
            lines.append("  content: unreadable, judge from the name/extension")

    if len(request.files) > len(shown):
        lines.append(
            f"\n(plus {len(request.files) - len(shown)} more files not shown; "
            "propose a scheme that covers the folder as a whole)"
        )
    return "\n".join(lines)


class SchemeProposerAgent:
    """Delegate implementation. One `propose()` call per run."""

    name = "scheme"

    AGENT = "scheme"

    def __init__(self, provider=None):
        # An injected provider is for tests; normally it is resolved per call
        # from config so editing config.json takes effect without a restart.
        self._provider = provider

    def _resolve(self):
        """Return (provider, model, effort) or raise DelegateUnavailable."""
        try:
            settings = load_settings()
        except ConfigError as exc:
            raise DelegateUnavailable(f"bad configuration: {exc}") from exc

        agent = settings.for_agent(self.AGENT)
        if not agent.model:
            raise DelegateUnavailable(
                f"no model configured for the {self.AGENT} proposer with provider "
                f"{settings.provider!r} — set FILE_ORGANISER_SCHEME_MODEL or "
                f'"{self.AGENT}": {{"model": ...}} in {settings.config_path}'
            )

        provider = self._provider
        if provider is None:
            try:
                provider = get_provider(settings)
            except ProviderError as exc:
                raise DelegateUnavailable(str(exc)) from exc
        return provider, agent.model, agent.effort

    def available(self) -> tuple[bool, str]:
        try:
            provider, model, _ = self._resolve()
        except DelegateUnavailable as exc:
            return False, str(exc)

        ok, detail = provider.available()
        if not ok:
            return False, detail
        return True, f"{provider.name}/{model} — {detail}"

    def propose(self, request: SchemeRequest) -> SchemeProposal:
        if not request.files:
            return SchemeProposal(categories=[], reason="nothing to group", proposed_by=self.name)

        provider, model, effort = self._resolve()

        try:
            parsed = provider.complete_json(
                model=model,
                system=SYSTEM_PROMPT.format(max_categories=MAX_CATEGORIES),
                prompt=_build_prompt(request),
                schema=_Scheme,
                # Designing a grouping is the one genuinely judgement-heavy
                # call in the system, and it happens once per run — worth more
                # thinking than the per-file assignments.
                effort=effort,
                max_tokens=4096,
            )
        except ProviderError as exc:
            raise DelegateUnavailable(str(exc)) from exc

        # Every proposed name becomes a folder, so it is sanitised here and
        # duplicates are collapsed before anything downstream sees it.
        described: dict[str, str] = {}
        for entry in parsed.categories:
            safe = normalise_category(entry.name)
            if safe is not None:
                described.setdefault(safe.casefold(), entry.description.strip())

        names = dedupe_categories([c.name for c in parsed.categories])
        names = [n for n in names if n.casefold() != FALLBACK_CATEGORY.casefold()]
        names = names[:MAX_CATEGORIES]

        if not names:
            raise DelegateUnavailable("the proposed scheme had no usable category names")

        existing = {c.casefold() for c in request.existing_categories}
        categories = [
            ProposedCategory(
                name=name,
                description=described.get(name.casefold(), ""),
                is_new=name.casefold() not in existing,
            )
            for name in names
        ]

        return SchemeProposal(
            categories=categories,
            reason=parsed.reason.strip(),
            proposed_by=self.name,
        )


register(SchemeProposerAgent())
