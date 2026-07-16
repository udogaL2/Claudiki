"""Юнит-тесты ядра моста. Всё синхронно — без потоков, без железа, без sleep.

Стратегия: `Bridge` получает управляемые зависимости — часы (FakeClock),
проверку живости (FakeLiveness) и сток снэпшотов (CollectingSink). Это позволяет
точно проверить автомат, порядок карточек, reaper, дебаунс-логику и формат JSON.
"""
import json
import os
import types

import pytest

import bridge as b


# --- Тестовые дублёры ---------------------------------------------------------
class FakeClock:
    def __init__(self, start=1000.0):
        self.t = start

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt
        return self.t


class FakeLiveness:
    """Считает живыми только pid из множества alive."""

    def __init__(self, alive=()):
        self.alive = set(alive)

    def __call__(self, pid):
        return pid in self.alive


class CollectingSink:
    def __init__(self):
        self.lines = []

    def send(self, line):
        self.lines.append(line)
        return True


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def liveness():
    return FakeLiveness()


@pytest.fixture
def sink():
    return CollectingSink()


@pytest.fixture
def bridge(clock, liveness, sink):
    cfg = b.Config(max_sessions=6)
    return b.Bridge(cfg, sink=sink, clock=clock, is_alive=liveness)


def ids(bridge):
    return [s["id"] for s in bridge.build_snapshot()["sessions"]]


def state_of(bridge, sid):
    return bridge.sessions[sid].state


# --- basename_of --------------------------------------------------------------
@pytest.mark.parametrize("cwd,expected", [
    ("/home/e/proj/worktree-4", "worktree-4"),
    ("/home/e/proj/worktree-4/", "worktree-4"),
    ("C:\\work\\bitrixpet", "bitrixpet"),
    ("C:\\work\\bitrixpet\\", "bitrixpet"),
    ("/single", "single"),
    ("", "session"),
    ("/", "session"),
    (None, "session"),
])
def test_basename_of(cwd, expected):
    assert b.basename_of(cwd) == expected


# --- transliterate ------------------------------------------------------------
@pytest.mark.parametrize("src,expected", [
    ("каталог", "katalog"),
    ("проект", "proekt"),
    ("Проект", "Proekt"),
    ("bitrixpet", "bitrixpet"),        # ASCII без изменений
    ("api-рефактор", "api-refaktor"),  # смесь
    ("щи", "shchi"),                    # многобуквенный маппинг
    ("объект", "obekt"),                # ъ выкидывается
    ("", ""),
])
def test_transliterate(src, expected):
    assert b.transliterate(src) == expected


def test_transliterate_drops_other_non_ascii():
    assert b.transliterate("café→π") == "caf"  # é, →, π невыразимы → выкинуты


# --- shorten_middle -----------------------------------------------------------
@pytest.mark.parametrize("s,n,expected", [
    ("short", 16, "short"),                 # короче лимита — как есть
    ("exactly-sixteen!", 16, "exactly-sixteen!"),  # ровно лимит
    ("feature-knowledgebase-migration-v2", 16, "feature-~tion-v2"),
    ("abcdefghijklmnop", 8, "abcd~nop"),
    ("toolongname", 3, "too"),              # слишком мало для маркера
    ("x", 0, ""),
])
def test_shorten_middle(s, n, expected):
    result = b.shorten_middle(s, n)
    assert result == expected
    assert len(result) <= n or n <= 0


def test_shorten_middle_keeps_worktrees_distinguishable():
    a = b.shorten_middle("feature-knowledgebase-migration-v2", 16)
    c = b.shorten_middle("feature-knowledgebase-migration-v3", 16)
    assert a != c  # общий префикс, но различимы по хвосту


# --- display_name (basename → транслит → обрезка) -----------------------------
def test_display_name_cyrillic_and_long():
    assert b.display_name("/home/e/проекты/каталог", 16) == "katalog"
    long = b.display_name("/repo/.worktrees/feature-knowledgebase-migration-v2", 16)
    assert long == "feature-~tion-v2" and len(long) <= 16


# --- coerce_pid ---------------------------------------------------------------
@pytest.mark.parametrize("value,expected", [
    (None, None),
    (123, 123),
    ("456", 456),
    ("nan", None),
    (object(), None),
    (12.9, 12),
])
def test_coerce_pid(value, expected):
    assert b.coerce_pid(value) == expected


# --- Автомат: start и переходы ------------------------------------------------
def test_handle_event_stores_display_name(bridge):
    # кириллица транслитерируется, длинное worktree-имя режется по центру
    bridge.handle_event({"session_id": "ru", "event": "start", "cwd": "/home/e/проекты/каталог"})
    assert bridge.sessions["ru"].name == "katalog"
    bridge.handle_event({"session_id": "wt", "event": "start",
                         "cwd": "/repo/.worktrees/feature-knowledgebase-migration-v2"})
    assert bridge.sessions["wt"].name == "feature-~tion-v2"


def test_start_creates_idle_session(bridge):
    dirty = bridge.handle_event({"session_id": "a", "event": "start",
                                 "cwd": "/p/proj", "pid": 42})
    assert dirty is True
    assert bridge.sessions["a"].state == b.IDLE
    assert bridge.sessions["a"].name == "proj"
    assert bridge.sessions["a"].pid == 42


@pytest.mark.parametrize("event,code", [
    ("working", b.WORKING),
    ("waiting", b.WAITING),
    ("idle", b.IDLE),
    ("error", b.ERROR),
])
def test_state_transitions(bridge, event, code):
    bridge.handle_event({"session_id": "a", "event": "start", "cwd": "/p/x"})  # старт → IDLE
    dirty = bridge.handle_event({"session_id": "a", "event": event})
    assert state_of(bridge, "a") == code
    # грязный только если состояние реально изменилось (старт уже IDLE)
    assert dirty is (code != b.IDLE)


def test_repeated_same_state_not_dirty(bridge):
    bridge.handle_event({"session_id": "a", "event": "start", "cwd": "/p/x"})
    assert bridge.handle_event({"session_id": "a", "event": "working"}) is True
    # повтор того же состояния не помечает грязным
    assert bridge.handle_event({"session_id": "a", "event": "working"}) is False


def test_start_again_resets_to_idle_and_updates(bridge):
    bridge.handle_event({"session_id": "a", "event": "start", "cwd": "/p/old", "pid": 1})
    bridge.handle_event({"session_id": "a", "event": "error"})
    bridge.handle_event({"session_id": "a", "event": "start", "cwd": "/p/new", "pid": 2})
    s = bridge.sessions["a"]
    assert s.state == b.IDLE and s.name == "new" and s.pid == 2


def test_unknown_event_ignored(bridge):
    bridge.handle_event({"session_id": "a", "event": "start", "cwd": "/p/x"})
    assert bridge.handle_event({"session_id": "a", "event": "flying"}) is False
    assert state_of(bridge, "a") == b.IDLE


def test_event_without_session_id_ignored(bridge):
    assert bridge.handle_event({"event": "working"}) is False
    assert bridge.sessions == {}


def test_unknown_session_created_on_the_fly(bridge):
    # не-start событие для незнакомой сессии → создаём с state из события и pid=None
    dirty = bridge.handle_event({"session_id": "z", "event": "waiting", "cwd": "/p/mcp"})
    assert dirty is True
    s = bridge.sessions["z"]
    assert s.state == b.WAITING and s.pid is None and s.name == "mcp"


def test_end_removes_session(bridge):
    bridge.handle_event({"session_id": "a", "event": "start", "cwd": "/p/x"})
    assert bridge.handle_event({"session_id": "a", "event": "end"}) is True
    assert "a" not in bridge.sessions


def test_end_unknown_session_not_dirty(bridge):
    assert bridge.handle_event({"session_id": "ghost", "event": "end"}) is False


def test_garbage_pid_becomes_none(bridge):
    bridge.handle_event({"session_id": "a", "event": "start", "cwd": "/p/x", "pid": "oops"})
    assert bridge.sessions["a"].pid is None


# --- Снэпшот ------------------------------------------------------------------
def test_snapshot_stable_order_by_first_seen(bridge, clock):
    for sid in ("first", "second", "third"):
        clock.advance(1)
        bridge.handle_event({"session_id": sid, "event": "start", "cwd": f"/p/{sid}"})
    # обновление статуса не меняет порядок
    bridge.handle_event({"session_id": "first", "event": "working"})
    assert ids(bridge) == ["first", "second", "third"]


def test_snapshot_respects_max_sessions(clock, liveness, sink):
    cfg = b.Config(max_sessions=3)
    br = b.Bridge(cfg, sink=sink, clock=clock, is_alive=liveness)
    for i in range(6):
        clock.advance(1)
        br.handle_event({"session_id": f"s{i}", "event": "idle", "cwd": f"/p/{i}"})
    snap = br.build_snapshot()
    assert len(snap["sessions"]) == 3
    assert [s["id"] for s in snap["sessions"]] == ["s0", "s1", "s2"]


def test_duplicate_names_get_session_suffix(clock, liveness, sink):
    # два claude в одном репо → одинаковый basename → добавляем суффикс session_id
    cfg = b.Config(max_sessions=6)
    br = b.Bridge(cfg, sink=sink, clock=clock, is_alive=liveness)
    br.handle_event({"session_id": "99ce5dce-aaaa", "event": "working", "cwd": "/x/Claudiki"})
    br.handle_event({"session_id": "8d7d7c21-bbbb", "event": "waiting", "cwd": "/x/Claudiki"})
    names = {s["id"]: s["name"] for s in br.build_snapshot()["sessions"]}
    assert names["99ce5dce-aaaa"] == "Claudiki#99ce"
    assert names["8d7d7c21-bbbb"] == "Claudiki#8d7d"
    assert names["99ce5dce-aaaa"] != names["8d7d7c21-bbbb"]


def test_unique_names_keep_no_suffix(bridge):
    bridge.handle_event({"session_id": "a1", "event": "idle", "cwd": "/x/alpha"})
    bridge.handle_event({"session_id": "b2", "event": "idle", "cwd": "/x/beta"})
    names = {s["name"] for s in bridge.build_snapshot()["sessions"]}
    assert names == {"alpha", "beta"}  # без суффиксов


def test_duplicate_long_names_stay_within_limit(clock, liveness, sink):
    cfg = b.Config(max_sessions=6, name_max=16)
    br = b.Bridge(cfg, sink=sink, clock=clock, is_alive=liveness)
    cwd = "/repo/.worktrees/feature-knowledgebase-migration"
    br.handle_event({"session_id": "aaaa1111", "event": "working", "cwd": cwd})
    br.handle_event({"session_id": "bbbb2222", "event": "working", "cwd": cwd})
    for s in br.build_snapshot()["sessions"]:
        assert len(s["name"]) <= 16


# --- Суб-агенты ---------------------------------------------------------------
def test_subagent_increment_decrement(bridge):
    bridge.handle_event({"session_id": "a", "event": "working", "cwd": "/p/x"})
    assert bridge.handle_event({"session_id": "a", "event": "subagent"}) is True
    assert bridge.sessions["a"].subagents == 1
    bridge.handle_event({"session_id": "a", "event": "subagent"})
    assert bridge.sessions["a"].subagents == 2
    bridge.handle_event({"session_id": "a", "event": "subagent_done"})
    assert bridge.sessions["a"].subagents == 1


def test_subagent_done_floors_at_zero(bridge):
    bridge.handle_event({"session_id": "a", "event": "working", "cwd": "/p/x"})
    bridge.handle_event({"session_id": "a", "event": "subagent_done"})
    assert bridge.sessions["a"].subagents == 0


def test_working_preserves_subagents(bridge):
    bridge.handle_event({"session_id": "a", "event": "working", "cwd": "/p/x"})
    bridge.handle_event({"session_id": "a", "event": "subagent"})
    bridge.handle_event({"session_id": "a", "event": "working"})   # PostToolUse — не сбрасывает
    assert bridge.sessions["a"].subagents == 1


def test_idle_resets_subagents(bridge):
    bridge.handle_event({"session_id": "a", "event": "working", "cwd": "/p/x"})
    bridge.handle_event({"session_id": "a", "event": "subagent"})
    bridge.handle_event({"session_id": "a", "event": "subagent"})
    assert bridge.handle_event({"session_id": "a", "event": "idle"}) is True
    assert bridge.sessions["a"].subagents == 0 and bridge.sessions["a"].state == b.IDLE


def test_start_resets_subagents(bridge):
    bridge.handle_event({"session_id": "a", "event": "working", "cwd": "/p/x"})
    bridge.handle_event({"session_id": "a", "event": "subagent"})
    bridge.handle_event({"session_id": "a", "event": "start", "cwd": "/p/x"})
    assert bridge.sessions["a"].subagents == 0


def test_subagent_unknown_session_created_working(bridge):
    assert bridge.handle_event({"session_id": "z", "event": "subagent", "cwd": "/p/mcp"}) is True
    assert bridge.sessions["z"].subagents == 1 and bridge.sessions["z"].state == b.WORKING


def test_snapshot_includes_sub_and_omits_zero(bridge):
    bridge.handle_event({"session_id": "a", "event": "working", "cwd": "/p/proj"})
    assert "sub" not in bridge.build_snapshot()["sessions"][0]   # 0 → не шлём
    bridge.handle_event({"session_id": "a", "event": "subagent"})
    assert bridge.build_snapshot()["sessions"][0]["sub"] == 1


def test_snapshot_sub_capped_at_5(bridge):
    bridge.handle_event({"session_id": "a", "event": "working", "cwd": "/p/proj"})
    for _ in range(9):
        bridge.handle_event({"session_id": "a", "event": "subagent"})
    assert bridge.build_snapshot()["sessions"][0]["sub"] == 5   # в снэпшоте кап 5
    assert bridge.sessions["a"].subagents == 9                   # внутри — реальное число


def test_snapshot_field_shape(bridge):
    bridge.handle_event({"session_id": "a", "event": "working", "cwd": "/p/proj"})
    snap = bridge.build_snapshot()
    assert snap["v"] == 1
    assert snap["sessions"][0] == {"id": "a", "name": "proj", "state": b.WORKING}


def test_snapshot_line_is_compact_json_with_newline(bridge):
    bridge.handle_event({"session_id": "a", "event": "working", "cwd": "/p/proj"})
    line = bridge.snapshot_line()
    assert line.endswith("\n")
    assert ", " not in line and ": " not in line  # компактно, без пробелов
    assert json.loads(line) == bridge.build_snapshot()


# --- Reaper -------------------------------------------------------------------
def test_reap_removes_dead_pids(bridge, liveness):
    liveness.alive = {100}
    bridge.handle_event({"session_id": "live", "event": "start", "cwd": "/p/a", "pid": 100})
    bridge.handle_event({"session_id": "dead", "event": "start", "cwd": "/p/b", "pid": 200})
    removed = bridge.reap()
    assert removed == ["dead"]
    assert "live" in bridge.sessions and "dead" not in bridge.sessions


def test_reap_keeps_sessions_without_pid(bridge):
    bridge.handle_event({"session_id": "nopid", "event": "waiting", "cwd": "/p/a"})
    assert bridge.reap() == []
    assert "nopid" in bridge.sessions


def test_reap_no_dead_returns_empty(bridge, liveness):
    liveness.alive = {100}
    bridge.handle_event({"session_id": "live", "event": "start", "cwd": "/p/a", "pid": 100})
    assert bridge.reap() == []


# --- push / дебаунс-коалессинг ------------------------------------------------
def test_push_sends_one_snapshot(bridge, sink):
    bridge.handle_event({"session_id": "a", "event": "working", "cwd": "/p/x"})
    assert bridge.push() is True
    assert len(sink.lines) == 1
    assert json.loads(sink.lines[0])["sessions"][0]["id"] == "a"


def test_push_diag_logs_and_counts(caplog):
    import logging
    cfg = b.Config(diag=True)
    clock = FakeClock()
    br = b.Bridge(cfg, sink=CollectingSink(), clock=clock, is_alive=FakeLiveness())
    br.handle_event({"session_id": "a", "event": "working", "cwd": "/p/proj"})
    with caplog.at_level(logging.INFO, logger="octo.bridge"):
        assert br.push("event") is True
        clock.advance(1.234)
        br.push("heartbeat")
    assert br._push_n == 2
    assert "PUSH #1 reason=event" in caplog.text
    assert "n=1" in caplog.text and "proj:W" in caplog.text
    assert "dt=1234ms" in caplog.text  # интервал между пушами виден в логе


def test_push_no_diag_is_silent(bridge, sink, caplog):
    import logging
    bridge.handle_event({"session_id": "a", "event": "working", "cwd": "/p/x"})
    with caplog.at_level(logging.INFO, logger="octo.bridge"):
        bridge.push()
    assert "PUSH #" not in caplog.text  # без OCTO_DIAG — тихо
    assert len(sink.lines) == 1


def test_multiple_events_coalesce_into_one_push(bridge, sink):
    # серия событий → мост «грязный», но пуш читает состояние один раз → одна строка
    for ev in ("working", "waiting", "idle", "error"):
        bridge.handle_event({"session_id": "a", "event": ev, "cwd": "/p/x"})
    bridge.push()
    assert len(sink.lines) == 1
    assert json.loads(sink.lines[0])["sessions"][0]["state"] == b.ERROR


# --- Config из окружения ------------------------------------------------------
def test_config_defaults(monkeypatch):
    for k in list(os.environ):
        if k.startswith("OCTO_"):
            monkeypatch.delenv(k, raising=False)
    cfg = b.Config()
    assert cfg.port == 8787
    assert cfg.serial_port == "auto"
    assert cfg.max_sessions == 6
    assert cfg.mock is False


def test_config_from_env(monkeypatch):
    monkeypatch.setenv("OCTO_BRIDGE_PORT", "9999")
    monkeypatch.setenv("OCTO_SERIAL_PORT", "COM7")
    monkeypatch.setenv("OCTO_MAX_SESSIONS", "2")
    monkeypatch.setenv("OCTO_MOCK", "1")
    cfg = b.Config()
    assert cfg.port == 9999 and cfg.serial_port == "COM7"
    assert cfg.max_sessions == 2 and cfg.mock is True


# --- parse_event_body ---------------------------------------------------------
def test_parse_event_body_empty():
    assert b.parse_event_body(b"") == {}


def test_parse_event_body_valid():
    assert b.parse_event_body(b'{"event":"idle"}') == {"event": "idle"}


def test_parse_event_body_invalid_raises():
    with pytest.raises(json.JSONDecodeError):
        b.parse_event_body(b"not json")


# --- pid_alive (реальная ОС) --------------------------------------------------
def test_pid_alive_none_is_true():
    assert b.pid_alive(None) is True


def test_pid_alive_self_is_true():
    assert b.pid_alive(os.getpid()) is True


def test_pid_alive_bogus_is_false():
    assert b.pid_alive(999_999_999) is False


# --- LoggingSink --------------------------------------------------------------
def test_logging_sink_returns_false():
    assert b.LoggingSink().send("{}\n") is False


# --- autodetect_port ----------------------------------------------------------
def _port(device, vid=None, pid=None, desc=""):
    return types.SimpleNamespace(device=device, vid=vid, pid=pid, description=desc)


def test_autodetect_prefers_known_usb_id():
    ports = [_port("COM1", 0x1111, 0x2222), _port("COM5", 0x1A86, 0x7523)]  # CH340
    assert b.autodetect_port(lambda: ports) == "COM5"


def test_autodetect_single_port_fallback():
    assert b.autodetect_port(lambda: [_port("COM9")]) == "COM9"


def test_autodetect_ambiguous_returns_none():
    ports = [_port("COM1", 0x1, 0x1), _port("COM2", 0x2, 0x2)]
    assert b.autodetect_port(lambda: ports) is None


def test_autodetect_no_ports_returns_none():
    assert b.autodetect_port(lambda: []) is None


# --- SerialSink с фейковым serial ---------------------------------------------
class FakePort:
    def __init__(self, fail_write=False):
        self.is_open = True
        self.written = []
        self.fail_write = fail_write

    def write(self, data):
        if self.fail_write:
            raise IOError("boom")
        self.written.append(data)

    def flush(self):
        pass

    def close(self):
        self.is_open = False


@pytest.fixture
def fake_serial(monkeypatch):
    """Подменяет модульный serial фейком, отдающим заранее заданный порт."""
    holder = {"port": FakePort(), "raise_open": False}

    def Serial(port, baud, **kw):
        if holder["raise_open"]:
            raise IOError("cannot open")
        holder["port"].opened_as = port
        return holder["port"]

    monkeypatch.setattr(b, "serial", types.SimpleNamespace(Serial=Serial))
    return holder


def test_serialsink_writes_line(fake_serial):
    sink = b.SerialSink("COM3", 115200)
    assert sink.send("hello\n") is True
    assert fake_serial["port"].written == [b"hello\n"]


def test_serialsink_write_failure_recovers(fake_serial):
    fake_serial["port"] = FakePort(fail_write=True)
    sink = b.SerialSink("COM3", 115200)
    assert sink.send("x\n") is False
    assert sink._ser is None  # переоткроется на следующей отправке


def test_serialsink_open_failure_returns_false(fake_serial):
    fake_serial["raise_open"] = True
    sink = b.SerialSink("COM3", 115200)
    assert sink.send("x\n") is False


def test_serialsink_auto_uses_autodetect(fake_serial):
    sink = b.SerialSink("auto", 115200, list_ports_fn=lambda: [_port("COMX", 0x1A86, 0x7523)])
    assert sink.send("x\n") is True
    assert fake_serial["port"].opened_as == "COMX"


def test_serialsink_auto_no_device_returns_false(fake_serial):
    sink = b.SerialSink("auto", 115200, list_ports_fn=lambda: [])
    assert sink.send("x\n") is False


def test_serialsink_none_when_serial_missing(monkeypatch):
    monkeypatch.setattr(b, "serial", None)
    sink = b.SerialSink("COM3", 115200)
    assert sink.send("x\n") is False


# --- HTTP-сервер (интеграция в процессе, эфемерный порт) ----------------------
import threading  # noqa: E402
import urllib.error  # noqa: E402
import urllib.request  # noqa: E402


def _serve(cfg_port=0):
    cfg = b.Config(port=cfg_port)
    server = b.bind_singleton(cfg)
    assert server is not None
    server.bridge = b.Bridge(cfg, sink=CollectingSink(), clock=FakeClock(), is_alive=FakeLiveness())
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server, port


def _post(port, path, body):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=body,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    return urllib.request.urlopen(req, timeout=2)


def test_http_event_and_status_roundtrip():
    server, port = _serve()
    try:
        with _post(port, "/event",
                   json.dumps({"session_id": "a", "event": "working", "cwd": "/p/proj"}).encode()) as r:
            assert r.status == 200
            assert json.loads(r.read())["ok"] is True

        with urllib.request.urlopen(f"http://127.0.0.1:{port}/status", timeout=2) as r:
            snap = json.loads(r.read())
        assert snap["v"] == 1
        assert snap["sessions"][0] == {"id": "a", "name": "proj", "state": b.WORKING}
    finally:
        server.shutdown()
        server.server_close()


def test_http_bad_json_still_returns_200():
    # хук не должен «зависнуть» — даже на мусоре сервер отвечает 200
    server, port = _serve()
    try:
        with _post(port, "/event", b"not json at all") as r:
            assert r.status == 200
    finally:
        server.shutdown()
        server.server_close()


def test_http_unknown_paths_404():
    server, port = _serve()
    try:
        with pytest.raises(urllib.error.HTTPError) as e1:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/nope", timeout=2)
        assert e1.value.code == 404
        with pytest.raises(urllib.error.HTTPError) as e2:
            _post(port, "/wrong", b"{}")
        assert e2.value.code == 404
    finally:
        server.shutdown()
        server.server_close()


def test_bind_singleton_second_instance_returns_none():
    server, port = _serve()
    try:
        assert b.bind_singleton(b.Config(port=port)) is None  # порт занят → второй инстанс None
    finally:
        server.shutdown()
        server.server_close()


def test_bind_singleton_reraises_other_oserror(monkeypatch):
    import errno

    def boom(*a, **k):
        raise OSError(errno.EACCES, "denied")

    monkeypatch.setattr(b, "ThreadingHTTPServer", boom)
    with pytest.raises(OSError):
        b.bind_singleton(b.Config(port=0))


# --- Фолбэк-ветки (без psutil, дефолтное автоопределение, кэш serial и пр.) ----
def test_pid_alive_without_psutil(monkeypatch):
    monkeypatch.setattr(b, "psutil", None)
    assert b.pid_alive(None) is True
    assert b.pid_alive(os.getpid()) is True
    assert b.pid_alive(999_999_999) is False


def test_autodetect_default_uses_real_comports():
    # без list_ports_fn берётся serial.tools.list_ports.comports — просто должно отработать
    result = b.autodetect_port()
    assert result is None or isinstance(result, str)


def test_autodetect_no_pyserial(monkeypatch):
    monkeypatch.setattr(b, "_list_ports", None)
    assert b.autodetect_port() is None


def test_serialsink_reuses_open_connection(fake_serial):
    sink = b.SerialSink("COM3", 115200)
    assert sink.send("a\n") is True
    assert sink.send("b\n") is True  # переиспользует уже открытый порт
    assert fake_serial["port"].written == [b"a\n", b"b\n"]


def test_serialsink_close_swallows_error(fake_serial):
    class BadClose(FakePort):
        def close(self):
            raise IOError("close failed")

    fake_serial["port"] = BadClose()
    sink = b.SerialSink("COM3", 115200)
    sink.send("x\n")       # открывает
    sink.close()            # не должно бросить, несмотря на ошибку close()
    assert sink._ser is None


def test_pid_updated_on_non_start_event(bridge):
    bridge.handle_event({"session_id": "a", "event": "start", "cwd": "/p/x"})  # pid=None
    assert bridge.sessions["a"].pid is None
    bridge.handle_event({"session_id": "a", "event": "working", "pid": 55})
    assert bridge.sessions["a"].pid == 55
