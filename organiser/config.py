"""Runtime settings: which provider, and which model each agent uses.

Resolution order, highest first:

1. environment variables  (per-run override, nothing written down)
2. `config.json` in the state dir  (your saved preference)
3. built-in defaults

Every resolved value remembers where it came from, so `cli.py config` can show
you not just the effective setting but which layer won. That is the thing you
actually want when a run used a model you didn't expect.

API keys are deliberately NOT part of this file. They stay in the provider's
own environment variable (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`) so a secret
never lands on disk in the state dir.
"""

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from .paths import state_dir

CONFIG_NAME = "config.json"

PROVIDERS = ("anthropic", "openai", "compatible")

# Only Anthropic gets a built-in default model. For OpenAI and OpenAI-compatible
# endpoints the model must be named explicitly: model line-ups change often and
# a wrong-but-plausible default fails at request time with a confusing error,
# where an unset one fails immediately with an actionable message.
DEFAULT_MODELS = {"anthropic": "claude-opus-5"}

# The scheme proposer makes one judgement call per run and deserves more
# thinking; the classifier fires once per file and is where cost accumulates.
DEFAULT_EFFORT = {"scheme": "medium", "classifier": "low"}


class ConfigError(ValueError):
    """Raised for a config that cannot be used as written."""


@dataclass
class AgentSettings:
    """Per-agent model settings. `model` may be None if nothing supplied one."""

    model: str | None
    effort: str | None


@dataclass
class Settings:
    provider: str
    base_url: str | None
    scheme: AgentSettings
    classifier: AgentSettings
    # field name -> "environment" | "config file" | "default"
    sources: dict[str, str] = field(default_factory=dict)
    config_path: str = ""

    def for_agent(self, agent: str) -> AgentSettings:
        return self.scheme if agent == "scheme" else self.classifier

    def to_dict(self) -> dict:
        return {
            "provider": self.provider,
            "base_url": self.base_url,
            "scheme": {"model": self.scheme.model, "effort": self.scheme.effort},
            "classifier": {
                "model": self.classifier.model,
                "effort": self.classifier.effort,
            },
            "sources": self.sources,
            "config_path": self.config_path,
        }


def config_path() -> Path:
    return state_dir() / CONFIG_NAME


def read_config_file() -> dict:
    """Return the saved config, or {} if there isn't a usable one."""
    path = config_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        raise ConfigError(f"{path} is not readable JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must contain a JSON object")
    return data


def _resolve(env_name: str, file_value, default):
    """Return (value, source) for one setting."""
    env_value = os.environ.get(env_name)
    if env_value not in (None, ""):
        return env_value, "environment"
    if file_value not in (None, ""):
        return file_value, "config file"
    return default, "default"


def _agent_settings(
    agent: str, data: dict, provider: str, sources: dict
) -> AgentSettings:
    section = data.get(agent) or {}
    if not isinstance(section, dict):
        raise ConfigError(f"{agent!r} in the config file must be a JSON object")

    # A model name in the config file belongs to the provider that file names.
    # If the effective provider was switched (usually by an env var), those
    # model names do not apply — carrying them over would quietly send, say,
    # an OpenAI model id to Anthropic and fail with a confusing 404.
    file_provider = str(data.get("provider") or "").strip().lower()
    file_model = section.get("model")
    if file_provider and file_provider != provider:
        file_model = None

    prefix = f"FILE_ORGANISER_{agent.upper()}"
    model, model_source = _resolve(
        f"{prefix}_MODEL", file_model, DEFAULT_MODELS.get(provider)
    )
    effort, effort_source = _resolve(
        f"{prefix}_EFFORT", section.get("effort"), DEFAULT_EFFORT[agent]
    )
    sources[f"{agent}.model"] = model_source
    sources[f"{agent}.effort"] = effort_source
    return AgentSettings(model=model, effort=effort)


def load_settings() -> Settings:
    """Resolve the effective settings. Read fresh each time, so editing the
    config file takes effect without restarting the server."""
    data = read_config_file()
    sources: dict[str, str] = {}

    provider, provider_source = _resolve(
        "FILE_ORGANISER_PROVIDER", data.get("provider"), "anthropic"
    )
    provider = str(provider).strip().lower()
    if provider not in PROVIDERS:
        raise ConfigError(
            f"unknown provider {provider!r}; expected one of {', '.join(PROVIDERS)}"
        )
    sources["provider"] = provider_source

    base_url, base_url_source = _resolve(
        "FILE_ORGANISER_BASE_URL", data.get("base_url"), None
    )
    sources["base_url"] = base_url_source

    if provider == "compatible" and not base_url:
        raise ConfigError(
            "provider 'compatible' needs a base_url — set FILE_ORGANISER_BASE_URL "
            "or \"base_url\" in the config file (e.g. http://localhost:11434/v1 "
            "for Ollama)"
        )

    settings = Settings(
        provider=provider,
        base_url=base_url,
        scheme=_agent_settings("scheme", data, provider, sources),
        classifier=_agent_settings("classifier", data, provider, sources),
        sources=sources,
        config_path=str(config_path()),
    )
    return settings
