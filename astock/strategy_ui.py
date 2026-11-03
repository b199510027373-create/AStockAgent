from __future__ import annotations

import gradio as gr
import pandas as pd

from .strategy_lab import PortfolioResult, portfolio_figure, portfolio_walk_forward
from .recommend import parse_universe
from .utils import is_code, normalize_code


def _pct(value, digits: int = 2) -> str:
    try:
        if value is None or pd.isna(value):
            return "—"
        return f"{float(value) * 100:.{digits}f}%"
    except Exception:
        return "—"


def _num(value, digits: int = 2) -> str:
    try:
        if value is None or pd.isna(value):
            return "—"
        return f"{float(value):.{digits}f}"
    except Exception:
        return "—"


def run_lab(
    codes_text: str,
    strategy: str,
    years: str,
    train_years: float,
    test_months: int,
    metric_label: str,
    progress=gr.Progress(),
):
    metric = "sharpe" if metric_label == "夏普" else "annual"
    try:
        codes = parse_universe(codes_text)
        if not codes:
            raise ValueError("股票池为空")
        if len(codes) > 10:
            codes = codes[:10]
        progress(0.1, desc=f"加载并回测 {len(codes)} 只股票")
        result = portfolio_walk_forward(
            codes, strategy, years, float(train_years), int(test_months), metric, max_assets=10
        )
        m = result.metrics
        summary = (
            f"### 多标的样本外（等权）\n"
            f"- **累计** {_pct(m.get('total_return'))}，**年化** {_pct(m.get('annual_return'))}，"
            f"**夏普** {_num(m.get('sharpe'))}，**最大回撤** {_pct(m.get('max_drawdown'))}\n"
            f"- 沪深300 {_pct(m.get('benchmark_return'))}，**超额** {_pct(m.get('excess_return'))}；"
            f"标的 {m.get('assets')} 只，最少 {m.get('windows')} 个窗口\n\n"
            f"> 参数网格 {len(result.params)} 组；每只股票独立做 walk-forward，再等权拼接样本外收益。"
        )
        status = result.status
        progress(1.0, desc="完成")
        return status, summary, result.per_stock, result.windows, portfolio_figure(result)
    except Exception as exc:  # noqa: BLE001
        return f"❌ 策略实验室失败：{exc}", "", pd.DataFrame(), pd.DataFrame(), None


def build_strategy_lab_tab() -> None:
    with gr.Tab("🧪 策略实验室"):
        gr.Markdown(
            "### MACD / RSI / MA 参数网格 + 多标的 Walk-forward\n"
            "每只股票独立做滚动训练/测试，测试窗口**跨窗口连续持仓**（现金、持股、成本价、信号延续），"
            "再把各标的样本外收益等权拼接成组合；用于比较不同策略/参数在样本外的表现。**不构成投资建议。**"
        )
        with gr.Row():
            codes = gr.Textbox(
                value="600519.SH,000001.SZ,600036.SH",
                label="股票池（最多 10 只）",
                lines=2,
                scale=3,
            )
            strategy = gr.Radio(
                ["MA双均线", "MACD", "RSI反转"], value="MACD", label="策略", scale=2
            )
            years = gr.Radio(["1年", "3年", "5年"], value="3年", label="数据区间", scale=1)
        with gr.Row():
            train_years = gr.Slider(1.0, 3.0, value=1.5, step=0.5, label="训练窗口（年）")
            test_months = gr.Radio([3, 6, 12], value=3, label="测试窗口（月）")
            metric_label = gr.Radio(["夏普", "年化收益"], value="夏普", label="训练目标")
            btn = gr.Button("运行多标的 Walk-forward", variant="primary", scale=2)

        status = gr.Markdown()
        summary = gr.Markdown()
        per_stock = gr.Dataframe(label="分标的样本外", interactive=False, wrap=True)
        windows = gr.Dataframe(label="窗口明细", interactive=False, wrap=True)
        chart = gr.Plot(label="等权组合样本外净值")

        inputs = [codes, strategy, years, train_years, test_months, metric_label]
        outputs = [status, summary, per_stock, windows, chart]
        btn.click(run_lab, inputs, outputs)
        codes.submit(run_lab, inputs, outputs)
