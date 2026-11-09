from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

# .env 优先；已存在的系统环境变量不会被覆盖
load_dotenv(BASE_DIR / ".env")


def _env_int(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} 必须是整数，当前为 {raw!r}") from exc


def _env_float(name: str, default: float) -> float:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} 必须是数字，当前为 {raw!r}") from exc


TUSHARE_TOKEN = (os.getenv("TUSHARE_TOKEN") or "").strip()
TUSHARE_API_URL = (
    (os.getenv("TUSHARE_API_URL") or "https://api.tushare.pro").strip().rstrip("/")
)
DEEPSEEK_API_KEY = (os.getenv("DEEPSEEK_API_KEY") or "").strip()
DEEPSEEK_BASE = (os.getenv("DEEPSEEK_BASE") or "https://api.deepseek.com").strip()

# Agent 模型网关（ADR-014）；业务代码只读这些值或直接使用 gateway 工厂。
AGENT_PROVIDER = (os.getenv("AGENT_PROVIDER") or "deepseek").strip().lower()
AGENT_MODEL = (os.getenv("AGENT_MODEL") or "").strip()
AGENT_BASE_URL = (os.getenv("AGENT_BASE_URL") or "").strip()
AGENT_API_KEY = (os.getenv("AGENT_API_KEY") or DEEPSEEK_API_KEY).strip()
AGENT_MAX_STEPS = _env_int("AGENT_MAX_STEPS", 12)
AGENT_TOKEN_BUDGET = _env_int("AGENT_TOKEN_BUDGET", 64_000)
AGENT_TIMEOUT_SECONDS = _env_float("AGENT_TIMEOUT_SECONDS", 60.0)
AGENT_USAGE_LOG = (os.getenv("AGENT_USAGE_LOG") or "").strip()

CACHE_DIR = Path(os.getenv("ASTOCK_CACHE_DIR") or (BASE_DIR / "cache"))
CACHE_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_START = "20150101"
