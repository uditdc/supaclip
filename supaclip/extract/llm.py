"""Shared text-LLM transport for the post-analysis passes.

The aggregate and summarize passes both send a text prompt to whichever
provider the analyzer is already using and expect JSON back. This is the one
place that knows how to talk to each provider.
"""

from __future__ import annotations

from dataclasses import dataclass

_DEFAULT_SYSTEM = "You are a careful video editor. Reply with JSON only."


@dataclass
class LLMConfig:
    model: str
    base_url: str
    api_key: str | None
    provider: str = "openai"  # "openai" (OpenAI-compat) or "google" (AI Studio)


def call_json(prompt: str, cfg: LLMConfig, *, system: str = _DEFAULT_SYSTEM) -> str:
    if cfg.provider == "google":
        return _call_google(prompt, cfg, system)
    return _call_openai(prompt, cfg, system)


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
