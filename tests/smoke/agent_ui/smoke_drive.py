"""浏览器冒烟驱动：对着夹具页面跑六条主路径，只输出断言结果。

六条路径（plan-20261119-001 验收 4）：

===========  ==========================================================
``upload``   两个附件上传后解析状态 2/2 成功，并显示页数/工作表
``stream``   回答按片段增长：至少 3 个不同长度，状态出现「正在输出回答」
``steps``    步骤时间线 ``ol.astock-steps`` ≥3 项且全为 ``data-status=ok``
``figure``   内嵌 ``iframe.astock-figure-frame`` 的 srcdoc 含图表标记，
             caption 带「数据截至/数据时间」，正文有「查看图表 spec / 来源」
``citations`` 锚点 ``a[href^="#cite-"]`` ≥2，点击 ``#cite-1`` 后 URL 带 hash、
             详情卡显示文件名与定位
``interrupt`` 流式阶段点「⏹ 中断」保留半截回答，且随后再问能正常完成
===========  ==========================================================

截图默认不写；需要证据时用 ``--shots-dir`` 显式指定目录（只截关键 3 张）。
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
import traceback
from pathlib import Path
from typing import Any

from .cdp import CDP, DEBUG_HOST_ENV, DEBUG_PORT_ENV, page_target

HERE = Path(__file__).resolve().parent
DEFAULT_URL = "http://127.0.0.1:7866"

#: 六条路径的稳定 id（README 与 tests/test_agent_ui_smoke_contract.py 都按它核对）。
CHECKS = ("upload", "stream", "steps", "figure", "citations", "interrupt")

#: 与 fixture_app.py 的夹具常量对齐：改了夹具必须同步改这里（契约测试会看）。
CHART_MARKER = "smoke-chart"
FIGURE_CAPTION = "营收趋势｜数据截至 2025-12-31｜数据时间 2026-03-20"

_OBSERVER_JS = """(() => {
  window.__smokeSeen = [];
  window.__smokeLens = [];
  const lens = () => {
    const nodes = document.querySelectorAll('.message-content');
    return nodes.length ? nodes[nodes.length - 1].innerText.length : 0;
  };
  new MutationObserver(() => {
    const text = document.body.innerText;
    for (const needle of ['正在输出回答', '正在分析问题', '已调用 read_file']) {
      if (text.includes(needle) && !window.__smokeSeen.includes(needle)) {
        window.__smokeSeen.push(needle);
      }
    }
    const size = lens();
    if (window.__smokeLens[window.__smokeLens.length - 1] !== size) {
      window.__smokeLens.push(size);
    }
  }).observe(document.body, {subtree: true, childList: true, characterData: true});
  return true;
})()"""

_LAST_ASSISTANT_JS = """(() => {
  const nodes = [...document.querySelectorAll('.message-content')];
  return nodes.length ? nodes[nodes.length - 1].innerText : '';
})()"""

_SET_QUESTION_JS = """(() => {
  const box = document.querySelector('textarea');
  if (!box) { return false; }
  const setter = Object.getOwnPropertyDescriptor(
    HTMLTextAreaElement.prototype, 'value').set;
  setter.call(box, %s);
  box.dispatchEvent(new Event('input', {bubbles: true}));
  return true;
})()"""


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _body(cdp: CDP) -> str:
    return str(cdp.eval("document.body.innerText") or "")


def _last_assistant(cdp: CDP) -> str:
    return str(cdp.eval(_LAST_ASSISTANT_JS) or "")


def _message_count(cdp: CDP) -> int:
    return int(cdp.eval("document.querySelectorAll('.message-content').length") or 0)


_COMPLETED_JS = "(document.body.innerText.match(/✅ 已完成/g) || []).length"


def _completed_count(cdp: CDP) -> int:
    """页面上「✅ 已完成」出现的次数，用来判断某一轮是否新增了终态。"""
    return int(cdp.eval(_COMPLETED_JS) or 0)


def _set_question(cdp: CDP, text: str) -> None:
    if not cdp.eval(_SET_QUESTION_JS % json.dumps(text, ensure_ascii=False)):
        raise RuntimeError("页面上找不到问题输入框（textarea）")


def _click(cdp: CDP, label: str) -> None:
    clicked = cdp.eval(
        "(() => {"
        " const hit = [...document.querySelectorAll('button')]"
        f".find((b) => b.innerText.trim() === {json.dumps(label, ensure_ascii=False)});"
        " if (!hit) { return false; }"
        " hit.click();"
        " return true;"
        "})()"
    )
    if not clicked:
        raise RuntimeError(f"页面上找不到按钮：{label}")


def _wait_text(cdp: CDP, needle: str, timeout: float) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        text = _body(cdp)
        if needle in text:
            return text
        time.sleep(0.2)
    raise TimeoutError(f"页面没有出现：{needle!r}")


def _wait_any(cdp: CDP, needles: tuple[str, ...], timeout: float) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        text = _body(cdp)
        if any(needle in text for needle in needles):
            return text
        time.sleep(0.2)
    raise TimeoutError(f"页面没有出现任意一条：{needles!r}")


#: main() 里设置的成本次运行的总截止时间；单步等待一律收敛到它，避免串起来超预算。
_DEADLINE: float | None = None


def _timeout(seconds: float) -> float:
    """把单步超时收敛到 ``--timeout`` 的总预算内；预算用尽就按超时失败。"""
    if _DEADLINE is None:
        return seconds
    left = _DEADLINE - time.monotonic()
    if left <= 0:
        raise AssertionError("driver timeout（--timeout 预算已用尽）")
    return min(seconds, left)


def _scroll_shot(cdp: CDP, selector: str, path: Path) -> str:
    cdp.eval(f"document.querySelector({json.dumps(selector)}).scrollIntoView({{block: 'center'}})")
    time.sleep(0.4)
    return cdp.screenshot(path)


# ---------------------------------------------------------------------------
# 六条路径
# ---------------------------------------------------------------------------
def check_upload(cdp: CDP, fixtures: Path) -> str:
    pdf = fixtures / "年报.pdf"
    xlsx = fixtures / "利润表.xlsx"
    for path in (pdf, xlsx):
        if not path.is_file():
            raise FileNotFoundError(f"缺少夹具：{path}（见 README「夹具」一节）")
    cdp.wait_for("document.querySelector('input[type=file]') !== null", timeout=_timeout(60))
    root = cdp.call("DOM.getDocument")["root"]["nodeId"]
    node = cdp.call("DOM.querySelector", nodeId=root, selector="input[type=file]")
    node_id = node.get("nodeId")
    if not node_id:
        raise RuntimeError("input[type=file] 节点取不到 nodeId")
    cdp.call("DOM.setFileInputFiles", files=[str(pdf), str(xlsx)], nodeId=node_id)
    text = _wait_text(cdp, "附件解析：2/2 成功", timeout=_timeout(90))
    expected = (
        "✅ 年报.pdf（PDF 3 页）",
        "✅ 利润表.xlsx（1 张表：利润表）",
    )
    missing = [needle for needle in expected if needle not in text]
    if missing:
        raise AssertionError(f"附件状态缺少 {missing}；页面尾部：{text[-600:]}")
    return "2/2 成功；年报.pdf（PDF 3 页）、利润表.xlsx（1 张表：利润表）"


def check_stream(cdp: CDP) -> str:
    cdp.eval(_OBSERVER_JS)
    _set_question(cdp, "请分析附件并画图")
    _click(cdp, "发送")
    _wait_text(cdp, "✅ 已完成", timeout=_timeout(180))
    seen = cdp.eval("window.__smokeSeen || []") or []
    lens = cdp.eval("window.__smokeLens || []") or []
    if "正在输出回答" not in seen:
        raise AssertionError(f"没有观察到「正在输出回答」状态：{seen}")
    growing = [int(value) for value in lens if isinstance(value, (int, float)) and value > 0]
    if len(set(growing)) < 3:
        raise AssertionError(f"回答没有分段增长（长度序列 {growing}）")
    return f"状态序列 {seen}；回答长度出现 {len(set(growing))} 个不同值"


def check_steps(cdp: CDP) -> str:
    count = int(cdp.eval("document.querySelectorAll('ol.astock-steps li').length") or 0)
    if count < 3:
        raise AssertionError(f"步骤时间线只有 {count} 项（期望 ≥3）")
    statuses = cdp.eval(
        "[...document.querySelectorAll('ol.astock-steps li')].map((li) => li.dataset.status)"
    ) or []
    if any(status != "ok" for status in statuses):
        raise AssertionError(f"步骤状态不是全 ok：{statuses}")
    text = _body(cdp)
    for needle in ("read_file", "已读取工具结果", "✅ 已完成"):
        if needle not in text:
            raise AssertionError(f"页面正文缺少 {needle!r}")
    args = int(cdp.eval("document.querySelectorAll('ol.astock-steps .step-args').length") or 0)
    elapsed = int(cdp.eval("document.querySelectorAll('ol.astock-steps .step-elapsed').length") or 0)
    if args < count or elapsed < count:
        raise AssertionError(f"步骤明细不全：args={args} elapsed={elapsed} 项数={count}")
    return f"{count} 个步骤全 ok，参数 {args} 条、耗时 {elapsed} 条"


def check_figure(cdp: CDP, shots_dir: Path | None) -> str:
    cdp.wait_for(
        "document.querySelectorAll('iframe.astock-figure-frame').length > 0", timeout=_timeout(60)
    )
    frames = cdp.eval(
        "[...document.querySelectorAll('iframe.astock-figure-frame')]"
        ".map((frame) => frame.getAttribute('srcdoc') || '')"
    ) or []
    if not frames:
        raise AssertionError("图表 iframe 数量为 0")
    bad = [index for index, srcdoc in enumerate(frames) if CHART_MARKER not in srcdoc]
    if bad:
        raise AssertionError(f"第 {bad} 个 iframe 的 srcdoc 不含 {CHART_MARKER}")
    text = _body(cdp)
    for needle in ("查看图表 spec / 来源", FIGURE_CAPTION):
        if needle not in text:
            raise AssertionError(f"图表区缺少 {needle!r}")
    shot = ""
    if shots_dir is not None:
        shot = _scroll_shot(cdp, "iframe.astock-figure-frame", shots_dir / "1-figure.png")
    return f"{len(frames)} 个内嵌 iframe（srcdoc 含 {CHART_MARKER}），caption 匹配" + (
        f"，截图 {shot}" if shot else ""
    )


def check_citations(cdp: CDP, shots_dir: Path | None) -> str:
    links = int(cdp.eval("document.querySelectorAll('a[href^=\"#cite-\"]').length") or 0)
    if links < 2:
        raise AssertionError(f"引用锚点只有 {links} 条（期望 ≥2）")
    cdp.eval("document.querySelector('a[href=\"#cite-1\"]').click()")
    cdp.wait_for("location.hash === '#cite-1'", timeout=_timeout(15))
    if not cdp.eval("document.querySelector('#cite-1') !== null"):
        raise AssertionError("#cite-1 详情卡不存在")
    text = _body(cdp)
    if "文件：年报.pdf" not in text:
        raise AssertionError("引用详情卡没有显示「文件：年报.pdf」")
    if "定位：" not in text:
        raise AssertionError("引用详情卡没有显示定位信息")
    shot = ""
    if shots_dir is not None:
        shot = _scroll_shot(cdp, "#cite-1", shots_dir / "2-citations.png")
    return f"{links} 条引用，点击 #cite-1 后 hash 生效并显示文件/定位" + (
        f"，截图 {shot}" if shot else ""
    )


def check_interrupt(cdp: CDP, shots_dir: Path | None) -> str:
    before = _message_count(cdp)
    _set_question(cdp, "第二次：先分析再画图，然后继续展开分析")
    _click(cdp, "发送")
    cdp.wait_for(
        "(() => {"
        " const nodes = [...document.querySelectorAll('.message-content')];"
        " const text = document.body.innerText;"
        " return nodes.length > 0 && nodes[nodes.length - 1].innerText.length > 0"
        " && text.includes('正在输出回答');"
        "})()",
        timeout=_timeout(120),
    )
    _click(cdp, "⏹ 中断")
    _wait_any(cdp, ("已停止输出", "已中断"), timeout=_timeout(60))
    partial = _last_assistant(cdp)
    if not partial.strip():
        raise AssertionError("中断后助手气泡被清空")
    if "保留" not in _body(cdp):
        raise AssertionError("中断状态没有说明「已生成内容保留」")
    shot = ""
    if shots_dir is not None:
        shot = cdp.screenshot(shots_dir / "3-interrupt.png")
    # 中断后立刻再问一轮，验证锁已释放（回归 iss-mwmvlhso 的同类问题）。
    # 完成判据必须是「本轮新增」的终态：只看全页有没有 ✅ 已完成，会被第一轮的
    # 完成标记或第三轮流式中间态提前满足，挂住的第三轮也会 PASS
    # （评审 iss-mwoy6ghr-f8e6cd）。
    completed_before = _completed_count(cdp)
    _set_question(cdp, "第三次：中断后马上再问")
    _click(cdp, "发送")
    cdp.wait_for(
        "(() => {"
        " const nodes = [...document.querySelectorAll('.message-content')];"
        " const last = nodes.length > 0 ? nodes[nodes.length - 1].innerText : '';"
        " const done = (document.body.innerText.match(/✅ 已完成/g) || []).length;"
        f" return nodes.length > {before} + 2"
        f" && done > {completed_before}"
        " && last.length > 60"
        " && !last.includes('正在输出回答')"
        " && !last.includes('正在分析问题');"
        "})()",
        timeout=_timeout(180),
    )
    completed_after = _completed_count(cdp)
    if completed_after <= completed_before:
        raise AssertionError(
            f"第三次提问没有新增完成标记：{completed_before} -> {completed_after}"
        )
    return (
        f"中断保留 {len(partial)} 字，随后再问一轮新增完成标记"
        f"（{completed_before}→{completed_after}）"
        + (f"，截图 {shot}" if shot else "")
    )


def check_audit(db_path: Path) -> str:
    if not db_path.is_file():
        raise FileNotFoundError(f"审计库不存在：{db_path}")
    with sqlite3.connect(db_path) as conn:
        runs = int(conn.execute("SELECT COUNT(*) FROM agent_runs").fetchone()[0])
        steps = int(conn.execute("SELECT COUNT(*) FROM agent_steps").fetchone()[0])
    if runs < 2 or steps < 4:
        raise AssertionError(f"审计落库不足：agent_runs={runs} agent_steps={steps}")
    return f"agent_runs={runs} agent_steps={steps}（AGENT-02）"


def check_degraded(cdp: CDP, url: str) -> str:
    cdp.navigate(url, timeout=_timeout(60))
    cdp.wait_for("document.querySelector('input[type=file]') !== null", timeout=_timeout(60))
    text = _body(cdp)
    if "模型未就绪" not in text:
        raise AssertionError(f"降级页没有「模型未就绪」横幅：{text[:400]}")
    return "无模型时页面可打开，横幅显示「模型未就绪」"


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Agent 会话 UI 六路径冒烟驱动")
    parser.add_argument("--url", default=os.environ.get("AGENT_SMOKE_URL", DEFAULT_URL))
    parser.add_argument(
        "--degraded-url",
        default=os.environ.get("AGENT_SMOKE_DEGRADED_URL", ""),
        help="可选：降级夹具地址；给了才跑降级横幅断言",
    )
    parser.add_argument("--debug-host", default=os.environ.get(DEBUG_HOST_ENV, ""))
    parser.add_argument("--debug-port", type=int, default=None)
    parser.add_argument(
        "--fixtures-dir", type=Path, default=HERE / "fixtures", help="附件夹具目录"
    )
    parser.add_argument(
        "--db", type=Path, default=None, help="夹具审计库路径；给了才核对落库"
    )
    parser.add_argument(
        "--shots-dir",
        type=Path,
        default=None,
        help="可选：截图目录（默认不写任何截图）",
    )
    parser.add_argument("--report", type=Path, default=None, help="可选：写 JSON 结果")
    parser.add_argument("--timeout", type=float, default=300.0, help="单次运行总超时（秒）")
    return parser.parse_args(argv)


def _apply_debug_args(args: argparse.Namespace) -> None:
    if args.debug_host:
        os.environ[DEBUG_HOST_ENV] = args.debug_host
    if args.debug_port:
        os.environ[DEBUG_PORT_ENV] = str(args.debug_port)


def _write_report(path: Path | None, payload: dict[str, Any]) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _console_safe() -> None:
    """GBK 控制台遇到 ⏹/emoji 时不要把断言输出本身打断。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(errors="replace")
            except (OSError, ValueError):  # 被重定向到不可重配的文件对象
                continue


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    _console_safe()
    _apply_debug_args(args)
    shots_dir: Path | None = args.shots_dir
    if shots_dir is not None:
        shots_dir.mkdir(parents=True, exist_ok=True)

    started = time.monotonic()
    global _DEADLINE
    _DEADLINE = started + args.timeout
    results: dict[str, Any] = {}
    report: dict[str, Any] = {
        "url": args.url,
        "timeout_s": args.timeout,
        "checks": results,
        "ok": False,
    }
    cdp: CDP | None = None
    try:
        cdp = CDP(page_target())
        cdp.navigate(args.url, timeout=_timeout(60))
        cdp.call("DOM.enable")
        phases: list[tuple[str, Any]] = [
            ("upload", lambda: check_upload(cdp, args.fixtures_dir)),
            ("stream", lambda: check_stream(cdp)),
            ("steps", lambda: check_steps(cdp)),
            ("figure", lambda: check_figure(cdp, shots_dir)),
            ("citations", lambda: check_citations(cdp, shots_dir)),
            ("interrupt", lambda: check_interrupt(cdp, shots_dir)),
        ]
        if args.db is not None:
            phases.append(("audit", lambda: check_audit(args.db)))
        if args.degraded_url:
            phases.append(("degraded", lambda: check_degraded(cdp, args.degraded_url)))
        else:
            # 单独跑驱动时降级断言没被要求；记进报告，避免和「跑过且通过」混淆。
            results["degraded"] = {"ok": False, "skipped": "no --degraded-url"}

        for name, phase in phases:
            begin = time.monotonic()
            detail = phase()
            results[name] = {
                "ok": True,
                "detail": detail,
                "seconds": round(time.monotonic() - begin, 2),
            }
            print(f"PASS {name} :: {detail}", flush=True)
    except Exception as exc:  # noqa: BLE001 - 失败要落报告并给非零退出码
        report["ok"] = False
        report["error"] = f"{type(exc).__name__}: {exc}"
        report["traceback"] = traceback.format_exc()
        print(f"SMOKE FAIL :: {report['error']}", flush=True)
        traceback.print_exc()
        _write_report(args.report, report | {"duration_s": round(time.monotonic() - started, 2)})
        if cdp is not None:
            cdp.close()
        return 1
    if cdp is not None:
        cdp.close()
    report["ok"] = True
    report["duration_s"] = round(time.monotonic() - started, 2)
    _write_report(args.report, report)
    passed = sum(
        1 for value in results.values() if isinstance(value, dict) and value.get("ok")
    )
    print(
        f"SMOKE OK :: {passed} 项断言全部通过，用时 {report['duration_s']}s",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
