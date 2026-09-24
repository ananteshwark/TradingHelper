"""The one place igs calls a language model: the Claude API, through the official SDK.

Every request goes through `Assistant.create`, which
  * refuses unless the assistant is enabled (the UI's Settings page, or
    config/assistant.yaml);
  * refuses once today's estimated spend (IST) reaches the daily budget;
  * asks for adaptive thinking at the feature's effort, server-side refusal fallbacks and
    automatic prompt caching;
  * logs the call's tokens and estimated cost to llm_call.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from typing import Any

import anthropic
import psycopg

from igs.config import AssistantConfig, load_assistant
from igs.timeutil import IST, utc_now

FALLBACK_BETA = "server-side-fallback-2026-07-01"
# Models that accept `fallbacks: "default"`.
FALLBACK_MODELS = frozenset({"claude-opus-5", "claude-fable-5-1"})
CACHE_READ_FACTOR, CACHE_WRITE_FACTOR = 0.1, 1.25


class AssistantUnavailable(RuntimeError):
    """Off, not installed, no credentials, or over budget: nothing was sent."""


class BudgetExceeded(AssistantUnavailable):
    pass


class AssistantError(RuntimeError):
    """The API answered with an error, or declined the request."""


def uses_effort(model: str) -> bool:
    """Adaptive thinking and `effort` apply to the current families; Haiku 4.5 takes
    neither (it would reject them)."""
    return not model.startswith("claude-haiku")


def make_client() -> anthropic.Anthropic:
    """The SDK finds credentials itself when a request is made: ANTHROPIC_API_KEY (igs also
    reads it from .env), ANTHROPIC_AUTH_TOKEN, or an `ant auth login` profile."""
    return anthropic.Anthropic()


def check_connection(cfg: AssistantConfig, client: Any = None) -> str:
    """Confirm the credentials work and the configured model is available to them, through
    the Models API (no tokens are used). Returns a short description of the model."""
    client = client if client is not None else make_client()
    try:
        info = client.models.retrieve(cfg.model)
    except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as exc:
        raise AssistantUnavailable(f"the Claude API rejected the credentials: {exc}") from exc
    except anthropic.NotFoundError as exc:
        raise AssistantError(f"model {cfg.model} is not available with these credentials") \
            from exc
    except anthropic.APIConnectionError as exc:
        raise AssistantError(f"cannot reach the Claude API: {exc}") from exc
    except anthropic.APIStatusError as exc:
        raise AssistantError(f"the Claude API returned {exc.status_code}: {exc.message}") \
            from exc
    except anthropic.AnthropicError as exc:
        raise AssistantUnavailable(f"{exc}; save an API key first") from exc
    return f"{info.display_name} ({info.id})"


def text_of(message: Any) -> str:
    return "".join(b.text for b in message.content if b.type == "text").strip()


def echo_content(message: Any) -> list:
    """Assistant content to send back on the next turn. After a server-side fallback,
    blocks before the last fallback marker other than text belong to the declined model
    and are dropped; the marker itself is only an audit record."""
    content = list(message.content)
    marks = [i for i, b in enumerate(content) if b.type == "fallback"]
    if not marks:
        return content
    last = marks[-1]
    return [b for i, b in enumerate(content)
            if b.type != "fallback" and (i > last or b.type == "text")]


@dataclass
class Assistant:
    conn: psycopg.Connection
    cfg: AssistantConfig
    client: Any

    @classmethod
    def open(cls, conn: psycopg.Connection, cfg: AssistantConfig | None = None,
             client: Any = None) -> Assistant:
        cfg = cfg or load_assistant()
        if not cfg.enabled:
            raise AssistantUnavailable("the research assistant is off: enable it and save an "
                                       "API key on the UI's Settings page (or set `enabled: "
                                       "true` in config/assistant.yaml and ANTHROPIC_API_KEY "
                                       "in .env)")
        return cls(conn, cfg, client if client is not None else make_client())

    # ------------------------------------------------------------------ budget

    def spent_today(self) -> float:
        start = dt.datetime.combine(utc_now().astimezone(IST).date(), dt.time(), tzinfo=IST)
        with self.conn.cursor() as cur:
            cur.execute("select coalesce(sum(cost_usd), 0)::float8 from llm_call "
                        "where called_at >= %s", (start,))
            return cur.fetchone()[0]

    def cost(self, message: Any) -> float:
        """Estimated USD. With fallbacks, every attempt in usage.iterations is priced at its
        own model's rate (attempts declined before output are not billed, so this errs
        high)."""
        u = message.usage
        parts = [it for it in (getattr(u, "iterations", None) or [])
                 if getattr(it, "input_tokens", None) is not None] or [u]
        total = 0.0
        for p in parts:
            model = getattr(p, "model", None) or message.model
            price = self.cfg.prices_usd_per_mtok.get(model) or \
                self.cfg.prices_usd_per_mtok[self.cfg.model]
            total += ((p.input_tokens or 0) * price.input
                      + (p.output_tokens or 0) * price.output
                      + (p.cache_read_input_tokens or 0) * price.input * CACHE_READ_FACTOR
                      + (p.cache_creation_input_tokens or 0) * price.input * CACHE_WRITE_FACTOR
                      ) / 1e6
        return total

    def _log(self, feature: str, message: Any) -> None:
        u = message.usage
        with self.conn.cursor() as cur:
            cur.execute(
                """insert into llm_call (feature, model, input_tokens, output_tokens,
                       cache_read_tokens, cache_write_tokens, cost_usd, stop_reason, request_id)
                   values (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (feature, message.model, u.input_tokens or 0, u.output_tokens or 0,
                 u.cache_read_input_tokens or 0, u.cache_creation_input_tokens or 0,
                 round(self.cost(message), 6), message.stop_reason,
                 getattr(message, "_request_id", None)))
        if not self.conn.autocommit:
            self.conn.commit()

    # ------------------------------------------------------------------ requests

    def create(self, feature: str, *, system: str, messages: list[dict],
               tools: list[dict] | None = None, schema: dict | None = None) -> Any:
        f = getattr(self.cfg.features, feature)
        spent, budget = self.spent_today(), self.cfg.daily_budget_usd
        if spent >= budget:
            raise BudgetExceeded(f"today's assistant spend ${spent:.2f} has reached the daily "
                                 f"budget of ${budget:.2f} (see Settings)")
        model = self.cfg.model
        output_config: dict[str, Any] = {}
        kwargs: dict[str, Any] = {
            "model": model, "max_tokens": f.max_tokens,
            "system": [{"type": "text", "text": system}],
            "messages": messages,
            "cache_control": {"type": "ephemeral"},
        }
        if uses_effort(model):
            kwargs["thinking"] = {"type": "adaptive"}
            output_config["effort"] = f.effort
        if schema is not None:
            output_config["format"] = {"type": "json_schema", "schema": schema}
        if output_config:
            kwargs["output_config"] = output_config
        if tools:
            kwargs["tools"] = tools
        if self.cfg.fallbacks and model in FALLBACK_MODELS:
            kwargs["betas"] = [FALLBACK_BETA]
            kwargs["fallbacks"] = self.cfg.fallbacks
        try:
            message = self.client.beta.messages.create(**kwargs)
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as exc:
            raise AssistantUnavailable(f"the Claude API rejected the credentials: {exc}") \
                from exc
        except anthropic.RateLimitError as exc:
            raise AssistantError("the Claude API is rate-limiting requests; try again "
                                 "shortly") from exc
        except anthropic.APIStatusError as exc:
            raise AssistantError(f"the Claude API returned {exc.status_code}: {exc.message}") \
                from exc
        except anthropic.APIConnectionError as exc:
            raise AssistantError(f"cannot reach the Claude API: {exc}") from exc
        except anthropic.AnthropicError as exc:     # client-side, e.g. no credentials found
            raise AssistantUnavailable(f"{exc}; save an API key on the Settings page "
                                      "or set ANTHROPIC_API_KEY in .env") from exc
        self._log(feature, message)
        if message.stop_reason == "refusal":
            raise AssistantError("the model declined this request"
                                 + (" (and so did the fallback model)" if kwargs.get("fallbacks")
                                    else ""))
        return message

    def structured(self, feature: str, *, system: str, prompt: str,
                   schema: dict) -> tuple[Any, Any]:
        """One request constrained to `schema`: (parsed JSON, the message)."""
        message = self.create(feature, system=system,
                              messages=[{"role": "user", "content": prompt}], schema=schema)
        if message.stop_reason == "max_tokens":
            raise AssistantError("the answer was cut off at max_tokens; raise it in "
                                 "config/assistant.yaml")
        try:
            return json.loads(text_of(message)), message
        except json.JSONDecodeError as exc:
            raise AssistantError(f"the model's structured answer was not valid JSON: {exc}") \
                from exc
