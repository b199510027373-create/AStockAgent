from __future__ import annotations

import os

import httpx
from openai import OpenAI

from .config import DEEPSEEK_API_KEY, DEEPSEEK_BASE

_CLIENT: OpenAI | None = None
_MODE: str | None = None
_PROXY = os.getenv("ASTOCK_LLM_PROXY", "http://127.0.0.1:7890")


def _build(mode: str) -> OpenAI:
    if not DEEPSEEK_API_KEY:
        raise RuntimeError("未配置 DEEPSEEK_API_KEY")
    if mode == "proxy":
        http_client = httpx.Client(proxy=_PROXY, timeout=60)
    else:
        # 绕过 Windows 系统代理，避免 Clash 等代理导致的 SSL EOF
        http_client = httpx.Client(trust_env=False, timeout=60)
    return OpenAI(
        api_key=DEEPSEEK_API_KEY,
        base_url=DEEPSEEK_BASE,
        http_client=http_client,
        max_retries=1,
    )


def get_client() -> OpenAI:
    global _CLIENT, _MODE
    if _CLIENT is None:
        for mode in ("direct", "proxy"):
            try:
                client = _build(mode)
                # 轻量探活
                client.models.list()
                _CLIENT, _MODE = client, mode
                break
            except Exception:
                continue
        if _CLIENT is None:
            raise RuntimeError("DeepSeek 客户端初始化失败（直连/代理均不可用）")
    return _CLIENT


def chat(
    messages: list[dict],
    max_tokens: int = 1000,
    temperature: float = 0.2,
    model: str = "deepseek-chat",
) -> str:
    global _CLIENT, _MODE
    last_error: Exception | None = None
    order: list[str] = []
    if _MODE:
        order.append(_MODE)
    order.extend(m for m in ("direct", "proxy") if m != _MODE)
    for mode in order:
        try:
            client = _CLIENT if (_CLIENT is not None and mode == _MODE) else _build(mode)
            resp = client.chat.completions.create(
                model=model,
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
            )
            _CLIENT, _MODE = client, mode
            return resp.choices[0].message.content or ""
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            continue
    raise RuntimeError(f"DeepSeek 调用失败：{last_error}")
