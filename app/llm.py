"""Velona LLM via its OpenAI-compatible endpoint."""
from __future__ import annotations

from typing import TypeVar

from langchain_openai import ChatOpenAI
from pydantic import BaseModel

from app.config import AppConfig

T = TypeVar("T", bound=BaseModel)


def get_llm(cfg: AppConfig) -> ChatOpenAI:
    llm = cfg.llm
    missing = [k for k in ("base_url", "api_key", "model") if not getattr(llm, k)]
    if missing:
        raise RuntimeError(f"LLM settings missing in .env: {', '.join('VELONA_' + m.upper() for m in missing)}")
    return ChatOpenAI(
        base_url=llm.base_url,
        api_key=llm.api_key,
        model=llm.model,
        temperature=llm.temperature,
        timeout=60,
        max_retries=2,
    )


def structured(cfg: AppConfig, schema: type[T]):
    """LLM runnable that returns an instance of `schema`."""
    return get_llm(cfg).with_structured_output(schema, method=cfg.llm.structured_output_method)
