from __future__ import annotations

import gradio as gr

from .use_cases import RunResult, run_alpha_banner, run_recommendation

NEWS_CHOICES = {"3日": 3, "7日": 7, "30日": 30}


def _outputs(result: RunResult):
    """页面只做结果映射，不再拼装 payload 或计算统计。"""
    payload = result.payload
    return (
        payload.get("status_text") or "",
        payload.get("table"),
        payload.get("report") or "",
        payload.get("evidence_md") or "",
        payload.get("files"),
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


def build_recommend_tab() -> gr.Markdown:
    with gr.Tab("📰 每日推荐"):
        banner = gr.Markdown(value=banner_markdown(), elem_id="alpha-banner")
        gr.Markdown(
            "### 每日股票推荐\n"
            "> ⚠️ **研究候选，不是交易指令。** 上方横幅实时展示实盘样本与显著性；"
            "若显示「样本不足」，说明还不能下结论。请结合仓位、流动性和自身风险承受能力复核。\n\n"
            "技术面（日线趋势/动量/量能/波动）+ 事件面（公告/研报/新闻/政策库）综合打分，"
            "再用 DeepSeek 做归因表述。**AI 不改变量化排序，不构成投资建议。**\n\n"
            "> 应用运行时，每个交易日 16:40 自动生成一次（`ASTOCK_AUTO_RECOMMEND=0` 可关闭；"
            "也可单独运行 `start_scheduler.bat`）。"
        )
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

        status = gr.Markdown()
        table = gr.Dataframe(label="推荐结果", interactive=False, wrap=True)
        report = gr.Markdown()
        with gr.Accordion("证据链（公告/研报/新闻/政策）", open=False):
            evidence = gr.Markdown()
        files = gr.File(label="下载结果（CSV / JSON）", file_count="multiple")

        btn.click(
            run_recommend,
            inputs=[universe, top_n, candidate_n, news_range, use_llm],
            outputs=[status, table, report, evidence, files],
        ).then(banner_markdown, outputs=banner)
        ledger_btn.click(
            load_latest_ledger,
            outputs=[status, table, report, evidence, files],
        ).then(banner_markdown, outputs=banner)
        universe.submit(
            run_recommend,
            inputs=[universe, top_n, candidate_n, news_range, use_llm],
            outputs=[status, table, report, evidence, files],
        ).then(banner_markdown, outputs=banner)

    return banner


def bind_banner_refresh(demo: gr.Blocks, banner: gr.Markdown) -> None:
    """Re-aggregate the banner on every page open/refresh.

    Building the Blocks runs once per process, so without this binding a
    scheduler run at 16:40 would never reach an already-open page.
    """
    demo.load(banner_markdown, outputs=banner)
