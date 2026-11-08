"""显式数据契约层（ADR-002）。

把 astock 内部长期隐式依赖的列约定集中到本模块，跨模块传递的数据先过校验：

- :class:`BarFrame`：行情 K 线（``ts_code/trade_date/OHLC/vol/amount[/adj_factor]``）
- :class:`EventFrame`：事件流（``code/kind/pub_time/title/source/url``）
- :class:`Evidence`：单条证据（``pub_time/title/source/url/kind``）
- :class:`RecommendationRun`：推荐批次 payload（``schema_version=2``）

约定：校验失败一律抛 :class:`ContractError`，消息里带上契约名与缺失/非法列名，
**不返回空 DataFrame、不静默补列**（评审 D18：``ts_code`` 缺列被 ``except`` 吞掉，
最终表现为"没有证据/没有行情"）。合法但为空的结果（例如区间内无交易日）由调用方
显式决定：``allow_empty=True`` 时允许 0 行的 DataFrame 通过列校验。
"""

from __future__ import annotations

import json
import math
from typing import Any, Iterable, Mapping, Sequence

import pandas as pd

__all__ = [
    "ContractError",
    "BAR_SCHEMA",
    "EVENT_SCHEMA",
    "EVIDENCE_REQUIRED_KEYS",
    "RECOMMENDATION_REQUIRED_KEYS",
    "BarFrame",
    "EventFrame",
    "Evidence",
    "RecommendationRun",
    "validate_bars",
    "validate_events",
    "validate_evidence",
    "validate_evidence_list",
    "validate_recommendation_run",
]


class ContractError(ValueError):
    """契约校验失败。

    Attributes:
        contract: 契约名（``BarFrame`` / ``EventFrame`` / ``Evidence`` / ``RecommendationRun``）。
        missing: 缺失的列名或字段名。
        invalid: 存在但类型/取值非法的列名或字段名。
    """

    def __init__(
        self,
        contract: str,
        detail: str,
        *,
        missing: Sequence[str] = (),
        invalid: Sequence[str] = (),
    ) -> None:
        self.contract = contract
        self.missing = list(missing)
        self.invalid = list(invalid)
        super().__init__(f"[{contract}] {detail}")


# --------------------------------------------------------------------------- #
# 内部工具
# --------------------------------------------------------------------------- #
def _is_frame(value: Any, contract: str) -> pd.DataFrame:
    if not isinstance(value, pd.DataFrame):
        raise ContractError(
            contract,
            f"期望 pandas.DataFrame，实际是 {type(value).__name__}",
        )
    return value


def _require_columns(
    df: pd.DataFrame, required: Iterable[str], contract: str, *, source: str = ""
) -> None:
    missing = [c for c in required if c not in df.columns]
    if missing:
        where = f"{source} " if source else ""
        raise ContractError(
            contract,
            f"{where}缺少必需列 {missing}；实际列: {list(df.columns)}",
            missing=missing,
        )


def _check_strings(
    df: pd.DataFrame,
    columns: Iterable[str],
    contract: str,
    *,
    non_empty: bool = False,
) -> None:
    """校验列值都是字符串。

    默认允许 NaN/None；``non_empty=True`` 时 NaN 与空串/纯空白都算违规——"缺列"之外
    的静默降级主要来自 ``astype(str)`` 把 NaN 变成 ``"nan"``，这里显式拦住。
    """
    bad_cols: list[str] = []
    for col in columns:
        values = df[col]
        present = values.dropna()
        not_str = [v for v in present if not isinstance(v, str)]
        if not_str:
            bad_cols.append(col)
            continue
        if non_empty:
            blank = values.isna() | values.astype(str).str.strip().eq("")
            if blank.any():
                bad_cols.append(col)
    if bad_cols:
        hint = "存在非字符串或空值" if non_empty else "存在非字符串值"
        raise ContractError(
            contract,
            f"列 {bad_cols} {hint}（例如 {_sample_value(df, bad_cols[0])!r}）",
            invalid=bad_cols,
        )


def _sample_value(df: pd.DataFrame, col: str) -> Any:
    values = df[col].dropna()
    return values.iloc[0] if not values.empty else None


def _to_datetime(
    df: pd.DataFrame, column: str, contract: str, *, required: bool = True
) -> pd.Series:
    """转成 datetime64；无法解析的值报错（带列名），不做静默丢行。"""
    series = df[column]
    if pd.api.types.is_datetime64_any_dtype(series) or isinstance(
        series.dtype, pd.DatetimeTZDtype
    ):
        parsed = series
    else:
        parsed = pd.to_datetime(series, errors="coerce")
        junk = series.notna() & parsed.isna()
        if junk.any():
            sample = series[junk].iloc[0]
            raise ContractError(
                contract,
                f"列 {column!r} 存在无法解析的时间值（例如 {sample!r}）",
                invalid=[column],
            )
    if required and parsed.isna().any():
        raise ContractError(
            contract,
            f"列 {column!r} 存在缺失时间值（{int(parsed.isna().sum())} 行）",
            invalid=[column],
        )
    return parsed


def _to_numeric(df: pd.DataFrame, columns: Iterable[str], contract: str) -> None:
    """校验列为数值（必要时尝试转换，转换失败报错；NaN 原样保留）。"""
    bad_cols: list[str] = []
    for col in columns:
        if col not in df.columns:
            continue
        series = df[col]
        if pd.api.types.is_numeric_dtype(series) or pd.api.types.is_bool_dtype(series):
            continue
        converted = pd.to_numeric(series, errors="coerce")
        lost = int((series.notna() & converted.isna()).sum())
        if lost:
            bad_cols.append(col)
        else:
            # 转换无损时写回，保证契约输出是数值 dtype（下游算术/序列化都依赖它）
            df[col] = converted
    if bad_cols:
        raise ContractError(
            contract,
            f"列 {bad_cols} 含无法转换为数值的值（例如 "
            f"{_sample_value(df, bad_cols[0])!r}）",
            invalid=bad_cols,
        )


# --------------------------------------------------------------------------- #
# BarFrame
# --------------------------------------------------------------------------- #
BAR_SCHEMA: dict[str, str] = {
    "ts_code": "string",
    "trade_date": "datetime64[ns]",
    "open": "float64",
    "high": "float64",
    "low": "float64",
    "close": "float64",
    "vol": "float64",
    "amount": "float64",
    "adj_factor": "float64",
}


class BarFrame:
    """行情 K 线契约（日线/分钟线/指数）。

    必需列：``ts_code``、``trade_date``、``open``、``high``、``low``、``close``、
    ``vol``、``amount``；``adj_factor`` 在 ``require_adj=True`` 时必需（前复权数据
    必须带复权因子，否则无法回算 raw 价格）。

    ``trade_date`` 允许传入可解析的时间字符串并归一化为 ``datetime64``；
    ``ts_code`` 不允许为空字符串，OHLC/vol/amount 必须是数值。
    """

    name = "BarFrame"
    required: tuple[str, ...] = (
        "ts_code",
        "trade_date",
        "open",
        "high",
        "low",
        "close",
        "vol",
        "amount",
    )
    price_columns: tuple[str, ...] = ("open", "high", "low", "close", "vol", "amount")

    @classmethod
    def validate(
        cls,
        df: Any,
        *,
        require_adj: bool = False,
        allow_empty: bool = False,
        source: str = "",
    ) -> pd.DataFrame:
        """校验并返回归一化后的行情表；失败抛 :class:`ContractError`。

        Args:
            df: 待校验的 DataFrame。
            require_adj: 是否要求 ``adj_factor`` 列（前复权数据）。
            allow_empty: 是否允许 0 行结果（区间内无交易日）。仍要求列齐全，
                除非是完全没有列的 ``pd.DataFrame()``（"无数据"哨兵）。
            source: 出错信息里的数据来源（如 ``load_daily``）。
        """
        frame = _is_frame(df, cls.name)
        if frame.empty and allow_empty and not len(frame.columns):
            # 只放过"无数据"哨兵（连列都没有）；0 行但列不齐仍要报错。
            return frame.copy()

        required = list(cls.required) + (["adj_factor"] if require_adj else [])
        _require_columns(frame, required, cls.name, source=source)
        _check_strings(frame, ["ts_code"], cls.name, non_empty=True)
        frame = frame.copy()
        frame["trade_date"] = _to_datetime(frame, "trade_date", cls.name)
        numeric = [c for c in cls.price_columns if c in frame.columns]
        if "adj_factor" in frame.columns:
            numeric.append("adj_factor")
        _to_numeric(frame, numeric, cls.name)
        if require_adj and frame["adj_factor"].isna().any():
            raise ContractError(
                cls.name,
                f"列 'adj_factor' 存在缺失值（{int(frame['adj_factor'].isna().sum())} 行）",
                invalid=["adj_factor"],
            )
        return frame


def validate_bars(
    df: Any,
    *,
    require_adj: bool = False,
    allow_empty: bool = False,
    source: str = "",
) -> pd.DataFrame:
    """``BarFrame`` 契约的便捷入口，见 :meth:`BarFrame.validate`。"""
    return BarFrame.validate(
        df, require_adj=require_adj, allow_empty=allow_empty, source=source
    )


# --------------------------------------------------------------------------- #
# EventFrame
# --------------------------------------------------------------------------- #
EVENT_SCHEMA: dict[str, str] = {
    "code": "string",
    "kind": "string",
    "pub_time": "datetime64[ns]",
    "title": "string",
    "source": "string",
    "url": "string",
}


class EventFrame:
    """事件流契约（新闻 / 公告 / 研报 / 政策）。

    必需列：``code``、``kind``、``pub_time``、``title``、``source``、``url``。
    ``code`` 允许为空串（全市场新闻/政策不对应单只股票）；个股事件（公告、研报）
    用 ``require_code=True`` 强制非空，避免"代码列缺失 → 过滤后为空"被当成没消息。
    ``url`` 允许为空串（部分来源无链接），但列必须存在。
    """

    name = "EventFrame"
    required: tuple[str, ...] = (
        "code",
        "kind",
        "pub_time",
        "title",
        "source",
        "url",
    )

    @classmethod
    def validate(
        cls,
        df: Any,
        *,
        require_code: bool = False,
        allow_empty: bool = False,
        source: str = "",
    ) -> pd.DataFrame:
        """校验并返回归一化后的事件表；失败抛 :class:`ContractError`。"""
        frame = _is_frame(df, cls.name)
        if frame.empty and allow_empty and not len(frame.columns):
            # 只放过"无数据"哨兵（连列都没有）；0 行但列不齐仍要报错。
            return frame.copy()

        _require_columns(frame, cls.required, cls.name, source=source)
        frame = frame.copy()
        _check_strings(frame, ["kind"], cls.name, non_empty=True)
        _check_strings(frame, ["title"], cls.name, non_empty=True)
        _check_strings(frame, ["source", "url"], cls.name)
        _check_strings(frame, ["code"], cls.name, non_empty=require_code)
        frame["pub_time"] = _to_datetime(frame, "pub_time", cls.name)
        return frame


def validate_events(
    df: Any,
    *,
    require_code: bool = False,
    allow_empty: bool = False,
    source: str = "",
) -> pd.DataFrame:
    """``EventFrame`` 契约的便捷入口，见 :meth:`EventFrame.validate`。"""
    return EventFrame.validate(
        df, require_code=require_code, allow_empty=allow_empty, source=source
    )


# --------------------------------------------------------------------------- #
# Evidence
# --------------------------------------------------------------------------- #
EVIDENCE_REQUIRED_KEYS: tuple[str, ...] = ("pub_time", "title", "source", "url", "kind")


class Evidence:
    """单条证据契约。

    必需字段：``pub_time``、``title``、``source``、``url``、``kind``；``url`` 可为
    空串（部分公告/研报没有链接），``pub_time`` 必须可解析。迁移期兼容旧字段
    ``date``：校验后统一补出 ``pub_time``（canonical），其余字段原样保留。
    """

    name = "Evidence"
    required_keys = EVIDENCE_REQUIRED_KEYS

    @classmethod
    def validate(cls, item: Any, *, index: int | None = None) -> dict:
        """校验单条证据并返回补全 ``pub_time`` 的新 dict。"""
        where = f"第 {index} 条" if index is not None else "证据"
        if not isinstance(item, Mapping):
            raise ContractError(
                cls.name, f"{where}应为 dict，实际是 {type(item).__name__}"
            )
        record = dict(item)
        if "pub_time" not in record and "date" in record:
            record["pub_time"] = record["date"]
        missing = [k for k in cls.required_keys if k not in record]
        if missing:
            raise ContractError(
                cls.name,
                f"{where}缺少字段 {missing}；实际字段: {sorted(record)}",
                missing=missing,
            )
        for key in ("title", "source", "kind"):
            value = record.get(key)
            if not isinstance(value, str) or not value.strip():
                raise ContractError(
                    cls.name,
                    f"{where}字段 {key!r} 必须是非空字符串，实际 {value!r}",
                    invalid=[key],
                )
        url = record.get("url")
        if url is not None and not isinstance(url, str):
            raise ContractError(
                cls.name, f"{where}字段 'url' 必须是字符串，实际 {url!r}", invalid=["url"]
            )
        try:
            parsed = pd.Timestamp(record["pub_time"])
        except (ValueError, TypeError) as exc:
            raise ContractError(
                cls.name,
                f"{where}字段 'pub_time' 无法解析：{record['pub_time']!r}（{exc}）",
                invalid=["pub_time"],
            ) from exc
        if pd.isna(parsed):
            raise ContractError(
                cls.name, f"{where}字段 'pub_time' 为空", invalid=["pub_time"]
            )
        # 保留调用方的时间表示（str / Timestamp 均可），只保证可解析
        return record

    @classmethod
    def validate_list(
        cls, items: Any, *, field: str = "evidence", allow_empty: bool = True
    ) -> list[dict]:
        """校验证据列表（``evidence_for_stock`` / payload 里的 evidence）。"""
        if items is None:
            if allow_empty:
                return []
            raise ContractError(cls.name, f"{field} 为空（期望证据列表）")
        if isinstance(items, pd.DataFrame):
            raise ContractError(
                cls.name, f"{field} 应为 list[dict]，实际是 DataFrame"
            )
        if not isinstance(items, Iterable) or isinstance(items, (str, bytes)):
            raise ContractError(
                cls.name, f"{field} 应为 list[dict]，实际是 {type(items).__name__}"
            )
        out = [
            cls.validate(item, index=i + 1) for i, item in enumerate(list(items))
        ]
        if not allow_empty and not out:
            raise ContractError(cls.name, f"{field} 为空（期望至少一条证据）")
        return out


def validate_evidence(item: Any, *, index: int | None = None) -> dict:
    """``Evidence`` 契约的便捷入口，见 :meth:`Evidence.validate`。"""
    return Evidence.validate(item, index=index)


def validate_evidence_list(
    items: Any, *, field: str = "evidence", allow_empty: bool = True
) -> list[dict]:
    """``Evidence`` 列表校验，见 :meth:`Evidence.validate_list`。"""
    return Evidence.validate_list(items, field=field, allow_empty=allow_empty)


# --------------------------------------------------------------------------- #
# RecommendationRun
# --------------------------------------------------------------------------- #
RECOMMENDATION_REQUIRED_KEYS: tuple[str, ...] = (
    "schema_version",
    "run_id",
    "generated_at",
    "data_as_of",
    "tradable_from",
    "source",
    "market",
    "table",
    "evidence",
    "params",
    "diagnostics",
)
RECOMMENDATION_TABLE_COLUMNS: tuple[str, ...] = (
    "排名",
    "代码",
    "名称",
    "综合分",
    "技术分",
    "事件分",
    "建议",
)


class RecommendationRun:
    """推荐批次 payload 契约（``schema_version=2``，DATA-01）。

    必需字段：``schema_version``（必须为 2）、``run_id``、``generated_at``、
    ``data_as_of``、``tradable_from``、``source``、``market``、``table``、
    ``evidence``、``params``、``diagnostics``。``table`` 每行必须含
    :data:`RECOMMENDATION_TABLE_COLUMNS`；``evidence`` 的每条记录再过
    :class:`Evidence`；整个 payload 必须可 ``json.dumps(..., allow_nan=False)``。
    """

    name = "RecommendationRun"
    required_keys = RECOMMENDATION_REQUIRED_KEYS
    table_columns = RECOMMENDATION_TABLE_COLUMNS
    schema_version = 2

    @classmethod
    def validate(cls, payload: Any, *, source: str = "") -> dict:
        """校验推荐批次 payload；失败抛 :class:`ContractError`。"""
        if not isinstance(payload, Mapping):
            raise ContractError(
                cls.name, f"期望 dict payload，实际是 {type(payload).__name__}"
            )
        data = dict(payload)
        missing = [k for k in cls.required_keys if k not in data]
        if missing:
            raise ContractError(
                cls.name,
                f"payload 缺少字段 {missing}；实际字段: {sorted(data)}",
                missing=missing,
            )
        version = data.get("schema_version")
        if version != cls.schema_version or isinstance(version, bool):
            raise ContractError(
                cls.name,
                f"schema_version 必须为 {cls.schema_version}，实际 {version!r}",
                invalid=["schema_version"],
            )
        run_id = data.get("run_id")
        if not isinstance(run_id, str) or not run_id.strip():
            raise ContractError(
                cls.name, f"run_id 必须是非空字符串，实际 {run_id!r}", invalid=["run_id"]
            )
        for key in ("generated_at",):
            try:
                if pd.isna(pd.Timestamp(data[key])):
                    raise ValueError("NaT")
            except (ValueError, TypeError) as exc:
                raise ContractError(
                    cls.name,
                    f"{key} 无法解析：{data[key]!r}（{exc}）",
                    invalid=[key],
                ) from exc
        gen_ts = pd.Timestamp(data["generated_at"])
        for key in ("data_as_of", "tradable_from"):
            value = data.get(key)
            if value in (None, ""):
                continue
            try:
                pd.Timestamp(value)
            except (ValueError, TypeError) as exc:
                raise ContractError(
                    cls.name,
                    f"{key} 应为日期，实际 {value!r}（{exc}）",
                    invalid=[key],
                ) from exc
        table = data.get("table")
        if not isinstance(table, list):
            raise ContractError(
                cls.name,
                f"table 应为 list[dict]，实际是 {type(table).__name__}",
                invalid=["table"],
            )
        for i, row in enumerate(table):
            if not isinstance(row, Mapping):
                raise ContractError(
                    cls.name,
                    f"table 第 {i} 行应为 dict，实际 {type(row).__name__}",
                    invalid=["table"],
                )
            row_missing = [c for c in cls.table_columns if c not in row]
            if row_missing:
                raise ContractError(
                    cls.name,
                    f"table 第 {i} 行缺少列 {row_missing}；实际列: {sorted(row)}",
                    missing=row_missing,
                )
        evidence = data.get("evidence")
        if not isinstance(evidence, Mapping):
            raise ContractError(
                cls.name,
                f"evidence 应为 dict[code, list]，实际是 {type(evidence).__name__}",
                invalid=["evidence"],
            )
        validated: dict[str, list[dict]] = {}
        for code, items in evidence.items():
            records = Evidence.validate_list(items, field=f"evidence[{code}]")
            for i, item in enumerate(records):
                pub = pd.Timestamp(item["pub_time"])
                if pub > gen_ts:
                    raise ContractError(
                        cls.name,
                        f"evidence[{code}][{i}] pub_time {pub} 晚于 generated_at "
                        f"{gen_ts}（DATA-02）",
                        invalid=["evidence", str(code)],
                    )
            validated[str(code)] = records
        # 全部校验通过后再整体替换，避免校验中途失败时改写调用方 payload。
        data["evidence"] = validated
        _check_json_safe(data, cls.name)
        return data


def validate_recommendation_run(payload: Any, *, source: str = "") -> dict:
    """``RecommendationRun`` 契约的便捷入口，见 :meth:`RecommendationRun.validate`。"""
    return RecommendationRun.validate(payload, source=source)


def _check_json_safe(value: Any, contract: str, path: str = "payload") -> None:
    """拒绝 NaN/Infinity——它们会让 ``json.dumps(..., allow_nan=False)`` 崩溃。"""
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ContractError(
                contract,
                f"{path} 含非有限数值 {value!r}（allow_nan=False 序列化会失败）",
                invalid=[path],
            )
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            _check_json_safe(item, contract, f"{path}.{key}")
        return
    if isinstance(value, (list, tuple)):
        for i, item in enumerate(value):
            _check_json_safe(item, contract, f"{path}[{i}]")
        return
    if isinstance(value, (str, int, bool)) or value is None:
        return
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ContractError(
            contract,
            f"{path} 含无法 JSON 序列化的值 {type(value).__name__}（{exc}）",
            invalid=[path],
        ) from exc
