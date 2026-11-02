from __future__ import annotations

from typing import Any

import gradio as gr
import pandas as pd

from .optimize import grid_heatmap, grid_search, load_data, oos_figure, walk_forward
from .utils import is_code, normalize_code

METRIC_MAP = {"夏普": "sharpe", "年化收益": "annual"}
YEAR_CHOICES = ["1年", "3年", "5年", "全部"]


def _empty():
    return pd.DataFrame(), pd.DataFrame(), gr.update(value=None)


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


def run_optimize(
    code_raw: str,
    years: str,
    fast_min: int,
    fast_max: int,
    fast_step: int,
    slow_min: int,
    slow_max: int,
    slow_step: int,
    train_years: float,
    test_months: int,
    metric_label: str,
    progress=gr.Progress(),
):
    code = normalize_code(code_raw)
    if not is_code(code):
        message = "❌ 代码格式不对，请输入如 `600519.SH`。"
        return message, "", pd.DataFrame(), None, pd.DataFrame(), None
    fast_values = list(range(int(fast_min), int(fast_max) + 1, max(1, int(fast_step))))
    slow_values = list(range(int(slow_min), int(slow_max) + 1, max(1, int(slow_step))))
    if not fast_values or not slow_values:
        return "❌ 参数范围为空。", "", pd.DataFrame(), None, pd.DataFrame(), None

    metric = METRIC_MAP.get(metric_label, "sharpe")
    try:
        progress(0.05, desc="加载日线与基准")
        daily, bench = load_data(code, years)
        progress(0.20, desc="全样本参数网格")
        grid = grid_search(daily, code, fast_values, slow_values, bench, metric)
        progress(0.55, desc="滚动 walk-forward")
        wf = walk_forward(
            daily,
            code,
            fast_values,
            slow_values,
            bench,
            train_years=float(train_years),
            test_months=int(test_months),
            metric=metric,
        )
        progress(0.95, desc="汇总")
        best = grid.best
        m = wf.metrics
        best_md = (
            f"### 全样本最优（in-sample，仅参考）\n"
            f"- fast={int(best['fast'])} / slow={int(best['slow'])}；"
            f"夏普 {_num(best.get('夏普'))}，年化 {_num(best.get('年化收益%'))}%，"
            f"最大回撤 {_num(best.get('最大回撤%'))}%，交易 {best.get('交易次数')} 次。\n"
            f"- **Walk-forward 样本外**：{m['windows']} 个窗口；"
            f"累计 {_pct(m.get('total_return'))}，"
            f"年化 {_pct(m.get('annual_return'))}，"
            f"夏普 {_num(m.get('sharpe'))}，最大回撤 {_pct(m.get('max_drawdown'))}；"
            f"基准 {_pct(m.get('benchmark_return'))}，"
            f"超额 {_pct(m.get('excess_return'))}。\n\n"
            f"### 全样本前 5 组\n"
            + grid.grid.head(5)[["fast", "slow", "夏普", "年化收益%", "最大回撤%", "交易次数"]].to_markdown(index=False)
        )
        status = (
            f"✅ {code} · {years} · 网格 {len(grid.grid)} 组 · "
            f"walk-forward {m['windows']} 窗口（训练 {m['train_bars']} 根 / 测试 {m['test_bars']} 根）· "
            f"数据截止 {daily['trade_date'].max():%Y-%m-%d}"
        )
        return (
            status,
            best_md,
            grid.grid,
            grid_heatmap(grid.grid, metric),
            wf.walk,
            oos_figure(wf.equity, f"{code} Walk-forward 样本外净值"),
        )
    except Exception as exc:  # noqa: BLE001
        return f"❌ 参数优化失败：{exc}", "", pd.DataFrame(), None, pd.DataFrame(), None


def build_optimize_tab() -> None:
    with gr.Tab("🧪 参数优化"):
        gr.Markdown(
            "### MA 参数网格 + Walk-forward\n"
            "先用全样本网格找参数平原，再用滚动训练/测试窗口检验样本外表现。"
            "**全样本最优只是参考，不能直接当作未来参数。**"
        )
        with gr.Row():
            code = gr.Textbox(value="600519.SH", label="代码", scale=2)
            years = gr.Radio(YEAR_CHOICES, value="3年", label="数据区间", scale=3)
            metric = gr.Radio(list(METRIC_MAP.keys()), value="夏普", label="优化目标", scale=2)

        with gr.Row():
            fast_min = gr.Slider(2, 30, value=3, step=1, label="fast 最小")
            fast_max = gr.Slider(3, 60, value=21, step=1, label="fast 最大")
            fast_step = gr.Slider(1, 10, value=2, step=1, label="fast 步长")
            slow_min = gr.Slider(5, 120, value=10, step=5, label="slow 最小")
            slow_max = gr.Slider(10, 250, value=60, step=5, label="slow 最大")
            slow_step = gr.Slider(1, 20, value=5, step=1, label="slow 步长")

        with gr.Row():
            train_years = gr.Slider(1.0, 3.0, value=1.5, step=0.5, label="训练窗口（年）")
            test_months = gr.Radio([3, 6, 12], value=3, label="测试窗口（月）")
            btn = gr.Button("运行网格 + Walk-forward", variant="primary", scale=2)

        status = gr.Markdown()
        best = gr.Markdown()
        grid = gr.Dataframe(label="全样本参数网格", interactive=False, wrap=True)
        heatmap = gr.Plot(label="参数热力图")
        walk = gr.Dataframe(label="Walk-forward 窗口", interactive=False, wrap=True)
        oos = gr.Plot(label="样本外净值")

        inputs = [
            code,
            years,
            fast_min,
            fast_max,
            fast_step,
            slow_min,
            slow_max,
            slow_step,
            train_years,
            test_months,
            metric,
        ]
        outputs = [status, best, grid, heatmap, walk, oos]
        btn.click(run_optimize, inputs, outputs)
        code.submit(run_optimize, inputs, outputs)
