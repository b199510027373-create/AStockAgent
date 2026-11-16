from __future__ import annotations

import gradio as gr
import pandas as pd

from .backtest import STRATEGIES, run_strategy
from .backtest_chart import backtest_figure, metrics_markdown
from .charts import empty_figure
from .events import stock_basic
from .utils import is_code, normalize_code

TRADE_COLUMNS = [
    "买入日期",
    "买入价",
    "卖出日期",
    "卖出价",
    "股数",
    "持有天数",
    "净盈亏",
    "收益率",
]
YEAR_CHOICES = ["1年", "3年", "5年", "全部"]


def _empty_trades() -> pd.DataFrame:
    return pd.DataFrame(columns=TRADE_COLUMNS)


def run(
    code_raw: str,
    strategy: str,
    years: str,
    fast: int,
    slow: int,
    initial_cash: float,
):
    code = normalize_code(code_raw)
    if not is_code(code):
        message = "❌ 代码格式不对，请输入如 `600519.SH`、`000001.SZ`。"
        return message, empty_figure("回测失败"), _empty_trades(), message
    if strategy == "MA双均线" and int(fast) >= int(slow):
        message = "❌ 快线周期必须小于慢线周期。"
        return message, empty_figure("参数不合法"), _empty_trades(), message

    try:
        basic = stock_basic()
        row = basic[basic["ts_code"] == code]
        name = str(row.iloc[0]["name"]) if len(row) else ""
        list_date = row.iloc[0].get("list_date") if len(row) else None
        result, daily = run_strategy(
            code,
            strategy,
            years=years,
            fast=int(fast),
            slow=int(slow),
            initial_cash=float(initial_cash),
            name=name,
            list_date=list_date,
        )
    except Exception as exc:  # noqa: BLE001
        message = f"❌ 回测失败：{exc}"
        return message, empty_figure("回测失败"), _empty_trades(), message

    label = strategy if strategy != "MA双均线" else f"MA双均线({int(fast)}/{int(slow)})"
    fig = backtest_figure(daily, result, code, label)
    metrics = metrics_markdown(result, label)

    trades = result.trades.copy()
    if trades.empty:
        trades = _empty_trades()
    else:
        trades["买入日期"] = pd.to_datetime(trades["买入日期"]).dt.strftime("%Y-%m-%d")
        trades["卖出日期"] = pd.to_datetime(trades["卖出日期"]).dt.strftime("%Y-%m-%d")

    status = (
        f"✅ **{code}** · {label} · {years} · "
        f"{int(result.metrics.get('bars', 0))} 根日线 · "
        f"初始资金 {float(initial_cash):,.0f} 元 · "
        f"最终权益 {result.metrics.get('final_equity', 0):,.2f} 元"
    )
    return metrics, fig, trades, status


def build_backtest_tab() -> None:
    with gr.Tab("🧪 回测"):
        gr.Markdown(
            "### 策略回测\n"
            "用日线历史数据验证策略：信号在收盘生成、**次日开盘成交**，"
            "包含 T+1、涨跌停不可成交、佣金/印花税/滑点。"
        )
        with gr.Row():
            code = gr.Textbox(
                value="600519.SH", label="代码", placeholder="600519.SH", scale=2
            )
            strategy = gr.Dropdown(
                choices=STRATEGIES,
                value="MA双均线",
                label="策略",
                scale=2,
            )
            years = gr.Radio(
                YEAR_CHOICES, value="3年", label="回测区间", scale=3
            )

        with gr.Row():
            fast = gr.Slider(2, 60, value=5, step=1, label="快线（MA）", scale=2)
            slow = gr.Slider(5, 250, value=20, step=1, label="慢线（MA）", scale=2)
            cash = gr.Number(
                value=500_000, label="初始资金（元）", minimum=10_000, step=10_000, scale=2
            )
            btn = gr.Button("运行回测", variant="primary", scale=1)

        status = gr.Markdown()
        metrics = gr.Markdown()
        chart = gr.Plot()
        trades = gr.Dataframe(
            label="交易明细（已平仓）",
            interactive=False,
            wrap=True,
        )

        inputs = [code, strategy, years, fast, slow, cash]
        outputs = [metrics, chart, trades, status]
        btn.click(run, inputs, outputs)
        code.submit(run, inputs, outputs)
