"""``tools/check_repo_source.py`` 的单元与 CLI 测试（plan-20261103-013）。

覆盖：
* 常见 remote 写法的归一化；
* 工作区根、origin、分支三项校验；
* detached HEAD 的 CI 例外；
* 历史 ``release/AStockAgent`` 只告警不阻断；
* CLI 退出码。
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="需要 git")


def _module() -> Any:
    spec = importlib.util.spec_from_file_location(
        "check_repo_source", ROOT / "tools" / "check_repo_source.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # dataclasses 在 ``from __future__ import annotations`` 下需要能从
    # sys.modules 找到模块本身，否则字符串注解解析会失败。
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(repo),
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def _is_ignored(path: str) -> bool:
    """按真实忽略规则判断路径，而不是对 .gitignore 做字符串匹配。"""

    return (
        subprocess.run(
            ["git", "check-ignore", "-q", path],
            cwd=str(ROOT),
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        ).returncode
        == 0
    )


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(
        tmp_path,
        "remote",
        "add",
        "origin",
        "git@github.com:b199510027373-create/AStockAgent.git",
    )
    return tmp_path


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (
            "https://github.com/b199510027373-create/AStockAgent.git",
            "github.com/b199510027373-create/AStockAgent",
        ),
        (
            "https://github.com/b199510027373-create/AStockAgent/",
            "github.com/b199510027373-create/AStockAgent",
        ),
        (
            "git@github.com:b199510027373-create/AStockAgent.git",
            "github.com/b199510027373-create/AStockAgent",
        ),
        (
            "ssh://git@github.com:22/b199510027373-create/AStockAgent.git",
            "github.com/b199510027373-create/AStockAgent",
        ),
        (
            "git://github.com/b199510027373-create/AStockAgent",
            "github.com/b199510027373-create/AStockAgent",
        ),
        (
            "github.com/b199510027373-create/AStockAgent",
            "github.com/b199510027373-create/AStockAgent",
        ),
        (
            "https://user:token@github.com/b199510027373-create/AStockAgent.git",
            "github.com/b199510027373-create/AStockAgent",
        ),
    ],
)
def test_normalize_remote_variants(raw: str, expected: str) -> None:
    assert _module().normalize_remote(raw) == expected


@pytest.mark.parametrize("raw", ["", "   ", "not-a-remote", "git@github.com:"])
def test_normalize_remote_rejects_unparseable(raw: str) -> None:
    module = _module()
    with pytest.raises(module.RemoteFormatError):
        module.normalize_remote(raw)


def test_check_passes_on_canonical_repo(repo: Path) -> None:
    result = _module().check_repo(repo)

    assert result.ok, result.problems
    assert result.toplevel == repo.resolve()
    assert result.branch == "main"
    assert result.warnings == ()


def test_check_rejects_wrong_remote(repo: Path) -> None:
    _git(repo, "remote", "set-url", "origin", "https://github.com/other/repo.git")

    result = _module().check_repo(repo)

    assert not result.ok
    assert any("origin 指向" in problem for problem in result.problems)


def test_check_rejects_subdirectory_as_root(repo: Path) -> None:
    nested = repo / "astock"
    nested.mkdir()

    result = _module().check_repo(nested)

    assert not result.ok
    assert any("工作区根与检查目录不一致" in problem for problem in result.problems)


def test_check_rejects_unexpected_branch(repo: Path) -> None:
    assert _module().check_repo(repo, require_branch="release").ok is False
    assert _module().check_repo(repo, require_branch=None).ok is True


def test_check_rejects_pushurl_to_other_remote(repo: Path) -> None:
    """fetch URL 正确但 pushurl 指向别处时，不能认为发布源可信。"""

    _git(
        repo,
        "config",
        "remote.origin.pushurl",
        "https://github.com/other/repo.git",
    )

    result = _module().check_repo(repo)

    assert result.ok is False
    assert any("pushurl" in problem for problem in result.problems)


def _commit(repo: Path) -> None:
    (repo / "README.md").write_text("test\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(
        repo,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-q",
        "-m",
        "init",
    )


def test_check_detached_head_needs_explicit_allow(repo: Path) -> None:
    _commit(repo)
    _git(repo, "checkout", "-q", "--detach", "HEAD")

    strict = _module().check_repo(repo, require_branch=None)
    allowed = _module().check_repo(
        repo, require_branch=None, allow_detached=True
    )

    assert strict.ok is False
    assert any("detached HEAD" in problem for problem in strict.problems)
    assert allowed.ok is True
    assert any("detached HEAD" in warning for warning in allowed.warnings)


def test_check_warns_about_legacy_release_clone(repo: Path) -> None:
    (repo / "release" / "AStockAgent" / ".git").mkdir(parents=True)

    result = _module().check_repo(repo)

    assert result.ok is True
    assert any("release/AStockAgent" in warning for warning in result.warnings)


def test_legacy_release_clone_is_blocked(tmp_path: Path) -> None:
    """旧副本自身也是 Git 仓库且 remote 相同，不能因为“自洽”就被当成发布源。"""

    clone = tmp_path / "release" / "AStockAgent"
    clone.mkdir(parents=True)
    _git(clone, "init", "-q", "-b", "main")
    _git(
        clone,
        "remote",
        "add",
        "origin",
        "https://github.com/b199510027373-create/AStockAgent.git",
    )

    result = _module().check_repo(clone)

    assert result.ok is False
    assert any("历史旧仓库" in problem for problem in result.problems)


def test_cli_exit_codes(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    module = _module()

    assert module.main(["--root", str(repo), "--any-branch"]) == 0
    assert "OK: 当前仓库是唯一发布源" in capsys.readouterr().out

    _git(repo, "remote", "set-url", "origin", "https://github.com/other/repo.git")
    assert module.main(["--root", str(repo), "--any-branch"]) == 1
    assert "ERROR:" in capsys.readouterr().err


def test_gitignore_hygiene_rules_ignore_real_paths() -> None:
    """用 git check-ignore 验证行为，而不是只检查 .gitignore 文本。"""

    ignored = (
        "astock-chat-x/",
        ".pytest-x/",
        "pip-x/",
        "tmp-x/",
        "_archive/x",
        ".devloop/drafts/x",
        ".devloop/handoffs/archive/x",
        ".devloop/reviews/archive/x",
        ".tmp-tests/x",
        "release/",
    )
    tracked = ("astock/data.py", "docs/repo_source.md", "tests/test_repo_source_check.py")

    for path in ignored:
        assert _is_ignored(path), f"应被忽略：{path}"
    for path in tracked:
        assert not _is_ignored(path), f"不应被忽略：{path}"


def test_pytest_tempdir_stays_inside_repo_ignored_dir() -> None:
    """明确断言 conftest 的设置，且该目录确实被 git 忽略。"""

    expected = (ROOT / ".tmp-tests").resolve()
    assert Path(tempfile.gettempdir()).resolve() == expected
    assert _is_ignored(".tmp-tests/x")


def test_current_workspace_is_single_source() -> None:
    """本仓库自身也必须通过：根目录、origin、分支（允许 PR detached）。"""

    module = _module()
    result = module.check_repo(
        ROOT, require_branch=None, allow_detached=True
    )

    assert result.ok, result.problems
    assert result.remote is not None
    assert module.normalize_remote(result.remote) == module.DEFAULT_REMOTE
