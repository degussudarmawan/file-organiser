"""The Classifier delegate agent.

Single responsibility, stateless, read-only. In: a filename, an optional
content snippet, and the list of allowed categories. Out: one category from
that list, a confidence level, and a one-line reason. It holds no state
between calls and never touches the filesystem — it is handed a snippet, not
a path.

If it cannot run (no credentials, network down, API error) it raises
DelegateUnavailable and the manager leaves the file alone rather than
guessing.
"""

from typing import Literal

from pydantic import BaseModel, Field

from .categories import CONFIDENCE_LEVELS, FALLBACK_CATEGORY
from .config import ConfigError, load_settings
from .delegates import (
    Delegate,
    DelegateDecision,
    DelegateRequest,
    DelegateUnavailable,
    register,
)
from .providers import ProviderError, get_provider

SYSTEM_PROMPT = """You are the classifier agent in a local file-organising \
system. You are given one file at a time — its name, its size, and a short \
snippet of its content when the file is readable text — and the list of \
categories allowed for this folder. Those categories were proposed for this \
specific folder from its actual contents, so they should fit well; treat them \
as the complete vocabulary.

Pick exactly one category from the allowed list. Rules:
- Use only categories from the allowed list. Never invent one.
- The filename is usually the strongest signal; the snippet is there to \
resolve cases where the name is uninformative or misleading.
- When the snippet is unavailable because the file is binary, decide from the \
name and extension alone and say so in your reason.
- Report confidence honestly. "high" means you would be surprised to be \
wrong. "medium" means the category is a reasonable read but another is \
plausible. "low" means you are essentially guessing — a human should look.
- Prefer "Other" with low confidence over forcing a file into a category that \
does not fit. A human reviews everything you are unsure about, so an honest \
"Other" is cheap and a confident wrong answer is expensive.
- The reason must be one short line explaining the signal you used."""


class Classification(BaseModel):
    """Schema the model's answer is validated against."""

    category: str = Field(description="Exactly one category from the allowed list.")
    confidence: Literal["high", "medium", "low"]
    reason: str = Field(description="One short line explaining the decision.")


def _build_prompt(request: DelegateRequest) -> str:
    lines = [
        f"Filename: {request.filename}",
        f"Size: {request.size_bytes} bytes",
        "Allowed categories: " + ", ".join(request.allowed_categories),
        "",
    ]
    if request.snippet_is_binary:
        lines.append("Content: binary, decide from name/extension")
    elif request.snippet:
        lines.append("Content snippet (truncated):")
        lines.append("---")
        lines.append(request.snippet)
        lines.append("---")
    else:
        lines.append("Content: unreadable, decide from name/extension")
    return "\n".join(lines)


class ClassifierAgent:
    """Delegate implementation. Construct once per run; holds a client, not
    conversation state — each `decide()` call is independent."""

    name = "classifier"

    AGENT = "classifier"

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
                f"no model configured for the {self.AGENT} with provider "
                f"{settings.provider!r} — set FILE_ORGANISER_CLASSIFIER_MODEL or "
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
        """Report honestly whether a classification would actually work.

        Checked before every scan, and it must not make a network call. The
        SDKs construct clients happily with no credentials and only fail at
        request time, so "a client exists" is not the question — a resolvable
        credential and a configured model are. Getting this wrong advertises
        the agent as available and then silently skips every delegated file.
        """
        try:
            provider, model, _ = self._resolve()
        except DelegateUnavailable as exc:
            return False, str(exc)

        ok, detail = provider.available()
        if not ok:
            return False, detail
        return True, f"{provider.name}/{model} — {detail}"

    def decide(self, request: DelegateRequest) -> DelegateDecision:
        provider, model, effort = self._resolve()

        try:
            parsed = provider.complete_json(
                model=model,
                system=SYSTEM_PROMPT,
                prompt=_build_prompt(request),
                schema=Classification,
                # Classification is a small judgement call, not a research
                # task — low effort keeps the per-file cost and latency down.
                effort=effort,
                max_tokens=1024,
            )
        except ProviderError as exc:
            raise DelegateUnavailable(str(exc)) from exc

        category = parsed.category
        confidence = parsed.confidence
        reason = parsed.reason.strip()

        # Defence in depth: the allowed list is now proposed per run, so the
        # request is the only authority on what counts. A category outside it
        # is treated as "could not place it", never as a new folder — coining
        # categories is the scheme proposer's job, not this agent's.
        if category not in request.allowed_categories:
            category = FALLBACK_CATEGORY
            confidence = "low"
            reason = f"returned an unknown category; filed as {FALLBACK_CATEGORY}"

        if confidence not in CONFIDENCE_LEVELS:
            confidence = "low"

        return DelegateDecision(
            category=category,
            confidence=confidence,
            reason=reason,
            decided_by=self.name,
        )


register(ClassifierAgent())
