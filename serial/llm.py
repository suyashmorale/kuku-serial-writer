"""Provider-agnostic LLM calls with structured output, prompt caching, cost caps and tracing.

Every call is attributed to (story, episode, step) so we can enforce a per-episode cost cap
and answer "where did the money and time go?" from the llm_calls table.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, TypeVar

from pydantic import BaseModel, ValidationError

from .config import Config, RoleConfig
from .schemas import strict_schema
from .store import Store

T = TypeVar("T", bound=BaseModel)


class BudgetExceeded(Exception):
    pass


class LLMError(Exception):
    pass


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


@dataclass
class RawResult:
    text: str
    usage: Usage
    stop_reason: str


# ---------------- providers ----------------

class AnthropicProvider:
    def __init__(self, cfg: Config):
        import anthropic

        self.client = anthropic.Anthropic(max_retries=3)
        self.fallbacks = bool(cfg.anthropic.get("refusal_fallbacks", False))

    def complete(self, rc: RoleConfig, system: str, user: str, schema: dict | None) -> RawResult:
        kwargs: dict[str, Any] = {
            "model": rc.model,
            "max_tokens": rc.max_tokens,
            # System prompt (instructions + story bible) is stable per story -> cache it.
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "messages": [{"role": "user", "content": user}],
        }
        oc: dict[str, Any] = {}
        if rc.effort:
            oc["effort"] = rc.effort
        if schema is not None:
            oc["format"] = {"type": "json_schema", "schema": schema}
        if oc:
            kwargs["output_config"] = oc
        if self.fallbacks:
            kwargs["extra_headers"] = {"anthropic-beta": "server-side-fallback-2026-07-01"}
            kwargs["extra_body"] = {"fallbacks": "default"}
        with self.client.messages.stream(**kwargs) as stream:
            resp = stream.get_final_message()
        text = "".join(b.text for b in resp.content if b.type == "text")
        u = resp.usage
        return RawResult(
            text=text,
            usage=Usage(u.input_tokens, u.output_tokens,
                        getattr(u, "cache_read_input_tokens", 0) or 0,
                        getattr(u, "cache_creation_input_tokens", 0) or 0),
            stop_reason=resp.stop_reason or "",
        )


class OpenAIProvider:
    def __init__(self, cfg: Config):
        import openai

        self.client = openai.OpenAI(max_retries=3)

    def complete(self, rc: RoleConfig, system: str, user: str, schema: dict | None,
                 cache_key: str | None = None) -> RawResult:
        kwargs: dict[str, Any] = {
            # Stable system prompt (instructions + bible) first: OpenAI caches prefixes >= 1024 tokens automatically.
            "model": rc.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "max_completion_tokens": rc.max_tokens,
        }
        if cache_key:
            kwargs["prompt_cache_key"] = cache_key  # routes same-prefix requests to the same cache
        if rc.effort:
            kwargs["reasoning_effort"] = rc.effort  # gpt-6 family: none|low|medium|high|xhigh|max
        if schema is not None:
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "output", "schema": schema, "strict": True},
            }
        resp = self.client.chat.completions.create(**kwargs)
        choice = resp.choices[0]
        u = resp.usage
        cached = getattr(getattr(u, "prompt_tokens_details", None), "cached_tokens", 0) or 0
        refused = bool(getattr(choice.message, "refusal", None)) or choice.finish_reason == "content_filter"
        return RawResult(
            text=choice.message.content or "",
            # completion_tokens already includes reasoning tokens, which bill at the output rate
            usage=Usage(u.prompt_tokens - cached, u.completion_tokens, cached, 0),
            stop_reason="refusal" if refused else {"length": "max_tokens"}.get(choice.finish_reason, "end_turn"),
        )


# ---------------- facade ----------------

class LLM:
    def __init__(self, cfg: Config, store: Store, fake_responder: Callable | None = None):
        self.cfg = cfg
        self.store = store
        self._providers: dict[str, Any] = {}
        self._fake = fake_responder
        self.trace_dir = cfg.path("traces")

    def _provider(self, name: str):
        if name not in self._providers:
            if name == "anthropic":
                self._providers[name] = AnthropicProvider(self.cfg)
            elif name == "openai":
                self._providers[name] = OpenAIProvider(self.cfg)
            elif name == "fake":
                from .fake import FakeProvider

                self._providers[name] = FakeProvider(self._fake)
            else:
                raise ValueError(f"unknown provider {name}")
        return self._providers[name]

    def _cost(self, model: str, u: Usage) -> float:
        p = self.cfg.pricing.get(model)
        if not p:
            return 0.0
        return (u.input_tokens * p["input"] + u.output_tokens * p["output"]
                + u.cache_read_tokens * p["cache_read"] + u.cache_write_tokens * p["cache_write"]) / 1e6

    def _check_budget(self, story_id: int | None, ep: int | None) -> None:
        if story_id is None:
            return
        cap = self.cfg.limits["story_cost_cap_usd"]
        if self.store.story_cost(story_id) >= cap:
            raise BudgetExceeded(f"story cost cap ${cap:.2f} reached")
        if ep is not None:
            ecap = self.cfg.limits["episode_cost_cap_usd"]
            spent = self.store.episode_cost(story_id, ep)
            if spent >= ecap:
                raise BudgetExceeded(f"episode {ep} cost cap ${ecap:.2f} reached (spent ${spent:.3f})")

    def _trace(self, story_id, ep, step, payload: dict) -> str:
        d = self.trace_dir / f"story{story_id}"
        d.mkdir(parents=True, exist_ok=True)
        f = d / f"{time.strftime('%Y%m%d-%H%M%S')}-ep{ep or 0:03d}-{step}-{time.time_ns() % 10**6}.json"
        f.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
        return str(f)

    def call(self, role: str, system: str, user: str, *, schema: type[T] | None = None,
             story_id: int | None = None, ep: int | None = None, step: str | None = None) -> T | str:
        """Run one call. Returns a validated model instance when `schema` is given, else text.
        Retries once on invalid JSON or truncation; raises BudgetExceeded before spending over cap."""
        rc = self.cfg.role(role)
        step = step or role
        provider = self._provider(rc.provider)
        json_schema = strict_schema(schema) if schema else None
        last_err = ""
        for attempt in (1, 2):
            self._check_budget(story_id, ep)
            t0 = time.time()
            status, err, parsed = "ok", None, None
            try:
                if rc.provider == "fake":
                    raw = provider.complete(role, system, user, schema)
                elif rc.provider == "openai":
                    raw = provider.complete(rc, system, user + last_err, json_schema,
                                            cache_key=f"serial-s{story_id}-{role}")
                else:
                    raw = provider.complete(rc, system, user + last_err, json_schema)
            except Exception as e:  # network/API errors already retried by the SDK
                self.store.log_call(story_id=story_id, ep=ep, step=step, role=role, provider=rc.provider,
                                    model=rc.model, latency_ms=int((time.time() - t0) * 1000), attempt=attempt,
                                    status="error", error=repr(e)[:500])
                raise LLMError(f"{role} call failed: {e}") from e
            latency = int((time.time() - t0) * 1000)
            if raw.stop_reason == "refusal":
                status, err = "refusal", "model declined"
            elif raw.stop_reason == "max_tokens":
                status, err = "truncated", "hit max_tokens"
            elif schema is not None:
                try:
                    parsed = schema.model_validate_json(raw.text)
                except ValidationError as e:
                    status, err = "invalid_json", str(e)[:500]
            trace = self._trace(story_id, ep, step, {
                "role": role, "provider": rc.provider, "model": rc.model, "attempt": attempt,
                "system": system, "user": user, "response": raw.text, "stop_reason": raw.stop_reason,
            })
            self.store.log_call(
                story_id=story_id, ep=ep, step=step, role=role, provider=rc.provider, model=rc.model,
                input_tokens=raw.usage.input_tokens, output_tokens=raw.usage.output_tokens,
                cache_read_tokens=raw.usage.cache_read_tokens, cache_write_tokens=raw.usage.cache_write_tokens,
                cost_usd=self._cost(rc.model, raw.usage), latency_ms=latency, attempt=attempt,
                status=status, error=err, trace_file=trace,
            )
            if status == "ok":
                return parsed if schema is not None else raw.text.strip()
            if status == "refusal":
                raise LLMError(f"{role}: model refused the request")
            last_err = f"\n\n(Your previous answer was rejected: {err}. Answer again, complete and valid.)"
        raise LLMError(f"{role}: failed after retry ({last_err.strip()})")
