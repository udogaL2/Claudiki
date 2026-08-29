"""Тесты обёртки-хука octo-notify.py — в первую очередь распознавания процесса
claude при обходе дерева. Регрессия: раньше матчер по подстроке "claude" в cmdline
ложно срабатывал на транзиентном шелле, чей аргумент содержал путь ~/.claude/...
(shell-snapshots / hooks), из-за чего reaper убивал карточку через пару секунд."""
import importlib.util
import os

import pytest

_HOOK_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "octo-notify.py")


@pytest.fixture(scope="module")
def hook():
    spec = importlib.util.spec_from_file_location("octonotify", _HOOK_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeProc:
    """Минимальный двойник psutil.Process для тестов матчера без железа."""

    def __init__(self, pid, name, cmdline, parent=None):
        self.pid = pid
        self._name = name
        self._cmdline = cmdline
        self._parent = parent

    def name(self):
        return self._name

    def cmdline(self):
        return self._cmdline

    def parent(self):
        return self._parent


# --- _looks_like_claude -------------------------------------------------------
def test_native_claude_exe_matches_by_name(hook):
    p = FakeProc(1, "claude.exe", [r"C:\Users\e\.local\bin\claude.exe"])
    assert hook._looks_like_claude(p) is True


def test_claude_binary_matches_by_name(hook):
    p = FakeProc(1, "claude", ["/home/e/.local/bin/claude"])
    assert hook._looks_like_claude(p) is True


def test_node_cli_matches_by_package(hook):
    p = FakeProc(1, "node", ["node", "/usr/lib/node_modules/@anthropic-ai/claude-code/cli.js"])
    assert hook._looks_like_claude(p) is True


def test_script_basename_matches(hook):
    p = FakeProc(1, "python.exe", ["python", "/opt/bin/claude"])
    assert hook._looks_like_claude(p) is True


def test_shell_with_dot_claude_path_does_not_match(hook):
    """Регрессия: транзиентный шелл с ~/.claude/... в аргументах НЕ должен матчиться."""
    p = FakeProc(
        1,
        "bash.exe",
        ["bash.exe", "-c", "source /c/Users/e/.claude/shell-snapshots/snapshot-bash-123.sh"],
    )
    assert hook._looks_like_claude(p) is False


def test_hook_process_with_dot_claude_path_does_not_match(hook):
    """Сам процесс хука (python ~/.claude/hooks/octo-notify.py) тоже не claude."""
    p = FakeProc(1, "python.exe", ["python", "/home/e/.claude/hooks/octo-notify.py"])
    assert hook._looks_like_claude(p) is False


# --- resolve_claude_pid: обход дерева -----------------------------------------
def _chain(hook, monkeypatch, leaf):
    """Подсовывает psutil.Process(getppid) → leaf и фиксированный getppid."""
    import psutil

    monkeypatch.setattr(hook.os, "getppid", lambda: leaf.pid)
    monkeypatch.setattr(psutil, "Process", lambda pid: leaf)


def test_resolve_walks_past_transient_shell_to_claude(hook, monkeypatch):
    claude = FakeProc(7720, "claude.exe", [r"C:\Users\e\.local\bin\claude.exe"])
    sh2 = FakeProc(300, "bash.exe", ["bash.exe", "-c", "source /c/Users/e/.claude/shell-snapshots/s.sh"], parent=claude)
    sh1 = FakeProc(200, "bash.exe", ["bash.exe", "-c", "..."], parent=sh2)
    hookproc = FakeProc(100, "python.exe", ["python", "/home/e/.claude/hooks/octo-notify.py"], parent=sh1)
    _chain(hook, monkeypatch, hookproc)
    assert hook.resolve_claude_pid() == 7720


def test_resolve_returns_none_when_no_claude_in_tree(hook, monkeypatch):
    root = FakeProc(2, "explorer.exe", ["explorer.exe"])
    child = FakeProc(1, "python.exe", ["python", "/home/e/.claude/hooks/octo-notify.py"], parent=root)
    _chain(hook, monkeypatch, child)
    assert hook.resolve_claude_pid() is None


def test_resolve_returns_none_without_psutil(hook, monkeypatch):
    # Симулируем отсутствие psutil: import внутри функции упадёт.
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name == "psutil":
            raise ImportError("no psutil")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    assert hook.resolve_claude_pid() is None


# --- конверт хука → payload моста ---------------------------------------------
def run_hook(hook, monkeypatch, envelope, argv=("octo-notify.py",)):
    """Прогоняет main() с подставным stdin и перехватывает отправленный payload."""
    import io
    import json
    import sys

    sent = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        sent["url"] = req.full_url
        sent["payload"] = json.loads(req.data.decode("utf-8"))
        return FakeResponse()

    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(envelope)))
    monkeypatch.setattr(sys, "argv", list(argv))
    monkeypatch.setattr(hook.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(hook, "resolve_claude_pid", lambda: 4242)
    hook.main()
    return sent.get("payload")


def test_hook_forwards_transcript_path(hook, monkeypatch):
    payload = run_hook(hook, monkeypatch, {
        "session_id": "abc", "hook_event_name": "UserPromptSubmit",
        "cwd": "/work/proj", "transcript_path": "/home/e/.claude/projects/x/abc.jsonl",
    })
    assert payload["transcript"] == "/home/e/.claude/projects/x/abc.jsonl"
    assert payload["event"] == "working" and payload["session_id"] == "abc"


def test_hook_transcript_empty_when_absent(hook, monkeypatch):
    payload = run_hook(hook, monkeypatch, {
        "session_id": "abc", "hook_event_name": "Stop", "cwd": "/work/proj",
    })
    assert payload["transcript"] == ""


def test_hook_start_carries_pid_and_transcript(hook, monkeypatch):
    payload = run_hook(hook, monkeypatch, {
        "session_id": "abc", "hook_event_name": "SessionStart",
        "cwd": "/work/proj", "transcript_path": "/t/abc.jsonl",
    })
    assert payload["pid"] == 4242 and payload["transcript"] == "/t/abc.jsonl"


def test_hook_carries_meta_orchestration_env(hook, monkeypatch):
    """Роль, имя сессии и родителя хук берёт ИЗ ОКРУЖЕНИЯ агента.

    Другого способа нет: `MJC_PARENT` кладёт плагин MetaJetCore во вкладку, и в реестре
    Claude Code этого поля не существует вовсе — без хука связь «агент → оркестратор»
    мосту взять неоткуда."""
    monkeypatch.setenv("CLAUDE_CODE_AGENT", "reviewer")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_NAME", "mjc-rev-sec")
    monkeypatch.setenv("MJC_PARENT", "mjc-orc")
    sent = run_hook(hook, monkeypatch, {"hook_event_name": "Stop", "session_id": "s1", "cwd": "/w"})
    assert sent["agent"] == "reviewer"
    assert sent["sess_name"] == "mjc-rev-sec"
    assert sent["parent"] == "mjc-orc"


def test_hook_omits_meta_keys_outside_orchestration(hook, monkeypatch):
    """Обычная сессия — обычный конверт: пустых ключей мост не должен разбирать."""
    for var in ("CLAUDE_CODE_AGENT", "CLAUDE_CODE_SESSION_NAME", "MJC_PARENT"):
        monkeypatch.delenv(var, raising=False)
    sent = run_hook(hook, monkeypatch, {"hook_event_name": "Stop", "session_id": "s1", "cwd": "/w"})
    assert not ({"agent", "sess_name", "parent"} & set(sent))
