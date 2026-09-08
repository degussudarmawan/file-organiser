"""The contract between the manager and the agents it delegates to.

The manager never imports a concrete agent. It asks this registry for a
delegate by name and talks to it through `Delegate` only. That is what makes
the manager/classifier split real rather than decorative: a dedupe agent or a
rename-suggestion agent registers itself here and the manager's control flow
does not change.

Delegates are decision-only. Nothing in this module — and nothing any delegate
implements — is allowed to touch the filesystem. Moving files is the manager's
job alone.
"""

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class FileSummary:
    """One file as a delegate sees it: never a path it could write to."""

    filename: str
    snippet: str | None
    snippet_is_binary: bool
    size_bytes: int


@dataclass(frozen=True)
class DelegateRequest:
    """Everything a delegate is given about one file. Read-only, no paths to
    write to — only the filename, a snippet, and the allowed vocabulary."""

    filename: str
    snippet: str | None
    snippet_is_binary: bool
    size_bytes: int
    allowed_categories: list[str]


@dataclass(frozen=True)
class SchemeRequest:
    """The corpus-level view handed to a scheme proposer.

    It sees every file the rules could not place *at once*, which is what lets
    it propose a grouping that fits the folder as a whole. Naming each file in
    isolation is what produces Invoices/Bills/Receipts for one concept.
    """

    files: list[FileSummary]
    # Categories already in play this run — folder names that exist in the
    # target and categories the rule pass emitted. The proposer is asked to
    # reuse these where they fit rather than coin a synonym.
    existing_categories: list[str]


@dataclass(frozen=True)
class ProposedCategory:
    name: str
    description: str
    is_new: bool = True


@dataclass(frozen=True)
class SchemeProposal:
    """A vocabulary for one run, proposed from the folder's actual contents."""

    categories: list[ProposedCategory]
    reason: str = ""
    proposed_by: str = ""

    def names(self) -> list[str]:
        return [c.name for c in self.categories]


@dataclass(frozen=True)
class DelegateDecision:
    """What a delegate hands back.

    `category` must be one of the allowed categories it was given, or **None**
    to mean "no opinion — leave this file where it is".

    That None case matters. Before it existed, a delegate that was working
    fine but simply unsure about one file had no way to say so: the only route
    to "leave it alone" was raising, which the manager records as the delegate
    having *errored*. So the honest answer was indistinguishable from a fault,
    and the alternative was to name a category anyway — which is the one thing
    this system is not supposed to do.

    `confidence` is high/medium/low, or n/a alongside a None category.
    """

    category: str | None
    confidence: str
    reason: str
    decided_by: str


class DelegateUnavailable(RuntimeError):
    """Raised when a delegate cannot run at all — no API key, network down,
    model error. The manager treats this as 'leave the file alone', never as
    'guess something'."""


@runtime_checkable
class Delegate(Protocol):
    name: str

    def available(self) -> tuple[bool, str]:
        """(is_available, human-readable reason). Checked before a run so the
        manager can degrade gracefully instead of failing mid-scan."""

    def decide(self, request: DelegateRequest) -> DelegateDecision:
        """Classify one file. Raise DelegateUnavailable if it cannot."""


@runtime_checkable
class SchemeProposer(Protocol):
    """A delegate that proposes the run's vocabulary instead of using one.

    Deliberately a separate protocol from `Delegate`: the classifier stays a
    stateless per-file decision agent that is handed allowed categories, and
    this agent is what produces that list. Either can be swapped without
    touching the other or the manager's control flow.
    """

    name: str

    def available(self) -> tuple[bool, str]: ...

    def propose(self, request: SchemeRequest) -> SchemeProposal:
        """Propose categories for this run. Raise DelegateUnavailable if it
        cannot — the manager then leaves the unplaceable files alone."""


_REGISTRY: dict[str, Delegate] = {}


def register(delegate: Delegate) -> None:
    _REGISTRY[delegate.name] = delegate


def get(name: str) -> Delegate | None:
    return _REGISTRY.get(name)


def registered_names() -> list[str]:
    return sorted(_REGISTRY)
