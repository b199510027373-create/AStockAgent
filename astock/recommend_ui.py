from __future__ import annotations

import gradio as gr
import pandas as pd

from . import ledger
from .recommend import run_daily_recommendation

NEWS_CHOICES = {"3日": 3, "7日": 7, "30日": 30}


def run_recommend(
    universe_text: str,
    top_n: int,
    candidate_n: int,
    news_range: str,
    use_llm: bool,
    progress=gr.Progress(),
):
    try:
        result = run_daily_recommendation(
            universe_text=universe_text or "",
            top_n=int(top_n),
            candidate_n=int(candidate_n),
            news_days=NEWS_CHOICES.get(news_range, 7),
            use_llm=bool(use_llm),
            progress=progress,
        )
        return (
            result.status,
            result.table,
            result.report,
            result.evidence_md,
            [result.csv_path, result.json_path],
        )
    except Exception as exc:  # noqa: BLE001
        return (
            f"❌ 生成失败：{exc}",
            pd.DataFrame(),
            "",
            "",
            None,
        )


def load_latest_ledger():
    """Load the latest recommendation from SQLite without re-running the pipeline."""
    run = ledger.latest_run()
    if not run:
        return "台账里还没有推荐记录。先运行一次生成，或执行 `python -m astock.ledger sync`。", pd.DataFrame(), "", "", None
    rows = []
    for item in run.get("items") or []:
        rows.append(
            {
                "排名": item.get("rank"),
                "代码": item.get("code"),
                "名称": item.get("name"),
                "行业": item.get("industry"),
                "综合分": item.get("composite"),
                "技术分": item.get("technical"),
                "事件分": item.get("event"),
                "建议": item.get("action"),
                "理由": item.get("reason"),
            }
        )
    table = pd.DataFrame(rows)
    report = (
        f"### SQLite 最近一次推荐\n"
        f"- run_id：`{run.get('run_id')}`\n"
        f"- data_as_of：`{run.get('data_as_of')}`\n"
        f"- tradable_from：`{run.get('tradable_from')}`\n"
        f"- generated_at：`{run.get('generated_at')}`\n"
        f"- source：`{run.get('source')}`\n"
    )
    lines = ["### 证据链（SQLite）", ""]
    for ev in (run.get("evidence") or [])[:30]:
        title = str(ev.get("title") or "")
        if ev.get("url"):
            title = f"[{title}]({ev['url']})"
        lines.append(
            f"- {str(ev.get('pub_time') or '')[:10]} · {ev.get('kind','')} · "
            f"{title}（{ev.get('source','')}）"
        )
    evidence_md = "\n".join(lines) if len(lines) > 2 else "暂无证据记录。"
    files = [x for x in (run.get("json_path"), run.get("csv_path")) if x]
    status = (
        f"✅ 已从 SQLite 读取 {run.get('run_id')}；"
        f"data_as_of={run.get('data_as_of')}，tradable_from={run.get('tradable_from')}；"
        f"{len(table)} 条推荐。"
    )
    return status, table, report, evidence_md, files or None


def build_recommend_tab() -> None:
    with gr.Tab("📰 每日推荐"):
        gr.Markdown(
            "### 每日股票推荐\n"
            "> ⚠️ **研究候选，不是交易指令。** 综合分尚未通过足够的实盘样本外验证；"
            "Top2 与其余的历史差异在统计上并不稳定。请结合仓位、流动性和自身风险承受能力复核。\n\n"
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
        )
        ledger_btn.click(
            load_latest_ledger,
            outputs=[status, table, report, evidence, files],
        )
        universe.submit(
            run_recommend,
            inputs=[universe, top_n, candidate_n, news_range, use_llm],
            outputs=[status, table, report, evidence, files],
        )
