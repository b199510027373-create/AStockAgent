"""用例层：推荐/跟踪的统一入口与结果契约（ADR-002）。

交互层（Gradio 页面、调度器脚本）只调用本模块，拿到 :class:`RunResult` 后
直接消费 ``payload``；不再自己拼装表格、报告、证据链或统计量。

``status`` 语义：

- ``success``：用例完成，结果可用。
- ``stale``：能拿到结果，但数据未更新/前向收益尚未产生，需要等数据源刷新。
- ``failed``：没有可用结果（未生成、台账无记录、计算异常等）。

``stale`` / ``failed`` 必须在 ``diagnostics["message"]`` 里给出可读诊断，
UI 会把它直接展示给用户。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import pandas as pd

from . import ledger
from .contracts import ContractError
from .data import last_completed_trade_date
from .recommend import (
    StaleDataError,
    generate_recommendation,
    normalize_table,
)
from .track import (
    alpha_banner_error_markdown,
    alpha_banner_markdown,
    alpha_banner_snapshot,
    compute_tracking,
    nav_figure,
    nav_status_text,
    portfolio_nav,
    significance_table,
    track_figure,
)

__all__ = [
    "HISTORY_COLUMNS",
    "RUN_STATUSES",
    "RunResult",
    "run_alpha_banner",
    "run_recommendation",
    "run_recommendation_history",
    "run_selected_recommendation",
    "run_tracking",
]

RUN_STATUSES = ("success", "stale", "failed")
RECOMMEND_COLUMNS = (
    "排名",
    "代码",
    "名称",
    "行业",
    "综合分",
    "技术分位",
    "技术分",
    "事件分",
    "收盘",
    "20日%",
    "RSI",
    "技术面",
    "理由",
)
HISTORY_COLUMNS = (
    "run_id",
    "data_as_of",
    "tradable_from",
    "generated_at",
    "source",
    "status",
    "items",
)


@dataclass
class RunResult:
    """用例层统一返回值。

    Attributes:
        run_id: 推荐批次 id；没有批次（失败/未生成）时为空字符串。
        status: ``success`` / ``stale`` / ``failed``。
        payload: 交互层可直接消费的结果（DataFrame/字符串/图等）。
        diagnostics: 诊断信息；stale/failed 必含可读 ``message``。
    """

    run_id: str
    status: str
    payload: dict[str, Any] = field(default_factory=dict)
    diagnostics: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.status not in RUN_STATUSES:
            raise ValueError(
                f"非法 RunResult.status={self.status!r}，只允许 {RUN_STATUSES}"
            )
        self.run_id = str(self.run_id or "")
        if not isinstance(self.payload, dict):
            raise TypeError("RunResult.payload 必须是 dict")
        if not isinstance(self.diagnostics, dict):
            raise TypeError("RunResult.diagnostics 必须是 dict")
        if self.status in ("stale", "failed"):
            message = str(self.diagnostics.get("message") or "").strip()
            if not message:
                raise ValueError(
                    f"status={self.status} 必须在 diagnostics['message'] 中给出可读诊断"
                )

    @property
    def ok(self) -> bool:
        return self.status == "success"

    @property
    def message(self) -> str:
        return str(self.diagnostics.get("message") or "")


# ---------------------------------------------------------------------------
# 推荐：生成与历史读取共用同一套渲染
# ---------------------------------------------------------------------------


def _empty_recommend_payload() -> dict[str, Any]:
    return {
        "table": pd.DataFrame(columns=list(RECOMMEND_COLUMNS)),
        "report": "",
        "generation_status": "",
        "evidence_md": "",
        "files": None,
        "csv_path": None,
        "json_path": None,
        "status_text": "",
        "status_line": "",
        "data_as_of": "",
        "tradable_from": "",
        "generated_at": "",
        "source": "",
        "run_id": "",
        "read_source": "ledger",
        "diagnostics": {},
        "params": {},
        "batch_md": "",
    }


def _history_report(payload: dict[str, Any]) -> str:
    return (
        "### SQLite 推荐记录\n"
        f"- run_id：`{payload.get('run_id') or ''}`\n"
        f"- data_as_of：`{payload.get('data_as_of') or ''}`\n"
        f"- tradable_from：`{payload.get('tradable_from') or ''}`\n"
        f"- generated_at：`{payload.get('generated_at') or ''}`\n"
        f"- source：`{payload.get('source') or ''}`\n"
    )


def _evidence_markdown_from_ledger(evidence: Any) -> str:
    """把台账证据（list，带 code）或生成证据（dict[code, list]）渲染成 Markdown。"""
    grouped: dict[str, list[dict]] = {}
    if isinstance(evidence, dict):
        for code, items in evidence.items():
            grouped[str(code)] = [ev for ev in (items or []) if isinstance(ev, dict)]
    elif isinstance(evidence, list):
        for ev in evidence:
            if not isinstance(ev, dict):
                continue
            grouped.setdefault(str(ev.get("code") or ""), []).append(ev)

    lines = ["### 证据链", ""]
    shown = 0
    for code, items in grouped.items():
        if shown >= 30:
            break
        if code:
            lines.append(f"**{code}**")
        for ev in items:
            if shown >= 30:
                break
            title = str(ev.get("title") or "")
            if ev.get("url"):
                title = f"[{title}]({ev['url']})"
            date = str(ev.get("pub_time") or "")[:10]
            lines.append(
                f"- {date} · {ev.get('kind', '')} · {title}（{ev.get('source', '')}）"
            )
            shown += 1
        lines.append("")
    if shown == 0:
        lines.append("暂无证据记录。")
    return "\n".join(lines)


def _diagnostics_markdown(diagnostics: Any) -> list[str]:
    """把台账里的批次 diagnostics 渲染成页面可读的明细，避免整段 JSON。"""
    if not isinstance(diagnostics, dict) or not diagnostics:
        return ["- 台账未记录 diagnostics（该批次可能由旧版本或 JSON 导入生成）。"]
    lines: list[str] = []
    for key in sorted(diagnostics):
        value = diagnostics[key]
        if key == "candidate_pool" and isinstance(value, (list, tuple)):
            lines.append(f"- `{key}`：{len(value)} 项")
        elif isinstance(value, (list, tuple)):
            if not value:
                lines.append(f"- `{key}`：0 项")
            else:
                head = ", ".join(str(x)[:80] for x in list(value)[:5])
                suffix = f" …（共 {len(value)} 项）" if len(value) > 5 else ""
                lines.append(f"- `{key}`：{head}{suffix}")
        elif isinstance(value, dict):
            lines.append(f"- `{key}`：{len(value)} 项")
        else:
            lines.append(f"- `{key}`：{str(value)[:200]}")
    if len(lines) > 40:
        lines = lines[:40] + [f"- …其余 {len(lines) - 40} 项已省略"]
    return lines


def _batch_markdown(
    payload: dict[str, Any],
    *,
    status: str,
    diagnostics: dict[str, Any],
) -> str:
    """批次身份、新鲜度与 diagnostics 的统一展示块（生成与历史读取共用）。"""
    run_id = str(payload.get("run_id") or "")
    if not run_id:
        return ""
    lines = [
        "### 批次信息（SQLite 台账）",
        f"- run_id：`{run_id}`",
        f"- data_as_of：`{payload.get('data_as_of') or '—'}`",
        f"- tradable_from：`{payload.get('tradable_from') or '—'}`",
        f"- generated_at：`{payload.get('generated_at') or '—'}`",
        f"- source：`{payload.get('source') or '—'}`",
        f"- 读取来源：`{payload.get('read_source') or 'ledger'}`",
        f"- 状态：`{status}`",
    ]
    if diagnostics.get("expected_trade_date"):
        lines.append(f"- expected_trade_date：`{diagnostics['expected_trade_date']}`")
    lines.extend(["", "<details><summary>diagnostics（台账记录）</summary>", ""])
    lines.extend(_diagnostics_markdown(payload.get("diagnostics")))
    lines.extend(["", "</details>"])
    generation_status = str(payload.get("generation_status") or "").strip()
    if generation_status:
        lines.extend(
            [
                "",
                "<details><summary>生成摘要（台账记录）</summary>",
                "",
                generation_status,
                "",
                "</details>",
            ]
        )
    if payload.get("json_path"):
        lines.extend(
            [
                "",
                f"> JSON 归档：`{payload['json_path']}`"
                "（仅归档下载，页面主读源为 SQLite 台账）",
            ]
        )
    return "\n".join(lines)


def _render_recommendation(
    payload: dict[str, Any],
    *,
    status: str,
    diagnostics: dict[str, Any],
) -> dict[str, Any]:
    """推荐用例的唯一渲染出口（生成、最新、指定 run_id 都走这里）。"""
    table = payload.get("table")
    if not isinstance(table, pd.DataFrame):
        table = pd.DataFrame(table or [])
    if table.empty and not len(table.columns):
        table = pd.DataFrame(columns=list(RECOMMEND_COLUMNS))
    # ADR-010 C1（兼容映射）：旧批次的「建议」/旧动作词在此统一映射为研究语义并列。
    table = normalize_table(table)

    report = str(payload.get("report") or "")
    if not report and payload.get("run_id"):
        # 只有真正读到了批次才补台账摘要；失败/未生成时留空，避免伪造“SQLite 记录”。
        report = _history_report(payload)
    evidence_md = str(payload.get("evidence_md") or "")
    if not evidence_md and payload.get("run_id"):
        evidence_md = _evidence_markdown_from_ledger(payload.get("evidence"))

    csv_path = payload.get("csv_path") or None
    json_path = payload.get("json_path") or None
    files = [str(x) for x in (csv_path, json_path) if x] or None

    base = str(payload.get("status_line") or "")
    message = str(diagnostics.get("message") or "").strip()
    if status == "failed":
        status_text = f"❌ {message}"
    elif status == "stale":
        status_text = f"⚠️ {message}"
        if base:
            status_text += f"\n\n{base}"
    else:
        status_text = base or f"已加载 {len(table)} 条推荐"
        # 对勾/警告由渲染器按 status 统一添加，status_line 只保留事实描述。
        if not status_text.lstrip().startswith(("✅", "⚠️", "❌")):
            status_text = f"✅ {status_text}"
        if diagnostics.get("calendar_error"):
            status_text += "\n\n⚠️ 交易日历不可用，未校验数据新鲜度。"

    # 分位口径披露（ADR-010 C1）：表格列名固定为「技术分位」，同屏给出池大小 N，
    # 避免跨 candidate_n 批次把不同基准的数值直接比较。
    basis_n = int(
        (diagnostics or {}).get("candidate_n")
        or (payload.get("params") or {}).get("candidate_n")
        or len(table)
        or 0
    )
    if basis_n and "技术分位" in table.columns:
        status_text += f"\n\n> 分位基准：候选池（N={basis_n}），数值为综合分在池内的 rank(pct)。"

    batch_md = _batch_markdown(payload, status=status, diagnostics=diagnostics)

    return {
        "table": table,
        "report": report,
        "generation_status": str(payload.get("generation_status") or ""),
        "evidence_md": evidence_md,
        "files": files,
        "csv_path": csv_path,
        "json_path": json_path,
        "status_text": status_text,
        "status_line": base,
        "data_as_of": str(payload.get("data_as_of") or ""),
        "tradable_from": str(payload.get("tradable_from") or ""),
        "generated_at": str(payload.get("generated_at") or ""),
        "source": str(payload.get("source") or ""),
        "run_id": str(payload.get("run_id") or ""),
        "read_source": str(payload.get("read_source") or "ledger"),
        "diagnostics": payload.get("diagnostics") or {},
        "params": payload.get("params") or {},
        "batch_md": batch_md,
    }


def _failed_recommendation(message: str, *, reason: str) -> RunResult:
    diagnostics = {"reason": reason, "message": message}
    payload = _render_recommendation(
        _empty_recommend_payload(), status="failed", diagnostics=diagnostics
    )
    return RunResult("", "failed", payload, diagnostics)


def _ledger_view(
    run: dict[str, Any],
    *,
    mode: str,
    now: datetime | None = None,
) -> RunResult:
    run_id = str(run.get("run_id") or "")
    data_as_of = str(run.get("data_as_of") or run.get("as_of") or "")
    tradable_from = str(run.get("tradable_from") or "")
    rows = [
        {
            "排名": item.get("rank"),
            "代码": item.get("code"),
            "名称": item.get("name"),
            "行业": item.get("industry"),
            "综合分": item.get("composite"),
            "技术分位": item.get("percentile"),
            "技术分": item.get("technical"),
            "事件分": item.get("event"),
            "收盘": item.get("close"),
            "20日%": item.get("ret20"),
            "RSI": item.get("rsi"),
            "技术面": item.get("action"),
            "理由": item.get("reason"),
        }
        for item in (run.get("items") or [])
    ]
    payload = {
        "table": pd.DataFrame(rows, columns=list(RECOMMEND_COLUMNS)),
        "report": str(run.get("report_md") or ""),
        "generation_status": str(run.get("status_line") or ""),
        "evidence": run.get("evidence") or [],
        "csv_path": run.get("csv_path"),
        "json_path": run.get("json_path"),
        "data_as_of": data_as_of,
        "tradable_from": tradable_from,
        "generated_at": str(run.get("generated_at") or ""),
        "source": str(run.get("source") or ""),
        "run_id": run_id,
        "read_source": "ledger",
        "diagnostics": (
            run.get("diagnostics") if isinstance(run.get("diagnostics"), dict) else {}
        ),
        "params": run.get("params") if isinstance(run.get("params"), dict) else {},
    }
    if mode == "generate":
        payload["status_line"] = (
            f"已生成并写入 SQLite 台账 {run_id}；从台账读取 data_as_of={data_as_of}，"
            f"tradable_from={tradable_from}；{len(rows)} 条推荐。"
        )
    else:
        payload["status_line"] = (
            f"已从 SQLite 读取 {run_id}；data_as_of={data_as_of}，"
            f"tradable_from={tradable_from}；{len(rows)} 条推荐。"
        )

    batch = payload["diagnostics"]
    diagnostics: dict[str, Any] = {
        "reason": {
            "generate": "generated",
            "latest": "ledger_latest",
        }.get(mode, "ledger_run"),
        "message": "",
        "data_as_of": data_as_of,
        "tradable_from": tradable_from,
        "run_id": run_id,
        "read_source": "ledger",
        "batch_diagnostics": batch,
    }
    if batch.get("candidate_n") not in (None, ""):
        try:
            diagnostics["candidate_n"] = int(batch["candidate_n"])
        except (TypeError, ValueError):
            pass

    now = now or datetime.now()
    stale = False
    expected = None
    if mode == "generate" and "stale_data" in batch:
        # 生成批次的新鲜度结论已在生成时计算并随台账持久化；此处直接采用，
        # 避免用读取时刻的日历重算导致同一批次出现两种口径。
        stale = bool(batch.get("stale_data"))
        persisted_expected = batch.get("expected_trade_date")
        if persisted_expected:
            try:
                diagnostics["expected_trade_date"] = pd.Timestamp(
                    persisted_expected
                ).strftime("%Y-%m-%d")
            except (TypeError, ValueError):
                diagnostics["expected_trade_date"] = str(persisted_expected)
        diagnostics["freshness_source"] = "generation"
        if not data_as_of:
            stale = True
            diagnostics["message"] = (
                "该推荐记录缺少 data_as_of，无法确认数据新鲜度；请重新生成后再使用。"
            )
        elif stale:
            expected_text = diagnostics.get("expected_trade_date") or "最近交易日"
            diagnostics["message"] = (
                f"数据未更新：data_as_of={data_as_of} < expected={expected_text}；"
                "本次结果仅供参考，建议等数据源更新后重新生成。"
            )
    else:
        try:
            expected = last_completed_trade_date(now)
        except Exception as exc:  # noqa: BLE001 - 日历不可用时不应阻断历史读取
            diagnostics["calendar_error"] = f"{type(exc).__name__}: {exc}"

        if expected is not None:
            diagnostics["expected_trade_date"] = pd.Timestamp(expected).strftime(
                "%Y-%m-%d"
            )
        diagnostics["freshness_source"] = "calendar"
        if not data_as_of:
            stale = True
            diagnostics["message"] = (
                "该推荐记录缺少 data_as_of，无法确认数据新鲜度；请重新生成后再使用。"
            )
        elif expected is None:
            # 日历不可用时无法确认新鲜度：保持 success，由渲染器附加“未校验”提示。
            diagnostics["freshness_checked"] = False
        else:
            try:
                as_of_ts = pd.Timestamp(data_as_of)
            except Exception:
                stale = True
                diagnostics["message"] = (
                    f"该推荐记录 data_as_of={data_as_of} 无法解析，无法确认数据新鲜度；"
                    "请重新生成后再使用。"
                )
            else:
                # 与生成侧 recommend.py 同口径：落后于最近交易日即 stale，不加收盘时间门限。
                stale = as_of_ts < pd.Timestamp(expected)
                if stale:
                    diagnostics["message"] = (
                        f"最近一次推荐 data_as_of={data_as_of} 早于最近交易日 "
                        f"{pd.Timestamp(expected):%Y-%m-%d}；历史结果仅供参考，请重新生成。"
                    )
    diagnostics["stale_data"] = stale
    status = "stale" if stale else "success"
    view = _render_recommendation(payload, status=status, diagnostics=diagnostics)
    return RunResult(run_id, status, view, diagnostics)


def run_recommendation(
    mode: str = "generate",
    *,
    run_id: str | None = None,
    universe_text: str = "",
    top_n: int = 5,
    candidate_n: int = 15,
    news_days: int = 7,
    use_llm: bool = True,
    progress=None,
    now: datetime | None = None,
) -> RunResult:
    """推荐用例统一入口。

    Args:
        mode: ``generate`` 生成新批次并立即从台账读回；``latest`` 读取台账
            最近一批；``run`` 按 ``run_id`` 读取指定批次。
        run_id: ``mode="run"`` 时必填。
        now: 仅历史读取用于判断数据是否落后；生成路径的流水线不使用此参数，
            批次新鲜度以生成时写入台账的 diagnostics 为准。
    """
    if mode not in ("generate", "latest", "run"):
        raise ValueError(f"未知 mode={mode!r}，只支持 generate/latest/run")
    if mode == "run" and not run_id:
        raise ValueError("mode='run' 时必须提供 run_id")

    if mode == "generate":
        try:
            rec = generate_recommendation(
                universe_text=universe_text,
                top_n=top_n,
                candidate_n=candidate_n,
                news_days=news_days,
                use_llm=use_llm,
                progress=progress,
            )
        except StaleDataError as exc:
            diagnostics = {
                "reason": "stale_data",
                "message": str(exc),
                "stale_data": True,
                "data_as_of": None,
                "expected_trade_date": None,
            }
            payload = _render_recommendation(
                _empty_recommend_payload(), status="stale", diagnostics=diagnostics
            )
            return RunResult("", "stale", payload, diagnostics)
        except ContractError as exc:
            return _failed_recommendation(f"契约校验失败：{exc}", reason="contract")
        except Exception as exc:  # noqa: BLE001 - 用例边界统一转 failed
            return _failed_recommendation(
                f"生成失败：{type(exc).__name__}: {exc}", reason="generate_error"
            )
        try:
            run = ledger.get_run(rec.run_id)
        except Exception as exc:  # noqa: BLE001 - 生成已落盘，但台账读取失败
            return _failed_recommendation(
                f"生成已归档 JSON（{rec.json_path}），但读取台账失败："
                f"{type(exc).__name__}: {exc}",
                reason="ledger_read_after_generate",
            )
        if not run:
            return _failed_recommendation(
                f"生成完成但 SQLite 台账中缺少 run_id={rec.run_id}；页面不从 JSON 读取，"
                "请执行 `python -m astock.ledger sync` 后重试"
                f"（JSON 归档：{rec.json_path}）。",
                reason="ledger_missing_after_generate",
            )
        return _ledger_view(run, mode="generate", now=now)

    try:
        run = ledger.latest_run() if mode == "latest" else ledger.get_run(str(run_id))
    except Exception as exc:  # noqa: BLE001 - 台账故障也走统一 failed 诊断
        return _failed_recommendation(
            f"读取台账失败：{type(exc).__name__}: {exc}", reason="ledger_error"
        )
    if not run:
        message = (
            "台账里还没有推荐记录。先运行一次生成，或执行 `python -m astock.ledger sync`。"
            if mode == "latest"
            else f"台账里找不到 run_id={run_id} 的推荐记录。"
        )
        return _failed_recommendation(message, reason="not_found")
    return _ledger_view(run, mode=mode, now=now)


def _history_payload(
    rows: list[dict[str, Any]], *, status_text: str
) -> dict[str, Any]:
    table = (
        pd.DataFrame(rows, columns=list(HISTORY_COLUMNS))
        if rows
        else pd.DataFrame(columns=list(HISTORY_COLUMNS))
    )
    run_ids = [str(row.get("run_id")) for row in rows if row.get("run_id")]
    return {"history": table, "run_ids": run_ids, "status_text": status_text}


def run_recommendation_history(limit: int = 50) -> RunResult:
    """历史批次列表（只读 ledger，不触发任何生成流水线）。"""
    try:
        rows = ledger.list_runs(limit=limit)
    except Exception as exc:  # noqa: BLE001 - 台账故障转统一 failed 诊断
        message = f"读取台账失败：{type(exc).__name__}: {exc}"
        diagnostics = {"reason": "ledger_list_error", "message": message}
        return RunResult(
            "",
            "failed",
            _history_payload([], status_text=f"❌ {message}"),
            diagnostics,
        )
    if not rows:
        message = (
            "台账里还没有推荐记录（空状态）。先点击「生成今日推荐」，"
            "或执行 `python -m astock.ledger sync` 导入历史 JSON；"
            "页面不会为了补列表而重跑流水线。"
        )
        diagnostics = {"reason": "ledger_list_empty", "message": message}
        return RunResult(
            "",
            "success",
            _history_payload([], status_text=f"ℹ️ {message}"),
            diagnostics,
        )
    list_diagnostics: dict[str, Any] = {
        "reason": "ledger_list",
        "message": "",
        "count": len(rows),
    }
    return RunResult(
        "",
        "success",
        _history_payload(
            rows,
            status_text=(
                f"✅ 已显示最近 {len(rows)} 个批次（上限 {limit}）；"
                "选择 run_id 后点「加载所选批次」查看详情与证据链。"
            ),
        ),
        list_diagnostics,
    )


def run_selected_recommendation(run_id: str | None) -> RunResult:
    """按用户下拉选择读取指定批次；空选择给出明确提示，不猜测最近一批。"""
    selected = str(run_id or "").strip()
    if not selected:
        return _failed_recommendation(
            "请先在下拉框选择一个 run_id，再点「加载所选批次」。",
            reason="missing_run_id",
        )
    return run_recommendation(mode="run", run_id=selected)


# ---------------------------------------------------------------------------
# 跟踪：把净值、显著性、图表编排收敛到用例层
# ---------------------------------------------------------------------------


def _empty_tracking_payload(status_text: str = "") -> dict[str, Any]:
    return {
        "detail": pd.DataFrame(),
        "summary": pd.DataFrame(),
        "nav": pd.DataFrame(),
        "dropped": pd.DataFrame(),
        "significance": pd.DataFrame(),
        "nav_figure": None,
        "excess_figure": None,
        "status_text": status_text,
    }


def _format_dates(detail: pd.DataFrame) -> pd.DataFrame:
    if detail.empty:
        return detail
    out = detail.copy()
    for col in ("推荐日", "入场日"):
        if col in out.columns:
            out[col] = pd.to_datetime(out[col]).dt.strftime("%Y-%m-%d")
    return out


def _usable_t1(detail: pd.DataFrame) -> int:
    if detail.empty or "超额T+1" not in detail.columns:
        return 0
    return int(pd.to_numeric(detail["超额T+1"], errors="coerce").notna().sum())


def run_tracking() -> RunResult:
    """跟踪用例统一入口：返回逐条记录、分组统计、净值图与显著性表。"""
    try:
        detail, summary, track_status = compute_tracking()
    except Exception as exc:  # noqa: BLE001 - 用例边界统一转 failed
        message = f"跟踪计算失败：{type(exc).__name__}: {exc}"
        failed_diagnostics = {
            "reason": "compute_error",
            "message": message,
            "error_type": type(exc).__name__,
        }
        payload = _empty_tracking_payload(f"❌ {message}")
        return RunResult("", "failed", payload, failed_diagnostics)

    if detail.empty:
        message = track_status or "暂无历史推荐。先运行一次「每日推荐」，或等收盘后的自动任务生成。"
        failed_diagnostics = {"reason": "no_tracking_data", "message": message}
        payload = _empty_tracking_payload(f"❌ {message}")
        return RunResult("", "failed", payload, failed_diagnostics)

    usable = _usable_t1(detail)
    t1_rows = 0
    if "T+1" in detail.columns:
        t1_rows = int(pd.to_numeric(detail["T+1"], errors="coerce").notna().sum())
    latest_as_of = ""
    if "推荐日" in detail.columns:
        try:
            latest_as_of = pd.Timestamp(detail["推荐日"].max()).strftime("%Y-%m-%d")
        except Exception:
            latest_as_of = str(detail["推荐日"].max())

    nav_error = ""
    try:
        nav, dropped = portfolio_nav(detail)
        nav_status = nav_status_text(nav, dropped)
        nav_chart = nav_figure(nav)
    except Exception as exc:  # noqa: BLE001 - 净值失败不拖垮分组统计
        nav, dropped = pd.DataFrame(), pd.DataFrame()
        nav_error = f"净值计算失败：{type(exc).__name__}: {exc}（下方表格不受影响）"
        nav_status = nav_error
        try:
            nav_chart = nav_figure(pd.DataFrame())
        except Exception:
            nav_chart = None

    diagnostics: dict[str, Any] = {
        "reason": "tracked" if usable else "awaiting_forward_data",
        "message": "",
        "records": len(detail),
        "usable_t1": usable,
        "latest_as_of": latest_as_of,
    }
    if nav_error:
        diagnostics["nav_error"] = nav_error

    status = "success"
    message = ""
    if usable == 0:
        status = "stale"
        if t1_rows:
            message = (
                "已有 T+1 个股收益，但缺沪深300 基准数据，超额统计暂不可用；"
                "等基准数据补齐后再刷新。"
            )
            diagnostics["reason"] = "awaiting_benchmark"
            diagnostics["bench_missing"] = True
        else:
            message = (
                f"已有 {len(detail)} 条推荐记录，但还没有 T+1 收盘数据；"
                "等下一个交易日收盘后再刷新。"
            )
        diagnostics["message"] = message

    significant = pd.DataFrame()
    try:
        significant = significance_table(detail)
    except Exception as exc:  # noqa: BLE001 - 统计失败仍保留逐条记录
        diagnostics["significance_error"] = f"{type(exc).__name__}: {exc}"

    try:
        excess_chart = track_figure(summary)
    except Exception as exc:  # noqa: BLE001
        excess_chart = None
        diagnostics["figure_error"] = f"{type(exc).__name__}: {exc}"

    status_text = f"{nav_status}\n\n{track_status}"
    if status == "stale":
        status_text = f"⚠️ {message}\n\n{status_text}"
    payload = {
        "detail": _format_dates(detail),
        "summary": summary,
        "nav": nav,
        "dropped": dropped,
        "significance": significant,
        "nav_figure": nav_chart,
        "excess_figure": excess_chart,
        "status_text": status_text,
    }
    return RunResult("", status, payload, diagnostics)


# ---------------------------------------------------------------------------
# 「无显著 alpha」横幅：实时聚合 ledger，UI 只消费
# ---------------------------------------------------------------------------


def run_alpha_banner() -> RunResult:
    """推荐页顶部横幅：数字全部来自 ledger + stats，不允许 UI 侧硬编码。"""
    try:
        banner = alpha_banner_snapshot()
        banner_md = alpha_banner_markdown(banner)
    except Exception as exc:  # noqa: BLE001 - 用例边界统一转 failed
        message = f"{type(exc).__name__}: {exc}"
        failed_payload = {
            "banner_md": alpha_banner_error_markdown(message),
            "banner_state": "error",
            "banner": None,
        }
        return RunResult(
            "",
            "failed",
            failed_payload,
            {"reason": "banner_error", "message": f"横幅统计失败：{message}"},
        )

    payload: dict[str, Any] = {
        "banner_md": banner_md,
        "banner_state": banner.state,
        "banner": banner,
        "horizon": banner.horizon,
        "live_days": banner.live_days,
        "independent_windows": banner.independent_windows,
        "effective_n": banner.effective_n,
        "p_value": banner.p_value,
    }
    diagnostics: dict[str, Any] = {
        "reason": "banner",
        "state": banner.state,
        "live_days": banner.live_days,
        "trustworthy": banner.trustworthy,
    }
    return RunResult("", "success", payload, diagnostics)
