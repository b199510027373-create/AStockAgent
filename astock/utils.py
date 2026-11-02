from __future__ import annotations

import re


def normalize_code(raw: str) -> str:
    """把 600519 / 600519.sh 统一成 600519.SH；无法识别则原样返回。"""
    s = (raw or "").strip().upper().replace(" ", "")
    if not s:
        return ""
    if re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", s):
        return s
    if re.fullmatch(r"\d{6}", s):
        if s.startswith(("6", "9")):
            return f"{s}.SH"
        if s.startswith(("0", "3")):
            return f"{s}.SZ"
        if s.startswith(("4", "8")):
            return f"{s}.BJ"
    return s


def is_code(code: str) -> bool:
    return bool(re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", code or ""))
