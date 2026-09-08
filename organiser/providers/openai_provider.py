"""OpenAI provider, and by extension any OpenAI-protocol endpoint.

`provider: "openai"` talks to OpenAI. `provider: "compatible"` points the same
client at a `base_url`, which covers Ollama, LM Studio, vLLM and OpenRouter —
they all speak this protocol. That is what keeps the tool able to run without
anything leaving the machine.

Uses the Responses API's structured-output parse, which validates against a
Pydantic model the same way the Anthropic path does.
"""

import os

from pydantic import BaseModel

from ..config import Settings
from . import ProviderError


class _MissingKey(Exception):
    """Internal signal: no credential, so don't bother constructing a client."""


class OpenAIProvider:
    def __init__(self, settings: Settings):
        self._settings = settings
        self.name = settings.provider  # "openai" or "compatible"
        self._client = None
        self._error: str | None = None
        try:
            import openai

            self._openai = openai
            kwargs = {}
            if settings.base_url:
                kwargs["base_url"] = settings.base_url
            if settings.provider == "compatible" and not os.environ.get("OPENAI_API_KEY"):
                # Local servers usually ignore the key but the SDK insists on
                # one being present, so supply a placeholder rather than making
                # people invent an env var to run Ollama.
                kwargs["api_key"] = "not-needed"
            elif settings.provider == "openai" and not os.environ.get("OPENAI_API_KEY"):
                # Checked before constructing: the SDK raises its own long
                # message here, and a short actionable one is more use in the
                # status strip than a wall of SDK prose.
                raise _MissingKey
            self._client = openai.OpenAI(**kwargs)
        except _MissingKey:
            self._error = "no credentials found — set OPENAI_API_KEY"
        except ImportError as exc:
            self._error = f"the `openai` package is not installed ({exc})"
        except Exception as exc:  # noqa: BLE001 - surfaced, not swallowed
            self._error = str(exc)

    def available(self) -> tuple[bool, str]:
        if self._client is None:
            return False, self._error or "OpenAI client could not be created"
        if self._settings.provider == "compatible":
            # No credential to check — reachability is only knowable by trying,
            # and available() must not make a network call.
            return True, f"using {self._settings.base_url}"
        if getattr(self._client, "api_key", None):
            return True, "ready"
        return False, "no credentials found — set OPENAI_API_KEY"

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
            raise ProviderError(self._error or "OpenAI client unavailable")

        openai = self._openai
        kwargs = {
            "model": model,
            "instructions": system,
            "input": prompt,
            "text_format": schema,
            "max_output_tokens": max_tokens,
        }
        if effort:
            # Only reasoning models accept this; a model that doesn't returns a
            # 400, which is handled below with an actionable message.
            kwargs["reasoning"] = {"effort": effort}

        try:
            response = self._client.responses.parse(**kwargs)
        except openai.AuthenticationError as exc:
            raise ProviderError(f"authentication failed: {exc}") from exc
        except openai.PermissionDeniedError as exc:
            raise ProviderError(f"permission denied: {exc}") from exc
        except openai.NotFoundError as exc:
            raise ProviderError(f"model or endpoint not found: {exc}") from exc
        except openai.RateLimitError as exc:
            raise ProviderError(f"rate limited: {exc}") from exc
        except openai.BadRequestError as exc:
            raise ProviderError(
                f"bad request — check the model id, and note that only reasoning "
                f"models accept an effort setting: {exc}"
            ) from exc
        except openai.APIStatusError as exc:
            raise ProviderError(f"API error {exc.status_code}: {exc}") from exc
        except openai.APIConnectionError as exc:
            detail = (
                f" (is anything serving {self._settings.base_url}?)"
                if self._settings.base_url
                else ""
            )
            raise ProviderError(f"could not reach the API{detail}: {exc}") from exc

        parsed = response.output_parsed
        if parsed is None:
            raise ProviderError(self._explain_empty(response))
        return parsed

    @staticmethod
    def _explain_empty(response) -> str:
        """Say *why* nothing parsed — a refusal and a truncation look identical
        from the caller's side otherwise."""
        for item in getattr(response, "output", []) or []:
            for content in getattr(item, "content", []) or []:
                if getattr(content, "type", None) == "refusal":
                    return f"the model declined this request: {content.refusal}"
        if getattr(response, "status", None) == "incomplete":
            details = getattr(response, "incomplete_details", None)
            reason = getattr(details, "reason", None) or "unknown reason"
            return f"the response was cut short ({reason})"
        return "no structured answer came back"
