from __future__ import annotations

import gradio as gr
import pandas as pd

from .use_cases import (
    HISTORY_COLUMNS,
    RunResult,
    run_alpha_banner,
    run_recommendation,
    run_recommendation_history,
    run_selected_recommendation,
)

NEWS_CHOICES = {"3日": 3, "7日": 7, "30日": 30}

# ADR-010 C2：静态提示不再复述横幅的样本不足结论，避免同屏出现两处「未验证」表述。
RESEARCH_NOTE = (
    "> ⚠️ **研究候选，不是交易指令。** 上方横幅实时展示实盘样本与显著性，"
    "请结合仓位、流动性和自身风险承受能力复核。\n\n"
    "技术面（日线趋势/动量/量能/波动）+ 事件面（公告/研报/新闻/政策库）综合打分，"
    "再用 DeepSeek 做归因表述。**AI 不改变量化排序，不构成投资建议。**\n\n"
    "> 应用运行时，每个交易日 16:40 自动生成一次（`ASTOCK_AUTO_RECOMMEND=0` 可关闭；"
    "也可单独运行 `start_scheduler.bat`）。"
)


def _outputs(result: RunResult):
    """页面只做结果映射，不再拼装 payload 或计算统计。"""
    payload = result.payload
    return (
        payload.get("status_text") or "",
        payload.get("table"),
        payload.get("report") or "",
        payload.get("evidence_md") or "",
        payload.get("files"),
        payload.get("batch_md") or "",
    )


def banner_markdown() -> str:
    """横幅数字由用例层从 ledger/stats 实时聚合，UI 只取 Markdown。"""
    return str(run_alpha_banner().payload.get("banner_md") or "")


def run_recommend(
    universe_text: str,
    top_n: int,
    candidate_n: int,
    news_range: str,
    use_llm: bool,
    progress=gr.Progress(),
):
    result = run_recommendation(
        mode="generate",
        universe_text=universe_text or "",
        top_n=int(top_n),
        candidate_n=int(candidate_n),
        news_days=NEWS_CHOICES.get(news_range, 7),
        use_llm=bool(use_llm),
        progress=progress,
    )
    return _outputs(result)


def load_latest_ledger():
    """读取台账最近一批；渲染与新鲜度判断都在用例层完成。"""
    return _outputs(run_recommendation(mode="latest"))


def _initial_history() -> tuple[pd.DataFrame, list[str], str]:
    """构建页面时的历史列表初始值；台账不可用时降级为空表，不阻断应用启动。"""
    try:
        payload = run_recommendation_history().payload
        table = payload.get("history")
        if not isinstance(table, pd.DataFrame):
            table = pd.DataFrame(columns=list(HISTORY_COLUMNS))
        return table, list(payload.get("run_ids") or []), str(
            payload.get("status_text") or ""
        )
    except Exception as exc:  # noqa: BLE001 - UI 装配不得因台账故障崩溃
        message = f"读取台账失败：{type(exc).__name__}: {exc}"
        return pd.DataFrame(columns=list(HISTORY_COLUMNS)), [], f"❌ {message}"


def load_history_list():
    """刷新历史批次列表与下拉选项；只读 ledger。"""
    table, run_ids, status_text = _initial_history()
    return table, gr.update(choices=run_ids), status_text


def load_history_run(run_id: str | None):
    """加载下拉框选中的批次；空选择由用例层给出明确提示。"""
    return _outputs(run_selected_recommendation(run_id))


def build_recommend_tab() -> gr.Markdown:
    history_value, history_choices, history_status_value = _initial_history()
    with gr.Tab("📰 每日推荐"):
        banner = gr.Markdown(value=banner_markdown(), elem_id="alpha-banner")
        gr.Markdown("### 每日股票推荐\n" + RESEARCH_NOTE)
        with gr.Row():
            universe = gr.Textbox(
                label="股票池（逗号分隔，留空用 stocks.csv）",
                placeholder="600519.SH,000001.SZ,300750.SZ",
                lines=2,
                scale=3,
            )
            top_n = gr.Slider(3, 10, value=5, step=1, label="输出数量", scale=1)
            candidate_n = gr.Slider(8, 30, value=15, step=1, label="技术预筛数量", scale=1)
            news_range = gr.Radio(
                list(NEWS_CHOICES.keys()), value="7日", label="事件窗口", scale=1
            )
        with gr.Row():
            use_llm = gr.Checkbox(value=True, label="用 DeepSeek 生成理由与风险")
            btn = gr.Button("生成今日推荐", variant="primary", scale=2)
            ledger_btn = gr.Button("加载最近一次（SQLite）", variant="secondary", scale=1)

        with gr.Row():
            history = gr.Dataframe(
                value=history_value,
                label="历史批次（SQLite 台账）",
                interactive=False,
                wrap=True,
                scale=3,
            )
            with gr.Column(scale=1):
                run_picker = gr.Dropdown(
                    choices=history_choices,
                    value=history_choices[0] if history_choices else None,
                    label="选择 run_id",
                )
                load_run_btn = gr.Button("加载所选批次", variant="secondary")
                refresh_history_btn = gr.Button("刷新历史列表", variant="secondary")
                history_status = gr.Markdown(value=history_status_value)

        status = gr.Markdown()
        table = gr.Dataframe(label="推荐结果", interactive=False, wrap=True)
        batch_info = gr.Markdown()
        report = gr.Markdown()
        with gr.Accordion("证据链（公告/研报/新闻/政策）", open=False):
            evidence = gr.Markdown()
        files = gr.File(label="下载结果（CSV / JSON）", file_count="multiple")

        detail_outputs = [status, table, report, evidence, files, batch_info]
        history_outputs = [history, run_picker, history_status]

        btn.click(
            run_recommend,
            inputs=[universe, top_n, candidate_n, news_range, use_llm],
            outputs=detail_outputs,
        ).then(load_history_list, outputs=history_outputs).then(
            banner_markdown, outputs=banner
        )
        ledger_btn.click(
            load_latest_ledger,
            outputs=detail_outputs,
        ).then(load_history_list, outputs=history_outputs).then(
            banner_markdown, outputs=banner
        )
        load_run_btn.click(
            load_history_run,
            inputs=[run_picker],
            outputs=detail_outputs,
        ).then(banner_markdown, outputs=banner)
        refresh_history_btn.click(load_history_list, outputs=history_outputs)
        universe.submit(
            run_recommend,
            inputs=[universe, top_n, candidate_n, news_range, use_llm],
            outputs=detail_outputs,
        ).then(load_history_list, outputs=history_outputs).then(
            banner_markdown, outputs=banner
        )

    return banner


def bind_banner_refresh(demo: gr.Blocks, banner: gr.Markdown) -> None:
    """Re-aggregate the banner on every page open/refresh.

    Building the Blocks runs once per process, so without this binding a
    scheduler run at 16:40 would never reach an already-open page.
    """
    demo.load(banner_markdown, outputs=banner)
