from __future__ import annotations

import gradio as gr
import pandas as pd

from .backfill import backfill_technical
from .track import (
    nav_figure,
    nav_status_text,
    portfolio_nav,
    significance_table,
    track_figure,
    track_recommendations,
)


def run_backfill(progress=gr.Progress()):
    try:
        _, message = backfill_technical(days=30, top_n=5, progress=progress)
        return message
    except Exception as exc:  # noqa: BLE001
        return f"❌ 回填失败：{exc}"


def run_track():
    detail, summary, status = track_recommendations()
    nav, dropped = (
        portfolio_nav(detail) if not detail.empty else (pd.DataFrame(), pd.DataFrame())
    )
    nav_status = nav_status_text(nav, dropped)
    if not detail.empty:
        detail = detail.copy()
        for col in ("推荐日", "入场日"):
            if col in detail.columns:
                detail[col] = pd.to_datetime(detail[col]).dt.strftime("%Y-%m-%d")
    significance = significance_table(detail) if not detail.empty else pd.DataFrame()
    return (
        f"{nav_status}\n\n{status}",
        nav_figure(nav),
        summary,
        significance,
        track_figure(summary),
        detail,
    )


def build_track_tab() -> None:
    with gr.Tab("📈 推荐跟踪"):
        gr.Markdown(
            "### 推荐跟踪\n"
            "首屏是**跟随操作的结果**：每个批次在 `tradable_from`（下一交易日）开盘等权买入 Top5，"
            "持有到下一批次入场日换仓，按交易日拼接成组合净值，与沪深300 对照。\n\n"
            "> 净值口径：qfq 收盘计价，批次间按收盘 → 开盘拼接，不计交易成本与隔夜跳空；"
            "入场日缺 bar 的批次会被剔除并在上方提示。"
            "批次不足 20 个时只是跟踪记录，不构成 alpha 证据。"
        )
        with gr.Row():
            btn = gr.Button("刷新跟踪", variant="primary")
            backfill_btn = gr.Button("回填近30个交易日（仅技术分）", variant="secondary")
            gr.Markdown(
                "<sub>平均超额表与显著性检验保留在下方折叠区，供研究复核："
                "它们基于 38 个重叠窗口，容易被误读成“跟着买能赚”，因此不再作为首屏指标。"
                "回填批次事件分固定 50，只用于检验技术分排序。</sub>"
            )
        status = gr.Markdown()
        nav_chart = gr.Plot(label="Top5 等权组合净值 vs 沪深300")
        with gr.Accordion("平均超额表 / 显著性检验（重叠窗口，仅供参考）", open=False):
            summary = gr.Dataframe(label="分组统计（平均超额）", interactive=False, wrap=True)
            significance = gr.Dataframe(label="显著性检验", interactive=False, wrap=True)
            excess_chart = gr.Plot(label="分组平均超额")
        with gr.Accordion("逐条记录", open=False):
            detail = gr.Dataframe(label="逐条记录", interactive=False, wrap=True)

        outputs = [status, nav_chart, summary, significance, excess_chart, detail]
        btn.click(run_track, outputs=outputs)
        backfill_btn.click(run_backfill, outputs=[status]).then(run_track, outputs=outputs)
