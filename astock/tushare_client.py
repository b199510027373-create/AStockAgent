from __future__ import annotations

import time
from typing import Any

import pandas as pd
import requests

from .config import TUSHARE_API_URL, TUSHARE_TOKEN


class TushareError(RuntimeError):
    """Tushare 接口调用失败。"""


class TusharePermissionError(TushareError):
    """当前 token 没有该接口权限（如分钟线）。"""


class TushareClient:
    """轻量 Tushare HTTP 客户端。

    与官方 SDK 的区别：
    1. 强制 ``trust_env = False``，绕过 Windows 系统代理（Clash 等）。
       否则 requests 会把请求发到 127.0.0.1:7890，代理再转发失败，表现为 502。
    2. 统一错误码、重试与权限提示。
    """

    def __init__(
        self,
        token: str | None = None,
        api_url: str | None = None,
        timeout: int = 30,
        retries: int = 3,
        backoff: float = 1.2,
    ) -> None:
        self.token = (token if token is not None else TUSHARE_TOKEN).strip()
        self.api_url = (api_url if api_url is not None else TUSHARE_API_URL).rstrip("/")
        self.timeout = timeout
        self.retries = max(1, retries)
        self.backoff = backoff

        if not self.token:
            raise TushareError("未配置 TUSHARE_TOKEN，请检查项目根目录 .env")

        self.session = requests.Session()
        # 关键：不要让系统代理接管请求
        self.session.trust_env = False

    def query(self, api_name: str, fields: str = "", **params: Any) -> pd.DataFrame:
        """调用一个 Tushare 接口，返回 DataFrame；空结果返回空表。"""
        payload: dict[str, Any] = {
            "api_name": api_name,
            "token": self.token,
            "params": params,
            "fields": fields,
        }
        url = f"{self.api_url}/{api_name}"
        last_error: Exception | None = None

        for attempt in range(self.retries):
            try:
                resp = self.session.post(url, json=payload, timeout=self.timeout)
            except requests.RequestException as exc:
                last_error = exc
                if attempt < self.retries - 1:
                    time.sleep(self.backoff * (attempt + 1))
                continue

            if resp.status_code == 403:
                raise TusharePermissionError(
                    f"{api_name}: 当前 token 无权限（HTTP 403）。"
                    "分钟线等接口需要单独联系数据商开通。"
                )
            if resp.status_code != 200:
                last_error = TushareError(
                    f"{api_name}: HTTP {resp.status_code} {resp.text[:160]}"
                )
                if attempt < self.retries - 1:
                    time.sleep(self.backoff * (attempt + 1))
                continue

            try:
                body = resp.json()
            except ValueError:
                last_error = TushareError(f"{api_name}: 返回不是 JSON: {resp.text[:160]}")
                if attempt < self.retries - 1:
                    time.sleep(self.backoff * (attempt + 1))
                continue

            code = body.get("code")
            if str(code) == "403":
                raise TusharePermissionError(
                    f"{api_name}: {body.get('msg') or '无权限'}"
                )
            if str(code) != "0":
                message = str(body.get("msg") or body)
                # 频率限制：退避后重试
                if any(word in message for word in ("频率", "每分钟", "限制", "busy")):
                    last_error = TushareError(f"{api_name}: {message}")
                    if attempt < self.retries - 1:
                        time.sleep(self.backoff * (attempt + 2))
                    continue
                raise TushareError(f"{api_name}: {message}")

            data = body.get("data") or {}
            columns = data.get("fields") or []
            items = data.get("items") or []
            if not columns:
                return pd.DataFrame()
            return pd.DataFrame(items, columns=columns)

        raise TushareError(
            f"{api_name}: 重试 {self.retries} 次仍失败：{last_error}"
        )

    def query_all(
        self,
        api_name: str,
        fields: str = "",
        limit: int = 5000,
        max_rows: int = 200_000,
        **params: Any,
    ) -> pd.DataFrame:
        """分页拉取全部数据（分钟线等大结果集用）。"""
        frames: list[pd.DataFrame] = []
        offset = 0
        while offset < max_rows:
            df = self.query(
                api_name, fields=fields, limit=limit, offset=offset, **params
            )
            if df.empty:
                break
            frames.append(df)
            if len(df) < limit:
                break
            offset += limit
        if not frames:
            return pd.DataFrame()
        out = pd.concat(frames, ignore_index=True)
        # 服务端忽略 offset 时，去重避免重复追加到 max_rows
        subset = [c for c in ("ts_code", "trade_date", "trade_time", "ann_date") if c in out.columns]
        return out.drop_duplicates(subset=subset or None, keep="first").reset_index(drop=True)

    # 明确允许的旧式调用；拼写错误直接报错，不再走到网络才 404
    _LEGACY_APIS = {
        "daily", "adj_factor", "weekly", "monthly", "index_daily", "trade_cal",
        "stock_basic", "daily_basic", "stk_mins", "rt_min", "anns_d",
        "report_rc", "research_report", "npr", "news", "major_news", "index_weight",
    }

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)
        if name not in self._LEGACY_APIS:
            raise AttributeError(f"TushareClient 不支持接口 {name!r}")

        def _call(**params: Any) -> pd.DataFrame:
            return self.query(name, **params)

        return _call


_CLIENT: TushareClient | None = None


def get_client() -> TushareClient:
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = TushareClient()
    return _CLIENT
