"""最小 CDP 客户端：连 headless Chrome/Edge，发断言，默认不落截图。

设计约束（plan-20261119-001 验收 1/2/5）：

- 不写死本机路径：调试主机/端口可用 ``AGENT_SMOKE_DEBUG_HOST`` /
  ``AGENT_SMOKE_DEBUG_PORT`` 覆盖，默认 ``127.0.0.1:9223``；
- 不引第三方浏览器驱动：只用 ``websocket-client``（gradio 的传递依赖）与标准库；
- 截图不是默认产物：由调用方显式给目录才写（见 :meth:`CDP.screenshot`）。
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import time
import urllib.request
from pathlib import Path
from typing import Any

import websocket

#: 默认调试端点；仅作为兜底，实际端口由 ``run_smoke.py`` 注入。
DEFAULT_DEBUG_HOST = "127.0.0.1"
DEFAULT_DEBUG_PORT = 9223
DEBUG_HOST_ENV = "AGENT_SMOKE_DEBUG_HOST"
DEBUG_PORT_ENV = "AGENT_SMOKE_DEBUG_PORT"

#: 本机回环别名。Edge 在部分 Windows 版本上只监听 ``::1``（localhost），而 Chrome 多见于
#: ``127.0.0.1``：两者都试一遍，避免「浏览器已启动、驱动却说连不上」的假失败。
LOOPBACK_ALIASES = ("127.0.0.1", "localhost", "[::1]")

__all__ = [
    "CDP",
    "DEBUG_HOST_ENV",
    "DEBUG_PORT_ENV",
    "DEFAULT_DEBUG_HOST",
    "DEFAULT_DEBUG_PORT",
    "LOOPBACK_ALIASES",
    "debug_url",
    "page_target",
]


def debug_url(*, host: str | None = None, port: int | None = None) -> str:
    """拼调试端点；参数 > 环境变量 > 默认值。"""
    resolved_host = host or os.environ.get(DEBUG_HOST_ENV) or DEFAULT_DEBUG_HOST
    raw_port: str | int | None = port if port is not None else os.environ.get(DEBUG_PORT_ENV)
    resolved_port = int(raw_port) if raw_port else DEFAULT_DEBUG_PORT
    return f"http://{resolved_host}:{resolved_port}"


def _candidate_bases(*, url: str | None = None) -> list[str]:
    """调试端点候选：显式 URL > （回环时）所有本机别名 > 单一配置地址。"""
    if url:
        return [url.rstrip("/")]
    primary = debug_url()
    resolved_host = os.environ.get(DEBUG_HOST_ENV) or DEFAULT_DEBUG_HOST
    if resolved_host not in LOOPBACK_ALIASES:
        return [primary]
    bases = [primary]
    for alias in LOOPBACK_ALIASES:
        candidate = debug_url(host=alias)
        if candidate not in bases:
            bases.append(candidate)
    return bases


def page_target(*, timeout: float = 30.0, url: str | None = None) -> str:
    """轮询 ``/json/list``，返回第一个 ``type == page`` 的 WebSocket 地址。"""
    bases = _candidate_bases(url=url)
    deadline = time.monotonic() + timeout
    last: Any = None
    while time.monotonic() < deadline:
        for base in bases:
            try:
                with urllib.request.urlopen(f"{base}/json/list", timeout=2) as response:
                    targets = json.loads(response.read().decode("utf-8"))
            except (OSError, ValueError) as exc:  # 浏览器进程还没监听端口，继续等
                last = f"{base}: {exc}"
                continue
            for item in targets:
                if item.get("type") == "page" and item.get("webSocketDebuggerUrl"):
                    return str(item["webSocketDebuggerUrl"])
            last = targets
        time.sleep(0.25)
    raise RuntimeError(f"CDP target not ready at {bases}: {last!r}")


class CDP:
    """一条 CDP WebSocket 连接上的同步调用封装（够冒烟用，不做事件订阅）。"""

    def __init__(self, url: str, *, timeout: float = 60.0) -> None:
        self._ws = websocket.create_connection(url, timeout=timeout)
        self._next_id = 0

    # ---- 原语 -------------------------------------------------------------
    def call(self, method: str, /, **params: Any) -> dict[str, Any]:
        """发一条命令并等对应 id 的响应；忽略浏览器主动推送的事件。"""
        self._next_id += 1
        command_id = self._next_id
        payload = {"id": command_id, "method": method, "params": params}
        self._ws.send(json.dumps(payload))
        while True:
            message = json.loads(self._ws.recv())
            if message.get("id") != command_id:
                continue
            if "error" in message:
                raise RuntimeError(f"{method}: {message['error']}")
            result = message.get("result")
            return result if isinstance(result, dict) else {}

    def eval(self, expression: str) -> Any:
        """执行 JS 表达式并取返回值（Promise 会被 await）。"""
        result = self.call(
            "Runtime.evaluate",
            expression=expression,
            returnByValue=True,
            awaitPromise=True,
        )
        details = result.get("exceptionDetails")
        if details:
            description = ""
            exception = details.get("exception")
            if isinstance(exception, dict):
                description = str(exception.get("description") or "")
            raise RuntimeError(f"JS error: {details.get('text')} {description}".strip())
        value = result.get("result")
        return value.get("value") if isinstance(value, dict) else None

    def wait_for(self, expression: str, *, timeout: float = 30.0, interval: float = 0.2) -> Any:
        """轮询 JS 表达式直到返回真值；超时抛 :class:`TimeoutError`。"""
        deadline = time.monotonic() + timeout
        last: Any = None
        while time.monotonic() < deadline:
            try:
                last = self.eval(expression)
            except RuntimeError as exc:  # 页面切换瞬间求值失败，下一轮重试
                last = f"{type(exc).__name__}: {exc}"
            else:
                if last:
                    return last
            time.sleep(interval)
        raise TimeoutError(f"等待超时：{expression}（最后结果 {last!r}）")

    # ---- 页面 -------------------------------------------------------------
    def navigate(self, url: str, *, timeout: float = 30.0) -> None:
        self.call("Page.navigate", url=url)
        self.wait_for("document.readyState === 'complete'", timeout=timeout)

    def screenshot(self, path: str | Path) -> str:
        """显式调用才写 PNG；返回值用于日志（路径相对于调用方给的目录）。"""
        raw = self.call("Page.captureScreenshot", format="png")["data"]
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(base64.b64decode(raw))
        return str(target)

    def close(self) -> None:
        with contextlib.suppress(OSError):  # 关闭失败不影响断言结论
            self._ws.close()
