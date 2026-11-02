from __future__ import annotations

import gradio as gr
import pandas as pd

from .charts import (
    candlestick_figure,
    empty_figure,
    normalized_compare_figure,
    summary_markdown,
)
from .data import enrich, load_daily, load_minute, resample_ohlcv
from .tushare_client import TushareError, TusharePermissionError
from .utils import is_code, normalize_code

YEAR_MAP = {"1年": 365, "3年": 365 * 3, "5年": 365 * 5, "全部": None}
OVERLAY_MAP = {
    "MA5": "ma5",
    "MA10": "ma10",
    "MA20": "ma20",
    "MA60": "ma60",
    "MA120": "ma120",
    "MA250": "ma250",
}
DEFAULT_OVERLAYS = ["MA5", "MA20", "MA60", "MA120"]
ALL_OVERLAYS = [
    "MA5",
    "MA10",
    "MA20",
    "MA60",
    "MA120",
    "MA250",
    "布林带",
    "周线MA20",
    "月线MA12",
]
MINUTE_LABELS = {
    "1分钟": "1min",
    "5分钟": "5min",
    "15分钟": "15min",
    "30分钟": "30min",
    "60分钟": "60min",
}
MINUTE_RANGES = {"当日": 1, "近5日": 5, "近20日": 20, "近60日": 60}
PERIOD_CHOICES = ["日线", "周线", "月线", "季线", *MINUTE_LABELS]
DAILY_PANELS = ["日线", "周线", "月线", "季线"]
PERIOD_OVERLAYS = {
    "周线": ["ma5", "ma10", "ma20", "ma60"],
    "月线": ["ma5", "ma10", "ma20"],
    "季线": ["ma5", "ma10"],
}


def _range(years: str) -> tuple[str, str]:
    end = pd.Timestamp.today().strftime("%Y%m%d")
    days = YEAR_MAP.get(years)
    if days is None:
        return "20150101", end
    start = (pd.Timestamp.today() - pd.Timedelta(days=days)).strftime("%Y%m%d")
    return start, end


def _update(fig, visible: bool = True):
    return gr.update(value=fig, visible=visible)


def _error(message: str):
    return [
        _update(empty_figure("加载失败")),
        _update(empty_figure("—"), False),
        _update(empty_figure("—"), False),
        _update(empty_figure("—"), False),
        "",
        message,
    ]


def render(
    code_raw: str,
    years: str,
    mode: str,
    overlays: list[str],
    main_period: str = "日线",
    panel_periods: list[str] | None = None,
    minute_range: str = "当日",
):
    code = normalize_code(code_raw)
    if not is_code(code):
        return _error("❌ 代码格式不对，请输入如 `600519.SH`、`000001.SZ`、`300750.SZ`。")

    main_period = main_period or "日线"
    minute_days = MINUTE_RANGES.get(minute_range, 1)
    sel = set(overlays or [])
    overlay_cols = [OVERLAY_MAP[k] for k in ALL_OVERLAYS if k in sel and k in OVERLAY_MAP]
    show_boll = "布林带" in sel
    show_week = "周线MA20" in sel
    show_month = "月线MA12" in sel

    try:
        start, end = _range(years)
        daily = load_daily(code, start=start, end=end, adj="qfq")
        if daily.empty:
            raise TushareError("返回为空")

        daily_e = enrich(daily)
        weekly = resample_ohlcv(daily, "W-FRI")
        monthly = resample_ohlcv(daily, "ME")
        quarterly = resample_ohlcv(daily, "QE")
        weekly_e = enrich(weekly)
        monthly_e = enrich(monthly)
        quarterly_e = enrich(quarterly)

        start_actual = daily["trade_date"].min()
        end_actual = daily["trade_date"].max()
        period_label = {
            "1年": "近1年",
            "3年": "近3年",
            "5年": "近5年",
            "全部": "全部",
        }.get(years, years)

        summary = summary_markdown(
            code,
            daily_e,
            weekly_e,
            monthly_e,
            start_actual.strftime("%Y%m%d"),
            end_actual.strftime("%Y%m%d"),
            "qfq",
        )

        minute_used = main_period in MINUTE_LABELS
        if mode == "四宫格":
            minute_used = any(p in MINUTE_LABELS for p in (panel_periods or []))

        status = (
            f"✅ **{code}** · {period_label} · 日线 {len(daily)} 根 / 周线 {len(weekly)} 根 / "
            f"月线 {len(monthly)} 根 / 季线 {len(quarterly)} 根 · 前复权 · "
            f"{start_actual:%Y-%m-%d} ~ {end_actual:%Y-%m-%d}"
        )
        if minute_used:
            mode_text = "当日实时（rt_min）" if minute_days <= 1 else f"近{minute_days}日（stk_mins + rt_min）"
            status += f" · **分钟线：{mode_text}，前复权，30 秒缓存**"

        def build_period(period: str):
            if period == "日线":
                return daily_e, ["ma5", "ma20", "ma60", "ma120"], show_boll, False
            if period in PERIOD_OVERLAYS:
                frame = {"周线": weekly_e, "月线": monthly_e, "季线": quarterly_e}[period]
                return frame, PERIOD_OVERLAYS[period], False, False
            frame = enrich(load_minute(code, MINUTE_LABELS[period], days=minute_days))
            cols = overlay_cols or ["ma5", "ma20", "ma60"]
            return frame, cols, show_boll, True

        if mode == "四宫格":
            periods = [
                p for p in PERIOD_CHOICES if p in (panel_periods or [])
            ] or DAILY_PANELS
            periods = periods[:4]
            figs = []
            for p in periods:
                frame, cols, boll, intraday = build_period(p)
                figs.append(
                    candlestick_figure(
                        frame,
                        f"{code} {p}" + (f" · {minute_range}" if intraday else ""),
                        overlay_cols=cols,
                        show_boll=boll,
                        show_range_selector=False,
                        intraday=intraday,
                        height=430,
                    )
                )
            updates = [_update(fig) for fig in figs]
            updates += [_update(empty_figure("—"), False)] * (4 - len(updates))
            return updates + [summary, status]

        if mode == "区间对比":
            p1 = normalized_compare_figure(daily_e, code)
            return [_update(p1)] + [_update(empty_figure("—"), False)] * 3 + [summary, status]

        if main_period == "日线":
            p1 = candlestick_figure(
                daily_e,
                f"{code} 日线 · 长短期趋势叠加",
                overlay_cols=overlay_cols,
                weekly=weekly_e if show_week else None,
                monthly=monthly_e if show_month else None,
                show_boll=show_boll,
                height=720,
            )
        elif main_period in MINUTE_LABELS:
            frame = enrich(load_minute(code, MINUTE_LABELS[main_period], days=minute_days))
            p1 = candlestick_figure(
                frame,
                f"{code} {main_period} · {minute_range}",
                overlay_cols=overlay_cols or ["ma5", "ma20", "ma60"],
                show_boll=show_boll,
                intraday=True,
                height=720,
            )
        else:
            frame, cols, boll, _ = build_period(main_period)
            p1 = candlestick_figure(
                frame,
                f"{code} {main_period}",
                overlay_cols=cols,
                show_boll=boll,
                height=720,
            )
        return [_update(p1)] + [_update(empty_figure("—"), False)] * 3 + [summary, status]

    except TusharePermissionError as exc:
        return _error(f"❌ 权限不足：{exc}")
    except TushareError as exc:
        return _error(f"❌ 数据加载失败：{exc}")
    except Exception as exc:  # noqa: BLE001
        return _error(f"❌ 未预期错误：{exc}")


def build_multiperiod_tab() -> None:
    with gr.Tab("📈 多周期"):
        gr.Markdown(
            "### 多周期 K 线\n"
            "支持 **日/周/月/季** 与 **1/5/15/30/60 分钟**。单图可叠加周线 MA20、月线 MA12；"
            "四宫格可选 2–4 个面板；区间对比把不同时间区间按进度归一化后叠加。"
        )
        with gr.Row():
            code = gr.Textbox(
                value="600519.SH",
                label="代码",
                placeholder="600519.SH / 000001.SZ / 300750.SZ",
                scale=2,
            )
            years = gr.Radio(
                list(YEAR_MAP.keys()), value="3年", label="历史区间（日线）", scale=2
            )
            mode = gr.Radio(
                ["单图叠加", "四宫格", "区间对比"],
                value="单图叠加",
                label="展示模式",
                scale=3,
            )

        with gr.Row():
            main_period = gr.Dropdown(
                PERIOD_CHOICES, value="日线", label="单图周期", scale=2
            )
            panel_periods = gr.CheckboxGroup(
                choices=PERIOD_CHOICES,
                value=DAILY_PANELS,
                label="四宫格面板",
                info="仅四宫格模式生效；可只选 2 个做对比，最多渲染 4 个。",
                scale=5,
            )

        overlays = gr.CheckboxGroup(
            choices=ALL_OVERLAYS,
            value=DEFAULT_OVERLAYS,
            label="叠加项（单图叠加模式生效）",
            info="分钟周期不叠加周/月趋势线；布林带为 20 周期、2 倍标准差。",
        )
        minute_range = gr.Radio(
            choices=list(MINUTE_RANGES.keys()),
            value="当日",
            label="分钟区间",
            info="仅分钟周期生效：当日走 rt_min；近5/20/60日走 stk_mins 并补当日实时。",
        )

        with gr.Row():
            btn = gr.Button("生成图表", variant="primary", scale=2)
            gr.Markdown(
                "<sub>日/周/月/季为前复权；分钟线来自 Tushare `rt_min`/`stk_mins`，前复权，"
                "成交量已统一为手、成交额为千元。</sub>"
            )

        status = gr.Markdown()
        with gr.Row():
            p1 = gr.Plot(label="主图", scale=1)
            p2 = gr.Plot(label="图 2", visible=False, scale=1)
        with gr.Row():
            p3 = gr.Plot(label="图 3", visible=False, scale=1)
            p4 = gr.Plot(label="图 4", visible=False, scale=1)
        summary = gr.Markdown()

        inputs = [code, years, mode, overlays, main_period, panel_periods, minute_range]
        outputs = [p1, p2, p3, p4, summary, status]
        btn.click(render, inputs, outputs)
        code.submit(render, inputs, outputs)
