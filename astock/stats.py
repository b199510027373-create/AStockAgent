"""统计口径集中层。

所有显著性检验必须从本模块出：
- Newey-West：重叠窗口用 lag = horizon - 1；
- t 分布 p 值（不再用正态近似）；
- 有效独立样本 = 日期数 / 前向窗口；
- Benjamini-Hochberg 多重检验校正。

样本不足时返回 p=None，而不是给一个看起来显著的数字。
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

MIN_EFFECTIVE_N = 10.0


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
    bt = math.exp(
        math.lgamma(a + b)
        - math.lgamma(a)
        - math.lgamma(b)
        + a * math.log(x)
        + b * math.log(1.0 - x)
    )
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
        if horizon is not None:
            lags = max(0, int(horizon) - 1)
        else:
            lags = max(1, int(4 * (n / 100) ** (2 / 9)))
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
        idx, p = vals[rank - 1]
        adj = min(1.0, p * m / rank)
        running = min(running, adj)
        adjusted[rank - 1] = running
    out: list[float | None] = [None for _ in pvalues]
    for (orig_idx, _), adj in zip(vals, adjusted):
        out[orig_idx] = adj
    return out
