"""校验当前目录就是唯一发布仓库，而不是历史 ``release/AStockAgent`` 副本。

背景（plan-20261103-013）
------------------------
2026-11-17 起仓库根 ``C:\\Users\\b1995\\Documents\\AStockAgent`` 自带 ``.git``，
``release/AStockAgent`` 只是旧的独立克隆，保留作历史参考。发布、提交、推送都必须
在仓库根完成。本脚本把这条约定变成可执行检查：

* ``git rev-parse --show-toplevel`` 必须等于传入的根目录；
* ``remote.origin.url`` 必须归一化为 ``github.com/b199510027373-create/AStockAgent``；
* 默认要求当前分支是 ``main``；PR 的 detached checkout 用 ``--allow-detached``；
* 检查目录本身就是 ``release/AStockAgent`` 时直接阻断；在根目录里发现该历史
  嵌套克隆只告警，提醒它不是发布源。

用法::

    python tools/check_repo_source.py
    python tools/check_repo_source.py --any-branch --allow-detached
    python tools/check_repo_source.py --root C:\\path\\to\\repo

退出码：0 表示通过，1 表示存在阻断问题（git 不可用、根目录不符、remote 不符、
分支不符或 detached）。告警不影响退出码。
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

DEFAULT_REMOTE = "github.com/b199510027373-create/AStockAgent"
DEFAULT_BRANCH = "main"
LEGACY_RELEASE_CLONE = Path("release") / "AStockAgent"


class RemoteFormatError(ValueError):
    """remote URL 无法归一化为 ``host/owner/repo``。"""


def normalize_remote(url: str) -> str:
    """把常见 Git remote 写法归一化为 ``host/owner/repo``。

    支持 ``https://``、``http://``、``ssh://``、``git://``、scp 风格的
    ``git@host:owner/repo.git``，以及省略协议的 ``host/owner/repo``。
    返回值保留路径大小写，仅把主机名转小写；比较时调用方应忽略大小写。
    """

    value = url.strip()
    if not value:
        raise RemoteFormatError("remote URL 为空")

    host = ""
    path = ""
    if "://" in value:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        path = parsed.path
    elif "@" in value and ":" in value and "/" not in value.split(":", 1)[0]:
        # git@github.com:owner/repo.git
        head, _, path = value.partition(":")
        host = head.rsplit("@", 1)[-1]
    elif "/" in value:
        # github.com/owner/repo（也兼容本地路径式 remote，但后面会因 host 不符失败）
        host, path = value.split("/", 1)
    else:
        raise RemoteFormatError(f"无法识别的 remote URL：{url!r}")

    path = path.strip().strip("/")
    if path.lower().endswith(".git"):
        path = path[:-4].rstrip("/")
    host = host.strip().lower()
    if not host or not path:
        raise RemoteFormatError(f"remote URL 缺少主机名或仓库路径：{url!r}")
    return f"{host}/{path}"


def _run_git(root: Path, *args: str) -> str | None:
    """在 ``root`` 下执行只读 git 命令；失败时返回 None。"""

    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=str(root),
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip()


def _same_path(left: Path, right: Path) -> bool:
    """Windows 下忽略大小写与短路径差异比较目录。"""

    return os.path.normcase(os.path.realpath(left)) == os.path.normcase(
        os.path.realpath(right)
    )


@dataclass(frozen=True)
class RepoCheck:
    """一次仓库真源检查的结果。"""

    root: Path
    toplevel: Path | None
    remote: str | None
    branch: str | None
    problems: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.problems


def check_repo(
    root: Path,
    *,
    expected_remote: str = DEFAULT_REMOTE,
    require_branch: str | None = DEFAULT_BRANCH,
    allow_detached: bool = False,
) -> RepoCheck:
    """检查 ``root`` 是否为唯一发布仓库。

    ``require_branch=None`` 表示不校验分支名；``allow_detached=True`` 允许
    detached HEAD（CI 对 PR 的 checkout 就是这种状态）。
    """

    resolved_root = Path(root).expanduser().resolve()
    problems: list[str] = []
    warnings: list[str] = []

    if (
        resolved_root.name.casefold() == "astockagent"
        and resolved_root.parent.name.casefold() == "release"
    ):
        problems.append(
            "检查目录是历史旧仓库 release/AStockAgent；它不是发布源，"
            "请在仓库根 C:\\Users\\b1995\\Documents\\AStockAgent 执行"
        )

    if shutil.which("git") is None:
        problems.append("git 不可用：请安装 Git 并确保它在 PATH 中")
        return RepoCheck(resolved_root, None, None, None, tuple(problems))

    toplevel_text = _run_git(resolved_root, "rev-parse", "--show-toplevel")
    toplevel: Path | None = None
    if not toplevel_text:
        problems.append(f"{resolved_root} 不是 Git 工作区（git rev-parse 失败）")
    else:
        toplevel = Path(toplevel_text).expanduser().resolve()
        if not _same_path(toplevel, resolved_root):
            problems.append(
                "git 工作区根与检查目录不一致："
                f"toplevel={toplevel}，期望={resolved_root}"
            )

    try:
        canonical_expected = normalize_remote(expected_remote)
    except RemoteFormatError:
        canonical_expected = expected_remote.strip()

    remote = _run_git(resolved_root, "config", "--get", "remote.origin.url")
    if not remote:
        problems.append("未配置 origin remote（git config --get remote.origin.url 失败）")
    else:
        try:
            actual_remote = normalize_remote(remote)
        except RemoteFormatError as exc:
            problems.append(str(exc))
        else:
            if actual_remote.casefold() != canonical_expected.casefold():
                problems.append(
                    f"origin 指向 {actual_remote}，期望 {canonical_expected}"
                )

    # ``remote.origin.pushurl``（或 url.<base>.pushInsteadOf）可以只改推送目标。
    # 用 ``git remote get-url --push`` 取生效值，避免 fetch URL 正确但 push 到别处。
    push_remote = _run_git(resolved_root, "remote", "get-url", "--push", "origin")
    if push_remote:
        try:
            actual_push_remote = normalize_remote(push_remote)
        except RemoteFormatError as exc:
            problems.append(f"origin.pushurl 无法解析：{exc}")
        else:
            if actual_push_remote.casefold() != canonical_expected.casefold():
                problems.append(
                    f"origin.pushurl 指向 {actual_push_remote}，"
                    f"期望 {canonical_expected}"
                )

    branch = _run_git(resolved_root, "symbolic-ref", "--quiet", "--short", "HEAD")
    if branch is None:
        if allow_detached:
            warnings.append("当前为 detached HEAD（CI PR checkout 正常状态）")
        else:
            problems.append("当前为 detached HEAD，无法确认发布分支")
    elif require_branch is not None and branch != require_branch:
        problems.append(f"当前分支为 {branch}，期望 {require_branch}")

    legacy_git = resolved_root / LEGACY_RELEASE_CLONE / ".git"
    if legacy_git.exists():
        warnings.append(
            f"{LEGACY_RELEASE_CLONE.as_posix()} 仍是历史嵌套仓库；"
            "它只作参考，不是发布源，请从仓库根提交/推送"
        )

    return RepoCheck(
        root=resolved_root,
        toplevel=toplevel,
        remote=remote,
        branch=branch,
        problems=tuple(problems),
        warnings=tuple(warnings),
    )


def _print_result(result: RepoCheck) -> None:
    print(f"检查目录: {result.root}")
    print(f"git 工作区根: {result.toplevel or '（不可用）'}")
    print(f"origin: {result.remote or '（未配置）'}")
    print(f"分支: {result.branch or '（detached HEAD）'}")
    for warning in result.warnings:
        print(f"WARN: {warning}")
    for problem in result.problems:
        print(f"ERROR: {problem}", file=sys.stderr)
    if result.ok:
        try:
            display_remote = normalize_remote(result.remote or DEFAULT_REMOTE)
        except RemoteFormatError:
            display_remote = result.remote or DEFAULT_REMOTE
        print(f"OK: 当前仓库是唯一发布源（{display_remote}）")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="校验当前目录是 AStockAgent 的唯一 Git 仓库（不是 release/AStockAgent 副本）",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path.cwd(),
        help="要检查的仓库根目录，默认当前目录",
    )
    parser.add_argument(
        "--expected-remote",
        default=DEFAULT_REMOTE,
        help=f"期望的 origin，默认 {DEFAULT_REMOTE}",
    )
    parser.add_argument(
        "--require-branch",
        default=DEFAULT_BRANCH,
        help=f"要求的分支名，默认 {DEFAULT_BRANCH}",
    )
    parser.add_argument(
        "--any-branch",
        action="store_true",
        help="不校验分支名（仍会拒绝 detached HEAD，除非同时传 --allow-detached）",
    )
    parser.add_argument(
        "--allow-detached",
        action="store_true",
        help="允许 detached HEAD（用于 GitHub Actions 的 PR checkout）",
    )
    args = parser.parse_args(argv)

    require_branch = None if args.any_branch else args.require_branch
    result = check_repo(
        args.root,
        expected_remote=args.expected_remote,
        require_branch=require_branch,
        allow_detached=args.allow_detached,
    )
    _print_result(result)
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
