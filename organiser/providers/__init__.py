"""Model providers: the only place in the codebase that talks to an SDK.

Both delegate agents ask a provider for one thing — "given this system prompt
and this prompt, return an object matching this schema" — so neither of them
imports an SDK, names a model, or knows which company is answering. Swapping
provider is a config value, and adding one is a single new module here.

The `Provider` protocol mirrors the `Delegate` protocol one layer down: the
agents are pluggable in the manager, and the providers are pluggable in the
agents.
"""

from typing import Protocol, runtime_checkable

from pydantic import BaseModel

from ..config import Settings


class ProviderError(RuntimeError):
    """A provider could not produce an answer.

    Agents translate this into `DelegateUnavailable`, which is what makes the
    manager leave files alone instead of guessing. Anything a provider raises
    that isn't this is a bug in the provider.
    """


@runtime_checkable
class Provider(Protocol):
    name: str

    def available(self) -> tuple[bool, str]:
        """(is_available, human-readable detail). Must not make a network call
        — it runs before every scan just to decide whether to bother."""

    def complete_json(
        self,
        *,
        model: str,
        system: str,
        prompt: str,
        schema: type[BaseModel],
        effort: str | None = None,
        max_tokens: int = 4096,
    ) -> BaseModel:
        """Return an instance of `schema`, or raise ProviderError."""


def get_provider(settings: Settings) -> Provider:
    """Build the provider named by `settings`.

    Imports are done lazily and per-provider so that having only one SDK
    installed is never an error for the provider you aren't using.
    """
    if settings.provider == "anthropic":
        from .anthropic_provider import AnthropicProvider

        return AnthropicProvider(settings)

    if settings.provider in ("openai", "compatible"):
        from .openai_provider import OpenAIProvider

        return OpenAIProvider(settings)

    raise ProviderError(f"unknown provider {settings.provider!r}")
