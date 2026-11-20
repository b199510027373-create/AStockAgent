"""固定启动方式：起夹具 app → 起 headless 浏览器 → 跑六路径断言 → 清理。

一条命令即可复现（README「快速开始」）：

.. code-block:: text

    python tests/smoke/agent_ui/run_smoke.py

只用标准库：不依赖本机路径、不依赖 ``gh``/CI、不写死端口。退出码等于驱动的
退出码（0 = 六条路径全过），因此可以直接被 nightly job 或手动脚本消费。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[2]
BROWSER_ENV = "AGENT_SMOKE_BROWSER"
DEFAULT_TIMEOUT = 300.0

__all__ = [
    "BROWSER_ENV",
    "REPO_ROOT",
    "browser_candidates",
    "find_browser",
    "main",
    "pick_free_port",
    "wait_for_log",
    "wait_port",
]


# ---------------------------------------------------------------------------
# 端口 / 浏览器发现
# ---------------------------------------------------------------------------
def pick_free_port() -> int:
    """借操作系统的临时端口分配，避免写死 7866/9223 撞车。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def browser_candidates() -> list[str]:
    """按平台列出常见 Chrome/Edge 路径（只列存在性由调用方判断）。"""
    candidates: list[str] = []
    if sys.platform.startswith("win"):
        for env_name in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
            base = os.environ.get(env_name)
            if not base:
                continue
            candidates.extend(
                [
                    str(Path(base) / "Microsoft/Edge/Application/msedge.exe"),
                    str(Path(base) / "Google/Chrome/Application/chrome.exe"),
                ]
            )
        candidates.extend(["msedge.exe", "chrome.exe"])
    elif sys.platform == "darwin":
        candidates.extend(
            [
                "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            ]
        )
    else:
        candidates.extend(
            [
                "google-chrome",
                "google-chrome-stable",
                "chromium",
                "chromium-browser",
                "microsoft-edge",
                "microsoft-edge-stable",
            ]
        )
    return candidates


def _usable_browser(path: str) -> bool:
    candidate = Path(path)
    if candidate.is_file():
        return True
    return shutil.which(path) is not None


def find_browser(explicit: str | None = None) -> str:
    """``--browser`` > ``AGENT_SMOKE_BROWSER`` > 平台候选；找不到就报清楚。"""
    requested = explicit or os.environ.get(BROWSER_ENV)
    if requested:
        resolved = shutil.which(requested) or requested
        if _usable_browser(resolved):
            return resolved
        raise FileNotFoundError(f"指定的浏览器不存在：{requested}")
    for candidate in browser_candidates():
        if _usable_browser(candidate):
            return shutil.which(candidate) or candidate
    raise FileNotFoundError(
        "找不到 Chrome/Edge；安装其一，或用 AGENT_SMOKE_BROWSER 指向可执行文件"
    )


# ---------------------------------------------------------------------------
# 进程 / 就绪探测
# ---------------------------------------------------------------------------
def wait_port(host: str, port: int, *, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.5)
            if sock.connect_ex((host, port)) == 0:
                return True
        time.sleep(0.2)
    return False


def wait_for_log(path: Path, needle: str, *, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_file() and needle in path.read_text(encoding="utf-8", errors="ignore"):
            return True
        time.sleep(0.2)
    return False


def wait_debug_endpoint(port: int, *, timeout: float) -> bool:
    """等 DevTools HTTP 端点起来；127.0.0.1 与 localhost 都试（Edge 可能只监听 IPv6）。"""
    candidates = [f"http://{host}:{port}/json/version" for host in ("127.0.0.1", "localhost")]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for url in candidates:
            try:
                with urllib.request.urlopen(url, timeout=1.5):
                    return True
            except (urllib.error.URLError, OSError):
                continue
        time.sleep(0.25)
    return False


def _start(args: list[str], *, env: dict[str, str], cwd: Path, log_path: Path) -> subprocess.Popen:
    log = log_path.open("w", encoding="utf-8")
    creationflags = 0
    if sys.platform.startswith("win"):
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.Popen(
        args,
        cwd=str(cwd),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=subprocess.STDOUT,
        creationflags=creationflags,
    )


def _stop(proc: subprocess.Popen | None, *, name: str) -> None:
    if proc is None:
        return
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        print(f"WARN {name} 未在 10s 内退出，强制结束", flush=True)
        proc.kill()
        proc.wait(timeout=10)


def _log_tail(path: Path, *, lines: int = 15) -> str:
    if not path.is_file():
        return f"(没有日志 {path})"
    content = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    return "\n".join(content[-lines:])


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Agent 会话 UI 冒烟（固定启动方式）")
    parser.add_argument("--workdir", type=Path, default=None, help="工作目录；默认临时目录")
    parser.add_argument("--port", type=int, default=None, help="夹具端口；默认自动挑空闲端口")
    parser.add_argument("--browser", default=None, help="Chrome/Edge 可执行文件路径")
    parser.add_argument(
        "--shots-dir",
        type=Path,
        default=None,
        help="可选：把关键 3 张截图写到该目录（默认不写）",
    )
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="驱动总超时（秒）")
    parser.add_argument(
        "--keep-workdir", action="store_true", help="保留工作目录（日志/报告/临时库）"
    )
    parser.add_argument("--no-degraded", action="store_true", help="跳过降级横幅断言")
    parser.add_argument("--chunk-delay", type=float, default=0.02, help="夹具流式片段延时")
    parser.add_argument("--answer-repeat", type=int, default=12, help="夹具长回答重复次数")
    return parser.parse_args(argv)


def _console_safe() -> None:
    """让中文/替换字符在 GBK 控制台也不会把汇总输出打断（只在编码层兜底）。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(errors="replace")
            except (OSError, ValueError):  # 被重定向到不可重配的文件对象
                continue


class _Abort(Exception):
    """启动阶段失败：交给统一收尾写 run.json，避免只 return 1 丢掉证据。"""


def _write_summary(workdir: Path, summary: dict[str, object]) -> Path:
    """无论成功失败都落 run.json；``stage`` 标明失败发生在启动的哪一段。"""
    path = workdir / "run.json"
    path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return path


def _as_text(value: object) -> str:
    """``TimeoutExpired.stdout`` 在 text 模式是 str，二进制模式下是 bytes。"""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def _driver_error(report_path: Path, exit_code: int) -> str:
    """驱动非零退出时，把它写进 report.json 的原因带进 run.json。"""
    try:
        payload = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):  # 报告缺失或半截：只保留退出码
        return f"driver exited with code {exit_code}"
    text = str(payload.get("error") or "").strip()
    if "driver timeout" in text:
        return "driver timeout"
    if text:
        return f"driver failed: {text}"
    return f"driver exited with code {exit_code}"


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    _console_safe()
    started = time.monotonic()
    workdir = (args.workdir or Path(tempfile.mkdtemp(prefix="agent-ui-smoke-"))).resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    app_log = workdir / "app.log"
    degraded_log = workdir / "degraded.log"
    browser_log = workdir / "browser.log"
    report_path = workdir / "report.json"

    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT)
    env["AGENT_SMOKE_WORKDIR"] = str(workdir)
    env["GRADIO_TEMP_DIR"] = str(workdir / "gradio")  # Gradio 上传/缓存不写系统临时目录
    env.pop("AGENT_FILES_DIR", None)  # 不把真实上传目录写进来
    env.setdefault("PYTHONUNBUFFERED", "1")
    env["PYTHONIOENCODING"] = "utf-8"  # 子进程日志统一 UTF-8，便于驱动与 _log_tail 读取
    env["PYTHONUTF8"] = "1"

    port = args.port or pick_free_port()
    debug_port = pick_free_port()
    while debug_port == port:
        debug_port = pick_free_port()
    degraded_port = pick_free_port()
    while degraded_port in {port, debug_port}:
        degraded_port = pick_free_port()

    app: subprocess.Popen | None = None
    degraded: subprocess.Popen | None = None
    browser: subprocess.Popen | None = None
    driver_exit = 1
    summary: dict[str, object] = {
        "workdir": str(workdir),
        "app_url": f"http://127.0.0.1:{port}",
        "debug_port": debug_port,
        "stage": "fixture",
        "degraded_expected": not args.no_degraded,
        "degraded_ran": False,
    }
    try:
        print(f"INFO 工作目录 {workdir}", flush=True)
        app = _start(
            [
                sys.executable,
                "-m",
                "tests.smoke.agent_ui.fixture_app",
                "--port",
                str(port),
                "--workdir",
                str(workdir),
                "--chunk-delay",
                str(args.chunk_delay),
                "--answer-repeat",
                str(args.answer_repeat),
            ],
            env=env,
            cwd=workdir,
            log_path=app_log,
        )
        if not wait_for_log(app_log, "READY url=", timeout=180):
            print(f"FAIL 夹具没有就绪，app.log 尾部：\n{_log_tail(app_log)}", flush=True)
            summary["error"] = "fixture app not ready"
            raise _Abort
        print(f"INFO 夹具就绪 http://127.0.0.1:{port}", flush=True)

        degraded_url = ""
        if not args.no_degraded:
            summary["stage"] = "degraded"
            degraded = _start(
                [
                    sys.executable,
                    "-m",
                    "tests.smoke.agent_ui.fixture_app",
                    "--port",
                    str(degraded_port),
                    "--scenario",
                    "degraded",
                    "--workdir",
                    str(workdir),
                ],
                env=env,
                cwd=workdir,
                log_path=degraded_log,
            )
            if wait_for_log(degraded_log, "READY url=", timeout=180):
                degraded_url = f"http://127.0.0.1:{degraded_port}"
                summary["degraded_ran"] = True
                print(f"INFO 降级夹具就绪 {degraded_url}", flush=True)
            else:
                print(
                    "FAIL 降级夹具没有就绪（默认要求该断言，"
                    f"--no-degraded 才能显式跳过）：\n{_log_tail(degraded_log)}",
                    flush=True,
                )
                summary["error"] = "degraded fixture not ready"
                raise _Abort
        else:
            print("INFO --no-degraded：跳过降级横幅断言", flush=True)

        summary["stage"] = "browser"
        browser_path = find_browser(args.browser)
        profile = workdir / "browser-profile"
        print(f"INFO 浏览器 {browser_path}（headless，调试端口 {debug_port}）", flush=True)
        browser = _start(
            [
                browser_path,
                "--headless=new",
                "--disable-gpu",
                "--disable-dev-shm-usage",
                "--no-first-run",
                "--no-default-browser-check",
                "--no-sandbox",
                f"--remote-debugging-port={debug_port}",
                f"--user-data-dir={profile}",
                "about:blank",
            ],
            env=env,
            cwd=workdir,
            log_path=browser_log,
        )
        if not wait_debug_endpoint(debug_port, timeout=60):
            print(f"FAIL 浏览器调试端口没起来：\n{_log_tail(browser_log)}", flush=True)
            summary["error"] = "browser debug endpoint not ready"
            raise _Abort

        summary["stage"] = "driver"
        driver_args = [
            sys.executable,
            "-m",
            "tests.smoke.agent_ui.smoke_drive",
            "--url",
            f"http://127.0.0.1:{port}",
            "--debug-port",
            str(debug_port),
            "--fixtures-dir",
            str(HERE / "fixtures"),
            "--db",
            str(workdir / "smoke.db"),
            "--report",
            str(report_path),
            "--timeout",
            str(args.timeout),
        ]
        if degraded_url:
            driver_args.extend(["--degraded-url", degraded_url])
        if args.shots_dir is not None:
            shots = args.shots_dir.resolve()
            shots.mkdir(parents=True, exist_ok=True)
            driver_args.extend(["--shots-dir", str(shots)])

        completed = subprocess.run(
            driver_args,
            cwd=str(workdir),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=args.timeout + 120,
        )
        driver_exit = completed.returncode
        driver_log = workdir / "driver.log"
        driver_log.write_text(
            completed.stdout + ("\n[stderr]\n" + completed.stderr if completed.stderr else ""),
            encoding="utf-8",
        )
        summary["driver_log"] = "driver.log"
        if driver_exit != 0:
            summary["error"] = _driver_error(workdir / "report.json", driver_exit)
        print(completed.stdout, end="", flush=True)
        if completed.stderr:
            print(completed.stderr, end="", file=sys.stderr, flush=True)
    except _Abort:
        driver_exit = 1
    except subprocess.TimeoutExpired as exc:
        print(f"FAIL 驱动超过 {args.timeout + 120}s 未结束", flush=True)
        summary["error"] = "driver timeout"
        driver_exit = 1
        # 强杀路径同样要留下 driver.log：否则 README 承诺的「失败保留现场」在这里不成立
        # （评审 iss-mwoy6git-5678ab）。
        driver_log = workdir / "driver.log"
        out = _as_text(exc.stdout)
        err = _as_text(exc.stderr)
        driver_log.write_text(
            out + ("\n[stderr]\n" + err if err.strip() else ""),
            encoding="utf-8",
        )
        summary["driver_log"] = "driver.log"
        print(f"INFO 强制结束前的输出已写入 {driver_log}", flush=True)
    except (OSError, RuntimeError) as exc:
        print(f"FAIL {type(exc).__name__}: {exc}", flush=True)
        summary["error"] = f"{type(exc).__name__}: {exc}"
        driver_exit = 1
    finally:
        _stop(browser, name="browser")
        _stop(degraded, name="degraded app")
        _stop(app, name="fixture app")

    summary["driver_exit"] = driver_exit
    summary["duration_s"] = round(time.monotonic() - started, 2)
    summary["ok"] = driver_exit == 0 and not summary.get("error")
    if summary["ok"]:
        summary["stage"] = "done"
    _write_summary(workdir, summary)
    print(f"INFO 证据目录 {workdir}（report.json / run.json / *.log）", flush=True)
    if driver_exit == 0 and not args.keep_workdir:
        shutil.rmtree(workdir, ignore_errors=True)
        print("INFO 已清理临时工作目录（--keep-workdir 可保留）", flush=True)
    return driver_exit


if __name__ == "__main__":
    sys.exit(main())
