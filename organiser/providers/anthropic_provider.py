"""Anthropic provider, using the official `anthropic` SDK."""

import os
from pathlib import Path

from pydantic import BaseModel

from ..config import Settings
from . import ProviderError

# `ant auth login` stores credential profiles here; the SDK finds them with no
# environment variable set.
_PROFILE_DIR = Path.home() / ".config" / "anthropic"


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, settings: Settings):
        self._settings = settings
        self._client = None
        self._error: str | None = None
        try:
            import anthropic

            self._anthropic = anthropic
            kwargs = {}
            if settings.base_url:
                kwargs["base_url"] = settings.base_url
            self._client = anthropic.Anthropic(**kwargs)
        except ImportError as exc:
            self._error = f"the `anthropic` package is not installed ({exc})"
        except Exception as exc:  # noqa: BLE001 - surfaced, not swallowed
            self._error = str(exc)

    def available(self) -> tuple[bool, str]:
        """Checked without a network call, so it must not confuse 'constructed
        a client' with 'has a usable credential' — the SDK builds happily with
        neither and only fails at request time."""
        if self._client is None:
            return False, self._error or "Anthropic client could not be created"
        if self._client.api_key or getattr(self._client, "auth_token", None):
            return True, "ready"
        if os.environ.get("ANTHROPIC_PROFILE") or _PROFILE_DIR.exists():
            return True, "using an `ant auth login` profile"
        return False, (
            "no credentials found — set ANTHROPIC_API_KEY or run `ant auth login`"
        )

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
        if self._client is None:
            raise ProviderError(self._error or "Anthropic client unavailable")

        anthropic = self._anthropic
        kwargs = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": prompt}],
            "output_format": schema,
        }
        if effort:
            kwargs["output_config"] = {"effort": effort}

        try:
            response = self._client.messages.parse(**kwargs)
        except anthropic.AuthenticationError as exc:
            raise ProviderError(f"authentication failed: {exc}") from exc
        except anthropic.PermissionDeniedError as exc:
            raise ProviderError(f"permission denied: {exc}") from exc
        except anthropic.NotFoundError as exc:
            raise ProviderError(f"model or endpoint not found: {exc}") from exc
        except anthropic.RateLimitError as exc:
            raise ProviderError(f"rate limited: {exc}") from exc
        except anthropic.BadRequestError as exc:
            raise ProviderError(f"bad request (check the model id): {exc}") from exc
        except anthropic.APIStatusError as exc:
            raise ProviderError(f"API error {exc.status_code}: {exc}") from exc
        except anthropic.APIConnectionError as exc:
            raise ProviderError(f"could not reach the API: {exc}") from exc

        if getattr(response, "stop_reason", None) == "refusal":
            raise ProviderError("the model declined this request")

        parsed = response.parsed_output
        if parsed is None:
            raise ProviderError("no structured answer came back")
        return parsed
