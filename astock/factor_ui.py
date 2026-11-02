from __future__ import annotations

import gradio as gr
import pandas as pd

from .factors import ic_figure, run_factor_ic


def run_factor(
    universe_text: str,
    universe_mode: str,
    years: str,
    horizon: int,
    rebalance: int,
    neutralize: bool,
    progress=gr.Progress(),
):
    try:
        result = run_factor_ic(
            universe_text=universe_text or "",
            years=years,
            horizon=int(horizon),
            rebalance=int(rebalance),
            universe_mode=universe_mode,
            neutralize=bool(neutralize),
            progress=progress,
        )
        return result.status, result.ic_table, result.quantile_table, ic_figure(result)
    except Exception as exc:  # noqa: BLE001
        return f"❌ 因子 IC 计算失败：{exc}", pd.DataFrame(), pd.DataFrame(), None


def build_factor_tab() -> None:
    with gr.Tab("📊 因子 IC"):
        gr.Markdown(
            "### 多因子 RankIC / 分层收益\n"
            "在股票池横截面上计算因子与未来 T+5/T+20 收益的 Spearman 相关（RankIC），"
            "并按因子分 5 层看 Q1–Q5 平均收益与 Q5-Q1。**股票池越小，IC 噪声越大，只作研究。**"
        )
        with gr.Row():
            universe = gr.Textbox(
                label="自定义股票池（逗号分隔，留空用下面选择）",
                placeholder="600519.SH,000001.SZ,300750.SZ",
                lines=2,
                scale=3,
            )
            universe_mode = gr.Radio(
                ["自选池", "市值前100", "市值前300", "动态前100（PIT）", "动态前300（PIT）"],
                value="自选池",
                label="股票池",
                scale=2,
            )
            years = gr.Radio(["1年", "3年"], value="3年", label="数据区间", scale=1)
        with gr.Row():
            horizon = gr.Radio([5, 20], value=5, label="前向窗口（交易日）", scale=1)
            rebalance = gr.Radio([5, 10], value=5, label="调仓间隔（交易日）", scale=1)
            neutralize = gr.Checkbox(value=True, label="行业 + 市值中性化", scale=1)
            btn = gr.Button("计算因子 IC", variant="primary", scale=2)
        gr.Markdown(
            "<sub>因子：20/60 日动量、5 日反转、20 日波动、量比、成交额比、Amihud 非流动性、"
            "MA20 乖离、RSI14、MACD 柱、ATR 占比。中性化对每日横截面回归行业哑变量 + log(总市值)，"
            "取残差；PIT 模式按每个调仓日的历史总市值选前 N，但仍只含当前仍上市股票。"
            "PIT/前300 首次加载较慢，建议先用自选池或前100。</sub>"
        )
        status = gr.Markdown()
        ic_table = gr.Dataframe(label="RankIC 汇总", interactive=False, wrap=True)
        quantile = gr.Dataframe(label="分层平均收益（%）", interactive=False, wrap=True)
        chart = gr.Plot(label="RankIC 均值")

        inputs = [universe, universe_mode, years, horizon, rebalance, neutralize]
        outputs = [status, ic_table, quantile, chart]
        btn.click(run_factor, inputs, outputs)
        universe.submit(run_factor, inputs, outputs)
