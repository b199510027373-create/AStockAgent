from __future__ import annotations

import re
import time
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

from .config import CACHE_DIR
from .contracts import ContractError, validate_events, validate_evidence_list
from .tushare_client import TushareError, get_client

EVENT_DIR = CACHE_DIR / "events"
EVENT_DIR.mkdir(parents=True, exist_ok=True)

_NEWS_TTL = 300.0
_REPORT_TTL = 600.0
_CACHE: dict[tuple, tuple[float, pd.DataFrame]] = {}
_BASIC: pd.DataFrame | None = None

POSITIVE_WORDS = [
    "预增", "扭亏", "增长", "回购", "增持", "中标", "签约", "签订", "合同",
    "获批", "投产", "涨价", "提价", "突破", "超预期", "分红", "订单", "合作",
    "买入", "增持", "强烈推荐", "推荐", "上调", "创新高", "放量", "利好",
    "上涨", "大涨", "小涨", "走强", "拉升", "反弹", "涨停",
]
POSITIVE_WORDS = list(dict.fromkeys(POSITIVE_WORDS))
NEGATIVE_WORDS = [
    "预减", "亏损", "减持", "质押", "诉讼", "仲裁", "处罚", "立案", "问询",
    "终止", "下修", "下调", "风险提示", "退市", "停牌", "冻结", "担保",
    "逾期", "违规", "跌停", "下滑", "不及预期", "卖出", "利空", "解禁",
    "下跌", "大跌", "走弱", "回落", "承压",
]
GENERIC_ALIASES = {
    "银行", "时代", "科技", "股份", "集团", "控股", "中国", "国际", "证券",
    "保险", "地产", "能源", "电力", "汽车", "医药", "白酒", "食品", "材料",
    "电子", "通信", "传媒", "环保", "机械", "化工", "钢铁", "煤炭", "有色",
    "化学", "办公", "发展", "实业", "公司", "技术", "产业", "电器", "财富",
    "网络", "信息", "生物", "重工", "新材", "控股", "投资", "资本",
}
REGION_ALIASES = {
    "贵州", "中国", "上海", "北京", "深圳", "广东", "浙江", "江苏", "山东",
    "四川", "福建", "湖南", "湖北", "河南", "河北", "安徽", "江西", "陕西",
    "重庆", "天津", "云南", "广西", "海南", "辽宁", "吉林", "黑龙江", "山西",
    "甘肃", "宁夏", "青海", "新疆", "西藏", "内蒙古",
}
RATING_WORDS = {
    "强烈推荐": 2.0,
    "买入": 2.0,
    "增持": 1.5,
    "推荐": 1.0,
    "优于大市": 1.0,
    "跑赢行业": 1.0,
    "中性": 0.0,
    "持有": 0.0,
    "减持": -1.5,
    "卖出": -2.0,
}
INDUSTRY_KEYWORDS = {
    "白酒": ["白酒", "酒类", "食品饮料"],
    "银行": ["银行", "信贷", "利率", "存款"],
    "电池": ["电池", "锂电", "储能", "动力电池"],
    "电气设备": ["电池", "锂电", "储能", "新能源", "光伏", "风电", "电网"],
    "半导体": ["半导体", "芯片", "集成电路", "国产替代"],
    "医药": ["医药", "创新药", "医保", "集采", "药品"],
    "汽车": ["汽车", "新能源车", "整车", "智驾", "零部件"],
    "地产": ["房地产", "楼市", "住房", "地产"],
    "券商": ["券商", "证券", "资本市场", "IPO"],
    "保险": ["保险", "保费", "寿险"],
    "煤炭": ["煤炭", "煤价"],
    "有色": ["有色", "铜价", "铝价", "锂价", "稀土"],
    "传媒": ["传媒", "游戏", "影视", "AI内容"],
    "计算机": ["计算机", "软件", "信创", "人工智能"],
    "通信": ["通信", "5G", "光模块", "算力"],
    "电力": ["电力", "电网", "电价", "绿电"],
}

_CODE_RE = re.compile(r"(?<!\d)(\d{6})(?:\.(SH|SZ|BJ))?(?!\d)", re.I)


def _clean_text(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", str(text or ""))
    return re.sub(r"\s+", " ", text).strip()


def _cache_get(key: tuple, ttl: float) -> pd.DataFrame | None:
    if key in _CACHE:
        ts, df = _CACHE[key]
        if time.time() - ts < ttl:
            return df.copy()
    return None


def _cache_put(key: tuple, df: pd.DataFrame) -> pd.DataFrame:
    _CACHE[key] = (time.time(), df.copy())
    return df


def _today_str() -> str:
    return datetime.now().strftime("%Y%m%d")


def _col(df: pd.DataFrame, name: str, default: str = "") -> pd.Series:
    """Return a string Series even when a vendor omits the column."""
    if name in df.columns:
        return df[name].fillna(default).astype(str)
    return pd.Series([default] * len(df), index=df.index, dtype=object)


def _require_source_columns(
    df: pd.DataFrame, columns: list[str], source: str
) -> None:
    """供应商返回缺少关键列时显式报错，不进入"补空串→过滤后为空"的静默路径。"""
    missing = [c for c in columns if c not in df.columns]
    if missing:
        raise ContractError(
            "EventFrame",
            f"{source} 返回缺少列 {missing}；实际列: {list(df.columns)}",
            missing=missing,
        )


def _require_parseable_dates(
    raw: pd.Series, parsed: pd.Series, source: str, *, column: str = "pub_time"
) -> None:
    """列还在、值格式变了时显式报错，而不是 coerce 成 NaT 后被 dropna 静默清空。"""
    bad = parsed.isna() & raw.notna() & raw.astype(str).str.strip().ne("")
    if bad.any():
        raise ContractError(
            "EventFrame",
            f"{source} 有 {int(bad.sum())} 行 {column} 无法解析"
            f"（例如 {raw[bad].iloc[0]!r}）",
            invalid=[column],
        )


def _date_str(days_ago: int) -> str:
    return (datetime.now() - timedelta(days=days_ago)).strftime("%Y%m%d")


def _aliases(name: str) -> str:
    base = str(name or "").strip()
    base = re.sub(r"^\*?ST", "", base)
    base = re.sub(r"[A-Za-zＡ-Ｚ]+$", "", base).strip()
    out = {base} if base else set()
    if len(base) >= 4 and not base.startswith("中国"):
        tail = base[-2:]
        if tail not in GENERIC_ALIASES and tail not in REGION_ALIASES:
            out.add(tail)
    return "|".join(sorted(a for a in out if a))


def stock_basic(refresh: bool = False) -> pd.DataFrame:
    """股票基础信息（名称/行业），本地缓存一天。"""
    global _BASIC
    if _BASIC is not None and not refresh:
        return _BASIC.copy()

    path = EVENT_DIR / "stock_basic.parquet"
    meta = path.with_suffix(".json")
    if path.exists() and not refresh:
        try:
            if meta.exists() and meta.read_text(encoding="utf-8").strip() == _today_str():
                _BASIC = pd.read_parquet(path)
                return _BASIC.copy()
        except Exception:
            pass

    df = get_client().query(
        "stock_basic",
        exchange="",
        list_status="L",
        fields="ts_code,name,industry,market",
    )
    if df.empty:
        raise TushareError("stock_basic 返回为空")
    df["name"] = df["name"].fillna("").astype(str)
    df["industry"] = df["industry"].fillna("").astype(str)
    df["market"] = df["market"].fillna("").astype(str)
    df["aliases"] = df["name"].map(_aliases)
    df.to_parquet(path, index=False)
    meta.write_text(_today_str(), encoding="utf-8")
    _BASIC = df
    return df.copy()


def _meta_for(code: str) -> dict:
    basic = stock_basic()
    row = basic[basic["ts_code"] == code]
    if row.empty:
        return {"ts_code": code, "name": code, "industry": "", "aliases": code}
    r = row.iloc[0]
    return {
        "ts_code": code,
        "name": str(r["name"]),
        "industry": str(r["industry"]),
        "aliases": str(r["aliases"]),
    }


def extract_entities(text: str) -> tuple[list[str], list[dict]]:
    """从用户问题中识别股票代码和股票名称。"""
    text = str(text or "")
    basic = stock_basic()
    by_code = set()
    for m in _CODE_RE.finditer(text):
        raw, suffix = m.group(1), (m.group(2) or "").upper()
        if suffix:
            by_code.add(f"{raw}.{suffix}")
            continue
        if raw.startswith(("6", "9")):
            by_code.add(f"{raw}.SH")
        elif raw.startswith(("0", "3")):
            by_code.add(f"{raw}.SZ")
        elif raw.startswith(("4", "8")):
            by_code.add(f"{raw}.BJ")

    named = basic[
        basic["name"].map(lambda n: bool(n) and n in text)
        | basic["aliases"].map(
            lambda s: any(a and a in text for a in str(s).split("|"))
        )
    ]
    for _, row in named.iterrows():
        by_code.add(str(row["ts_code"]))

    metas = [_meta_for(code) for code in sorted(by_code)]
    return [m["ts_code"] for m in metas], metas


def _normalize_news(df: pd.DataFrame, source: str, kind: str = "news") -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    _require_source_columns(df, ["title"], f"news[{source}]")
    df = df.copy()
    if "datetime" in df.columns:
        df = df.rename(columns={"datetime": "pub_time"})
    if "pub_time" not in df.columns:
        raise ContractError(
            "EventFrame",
            f"news[{source}] 返回缺少列 ['datetime'/'pub_time']；实际列: {list(df.columns)}",
            missing=["pub_time"],
        )
    raw_time = df["pub_time"]
    df["pub_time"] = pd.to_datetime(df["pub_time"], errors="coerce")
    _require_parseable_dates(raw_time, df["pub_time"], f"news[{source}]")
    df["title"] = df["title"].map(_clean_text)
    if "content" not in df.columns:
        df["content"] = ""
    df["content"] = df["content"].map(_clean_text)
    if "src" in df.columns:
        df["source"] = df["src"].fillna(source).astype(str)
    else:
        df["source"] = source
    df["kind"] = kind
    if "url" not in df.columns:
        df["url"] = ""
    df["url"] = df["url"].fillna("").astype(str)
    # 全市场快讯不对应单只股票；code 仍必须存在（EventFrame 契约）
    df["code"] = ""
    df = df.dropna(subset=["pub_time"])
    return validate_events(
        df[["code", "pub_time", "title", "content", "source", "kind", "url"]],
        source=f"news[{source}]",
    )


def fetch_news(days: int = 2, limit: int = 800, refresh: bool = False) -> pd.DataFrame:
    """市场快讯（新浪/同花顺/东财）。"""
    days = max(1, int(days))
    key = ("news", days, limit)
    cached = None if refresh else _cache_get(key, _NEWS_TTL)
    if cached is not None:
        return cached

    end = datetime.now()
    start = end - timedelta(days=days)
    frames = []
    for src in ("sina", "10jqka", "eastmoney"):
        try:
            df = get_client().query(
                "news",
                src=src,
                start_date=start.strftime("%Y-%m-%d %H:%M:%S"),
                end_date=end.strftime("%Y-%m-%d %H:%M:%S"),
                fields="datetime,title,content",
                limit=limit,
            )
            n = _normalize_news(df, src, "news")
            if not n.empty:
                frames.append(n)
        except ContractError:
            # 契约错误必须冒泡：供应商列漂移不能被当成"这个源没新闻"
            raise
        except Exception:
            continue

    if not frames:
        return _cache_put(key, pd.DataFrame())
    out = pd.concat(frames, ignore_index=True)
    out = (
        out.drop_duplicates("title")
        .sort_values("pub_time", ascending=False)
        .reset_index(drop=True)
    )
    out = validate_events(out, source="fetch_news")
    return _cache_put(key, out)


def fetch_major_news(days: int = 7, refresh: bool = False) -> pd.DataFrame:
    key = ("major", days)
    cached = None if refresh else _cache_get(key, _NEWS_TTL)
    if cached is not None:
        return cached

    end = datetime.now()
    start = end - timedelta(days=days)
    try:
        df = get_client().query(
            "major_news",
            src="新华网",
            start_date=start.strftime("%Y-%m-%d %H:%M:%S"),
            end_date=end.strftime("%Y-%m-%d %H:%M:%S"),
            fields="title,pub_time,src,url",
            limit=500,
        )
        out = _normalize_news(df, "新华网", "news")
    except ContractError:
        raise
    except Exception:
        out = pd.DataFrame()
    return _cache_put(key, validate_events(out, allow_empty=True, source="fetch_major_news"))


def fetch_policy(days: int = 7, refresh: bool = False) -> pd.DataFrame:
    key = ("policy", days)
    cached = None if refresh else _cache_get(key, _NEWS_TTL)
    if cached is not None:
        return cached

    end = datetime.now()
    start = end - timedelta(days=days)
    try:
        df = get_client().query(
            "npr",
            start_date=start.strftime("%Y-%m-%d"),
            end_date=end.strftime("%Y-%m-%d"),
            fields="pubtime,title,pcode,puborg,ptype",
            limit=500,
        )
        if df.empty:
            return _cache_put(key, pd.DataFrame())
        _require_source_columns(df, ["pubtime", "title"], "npr")
        df = df.rename(columns={"pubtime": "pub_time"})
        raw_time = df["pub_time"]
        df["pub_time"] = pd.to_datetime(df["pub_time"], errors="coerce")
        _require_parseable_dates(raw_time, df["pub_time"], "npr")
        df["title"] = df["title"].map(_clean_text)
        df["content"] = _col(df, "ptype").map(_clean_text)
        df["source"] = _col(df, "puborg", "政策")
        df["kind"] = "policy"
        df["url"] = ""
        df["code"] = ""
        out = validate_events(
            df.dropna(subset=["pub_time"])[
                ["code", "pub_time", "title", "content", "source", "kind", "url"]
            ],
            source="npr",
        )
    except ContractError:
        raise
    except Exception:
        out = pd.DataFrame()
    return _cache_put(key, out)


def fetch_announcements(codes: list[str], days: int = 30) -> pd.DataFrame:
    """按股票逐只拉公告；候选池通常 10–15 只，成本可控。"""
    if not codes:
        return pd.DataFrame()
    frames = []
    end = _today_str()
    start = _date_str(days)
    for code in codes:
        key = ("anns", code, days, end)
        cached = _cache_get(key, _REPORT_TTL)
        if cached is not None:
            if not cached.empty:
                frames.append(cached)
            continue
        try:
            df = get_client().query(
                "anns_d",
                ts_code=code,
                start_date=start,
                end_date=end,
                fields="ann_date,ts_code,name,title,url",
                limit=200,
            )
            if not df.empty:
                df = df.copy()
                _require_source_columns(df, ["ann_date", "title"], f"anns_d[{code}]")
                raw_date = df["ann_date"]
                df["pub_time"] = pd.to_datetime(df["ann_date"], format="%Y%m%d", errors="coerce")
                _require_parseable_dates(raw_date, df["pub_time"], f"anns_d[{code}]")
                df["title"] = df["title"].map(_clean_text)
                df["content"] = ""
                df["source"] = "公告"
                df["kind"] = "announcement"
                df["url"] = _col(df, "url")
                df["name"] = _col(df, "name")
                df["ts_code"] = _col(df, "ts_code")
                # 供应商不回 ts_code 时用请求代码补全；旧实现补空串后按 code 过滤，
                # 结果"公告永远为空"（评审 D18）。
                df["code"] = df["ts_code"].where(df["ts_code"].str.strip().ne(""), code)
                df = validate_events(
                    df.dropna(subset=["pub_time"])[
                        ["code", "pub_time", "ts_code", "name", "title", "content", "source", "kind", "url"]
                    ],
                    require_code=True,
                    source=f"anns_d[{code}]",
                )
                _cache_put(key, df)
                frames.append(df)
            else:
                _cache_put(key, pd.DataFrame())
        except ContractError:
            raise
        except Exception:
            continue
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, ignore_index=True)
    out = validate_events(out, require_code=True, source="fetch_announcements")
    return out.sort_values("pub_time", ascending=False)


def fetch_reports(days: int = 7, codes: list[str] | None = None) -> pd.DataFrame:
    """研报评级 + 研报标题。

    days <= 7 时按日期全市场拉取；更长窗口按候选股票逐只拉，避免单次行数上限。
    """
    days = max(1, int(days))
    key = ("reports", days, tuple(codes or []))
    cached = _cache_get(key, _REPORT_TTL)
    if cached is not None:
        return cached

    frames = []
    if days <= 7 or not codes:
        try:
            rc = get_client().query_all(
                "report_rc",
                start_date=_date_str(days),
                end_date=_today_str(),
                fields="ts_code,name,report_date,report_title,org_name,rating,max_price,min_price",
                limit=5000,
                max_rows=20000,
            )
            if not rc.empty:
                rc = rc.copy()
                _require_source_columns(
                    rc, ["ts_code", "report_date", "report_title"], "report_rc"
                )
                raw_date = rc["report_date"]
                rc["pub_time"] = pd.to_datetime(rc["report_date"], format="%Y%m%d", errors="coerce")
                _require_parseable_dates(raw_date, rc["pub_time"], "report_rc")
                rc["title"] = rc["report_title"].map(_clean_text)
                rc["content"] = _col(rc, "rating")
                rc["source"] = _col(rc, "org_name")
                rc["kind"] = "rating"
                rc["url"] = ""
                rc["code"] = rc["ts_code"].fillna("").astype(str)
                rc = validate_events(
                    rc.dropna(subset=["pub_time"])[
                        ["code", "pub_time", "ts_code", "name", "title", "content", "source", "kind", "url", "rating"]
                    ],
                    require_code=True,
                    source="report_rc",
                )
                frames.append(rc)
        except ContractError:
            raise
        except Exception:
            pass

        try:
            rr = get_client().query_all(
                "research_report",
                start_date=_date_str(days),
                end_date=_today_str(),
                fields="trade_date,title,ts_code,name,inst_csname,ind_name,url",
                limit=5000,
                max_rows=20000,
            )
            if not rr.empty:
                rr = rr.copy()
                _require_source_columns(
                    rr, ["ts_code", "trade_date", "title"], "research_report"
                )
                raw_date = rr["trade_date"]
                rr["pub_time"] = pd.to_datetime(rr["trade_date"], format="%Y%m%d", errors="coerce")
                _require_parseable_dates(raw_date, rr["pub_time"], "research_report")
                rr["title"] = rr["title"].map(_clean_text)
                rr["content"] = _col(rr, "ind_name")
                rr["source"] = _col(rr, "inst_csname")
                rr["kind"] = "research"
                rr["url"] = _col(rr, "url")
                rr["rating"] = ""
                rr["code"] = rr["ts_code"].fillna("").astype(str)
                rr = validate_events(
                    rr.dropna(subset=["pub_time"])[
                        ["code", "pub_time", "ts_code", "name", "title", "content", "source", "kind", "url", "rating"]
                    ],
                    require_code=True,
                    source="research_report",
                )
                frames.append(rr)
        except ContractError:
            raise
        except Exception:
            pass
    else:
        for code in codes:
            try:
                rc = get_client().query(
                    "report_rc",
                    ts_code=code,
                    start_date=_date_str(days),
                    end_date=_today_str(),
                    fields="ts_code,name,report_date,report_title,org_name,rating,max_price,min_price",
                    limit=500,
                )
                if not rc.empty:
                    rc = rc.copy()
                    _require_source_columns(
                        rc, ["ts_code", "report_date", "report_title"], f"report_rc[{code}]"
                    )
                    raw_date = rc["report_date"]
                    rc["pub_time"] = pd.to_datetime(rc["report_date"], format="%Y%m%d", errors="coerce")
                    _require_parseable_dates(raw_date, rc["pub_time"], f"report_rc[{code}]")
                    rc["title"] = rc["report_title"].map(_clean_text)
                    rc["content"] = _col(rc, "rating")
                    rc["source"] = _col(rc, "org_name")
                    rc["kind"] = "rating"
                    rc["url"] = ""
                    rc["code"] = rc["ts_code"].fillna("").astype(str)
                    rc = validate_events(
                        rc.dropna(subset=["pub_time"])[
                            ["code", "pub_time", "ts_code", "name", "title", "content", "source", "kind", "url", "rating"]
                        ],
                        require_code=True,
                        source=f"report_rc[{code}]",
                    )
                    frames.append(rc)
                rr = get_client().query(
                    "research_report",
                    ts_code=code,
                    start_date=_date_str(days),
                    end_date=_today_str(),
                    fields="trade_date,title,ts_code,name,inst_csname,ind_name,url",
                    limit=500,
                )
                if not rr.empty:
                    rr = rr.copy()
                    _require_source_columns(
                        rr, ["ts_code", "trade_date", "title"], f"research_report[{code}]"
                    )
                    raw_date = rr["trade_date"]
                    rr["pub_time"] = pd.to_datetime(rr["trade_date"], format="%Y%m%d", errors="coerce")
                    _require_parseable_dates(raw_date, rr["pub_time"], f"research_report[{code}]")
                    rr["title"] = rr["title"].map(_clean_text)
                    rr["content"] = _col(rr, "ind_name")
                    rr["source"] = _col(rr, "inst_csname")
                    rr["kind"] = "research"
                    rr["url"] = _col(rr, "url")
                    rr["rating"] = ""
                    rr["code"] = rr["ts_code"].fillna("").astype(str)
                    rr = validate_events(
                        rr.dropna(subset=["pub_time"])[
                            ["code", "pub_time", "ts_code", "name", "title", "content", "source", "kind", "url", "rating"]
                        ],
                        require_code=True,
                        source=f"research_report[{code}]",
                    )
                    frames.append(rr)
            except ContractError:
                raise
            except Exception:
                continue

    if not frames:
        return _cache_put(key, pd.DataFrame())
    out = pd.concat(frames, ignore_index=True).drop_duplicates("title")
    out = validate_events(out, require_code=True, source="fetch_reports")
    return _cache_put(key, out.sort_values("pub_time", ascending=False).reset_index(drop=True))


def sentiment(text: str) -> tuple[int, int]:
    text = str(text or "")
    positive = sum(text.count(word) for word in POSITIVE_WORDS)
    negative = sum(text.count(word) for word in NEGATIVE_WORDS)
    return positive, negative


def _matches(text: str, aliases: set[str]) -> bool:
    text = str(text or "")
    return any(alias and alias in text for alias in aliases)


def _industry_terms(industry: str) -> list[str]:
    industry = str(industry or "")
    terms: list[str] = []
    for key, words in INDUSTRY_KEYWORDS.items():
        if key in industry:
            terms.extend(words)
    return terms


def evidence_for_stock(
    code: str,
    name: str,
    industry: str,
    announcements: pd.DataFrame | None,
    reports: pd.DataFrame | None,
    news: pd.DataFrame | None,
    major: pd.DataFrame | None,
    policy: pd.DataFrame | None,
    limit: int = 8,
) -> list[dict]:
    meta = _meta_for(code)
    aliases = set((meta.get("aliases") or name or code).split("|"))
    aliases.add(name)
    aliases.add(code)
    industry_terms = _industry_terms(industry)

    items: list[dict] = []

    if announcements is not None and not announcements.empty:
        anns = announcements
        if "code" in anns.columns:
            anns = anns[anns["code"] == code]
        elif "ts_code" in anns.columns:
            anns = anns[anns["ts_code"] == code]
        for _, r in anns.iterrows():
            items.append(
                {
                    "kind": "公告",
                    "pub_time": r["pub_time"],
                    "title": r["title"],
                    "source": "公告",
                    "url": r.get("url", ""),
                    "content": "",
                }
            )

    if reports is not None and not reports.empty:
        rr = reports
        if "ts_code" in rr.columns:
            rr = rr[(rr["ts_code"] == code) | ((rr["ts_code"].fillna("") == "") & rr["name"].fillna("").eq(name))]
        for _, r in rr.iterrows():
            items.append(
                {
                    "kind": "研报",
                    "pub_time": r["pub_time"],
                    "title": r["title"],
                    "source": r.get("source", ""),
                    "url": r.get("url", ""),
                    "content": r.get("content", ""),
                    "rating": r.get("rating", ""),
                }
            )

    def _append_news(df: pd.DataFrame | None, kind: str) -> None:
        if df is None or df.empty:
            return
        for _, r in df.iterrows():
            text = f"{r.get('title', '')} {str(r.get('content', ''))[:300]}"
            if _matches(text, aliases):
                items.append(
                    {
                        "kind": kind,
                        "pub_time": r["pub_time"],
                        "title": r["title"],
                        "source": r.get("source", ""),
                        "url": r.get("url", ""),
                        "content": str(r.get("content", ""))[:200],
                    }
                )

    _append_news(news, "新闻")
    _append_news(major, "新闻")

    if policy is not None and not policy.empty and industry_terms:
        for _, r in policy.iterrows():
            text = f"{r.get('title', '')} {r.get('content', '')}"
            if any(term in text for term in industry_terms):
                items.append(
                    {
                        "kind": "政策",
                        "pub_time": r["pub_time"],
                        "title": r["title"],
                        "source": r.get("source", "政策"),
                        "url": "",
                        "content": str(r.get("content", "")),
                    }
                )

    if not items:
        return []
    out = pd.DataFrame(items).drop_duplicates("title")
    out = out.sort_values("pub_time", ascending=False).head(limit)
    return validate_evidence_list(
        out.to_dict("records"), field=f"evidence_for_stock({code})"
    )


def score_events(evidence: list[dict]) -> tuple[float, dict]:
    """把证据聚合成 0–100 的事件分（加权情绪均值）。

    - 每条证据先算极性（正/负词、研报评级），再按类型权重和时效权重加权；
    - 证据条数只影响平均的稳定性，不再让“新闻多 = 接近 100”；
    - 没有证据时固定 50，中性证据也接近 50。
    """
    stats = {"pos": 0, "neg": 0, "news": 0, "anns": 0, "reports": 0, "policy": 0, "n": len(evidence)}
    if not evidence:
        return 50.0, stats

    type_weights = {"公告": 1.0, "研报": 1.2, "新闻": 0.8, "政策": 0.7}
    now = pd.Timestamp.now()
    weighted_sum = 0.0
    weight_sum = 0.0
    for item in evidence:
        title = str(item.get("title", ""))
        positive, negative = sentiment(title)
        stats["pos"] += positive
        stats["neg"] += negative
        kind = str(item.get("kind", ""))
        if kind == "公告":
            stats["anns"] += 1
        elif kind == "研报":
            stats["reports"] += 1
        elif kind == "新闻":
            stats["news"] += 1
        elif kind == "政策":
            stats["policy"] += 1

        denom = max(1, positive + negative)
        polarity = (positive - negative) / denom
        if kind == "研报":
            rating = str(item.get("rating", ""))
            rating_bonus = next((v for k, v in RATING_WORDS.items() if k in rating), 0.0)
            polarity += 0.35 * (rating_bonus / 2.0)
        elif kind == "政策":
            polarity += 0.05
        polarity = max(-1.0, min(1.0, polarity))
        item_score = 50.0 + 50.0 * polarity

        weight = type_weights.get(kind, 0.7)
        try:
            # pub_time 是契约字段；date 是旧版证据 dict 的兼容字段
            pub = pd.Timestamp(item.get("pub_time") or item.get("date"))
            age = max(0, (now.normalize() - pub.normalize()).days)
            weight *= 1.0 / (1.0 + age / 45.0)
        except Exception:
            pass
        weighted_sum += item_score * weight
        weight_sum += weight

    score = weighted_sum / weight_sum if weight_sum > 0 else 50.0
    return round(max(0.0, min(100.0, score)), 1), stats


def build_ai_context(message: str, limit: int = 8) -> tuple[str, list[dict]]:
    """给 AI 对话检索新闻/公告/研报/政策，返回可引用的上下文。"""
    codes, metas = extract_entities(message)
    try:
        news = fetch_news(days=3)
        major = fetch_major_news(days=7)
        policy = fetch_policy(days=7)
        announcements = fetch_announcements(codes, days=30) if codes else pd.DataFrame()
        reports = fetch_reports(days=7, codes=codes or None)
    except ContractError:
        # 契约错误不能退化成"没有上下文"，否则供应商列漂移会被当成无新闻
        raise
    except Exception:
        return "", []

    items: list[dict] = []
    for meta in metas:
        items.extend(
            evidence_for_stock(
                meta["ts_code"],
                meta["name"],
                meta["industry"],
                announcements,
                reports,
                news,
                major,
                policy,
                limit=5,
            )
        )

    if not items and codes:
        return (
            "【参考资料】未检索到 "
            + "、".join(m["name"] for m in metas)
            + " 近期的公告/研报/新闻/政策，回答时请明确说明资料不足。",
            [],
        )

    if not items:
        # 没有明确股票：给市场新闻 + 政策
        for df, kind in ((news, "新闻"), (major, "新闻"), (policy, "政策")):
            if df is None or df.empty:
                continue
            for _, r in df.head(4).iterrows():
                items.append(
                    {
                        "kind": kind,
                        "pub_time": r["pub_time"],
                        "title": r["title"],
                        "source": r.get("source", ""),
                        "url": r.get("url", ""),
                        "content": str(r.get("content", ""))[:160],
                    }
                )

    if not items:
        return "", []
    items = validate_evidence_list(items, field="build_ai_context")
    items = sorted(items, key=lambda x: x["pub_time"], reverse=True)[:limit]
    lines = [f"【参考资料，截至 {datetime.now():%Y-%m-%d %H:%M}】"]
    citations = []
    for i, item in enumerate(items, 1):
        date = pd.Timestamp(item["pub_time"]).strftime("%Y-%m-%d")
        line = f"[{i}] {date} {item['kind']}｜{item['title']}（{item.get('source', '')}）"
        if item.get("url"):
            line += f" {item['url']}"
        lines.append(line)
        citations.append(item)
    return "\n".join(lines), citations
