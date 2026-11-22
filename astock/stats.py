"""统计口径集中层。

所有显著性检验必须从本模块出：
- Newey-West：重叠窗口用 lag = horizon - 1；
- t 分布 p 值（不再用正态近似）；
- 有效独立样本 = 日期数 / 前向窗口；
- Benjamini-Hochberg 多重检验校正。

样本不足时返回 p=None，而不是给一个看起来显著的数字。

图表 transforms 的分组聚合（``mean/sum/min/max/count/last``）同样必须走本模块
（D11，2026-11-14 裁决）：图上均值与统计表同口径，空组/NaN 组保留并显式告警，
不允许渲染器另写一套 pandas 聚合。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

MIN_EFFECTIVE_N = 10.0

#: 分组聚合函数白名单（与 ChartSpec ``metrics`` 同构；单一定义点）。
GROUP_AGG_FUNCS = ("sum", "mean", "min", "max", "count", "last")


def _betacf(a: float, b: float, x: float) -> float:
    """Continued fraction for incomplete beta (Numerical Recipes)."""
    max_iter = 200
    eps = 3.0e-14
    fpmin = 1.0e-300
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < fpmin:
        d = fpmin
    d = 1.0 / d
    h = d
    for m in range(1, max_iter + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < fpmin:
            d = fpmin
        c = 1.0 + aa / c
        if abs(c) < fpmin:
            c = fpmin
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < fpmin:
            d = fpmin
        c = 1.0 + aa / c
        if abs(c) < fpmin:
            c = fpmin
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < eps:
            break
    return h


def _betai(a: float, b: float, x: float) -> float:
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    bt = math.exp(math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log(1.0 - x))
    if x < (a + 1.0) / (a + b + 2.0):
        return bt * _betacf(a, b, x) / a
    return 1.0 - bt * _betacf(b, a, 1.0 - x) / b


def student_t_p(t: float, df: float) -> float | None:
    """Two-sided Student-t p-value."""
    if not math.isfinite(t) or df <= 0:
        return None
    x = df / (df + t * t)
    try:
        return float(_betai(df / 2.0, 0.5, x))
    except (ValueError, OverflowError):
        return None


def normal_p(t: float) -> float:
    return float(2 * (1 - 0.5 * (1 + math.erf(abs(t) / math.sqrt(2)))))


@dataclass
class NWResult:
    n: int
    mean: float
    std: float
    t: float | None
    p: float | None
    se: float | None
    lag: int
    effective_n: float
    usable: bool


def effective_n(n: int, horizon: int) -> float:
    return max(1.0, float(n) / max(1, int(horizon)))


def newey_west(values, lags: int | None = None, horizon: int | None = None) -> NWResult:
    """Newey-West mean t test with Bartlett kernel.

    lags=None + horizon=h => lag=h-1，直接按重叠窗口阶数校正。
    """
    arr = np.asarray(values, dtype=float)
    arr = arr[np.isfinite(arr)]
    n = len(arr)
    if n < 2:
        return NWResult(n, float("nan"), float("nan"), None, None, None, 0, 0.0, False)
    mean = float(arr.mean())
    std = float(arr.std(ddof=1)) if n > 1 else 0.0
    if lags is None:
        lags = max(0, int(horizon) - 1) if horizon is not None else max(1, int(4 * (n / 100) ** (2 / 9)))
    lags = max(0, min(int(lags), max(0, n - 2)))
    e = arr - mean
    gamma0 = float((e * e).sum() / n)
    var = gamma0
    for lag in range(1, lags + 1):
        gamma = float((e[lag:] * e[:-lag]).sum() / n)
        var += 2.0 * (1.0 - lag / (lags + 1.0)) * gamma
    if var <= 0 or not math.isfinite(var):
        return NWResult(n, mean, std, None, None, None, lags, effective_n(n, horizon or 1), False)
    se = math.sqrt(var / n)
    t = mean / se if se > 0 else None
    p = student_t_p(t, n - 1) if t is not None else None
    eff = effective_n(n, horizon or 1)
    return NWResult(n, mean, std, t, p, se, lags, eff, eff >= MIN_EFFECTIVE_N)


def benjamini_hochberg(pvalues) -> list[float | None]:
    """BH adjusted p-values; None entries are preserved."""
    vals = [(i, p) for i, p in enumerate(pvalues) if p is not None and math.isfinite(float(p))]
    if not vals:
        return [None for _ in pvalues]
    vals.sort(key=lambda x: x[1])
    m = len(vals)
    adjusted = [None] * m
    running = 1.0
    for rank in range(m, 0, -1):
        _idx, p = vals[rank - 1]
        adj = min(1.0, p * m / rank)
        running = min(running, adj)
        adjusted[rank - 1] = running
    out: list[float | None] = [None for _ in pvalues]
    for (orig_idx, _), adj in zip(vals, adjusted):
        out[orig_idx] = adj
    return out


@dataclass
class GroupAggResult:
    """分组聚合结果（D11）：``frame`` 为聚合后的表，``warnings`` 供图注/审计留痕。"""

    frame: pd.DataFrame
    warnings: list[str] = field(default_factory=list)


def aggregate_groups(
    frame: pd.DataFrame,
    *,
    by,
    metrics,
    dropna: bool = False,
) -> GroupAggResult:
    """按 ``by`` 分组做 ``metrics`` 聚合（图表 transforms 的唯一实现，D11）。

    ``metrics`` 形如 ``{"close": "mean", "vol": "sum"}``，输出列名 ``<column>_<func>``。

    口径（2026-11-14 D11）：

    - ``dropna=False``（默认）保留 NaN 分组键，不静默丢组；
    - 空组/全 NaN 组保留，结果列按 pandas 语义为 NaN/0，并逐组写入 ``warnings``；
    - 分组顺序按首次出现（stable），重复取值保持原顺序；
    - 输出名与既有列或彼此冲突时追加数字后缀，并写入 ``warnings``；
    - 未支持的聚合函数直接抛 ``ValueError``（白名单 :data:`GROUP_AGG_FUNCS`）。
    """
    if not isinstance(frame, pd.DataFrame):
        raise TypeError("aggregate_groups 需要 pandas.DataFrame")
    by_list = [by] if isinstance(by, str) else list(by or [])
    if not by_list:
        raise ValueError("groupby 至少需要一个分组列")
    metrics = dict(metrics or {})
    if not metrics:
        raise ValueError("metrics 不能为空")
    warnings: list[str] = []
    for column in by_list:
        if column not in frame.columns:
            raise ValueError(f"分组列不存在：{column!r}")
    for column, func in metrics.items():
        if column not in frame.columns:
            raise ValueError(f"聚合列不存在：{column!r}")
        if str(func) not in GROUP_AGG_FUNCS:
            raise ValueError(f"不支持的聚合函数 {func!r}（允许 {GROUP_AGG_FUNCS}）")

    # 输出名冲突：与输入列同名、或两个 metric 生成同名时追加后缀（显式告警，不静默覆盖）。
    used = set(frame.columns) | set(by_list)
    rename: dict[str, str] = {}
    for column, func in metrics.items():
        base = f"{column}_{func}"
        name = base
        index = 2
        while name in used:
            name = f"{base}_{index}"
            index += 1
        if name != base:
            warnings.append(f"聚合输出 {base} 与既有列同名，改写为 {name}")
        used.add(name)
        rename[base] = name

    agg_spec = {f"{column}_{func}": (column, str(func)) for column, func in metrics.items()}
    grouped = frame.groupby(by_list, dropna=dropna, sort=False)
    aggregated = grouped.agg(**agg_spec)
    if rename:
        aggregated = aggregated.rename(columns=rename)
    result = aggregated.reset_index()

    # 空组/全 NaN 组：显式告警，绝不悄悄丢行（dropna=False 时 NaN 键本身也是一组）。
    key_rows = [tuple(row) for row in result[by_list].itertuples(index=False, name=None)]
    for column, func in metrics.items():
        out_name = rename[f"{column}_{func}"]
        if out_name not in result.columns:
            continue
        values = result[out_name]
        blanks = values == 0 if str(func) == "count" else values.isna()
        for keys, is_blank in zip(key_rows, blanks):
            if not bool(is_blank):
                continue
            label = keys[0] if len(by_list) == 1 else keys
            suffix = "记 0" if str(func) == "count" else "全为 NaN"
            warnings.append(f"分组 {label!r} 的 {out_name} {suffix}，已保留空组")
    return GroupAggResult(frame=result, warnings=warnings)
