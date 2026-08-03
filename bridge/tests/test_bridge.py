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
    """Монотонные часы + сдвигающееся вместе с ними wall-время.

    Мосту нужны оба: монотонное — для дебаунса/возраста, абсолютное (мс) — для
    «брошенности» сессии и сравнения со временем из реестра Claude Code.
    """

    def __init__(self, start=1000.0, wall_start=1_700_000_000.0):
        self.t = start
        self.w = wall_start

    def __call__(self):
        return self.t

    def wall(self):
        return self.w

    def advance(self, dt):
        self.t += dt
        self.w += dt
        return self.t

    def advance_hours(self, hours):
        return self.advance(hours * 3600)


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


def make_bridge(cfg, clock, liveness, sink, probe=lambda: None):
    """Мост с полностью управляемыми зависимостями.

    probe по умолчанию отдаёт None («реестра нет») — чтобы тесты никогда не
    заглядывали в реальный ~/.claude и оставались детерминированными.
    """
    return b.Bridge(cfg, sink=sink, clock=clock, is_alive=liveness,
                    wall_clock=clock.wall, registry_probe=probe)


@pytest.fixture
def bridge(clock, liveness, sink):
    return make_bridge(b.Config(max_sessions=6), clock, liveness, sink)


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
    # при переполнении на экран идут САМЫЕ СВЕЖИЕ, а не самые старые: старые
    # брошенные сессии не должны вытеснять те, где сейчас работают
    cfg = b.Config(max_sessions=3)
    br = make_bridge(cfg, clock, liveness, sink)
    for i in range(6):
        clock.advance(1)
        br.handle_event({"session_id": f"s{i}", "event": "idle", "cwd": f"/p/{i}"})
    snap = br.build_snapshot()
    assert len(snap["sessions"]) == 3
    # выбраны свежие (s3..s5), а разложены по времени появления — карточки не прыгают
    assert [s["id"] for s in snap["sessions"]] == ["s3", "s4", "s5"]


def test_duplicate_names_get_session_suffix(clock, liveness, sink):
    # два claude в одном репо → одинаковый basename → добавляем суффикс session_id
    cfg = b.Config(max_sessions=6)
    br = make_bridge(cfg, clock, liveness, sink)
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
    br = make_bridge(cfg, clock, liveness, sink)
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


def test_idle_preserves_subagents(bridge):
    # Субагенты фоновые: Stop родителя (idle) приходит, пока они ещё работают.
    # Пузырьки должны пережить конец хода — декремент только по subagent_done.
    bridge.handle_event({"session_id": "a", "event": "working", "cwd": "/p/x"})
    bridge.handle_event({"session_id": "a", "event": "subagent"})
    bridge.handle_event({"session_id": "a", "event": "subagent"})
    assert bridge.handle_event({"session_id": "a", "event": "idle"}) is True
    assert bridge.sessions["a"].subagents == 2 and bridge.sessions["a"].state == b.IDLE


def test_background_subagent_survives_parent_stop(bridge):
    # Полный жизненный цикл фонового субагента: spawn → Stop родителя (серый,
    # пузырёк жив) → PostToolUse субагента (снова WORKING) → финальный SubagentStop.
    bridge.handle_event({"session_id": "a", "event": "working", "cwd": "/p/x"})
    bridge.handle_event({"session_id": "a", "event": "subagent"})
    bridge.handle_event({"session_id": "a", "event": "idle", "subs": 1})   # Stop родителя
    snap = {s["id"]: s for s in bridge.build_snapshot()["sessions"]}
    assert snap["a"]["state"] == b.IDLE and snap["a"]["sub"] == 1
    bridge.handle_event({"session_id": "a", "event": "working"})    # PostToolUse субагента
    assert bridge.sessions["a"].subagents == 1
    bridge.handle_event({"session_id": "a", "event": "subagent_done", "subs": 0})
    assert bridge.sessions["a"].subagents == 0
    assert "sub" not in {s["id"]: s for s in bridge.build_snapshot()["sessions"]}["a"]


def test_intermediate_subagent_stop_keeps_bubble(bridge):
    # SubagentStop срабатывает на каждую остановку субагента, в т.ч. промежуточную
    # (у него живые фоновые дети → в background_tasks он ещё running). Пузырёк
    # должен пережить такой «ложный финиш» благодаря абсолютному subs из конверта.
    bridge.handle_event({"session_id": "a", "event": "working", "cwd": "/p/x"})
    bridge.handle_event({"session_id": "a", "event": "subagent"})
    bridge.handle_event({"session_id": "a", "event": "subagent_done", "subs": 1})
    assert bridge.sessions["a"].subagents == 1
    bridge.handle_event({"session_id": "a", "event": "subagent_done", "subs": 0})
    assert bridge.sessions["a"].subagents == 0


def test_subagent_done_without_subs_falls_back_to_decrement(bridge):
    # Старая обёртка (конверт без background_tasks) → прежний декремент.
    bridge.handle_event({"session_id": "a", "event": "working", "cwd": "/p/x"})
    bridge.handle_event({"session_id": "a", "event": "subagent"})
    bridge.handle_event({"session_id": "a", "event": "subagent_done"})
    assert bridge.sessions["a"].subagents == 0


def test_idle_subs_sync_clears_leaked_counter(bridge):
    # Утёкший счётчик (потерянный SubagentStop) лечится синхронизацией на Stop.
    bridge.handle_event({"session_id": "a", "event": "working", "cwd": "/p/x"})
    bridge.handle_event({"session_id": "a", "event": "subagent"})
    bridge.handle_event({"session_id": "a", "event": "subagent"})
    assert bridge.handle_event({"session_id": "a", "event": "idle", "subs": 0}) is True
    assert bridge.sessions["a"].subagents == 0


def test_subs_ignores_garbage(bridge):
    bridge.handle_event({"session_id": "a", "event": "working", "cwd": "/p/x"})
    bridge.handle_event({"session_id": "a", "event": "subagent"})
    bridge.handle_event({"session_id": "a", "event": "idle", "subs": "мусор"})
    assert bridge.sessions["a"].subagents == 1   # некорректный subs — игнор, счётчик цел


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
    br = make_bridge(cfg, clock, FakeLiveness(), CollectingSink())
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
    server.bridge = make_bridge(cfg, FakeClock(), FakeLiveness(), CollectingSink())
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


# --- Отбор карточек: приоритет и брошенные сессии -----------------------------
def test_stale_idle_session_hidden(bridge, clock):
    bridge.handle_event({"session_id": "a", "event": "idle", "cwd": "/p/a"})
    clock.advance_hours(5)   # больше stale_idle_hours (дефолт 4)
    bridge.handle_event({"session_id": "b", "event": "idle", "cwd": "/p/b"})
    visible, hidden = bridge.select_visible()
    assert [s.session_id for s in visible] == ["b"]
    assert [s.session_id for s, _ in hidden] == ["a"]
    assert "idle 5.0ч" in hidden[0][1]


def test_short_stale_threshold_applies_only_to_idle(bridge, clock):
    # WAITING ждёт человека и по короткому порогу не скрывается
    for sid, event in (("w", "waiting"), ("e", "error"), ("k", "working"), ("i", "idle")):
        bridge.handle_event({"session_id": sid, "event": event, "cwd": f"/p/{sid}"})
    clock.advance_hours(10)     # > stale_idle_hours (4), но < stale_any_hours (24)
    _, hidden = bridge.select_visible()
    assert [s.session_id for s, _ in hidden] == ["i"]


def test_long_stale_threshold_applies_to_every_state(bridge, clock):
    # статус тоже протухает: «работает» сутки без событий — значит брошена
    for sid, event in (("w", "waiting"), ("e", "error"), ("k", "working")):
        bridge.handle_event({"session_id": sid, "event": event, "cwd": f"/p/{sid}"})
    clock.advance_hours(30)
    visible, hidden = bridge.select_visible()
    assert visible == []
    assert all("тишина 30.0ч" in reason for _, reason in hidden)


def test_stale_any_threshold_disabled_by_zero(clock, liveness, sink):
    br = make_bridge(b.Config(max_sessions=6, stale_any_hours=0), clock, liveness, sink)
    br.handle_event({"session_id": "k", "event": "working", "cwd": "/p/k"})
    clock.advance_hours(500)
    visible, hidden = br.select_visible()
    assert [s.session_id for s in visible] == ["k"] and hidden == []


def test_short_stale_threshold_disabled_by_zero(clock, liveness, sink):
    # нулём отключается только короткий порог; длинный (24ч) продолжает работать
    br = make_bridge(b.Config(max_sessions=6, stale_idle_hours=0), clock, liveness, sink)
    br.handle_event({"session_id": "a", "event": "idle", "cwd": "/p/a"})
    clock.advance_hours(10)
    visible, hidden = br.select_visible()
    assert [s.session_id for s in visible] == ["a"] and hidden == []
    clock.advance_hours(20)
    _, hidden = br.select_visible()
    assert [s.session_id for s, _ in hidden] == ["a"]


def test_session_without_activity_stamp_not_penalised(bridge, clock):
    # сессии из mock_loop не имеют last_active_ms — их нельзя считать брошенными
    bridge.handle_event({"session_id": "a", "event": "idle", "cwd": "/p/a"})
    bridge.sessions["a"].last_active_ms = 0.0
    clock.advance_hours(99)
    assert bridge.stale_reason(bridge.sessions["a"], clock.wall() * 1000) is None


def test_working_wins_slots_over_idle(clock, liveness, sink):
    br = make_bridge(b.Config(max_sessions=2), clock, liveness, sink)
    for sid in ("i1", "i2", "i3"):
        clock.advance(1)
        br.handle_event({"session_id": sid, "event": "idle", "cwd": f"/p/{sid}"})
    clock.advance(1)
    br.handle_event({"session_id": "busy", "event": "working", "cwd": "/p/busy"})
    clock.advance(1)
    br.handle_event({"session_id": "asks", "event": "waiting", "cwd": "/p/asks"})
    # WORKING важнее WAITING, WAITING важнее IDLE
    assert [s["id"] for s in br.build_snapshot()["sessions"]] == ["busy", "asks"]


def test_stale_session_hidden_even_with_free_slots(clock, liveness, sink):
    # слот остаётся пустым: «здесь никто не работает» — это правда, а карточка
    # трёхдневной давности только мешает читать экран
    br = make_bridge(b.Config(max_sessions=6), clock, liveness, sink)
    br.handle_event({"session_id": "old", "event": "idle", "cwd": "/p/old"})
    clock.advance_hours(50)
    br.handle_event({"session_id": "new", "event": "working", "cwd": "/p/new"})
    visible, hidden = br.select_visible()
    assert [s.session_id for s in visible] == ["new"]
    assert [s.session_id for s, _ in hidden] == ["old"]


def test_hidden_sessions_logged_once(bridge, clock, caplog):
    import logging
    br = bridge
    br.handle_event({"session_id": "a", "event": "idle", "cwd": "/p/a"})
    clock.advance_hours(9)
    br.handle_event({"session_id": "b", "event": "working", "cwd": "/p/b"})
    with caplog.at_level(logging.INFO, logger="octo.bridge"):
        br.build_snapshot()
        br.build_snapshot()   # heartbeat строит снэпшот раз в 5с — лог не должен плыть
    assert sum("не на экране" in r.message for r in caplog.records) == 1


def test_order_prefers_registry_started_ms(bridge, clock):
    # порядок на экране — по времени старта сессии, даже если мост узнал о ней позже
    bridge.handle_event({"session_id": "late-but-old", "event": "working", "cwd": "/p/a"})
    clock.advance(5)
    bridge.handle_event({"session_id": "early", "event": "working", "cwd": "/p/b"})
    bridge.sessions["late-but-old"].started_ms = 1.0          # родилась давно
    bridge.sessions["early"].started_ms = clock.wall() * 1000  # родилась только что
    assert ids(bridge) == ["late-but-old", "early"]


# --- Чтение реестра сессий Claude Code ----------------------------------------
def registry_fs(files):
    """Фейковая ФС для read_session_registry: {путь: dict|None}."""
    import fnmatch

    def lister(pattern):
        return [p for p in files if fnmatch.fnmatch(p, pattern)]

    def reader(path):
        return files.get(path)

    return {"lister": lister, "reader": reader}


def test_read_registry_interactive_sessions():
    fs = registry_fs({
        "/r/sessions/100.json": {
            "pid": 100, "sessionId": "sid-a", "cwd": "/work/proj", "kind": "interactive",
            "name": "proj-a1", "status": "busy", "statusUpdatedAt": 1700, "startedAt": 1000,
            "procStart": "777",
        },
    })
    entries = b.read_session_registry("/r", **fs)
    assert entries == [{
        "session_id": "sid-a", "pid": 100, "cwd": "/work/proj", "kind": "interactive",
        "name": "proj-a1", "status": "busy", "status_ms": 1700.0, "started_ms": 1000.0,
        "proc_start": "777",
    }]


def test_read_registry_skips_broken_and_nameless():
    fs = registry_fs({
        "/r/sessions/1.json": None,                    # битый JSON
        "/r/sessions/2.json": {"pid": 2},              # нет sessionId
        "/r/sessions/3.json": {"sessionId": "ok"},
    })
    entries = b.read_session_registry("/r", **fs)
    assert [e["session_id"] for e in entries] == ["ok"]


def test_read_registry_roster_supplements_not_overwrites():
    # у фоновой сессии status есть только в sessions/, а живой PID воркера — в roster
    fs = registry_fs({
        "/r/sessions/900.json": {"pid": None, "sessionId": "bg", "status": "idle",
                                 "statusUpdatedAt": 500, "kind": "interactive"},
        "/r/daemon/roster.json": {"workers": {"bg8": {
            "sessionId": "bg", "pid": 900, "procStart": "42", "startedAt": 111,
            "cwd": "/work/bg"}}},
    })
    entry, = b.read_session_registry("/r", **fs)
    assert entry["pid"] == 900 and entry["proc_start"] == "42"
    assert entry["status"] == "idle" and entry["kind"] == "background"
    assert entry["started_ms"] == 111.0


def test_read_registry_roster_only_worker():
    fs = registry_fs({"/r/daemon/roster.json": {"updatedAt": 9, "workers": {
        "w": {"sessionId": "solo", "pid": 5, "cwd": "/w"},
        "bad": {"pid": 6},                     # без sessionId — пропуск
        "junk": "not a dict",
    }}})
    entries = b.read_session_registry("/r", **fs)
    assert [e["session_id"] for e in entries] == ["solo"]
    assert entries[0]["kind"] == "background" and entries[0]["status_ms"] == 9.0


def test_read_registry_jobs_only_enrich_known_sessions():
    # в jobs/ лежат и завершённые (state=done), и отложенные задания без процесса —
    # самостоятельным источником сессий они быть не могут
    fs = registry_fs({
        "/r/sessions/7.json": {"pid": 7, "sessionId": "live", "status": "idle"},
        "/r/jobs/live/state.json": {"sessionId": "live", "state": "blocked", "name": "фон"},
        "/r/jobs/gone/state.json": {"sessionId": "finished", "state": "done", "name": "старьё"},
        "/r/jobs/empty/state.json": None,
    })
    entries = b.read_session_registry("/r", **fs)
    assert [e["session_id"] for e in entries] == ["live"]
    assert entries[0]["status"] == "blocked" and entries[0]["name"] == "фон"


def test_read_registry_absent_returns_none():
    # ни одного источника → None («реестра нет»), а не пустой список
    assert b.read_session_registry("/r", **registry_fs({})) is None
    assert b.read_session_registry("/r", **registry_fs({"/r/jobs/x/state.json": {"sessionId": "z"}})) is None


def test_read_registry_default_root_and_readers(tmp_path, monkeypatch):
    (tmp_path / ".claude" / "sessions").mkdir(parents=True)
    (tmp_path / ".claude" / "sessions" / "5.json").write_text(
        json.dumps({"pid": 5, "sessionId": "real", "status": "idle"}), encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    entries = b.read_session_registry()
    assert [e["session_id"] for e in entries] == ["real"]


def test_read_json_bad_file(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert b._read_json(str(bad)) is None
    assert b._read_json(str(tmp_path / "missing.json")) is None
    lst = tmp_path / "list.json"
    lst.write_text("[1,2]", encoding="utf-8")
    assert b._read_json(str(lst)) is None   # не dict — не наш формат


# --- Живость записи реестра ---------------------------------------------------
def test_registry_entry_alive_variants(monkeypatch):
    alive = FakeLiveness([10])
    assert b.registry_entry_alive({"pid": None}, alive) is True          # фоновая без PID
    assert b.registry_entry_alive({"pid": 11}, alive) is False           # PID мёртв
    assert b.registry_entry_alive({"pid": 10, "proc_start": None}, alive) is True
    monkeypatch.setattr(b, "proc_start_ticks", lambda pid: "999")
    assert b.registry_entry_alive({"pid": 10, "proc_start": "999"}, alive) is True
    # PID переиспользован другим процессом — файл реестра остался с прошлого
    assert b.registry_entry_alive({"pid": 10, "proc_start": "111"}, alive) is False
    monkeypatch.setattr(b, "proc_start_ticks", lambda pid: None)
    assert b.registry_entry_alive({"pid": 10, "proc_start": "111"}, alive) is True  # не Linux


def test_proc_start_ticks_real():
    mine = b.proc_start_ticks(os.getpid())
    assert mine is None or mine.isdigit()      # None вне Linux
    assert b.proc_start_ticks(999_999_999) is None


# --- Сверка состава с реестром ------------------------------------------------
def entry(sid, **kw):
    return b._entry(sid, **kw)


def test_reconcile_none_is_noop(bridge):
    bridge.handle_event({"session_id": "a", "event": "idle", "cwd": "/p/a"})
    assert bridge.reconcile(None) is False          # probe вернул None — реестра нет
    assert "a" in bridge.sessions
    assert bridge._registry_ok is False


def test_reconcile_empty_list_does_not_delete(bridge, clock):
    bridge.handle_event({"session_id": "a", "event": "idle", "cwd": "/p/a"})
    clock.advance(60)
    assert bridge.reconcile([]) is False
    assert "a" in bridge.sessions


def test_reconcile_removes_session_absent_from_registry(bridge, clock):
    bridge.handle_event({"session_id": "ghost", "event": "idle", "cwd": "/p/x"})  # pid=None
    clock.advance(60)
    assert bridge.reconcile([entry("other", pid=1)]) is True
    assert "ghost" not in bridge.sessions


def test_reconcile_removes_ghost_whose_pid_belongs_to_another_session(bridge, clock, liveness):
    # /clear или resume в живом процессе: session_id сменился, PID остался живым
    liveness.alive.add(500)
    bridge.handle_event({"session_id": "old-id", "event": "working", "cwd": "/p/x", "pid": 500})
    clock.advance(60)
    changed = bridge.reconcile([entry("new-id", pid=500, cwd="/p/x")])
    assert changed is True
    assert "old-id" not in bridge.sessions and "new-id" in bridge.sessions


def test_reconcile_grace_protects_young_session(bridge, clock):
    bridge.handle_event({"session_id": "newborn", "event": "working", "cwd": "/p/x"})
    clock.advance(5)          # меньше reconcile_grace_sec (30с) — реестр мог отстать
    assert bridge.reconcile([entry("other", pid=1)]) is False
    assert "newborn" in bridge.sessions


def test_reconcile_keeps_session_with_live_pid_unknown_to_registry(bridge, clock, liveness):
    # реестр не знает этот PID (старая версия CLI / другой запуск) — не наше дело удалять
    liveness.alive.add(700)
    bridge.handle_event({"session_id": "mine", "event": "working", "cwd": "/p/x", "pid": 700})
    clock.advance(60)
    assert bridge.reconcile([entry("other", pid=800)]) is False
    assert "mine" in bridge.sessions


def test_reconcile_adds_unknown_sessions(bridge):
    changed = bridge.reconcile([
        entry("bg", pid=None, cwd="/work/proj", kind="background", status="blocked",
              name="фоновая", status_ms=5000.0, started_ms=4000.0),
    ])
    assert changed is True
    s = bridge.sessions["bg"]
    assert s.state == b.WAITING and s.name == "proj" and s.source == "registry"
    assert s.reg_name == "фоновая" and s.started_ms == 4000.0 and s.last_active_ms == 5000.0


def test_reconcile_unknown_status_defaults_to_idle(bridge):
    bridge.reconcile([entry("x", pid=None, cwd="/w/p", status="взлетает")])
    assert bridge.sessions["x"].state == b.IDLE


def test_reconcile_fills_missing_pid_and_started(bridge, clock, liveness):
    bridge.handle_event({"session_id": "a", "event": "working", "cwd": "/p/a"})  # pid=None
    liveness.alive.add(321)
    bridge.reconcile([entry("a", pid=321, started_ms=1234.0, name="имя")])
    s = bridge.sessions["a"]
    assert s.pid == 321 and s.started_ms == 1234.0 and s.reg_name == "имя"


def test_reconcile_status_applied_only_when_registry_is_fresher(bridge, clock):
    bridge.handle_event({"session_id": "a", "event": "working", "cwd": "/p/a"})
    stale_ms = clock.wall() * 1000 - 10_000
    assert bridge.reconcile([entry("a", pid=None, status="idle", status_ms=stale_ms)]) is False
    assert bridge.sessions["a"].state == b.WORKING      # хук свежее — он и прав
    fresh_ms = clock.wall() * 1000 + 10_000
    assert bridge.reconcile([entry("a", pid=None, status="idle", status_ms=fresh_ms)]) is True
    assert bridge.sessions["a"].state == b.IDLE         # реестр свежее — верим ему


def test_reconcile_uses_probe_when_no_argument(clock, liveness, sink):
    calls = []

    def probe():
        calls.append(1)
        return [entry("from-probe", pid=None, cwd="/w/p")]

    br = make_bridge(b.Config(max_sessions=6), clock, liveness, sink, probe=probe)
    assert br.reconcile() is True
    assert calls == [1] and "from-probe" in br.sessions
    assert br._registry_ok is True and br._registry_n == 1


def test_reconcile_ignores_dead_registry_entries(bridge, liveness):
    # запись осталась после kill -9 — PID мёртв, сессию не поднимаем
    assert bridge.reconcile([entry("zombie", pid=4242, cwd="/w/p")]) is False
    assert bridge.sessions == {}


# --- Управление: reset и drop -------------------------------------------------
def test_reset_forgets_all_and_rebuilds_from_registry(clock, liveness, sink):
    br = make_bridge(b.Config(max_sessions=6), clock, liveness, sink,
                     probe=lambda: [entry("live", pid=None, cwd="/w/live")])
    br.handle_event({"session_id": "junk", "event": "idle", "cwd": "/p/junk"})
    assert br.reset() == 1
    assert "junk" not in br.sessions and "live" in br.sessions


def test_mute_hides_card_until_next_activity(bridge, clock):
    # удалять нельзя: сверка вернула бы сессию через OCTO_REGISTRY_SEC и кнопка
    # выглядела бы сломанной — поэтому «скрыть», а не «удалить»
    bridge.handle_event({"session_id": "a", "event": "working", "cwd": "/p/a"})
    assert bridge.mute("a") is True
    assert bridge.mute("nope") is False
    assert "a" in bridge.sessions                       # осталась в состоянии
    visible, hidden = bridge.select_visible()
    assert visible == [] and hidden[0][1] == "скрыта вручную"

    clock.advance(60)
    bridge.handle_event({"session_id": "a", "event": "working"})   # проявила жизнь
    visible, hidden = bridge.select_visible()
    assert [s.session_id for s in visible] == ["a"] and hidden == []


def test_mute_survives_reconcile(bridge, clock):
    bridge.handle_event({"session_id": "a", "event": "working", "cwd": "/p/a"})
    bridge.mute("a")
    stale_ms = clock.wall() * 1000 - 5000
    bridge.reconcile([b._entry("a", pid=None, status="busy", status_ms=stale_ms)])
    assert bridge.select_visible()[0] == []             # сверка не «расскрывает»


def test_reset_clears_mute(bridge):
    bridge.handle_event({"session_id": "a", "event": "working", "cwd": "/p/a"})
    bridge.mute("a")
    bridge.reset()
    assert bridge.sessions == {}


# --- Дамп состояния (/debug) --------------------------------------------------
def test_build_debug_shape(bridge, clock, liveness):
    liveness.alive.add(11)
    bridge.handle_event({"session_id": "work", "event": "working", "cwd": "/p/w", "pid": 11})
    bridge.handle_event({"session_id": "dead", "event": "idle", "cwd": "/p/d", "pid": 12})
    clock.advance_hours(9)
    bridge.handle_event({"session_id": "fresh", "event": "waiting", "cwd": "/p/f"})
    bridge.push("test")
    dbg = bridge.build_debug()

    assert dbg["bridge"]["sessions_total"] == 3
    assert dbg["bridge"]["push_n"] == 1
    assert dbg["config"]["max_sessions"] == 6
    assert dbg["snapshot"]["v"] == 1
    by_id = {s["id"]: s for s in dbg["sessions"]}
    assert by_id["work"]["pid_alive"] is True
    assert by_id["dead"]["pid_alive"] is False
    assert by_id["fresh"]["pid_alive"] is None          # PID неизвестен
    assert by_id["fresh"]["state_name"] == "WAITING"
    assert by_id["dead"]["visible"] is False
    assert "idle 9.0ч" in by_id["dead"]["hidden_reason"]
    assert by_id["work"]["visible"] is True and by_id["work"]["hidden_reason"] is None


def test_build_debug_reports_sink_status(bridge):
    class SinkWithStatus(CollectingSink):
        def status(self):
            return {"kind": "serial", "connected": True}

    bridge.sink = SinkWithStatus()
    assert bridge.build_debug()["bridge"]["sink"] == {"kind": "serial", "connected": True}
    bridge.sink = CollectingSink()          # без status() — просто имя класса
    assert bridge.build_debug()["bridge"]["sink"] == "CollectingSink"


def test_sink_status_methods(fake_serial):
    sink = b.SerialSink("COM3", 115200)
    assert sink.status() == {"kind": "serial", "port": "COM3", "resolved": None,
                             "connected": False, "opens": 0}
    sink.send("x\n")
    st = sink.status()
    assert st["connected"] is True and st["opens"] == 1
    assert b.LoggingSink().status() == {"kind": "logging", "connected": False}


# --- Логи: файл с ротацией по умолчанию ---------------------------------------
def test_default_log_path_respects_xdg(monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", "/tmp/state")
    assert b.default_log_path() == "/tmp/state/octodash/bridge.log"


def test_setup_log_handlers_creates_file(tmp_path):
    path = tmp_path / "logs" / "bridge.log"
    handlers, used = b.setup_log_handlers(b.Config(log_file=str(path)))
    try:
        assert used == str(path) and path.parent.is_dir()
        assert len(handlers) == 2
    finally:
        for h in handlers:
            h.close()


def test_setup_log_handlers_can_be_disabled():
    handlers, used = b.setup_log_handlers(b.Config(log_file="-"))
    assert used == "" and len(handlers) == 1


def test_setup_log_handlers_survives_unwritable_path(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    handlers, used = b.setup_log_handlers(b.Config(log_file=str(blocker / "sub" / "log")))
    assert used == "" and len(handlers) == 1      # не смогли — работаем без файла


# --- HTTP: наблюдаемость и управление -----------------------------------------
def _serve_with(probe):
    """HTTP-сервер на эфемерном порту с управляемым реестром."""
    cfg = b.Config(port=0)
    server = b.bind_singleton(cfg)
    assert server is not None
    server.bridge = make_bridge(cfg, FakeClock(), FakeLiveness(), CollectingSink(), probe=probe)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, port


def _delete(port, path):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method="DELETE")
    return urllib.request.urlopen(req, timeout=2)


def test_http_debug_exposes_full_state():
    server, port = _serve_with(lambda: None)
    try:
        _post(port, "/event", json.dumps(
            {"session_id": "sid-1", "event": "working", "cwd": "/p/proj", "pid": 4242}).encode())
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/debug", timeout=2) as r:
            dbg = json.loads(r.read())
        assert dbg["bridge"]["sessions_total"] == 1
        assert dbg["sessions"][0]["id"] == "sid-1"
        assert dbg["sessions"][0]["pid"] == 4242
        assert dbg["config"]["max_sessions"] == 6
        assert dbg["snapshot"]["sessions"][0]["name"] == "proj"
    finally:
        server.shutdown()
        server.server_close()


def test_http_resync_runs_reconcile():
    server, port = _serve_with(lambda: [b._entry("from-registry", pid=None, cwd="/w/reg")])
    try:
        with _post(port, "/resync", b"") as r:
            body = json.loads(r.read())
        assert body["ok"] is True and body["changed"] is True
        assert "from-registry" in server.bridge.sessions
    finally:
        server.shutdown()
        server.server_close()


def test_http_reset_forgets_and_rebuilds():
    server, port = _serve_with(lambda: [b._entry("kept", pid=None, cwd="/w/kept")])
    try:
        _post(port, "/event", json.dumps(
            {"session_id": "junk", "event": "idle", "cwd": "/p/junk"}).encode())
        with _post(port, "/reset", b"") as r:
            body = json.loads(r.read())
        assert body["forgotten"] == 1
        assert "junk" not in server.bridge.sessions and "kept" in server.bridge.sessions
    finally:
        server.shutdown()
        server.server_close()


def test_http_delete_session():
    server, port = _serve_with(lambda: None)
    try:
        _post(port, "/event", json.dumps(
            {"session_id": "drop-me", "event": "idle", "cwd": "/p/x"}).encode())
        with _delete(port, "/sessions/drop-me") as r:
            assert json.loads(r.read())["hidden"] == "drop-me"
        assert server.bridge.build_snapshot()["sessions"] == []   # ушла с экрана
        with pytest.raises(urllib.error.HTTPError) as unknown:
            _delete(port, "/sessions/never-existed")
        assert unknown.value.code == 404
        with pytest.raises(urllib.error.HTTPError) as noid:
            _delete(port, "/sessions/")
        assert noid.value.code == 404
    finally:
        server.shutdown()
        server.server_close()


def test_bind_singleton_allows_reuse_on_posix_only(monkeypatch):
    # SO_REUSEADDR нужен, чтобы перезапуск не упирался в TIME_WAIT, но на Windows
    # он позволил бы второму инстансу забиндиться поверх первого
    monkeypatch.setattr(b.os, "name", "posix")
    server = b.bind_singleton(b.Config(port=0))
    try:
        assert b.ThreadingHTTPServer.allow_reuse_address is True
    finally:
        server.server_close()
    monkeypatch.setattr(b.os, "name", "nt")
    server = b.bind_singleton(b.Config(port=0))
    try:
        assert b.ThreadingHTTPServer.allow_reuse_address is False
    finally:
        server.server_close()


# --- Веб-морда ----------------------------------------------------------------
@pytest.mark.parametrize("accept,expected", [
    ("text/html,application/xhtml+xml", True),      # браузер
    ("*/*", False),                                  # curl
    ("application/json", False),
    (None, False),
])
def test_wants_html(accept, expected):
    assert b.wants_html(accept) is expected


def test_ui_page_is_self_contained():
    # ни одного внешнего ресурса: мост не должен зависеть от сети, чтобы показать себя
    assert "src=" not in b.UI_HTML and "href=" not in b.UI_HTML
    assert "http://" not in b.UI_HTML and "https://" not in b.UI_HTML
    assert 'fetch("/debug"' in b.UI_HTML          # данные берёт из того же /debug
    assert "/resync" in b.UI_HTML and "/reset" in b.UI_HTML and "/sessions/" in b.UI_HTML


def test_http_ui_served_as_html():
    server, port = _serve_with(lambda: None)
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/ui", timeout=2) as r:
            assert r.headers["Content-Type"] == "text/html; charset=utf-8"
            assert b"OctoDash" in r.read()
    finally:
        server.shutdown()
        server.server_close()


def test_http_root_negotiates_html_or_json():
    server, port = _serve_with(lambda: None)
    try:
        browser = urllib.request.Request(f"http://127.0.0.1:{port}/",
                                         headers={"Accept": "text/html"})
        with urllib.request.urlopen(browser, timeout=2) as r:
            assert b"<!doctype html>" in r.read()[:40]
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2) as r:
            assert json.loads(r.read())["v"] == 1      # curl получает снэпшот, как раньше
    finally:
        server.shutdown()
        server.server_close()


def test_http_favicon_is_no_content():
    # 204 вместо 404: консоль браузера должна быть чистой, чтобы реальная
    # ошибка в ней сразу бросалась в глаза
    server, port = _serve_with(lambda: None)
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/favicon.ico", timeout=2) as r:
            assert r.status == 204 and r.read() == b""
    finally:
        server.shutdown()
        server.server_close()
