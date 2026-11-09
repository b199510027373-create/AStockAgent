from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

# .env 优先；已存在的系统环境变量不会被覆盖
load_dotenv(BASE_DIR / ".env")


TUSHARE_TOKEN = (os.getenv("TUSHARE_TOKEN") or "").strip()
TUSHARE_API_URL = (
    (os.getenv("TUSHARE_API_URL") or "https://api.tushare.pro").strip().rstrip("/")
)
DEEPSEEK_API_KEY = (os.getenv("DEEPSEEK_API_KEY") or "").strip()
DEEPSEEK_BASE = (os.getenv("DEEPSEEK_BASE") or "https://api.deepseek.com").strip()

CACHE_DIR = Path(os.getenv("ASTOCK_CACHE_DIR") or (BASE_DIR / "cache"))
CACHE_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_START = "20150101"
