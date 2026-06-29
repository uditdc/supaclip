"""Shared text-LLM transport for the post-analysis passes.

The aggregate and summarize passes both send a text prompt to whichever
provider the analyzer is already using and expect JSON back. This is the one
place that knows how to talk to each provider.
"""

from __future__ import annotations

import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar

_DEFAULT_SYSTEM = "You are a careful video editor. Reply with JSON only."

_T = TypeVar("_T")


def retry_call(
    fn: Callable[[], _T], *, attempts: int = 4, base_delay: float = 2.0, label: str = "llm"
) -> _T:
    """Call `fn`, retrying transient failures with exponential backoff.

    Free-tier endpoints return 429/5xx under load; without backoff a single
    rate-limit aborts a hundreds-of-call extract. Retries every exception
    (provider error types vary) up to `attempts`, then re-raises the last one.
    """
    last: Exception | None = None
    for i in range(attempts):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            last = e
            if i == attempts - 1:
                break
            delay = base_delay * (2**i)
            print(
                f"  [{label}] call failed ({type(e).__name__}); retry "
                f"{i + 1}/{attempts - 1} in {delay:.0f}s",
                file=sys.stderr,
            )
            time.sleep(delay)
    assert last is not None
    raise last


@dataclass
class LLMConfig:
    model: str
    base_url: str
    api_key: str | None
    provider: str = "openai"  # "openai" (OpenAI-compat) or "google" (AI Studio)


def call_json(prompt: str, cfg: LLMConfig, *, system: str = _DEFAULT_SYSTEM) -> str:
    fn = _call_google if cfg.provider == "google" else _call_openai
    return retry_call(lambda: fn(prompt, cfg, system), label=f"llm:{cfg.provider}")


def _call_openai(prompt: str, cfg: LLMConfig, system: str) -> str:
    from openai import OpenAI

    client = OpenAI(base_url=cfg.base_url, api_key=cfg.api_key or "ollama")
    resp = client.chat.completions.create(
        model=cfg.model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        temperature=0.2,
    )
    return resp.choices[0].message.content or ""


def _call_google(prompt: str, cfg: LLMConfig, system: str) -> str:
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=cfg.api_key)
    resp = client.models.generate_content(
        model=normalize_google_model(cfg.model),
        contents=[system + "\n\n" + prompt],
        config=types.GenerateContentConfig(
            temperature=0.2,
            response_mime_type="application/json",
        ),
    )
    return getattr(resp, "text", "") or ""


def normalize_google_model(model: str) -> str:
    m = (model or "").strip()
    if m.startswith("google/"):
        m = m[len("google/"):]
    if m.endswith(":free"):
        m = m[: -len(":free")]
    return m or "gemini-2.0-flash"
