from __future__ import annotations

import gradio as gr
import pandas as pd

from .backfill import backfill_technical
from .track import significance_table, track_figure, track_recommendations


def run_backfill(progress=gr.Progress()):
    try:
        _, message = backfill_technical(days=30, top_n=5, progress=progress)
        return message
    except Exception as exc:  # noqa: BLE001
        return f"❌ 回填失败：{exc}"


def run_track():
    detail, summary, status = track_recommendations()
    if not detail.empty:
        detail = detail.copy()
        for col in ("推荐日", "入场日"):
            if col in detail.columns:
                detail[col] = pd.to_datetime(detail[col]).dt.strftime("%Y-%m-%d")
    significance = significance_table(detail) if not detail.empty else pd.DataFrame()
    return status, summary, significance, detail, track_figure(summary)


def build_track_tab() -> None:
    with gr.Tab("📈 推荐跟踪"):
        gr.Markdown(
            "### 推荐跟踪\n"
            "读取 `data/recommendations/recommend_*.json`，从 `tradable_from`（下一交易日）**开盘**买入，"
            "跟踪 **T+1 / T+5 / T+20** 收盘，对比沪深300计算超额。\n\n"
            "> 显著性：Newey-West（lag = horizon−1）+ block bootstrap + BH 多重检验；"
            "有效独立样本 <10 时隐藏 p 值。T+20 在当前 38 个批次下约只有 2 个独立窗口，"
            "不能据此判断 alpha。回填批次事件分固定 50，只用于检验技术分排序。"
        )
        with gr.Row():
            btn = gr.Button("刷新跟踪", variant="primary")
            backfill_btn = gr.Button("回填近30个交易日（仅技术分）", variant="secondary")
            gr.Markdown(
                "<sub>前向收益口径：推荐日收盘 → T+N 收盘；超额 = 个股收益 - 沪深300 收益。"
                "t 值用正态近似，置信区间为 bootstrap 95%；样本不足 20 条时只作描述统计。"
                "回填批次事件分固定 50，只用于检验技术分排序。</sub>"
            )
        status = gr.Markdown()
        summary = gr.Dataframe(label="分组统计", interactive=False, wrap=True)
        significance = gr.Dataframe(label="显著性检验", interactive=False, wrap=True)
        chart = gr.Plot()
        detail = gr.Dataframe(label="逐条记录", interactive=False, wrap=True)

        btn.click(run_track, outputs=[status, summary, significance, detail, chart])
        backfill_btn.click(run_backfill, outputs=[status]).then(
            run_track, outputs=[status, summary, significance, detail, chart]
        )
