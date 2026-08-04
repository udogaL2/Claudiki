"""Тесты CLI octoctl. Сеть не трогаем — подменяем octoctl.call."""
import json
import urllib.error

import pytest

import octoctl as c


DEBUG_SAMPLE = {
    "bridge": {
        "pid": 4242, "uptime_sec": 7200, "sessions_total": 3, "visible": 2,
        "push_n": 17, "last_push_sec_ago": 1.2, "reconcile_n": 5,
        "registry_ok": True, "registry_sessions": 3,
        "sink": {"kind": "serial", "port": "auto", "resolved": "/dev/ttyUSB0",
                 "connected": True, "opens": 1},
    },
    "config": {"max_sessions": 6, "stale_idle_hours": 4.0, "log_file": ""},
    "sessions": [
        {"id": "aaaa1111-x", "name": "proj", "registry_name": "proj-a1", "state": 0,
         "state_name": "WORKING", "pid": 100, "pid_alive": True, "subagents": 2,
         "idle_hours": 0.02, "visible": True, "hidden_reason": None},
        {"id": "bbbb2222-y", "name": "old", "registry_name": None, "state": 2,
         "state_name": "IDLE", "pid": 200, "pid_alive": False, "subagents": 0,
         "idle_hours": 87.6, "visible": False, "hidden_reason": "idle 87.6ч > 4ч"},
        {"id": "cccc3333-z", "name": "nopid", "registry_name": None, "state": 1,
         "state_name": "WAITING", "pid": None, "pid_alive": None, "subagents": 0,
         "idle_hours": None, "visible": True, "hidden_reason": None},
    ],
}


@pytest.fixture
def fake_call(monkeypatch):
    calls = []

    def call(path, method="GET", timeout=5.0):
        calls.append((method, path))
        if path == "/debug":
            return DEBUG_SAMPLE
        if path == "/status":
            return {"v": 1, "sessions": [{"id": "a", "name": "proj", "state": 0, "sub": 2}]}
        if path == "/shot":
            return {"ok": True, "path": "/tmp/octodash-shot.png", "w": 320, "h": 240}
        if path == "/resync":
            return {"ok": True, "changed": True}
        if path == "/reset":
            return {"ok": True, "forgotten": 4}
        return {"ok": True}

    monkeypatch.setattr(c, "call", call)
    return calls


def test_fmt_sessions_marks_visible_and_dead_pid():
    out = c.fmt_sessions(DEBUG_SAMPLE)
    lines = out.splitlines()
    assert lines[1].startswith("• aaaa1111")      # видимая — с маркером
    assert "proj-a1" in lines[1] and "+2sub" in lines[1]
    assert lines[2].startswith("  bbbb2222")      # скрытая — без маркера
    assert "200†" in lines[2] and "idle 87.6ч" in lines[2]
    assert "—" in lines[3]                         # PID неизвестен


def test_fmt_sessions_formats_idle_in_minutes_and_hours():
    out = c.fmt_sessions(DEBUG_SAMPLE)
    assert "1м" in out       # 0.02ч → минуты
    assert "87.6ч" in out


def test_fmt_head_summarises_bridge():
    head = c.fmt_head(DEBUG_SAMPLE)
    assert "pid=4242" in head and "uptime=2.0ч" in head
    assert "на экране=2/6" in head
    assert "реестр=ok" in head
    assert "/dev/ttyUSB0 подключён" in head


@pytest.mark.parametrize("ok,expected", [(False, "НЕДОСТУПЕН"), (None, "не сверялся")])
def test_fmt_head_registry_states(ok, expected):
    sample = json.loads(json.dumps(DEBUG_SAMPLE))
    sample["bridge"]["registry_ok"] = ok
    assert expected in c.fmt_head(sample)


def test_resolve_id_prefix_exact_and_ambiguous(capsys):
    assert c.resolve_id(DEBUG_SAMPLE, "aaaa") == "aaaa1111-x"
    assert c.resolve_id(DEBUG_SAMPLE, "nope") is None
    doubled = {"sessions": [{"id": "dup-1"}, {"id": "dup-2"}]}
    assert c.resolve_id(doubled, "dup") is None
    assert "неоднозначен" in capsys.readouterr().err


def test_cmd_status(fake_call, capsys):
    assert c.cmd_status() == 0
    out = capsys.readouterr().out
    assert "на экране 1 карточек" in out and "proj" in out and "+2sub" in out


def test_cmd_debug_text_and_json(fake_call, capsys):
    assert c.cmd_debug(False) == 0
    text = capsys.readouterr().out
    assert "pid=4242" in text and "• aaaa1111" in text

    assert c.cmd_debug(True) == 0
    assert json.loads(capsys.readouterr().out)["bridge"]["pid"] == 4242


def test_cmd_resync_and_reset(fake_call, capsys):
    assert c.cmd_resync() == 0
    assert "изменения: да" in capsys.readouterr().out
    assert c.cmd_reset() == 0
    assert "забыто сессий: 4" in capsys.readouterr().out
    assert ("POST", "/resync") in fake_call and ("POST", "/reset") in fake_call


def test_cmd_hide_resolves_prefix(fake_call, capsys):
    assert c.cmd_hide("aaaa") == 0
    assert ("DELETE", "/sessions/aaaa1111-x") in fake_call
    assert "скрыта aaaa1111" in capsys.readouterr().out


def test_cmd_hide_unknown_session(fake_call, capsys):
    assert c.cmd_hide("zzzz") == 1
    assert "не найдена" in capsys.readouterr().err


def test_main_dispatch(fake_call, capsys):
    assert c.main(["status"]) == 0
    assert c.main(["debug", "--json"]) == 0
    assert c.main(["resync"]) == 0
    assert c.main(["reset"]) == 0
    assert c.main(["hide", "aaaa"]) == 0
    assert c.main(["rm", "aaaa"]) == 0          # старое имя оставлено алиасом
    capsys.readouterr()
    assert c.main([]) == 1                      # без команды — справка и код 1
    assert "usage" in capsys.readouterr().out.lower()


def test_main_reports_unreachable_bridge(monkeypatch, capsys):
    def boom(*a, **k):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(c, "call", boom)
    assert c.main(["status"]) == 1
    assert "не отвечает" in capsys.readouterr().err


def test_main_reports_http_error(monkeypatch, capsys):
    def boom(*a, **k):
        raise urllib.error.HTTPError("u", 404, "Not Found", {}, None)

    monkeypatch.setattr(c, "call", boom)
    assert c.main(["rm", "x"]) == 1
    assert "ответил 404" in capsys.readouterr().err


def test_restart_spawns_when_bridge_absent(monkeypatch, capsys):
    spawned = []

    def boom(*a, **k):
        raise urllib.error.URLError("nobody home")

    monkeypatch.setattr(c, "call", boom)
    monkeypatch.setattr(c, "spawn_bridge", lambda: spawned.append(1) or 0)
    assert c.cmd_restart() == 0
    assert spawned == [1]
    assert "просто запускаю новый" in capsys.readouterr().out


def test_restart_kills_and_respawns(monkeypatch, capsys):
    killed, spawned = [], []
    monkeypatch.setattr(c.os, "kill", lambda pid, sig: killed.append((pid, sig)))
    monkeypatch.setattr(c, "spawn_bridge", lambda: spawned.append(1) or 0)
    monkeypatch.setattr(c.time, "sleep", lambda s: None)
    monkeypatch.setattr(c, "call", lambda path, method="GET", timeout=5.0: DEBUG_SAMPLE
                        if path == "/debug" else {"v": 1, "sessions": []})
    assert c.cmd_restart() == 0
    assert killed[0][0] == 4242 and spawned == [1]
    assert "перезапущен: 4242" in capsys.readouterr().out


def test_bridge_pid_prefers_debug(fake_call):
    assert c.bridge_pid() == 4242


def test_bridge_pid_falls_back_on_missing_endpoint(monkeypatch):
    # мост живой, но старой версии: /debug → 404, PID ищем по слушателю порта
    def only_404(*a, **k):
        raise urllib.error.HTTPError("u", 404, "Not Found", {}, None)

    monkeypatch.setattr(c, "call", only_404)
    monkeypatch.setattr(c, "find_bridge_pid", lambda: 777)
    assert c.bridge_pid() == 777


def test_restart_handles_old_version(monkeypatch, capsys):
    killed, spawned, calls = [], [], []

    def call(path, method="GET", timeout=5.0):
        calls.append(path)
        if path == "/debug" and len(calls) == 1:
            raise urllib.error.HTTPError("u", 404, "Not Found", {}, None)  # старый мост
        if path == "/debug":
            return {"bridge": {"pid": 999}}
        return {"v": 1, "sessions": []}

    monkeypatch.setattr(c, "call", call)
    monkeypatch.setattr(c, "find_bridge_pid", lambda: 555)
    monkeypatch.setattr(c.os, "kill", lambda pid, sig: killed.append(pid))
    monkeypatch.setattr(c, "spawn_bridge", lambda: spawned.append(1) or 0)
    monkeypatch.setattr(c.time, "sleep", lambda s: None)
    assert c.cmd_restart() == 0
    assert killed[0] == 555 and spawned == [1]
    assert "перезапущен: 555 -> 999" in capsys.readouterr().out


def test_restart_without_pid_tells_what_to_do(monkeypatch, capsys):
    monkeypatch.setattr(c, "bridge_pid", lambda: None)
    assert c.cmd_restart() == 1
    assert "вручную" in capsys.readouterr().err


def test_find_bridge_pid_without_psutil(monkeypatch):
    import builtins
    real_import = builtins.__import__

    def no_psutil(name, *a, **k):
        if name == "psutil":
            raise ImportError("нет psutil")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", no_psutil)
    assert c.find_bridge_pid() is None


def test_find_bridge_pid_locates_listener(monkeypatch):
    import types
    fake = types.SimpleNamespace(
        CONN_LISTEN="LISTEN",
        net_connections=lambda kind: [
            types.SimpleNamespace(pid=None, status="LISTEN",
                                  laddr=types.SimpleNamespace(port=int(c.PORT))),
            types.SimpleNamespace(pid=1, status="ESTABLISHED",
                                  laddr=types.SimpleNamespace(port=int(c.PORT))),
            types.SimpleNamespace(pid=42, status="LISTEN",
                                  laddr=types.SimpleNamespace(port=int(c.PORT))),
        ])
    monkeypatch.setitem(__import__("sys").modules, "psutil", fake)
    assert c.find_bridge_pid() == 42


def test_main_hints_on_old_bridge(monkeypatch, capsys):
    def boom(*a, **k):
        raise urllib.error.HTTPError("u", 404, "Not Found", {}, None)

    monkeypatch.setattr(c, "call", boom)
    assert c.main(["debug"]) == 1
    assert "старой версией" in capsys.readouterr().err


def test_shot_prints_path(fake_call, capsys):
    assert c.main(["shot"]) == 0
    out = capsys.readouterr().out
    assert "320x240" in out and "octodash-shot.png" in out
    assert ("POST", "/shot") in fake_call


def test_shot_reports_failure(monkeypatch, capsys):
    monkeypatch.setattr(c, "call", lambda p, m="GET", timeout=5.0: {"ok": False})
    assert c.main(["shot"]) == 1
    assert "не получился" in capsys.readouterr().err


def test_shot_on_old_bridge_suggests_restart(monkeypatch, capsys):
    def call(path, method="GET", timeout=5.0):
        raise urllib.error.HTTPError(path, 404, "Not Found", None, None)
    monkeypatch.setattr(c, "call", call)
    assert c.main(["shot"]) == 1
    assert "octoctl restart" in capsys.readouterr().err
