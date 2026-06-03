"""OpenAI 兼容 Chat 模型（LangChain），替代 HelloAgentsLLM。"""

from __future__ import annotations

import os
import time
from typing import Any, Dict, List, Optional

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from ..config import get_settings

_chat_model: Optional[ChatOpenAI] = None


def get_chat_model() -> ChatOpenAI:
    global _chat_model
    if _chat_model is None:
        settings = get_settings()
        api_key = (
            os.getenv("LLM_API_KEY")
            or os.getenv("OPENAI_API_KEY")
            or "ollama"
        )
        base_url = os.getenv("LLM_BASE_URL") or None
        model = os.getenv("LLM_MODEL_ID") or "gpt-4o-mini"
        _chat_model = ChatOpenAI(
            model=model,
            api_key=api_key,
            base_url=base_url,
            temperature=0.35,
            max_tokens=int(os.getenv("LLM_MAX_TOKENS", "8192")),
            timeout=int(os.getenv("LLM_TIMEOUT", "120")),
        )
    return _chat_model


def _to_lc_messages(messages: List[Dict[str, str]]) -> list:
    out = []
    for m in messages:
        role = m.get("role", "user")
        content = m.get("content", "")
        if role == "system":
            out.append(SystemMessage(content=content))
        elif role == "assistant":
            out.append(AIMessage(content=content))
        else:
            out.append(HumanMessage(content=content))
    return out


def invoke_chat(
    messages: List[Dict[str, str]],
    *,
    temperature: float = 0.35,
    max_tokens: int = 8192,
    max_retries: int = 4,
) -> str:
    """同步调用，对 5xx/429/超时重试。"""
    llm = get_chat_model().bind(temperature=temperature, max_tokens=max_tokens)
    lc_messages = _to_lc_messages(messages)
    last_err: Optional[Exception] = None
    for attempt in range(max_retries):
        try:
            resp = llm.invoke(lc_messages)
            return (resp.content or "").strip()
        except Exception as e:
            last_err = e
            msg = str(e).lower()
            if any(
                x in msg
                for x in ("500", "502", "503", "429", "timeout", "timed out", "connection")
            ):
                time.sleep(1.0 * (2**attempt))
                continue
            raise
    if last_err:
        raise last_err
    raise RuntimeError("LLM 调用失败")
