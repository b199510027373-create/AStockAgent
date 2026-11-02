from __future__ import annotations

import numpy as np
import pandas as pd


def sma(series: pd.Series, n: int) -> pd.Series:
    return series.rolling(n, min_periods=n).mean()


def ema(series: pd.Series, n: int) -> pd.Series:
    return series.ewm(span=n, adjust=False, min_periods=n).mean()


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    """Wilder RSI。分母为 0 时按标准处理：只涨为 100，完全走平为 50。"""
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    avg_loss = loss.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    out = 100 - 100 / (1 + rs)
    out = out.where(avg_loss > 0, 100.0)
    out = out.where(~((avg_loss == 0) & (avg_gain == 0)), 50.0)
    return out


def macd(
    close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9
) -> pd.DataFrame:
    dif = ema(close, fast) - ema(close, slow)
    dea = dif.ewm(span=signal, adjust=False, min_periods=signal).mean()
    hist = (dif - dea) * 2
    return pd.DataFrame({"dif": dif, "dea": dea, "macd_hist": hist})


def boll(close: pd.Series, n: int = 20, k: float = 2.0) -> pd.DataFrame:
    mid = sma(close, n)
    std = close.rolling(n, min_periods=n).std(ddof=0)
    return pd.DataFrame(
        {"boll_mid": mid, "boll_upper": mid + k * std, "boll_lower": mid - k * std}
    )


def kdj(
    df: pd.DataFrame, n: int = 9, k_period: int = 3, d_period: int = 3
) -> pd.DataFrame:
    low_min = df["low"].rolling(n, min_periods=n).min()
    high_max = df["high"].rolling(n, min_periods=n).max()
    rsv = (df["close"] - low_min) / (high_max - low_min).replace(0, np.nan) * 100
    k = rsv.ewm(alpha=1 / k_period, adjust=False, min_periods=n).mean()
    d = k.ewm(alpha=1 / d_period, adjust=False, min_periods=n).mean()
    j = 3 * k - 2 * d
    return pd.DataFrame({"k": k, "d": d, "j": j})


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    high_low = df["high"] - df["low"]
    high_close = (df["high"] - df["close"].shift()).abs()
    low_close = (df["low"] - df["close"].shift()).abs()
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def obv(df: pd.DataFrame) -> pd.Series:
    direction = np.sign(df["close"].diff()).fillna(0)
    return (direction * df["vol"]).cumsum()


def cci(df: pd.DataFrame, n: int = 20) -> pd.Series:
    tp = (df["high"] + df["low"] + df["close"]) / 3
    ma = tp.rolling(n, min_periods=n).mean()
    md = (tp - ma).abs().rolling(n, min_periods=n).mean()
    return (tp - ma) / (0.015 * md.replace(0, np.nan))


def vr(df: pd.DataFrame, n: int = 26) -> pd.Series:
    up = df["vol"].where(df["close"] > df["close"].shift(), 0.0)
    down = df["vol"].where(df["close"] < df["close"].shift(), 0.0)
    down_sum = down.rolling(n, min_periods=n).sum()
    return up.rolling(n, min_periods=n).sum() / down_sum.replace(0, np.nan) * 100


def psy(close: pd.Series, n: int = 12) -> pd.Series:
    return (close > close.shift()).rolling(n, min_periods=n).sum() / n * 100


def add_all(df: pd.DataFrame) -> pd.DataFrame:
    """在 OHLCV DataFrame 上追加全套指标，不删除任何行。"""
    out = df.copy()
    close = out["close"]

    for n in (5, 10, 20, 60, 120, 250):
        out[f"ma{n}"] = sma(close, n)
    out["ema12"] = ema(close, 12)
    out["ema26"] = ema(close, 26)
    out["rsi14"] = rsi(close, 14)

    out = out.join(macd(close))
    out = out.join(boll(close, 20, 2.0))
    out = out.join(kdj(out))
    out["atr14"] = atr(out)
    out["obv"] = obv(out)
    out["cci20"] = cci(out)
    out["vr26"] = vr(out)
    out["psy12"] = psy(close, 12)
    out["vol_ma5"] = sma(out["vol"], 5)
    out["vol_ma20"] = sma(out["vol"], 20)

    return out.replace([np.inf, -np.inf], np.nan)
