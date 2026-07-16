#!/usr/bin/env python3
"""OctoDash bridge.

Единственный долгоживущий процесс. Держит правду о состоянии всех сессий Claude
Code, принимает события от хуков по HTTP и шлёт агрегированный снэпшот на ESP по
USB serial. Подробности контракта — в CLAUDE.md.

Запуск:
    python bridge.py

Mock-режим (6 фейковых сессий, без реальных хуков):
    OCTO_MOCK=1 python bridge.py

Архитектура кода (ради тестируемости):
    * Чистое ядро (`Bridge`, `build_snapshot`, `handle_event`, `reap`) не знает ни
      о потоках, ни о serial, ни о реальном времени — время, проверка PID и «сток»
      снэпшотов внедряются через параметры конструктора.
    * IO/потоки (`SerialSink`, `*_loop`, HTTP-сервер, `main`) — тонкая обёртка вокруг
      ядра. Тесты дёргают ядро синхронно, без железа и без sleep.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import sys
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Protocol

# --- Опциональные зависимости -------------------------------------------------
try:
    import serial  # pyserial
    from serial.tools import list_ports as _list_ports
except ImportError:  # pragma: no cover - serial необязателен для приёма хуков
    serial = None
    _list_ports = None

try:
    import psutil
except ImportError:  # pragma: no cover - есть ctypes/os.kill фолбэк
    psutil = None


# --- Коды состояний (совпадают с enum прошивки) -------------------------------
WORKING, WAITING, IDLE, ERROR = 0, 1, 2, 3

# event хука -> код состояния
EVENT_TO_STATE = {
    "working": WORKING,
    "waiting": WAITING,
    "idle": IDLE,
    "error": ERROR,
}


# --- Конфигурация -------------------------------------------------------------
def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _env_bool(name: str) -> bool:
    return _env(name, "0") not in ("0", "", "false", "False")


@dataclass
class Config:
    host: str = field(default_factory=lambda: _env("OCTO_BRIDGE_HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: int(_env("OCTO_BRIDGE_PORT", "8787")))
    # "auto" → определить порт по USB VID:PID (см. autodetect_port).
    # Явное значение (COM3, /dev/ttyUSB0) отключает автоопределение.
    serial_port: str = field(default_factory=lambda: _env("OCTO_SERIAL_PORT", "auto"))
    serial_baud: int = field(default_factory=lambda: int(_env("OCTO_SERIAL_BAUD", "115200")))
    heartbeat_sec: float = field(default_factory=lambda: float(_env("OCTO_HEARTBEAT_SEC", "5")))
    reaper_sec: float = field(default_factory=lambda: float(_env("OCTO_REAPER_SEC", "2")))
    debounce_ms: int = field(default_factory=lambda: int(_env("OCTO_DEBOUNCE_MS", "100")))
    max_sessions: int = field(default_factory=lambda: int(_env("OCTO_MAX_SESSIONS", "6")))
    name_max: int = field(default_factory=lambda: int(_env("OCTO_NAME_MAX", "16")))
    mock: bool = field(default_factory=lambda: _env_bool("OCTO_MOCK"))


# --- Модель сессии ------------------------------------------------------------
@dataclass
class Session:
    session_id: str
    name: str
    state: int
    pid: int | None
    first_seen: float
    last_event: float


def basename_of(cwd: str) -> str:
    """basename(cwd) — работает и для posix, и для windows путей."""
    cleaned = (cwd or "").replace("\\", "/").rstrip("/")
    base = cleaned.rsplit("/", 1)[-1] if cleaned else ""
    return base or "session"


# Классический шрифт Adafruit GFX — CP437, без Unicode. Кириллицу он не умеет
# (каждый UTF-8 байт → чужой глиф), поэтому имена транслитерируем в ASCII на мосту.
_RU_MAP = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}
_TRANSLIT = dict(_RU_MAP)
for _k, _v in _RU_MAP.items():
    _TRANSLIT[_k.upper()] = _v.capitalize()  # 'ж'->'zh' => 'Ж'->'Zh'; '' остаётся ''


def transliterate(s: str) -> str:
    """RU → латиница. ASCII сохраняется как есть, прочий не-ASCII отбрасывается."""
    out = []
    for ch in s:
        if ch in _TRANSLIT:
            out.append(_TRANSLIT[ch])
        elif ord(ch) < 128:
            out.append(ch)
        # иначе символ невыразим классическим шрифтом → выкидываем
    return "".join(out)


def shorten_middle(s: str, n: int) -> str:
    """Обрезка по середине с маркером '~': сохраняет и голову, и хвост.

    Для worktree с общим префиксом это единственный способ оставить карточки
    различимыми (обрезка с головы схлопнула бы их в одинаковые огрызки).
    """
    if n <= 0:
        return ""
    if len(s) <= n:
        return s
    if n <= 3:  # слишком мало для маркера — просто режем
        return s[:n]
    keep = n - 1  # один символ на маркер '~'
    head = (keep + 1) // 2
    tail = keep - head
    return s[:head] + "~" + (s[-tail:] if tail else "")


def display_name(cwd: str, max_len: int = 16) -> str:
    """Готовое к выводу на ESP имя карточки: basename → транслит → обрезка по центру."""
    return shorten_middle(transliterate(basename_of(cwd)), max_len)


def coerce_pid(value: object) -> int | None:
    """Приводит присланный pid к int|None, не роняясь на мусоре."""
    if value is None:
        return None
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


# --- Проверка живости PID -----------------------------------------------------
def pid_alive(pid: int | None) -> bool:
    """True, если процесс жив (или pid неизвестен — тогда не трогаем сессию).

    ВАЖНО: на Windows нельзя использовать os.kill(pid, 0) — там любой сигнал,
    кроме CTRL_*, приводит к TerminateProcess, т.е. os.kill(pid, 0) убил бы
    процесс. Поэтому используем psutil, а фолбэк — ctypes OpenProcess.
    """
    if pid is None:
        return True
    if psutil is not None:
        return psutil.pid_exists(pid)
    if os.name == "nt":
        return _win_pid_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # существует, но не наш — считаем живым
    return True


def _win_pid_alive(pid: int) -> bool:  # pragma: no cover - только Windows-фолбэк
    import ctypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return code.value == STILL_ACTIVE
        return True
    finally:
        kernel32.CloseHandle(handle)


# --- Автоопределение serial-порта --------------------------------------------
# USB VID:PID распространённых UART-мостов на ESP-платах. WeMos D1 mini обычно CH340.
KNOWN_USB_IDS = {
    (0x1A86, 0x7523): "CH340",
    (0x1A86, 0x55D4): "CH9102/CH343",
    (0x10C4, 0xEA60): "CP210x",
    (0x0403, 0x6001): "FTDI FT232",
    (0x239A, None):   "Adafruit",
}


def autodetect_port(list_ports_fn: Callable[[], list] | None = None) -> str | None:
    """Ищет serial-порт ESP по USB VID:PID. Фолбэк — единственный доступный порт.

    `list_ports_fn` возвращает объекты с полями .device/.vid/.pid/.description
    (по умолчанию serial.tools.list_ports.comports). Вынесено параметром ради тестов.
    """
    if list_ports_fn is None:
        if _list_ports is None:
            return None
        list_ports_fn = _list_ports.comports
    ports = list(list_ports_fn())
    if not ports:
        return None
    for p in ports:
        if (p.vid, p.pid) in KNOWN_USB_IDS or (p.vid, None) in KNOWN_USB_IDS:
            return p.device
    if len(ports) == 1:
        return ports[0].device
    return None  # неоднозначно — пусть решает OCTO_SERIAL_PORT


# --- Сток снэпшотов (куда мост отдаёт строки) ---------------------------------
class Sink(Protocol):
    """Абстракция получателя снэпшота. В проде — serial, в тестах — сборщик строк."""

    def send(self, line: str) -> bool:  # returns True если доставлено
        ...


class LoggingSink:
    """Дефолтный сток, когда serial недоступен/не нужен: просто логирует строку."""

    def __init__(self, logger: logging.Logger | None = None):
        self.log = logger or logging.getLogger("octo.sink")

    def send(self, line: str) -> bool:
        self.log.debug("snapshot (serial недоступен): %s", line.strip())
        return False


class SerialSink:
    """Пишет строки в serial с ленивым открытием и автопереоткрытием при ошибке."""

    def __init__(
        self,
        port: str,
        baud: int,
        logger: logging.Logger | None = None,
        list_ports_fn: Callable[[], list] | None = None,
    ):
        self.port = port
        self.baud = baud
        self.log = logger or logging.getLogger("octo.serial")
        self._list_ports_fn = list_ports_fn
        self._ser = None
        self._last_warn = 0.0

    def _auto(self) -> bool:
        return self.port in ("", "auto", None)

    def _resolve_port(self) -> str | None:
        if not self._auto():
            return self.port
        return autodetect_port(self._list_ports_fn)  # каждый раз заново → переподключение подхватится

    def _warn_throttled(self, msg: str, *args) -> None:
        now = time.monotonic()
        if now - self._last_warn > 5:
            self.log.warning(msg, *args)
            self._last_warn = now

    def _ensure(self):
        if serial is None:
            return None
        if self._ser is not None and self._ser.is_open:
            return self._ser
        port = self._resolve_port()
        if port is None:
            self._warn_throttled("serial не найден (автоопределение): ESP не воткнут?")
            return None
        try:
            self._ser = serial.Serial(port, self.baud, timeout=1, write_timeout=1)
            self.log.info("serial открыт: %s @ %d%s", port, self.baud, " (auto)" if self._auto() else "")
            return self._ser
        except Exception as exc:  # порт недоступен — не падаем
            self._warn_throttled("не удалось открыть serial %s: %s", port, exc)
            self._ser = None
            return None

    def send(self, line: str) -> bool:
        ser = self._ensure()
        if ser is None:
            self.log.debug("snapshot (serial недоступен): %s", line.strip())
            return False
        try:
            ser.write(line.encode("utf-8"))
            ser.flush()
            self.log.debug("→ ESP: %s", line.strip())
            return True
        except Exception as exc:
            self.log.warning("ошибка записи в serial: %s — переоткрою", exc)
            self.close()
            return False

    def close(self) -> None:
        if self._ser is not None:
            try:
                self._ser.close()
            except Exception:
                pass
        self._ser = None


# --- Мост (чистое ядро) -------------------------------------------------------
class Bridge:
    def __init__(
        self,
        cfg: Config,
        *,
        sink: Sink | None = None,
        clock: Callable[[], float] = time.monotonic,
        is_alive: Callable[[int | None], bool] = pid_alive,
        logger: logging.Logger | None = None,
    ):
        self.cfg = cfg
        self.log = logger or logging.getLogger("octo.bridge")
        self._clock = clock
        self._is_alive = is_alive
        self.sink: Sink = sink or LoggingSink(self.log)

        self.lock = threading.Lock()
        self.sessions: dict[str, Session] = {}

        self._dirty = threading.Event()
        self._stop = threading.Event()

    # -- обработка события от хука; возвращает True, если снэпшот стал грязным --
    def handle_event(self, data: dict) -> bool:
        event = str(data.get("event", "")).lower()
        session_id = data.get("session_id")
        if not session_id:
            self.log.warning("событие без session_id: %r", data)
            return False

        now = self._clock()
        cwd = data.get("cwd", "")
        name = self._name(cwd)
        pid = coerce_pid(data.get("pid"))

        with self.lock:
            sess = self.sessions.get(session_id)

            if event == "end":
                if self.sessions.pop(session_id, None) is not None:
                    self.log.info("end: удалена сессия %s", session_id)
                    return self._dirty_set()
                return False

            if event == "start":
                if sess is None:
                    self.sessions[session_id] = Session(session_id, name, IDLE, pid, now, now)
                    self.log.info("start: %s (%s) pid=%s", session_id, name, pid)
                else:
                    sess.state = IDLE
                    sess.last_event = now
                    if pid is not None:
                        sess.pid = pid
                    if cwd:
                        sess.name = name
                return self._dirty_set()

            new_state = EVENT_TO_STATE.get(event)
            if new_state is None:
                self.log.warning("неизвестный event %r для %s — игнор", event, session_id)
                return False

            if sess is None:
                # событие для незнакомой сессии — создаём на лету (мост мог рестартнуть)
                self.sessions[session_id] = Session(session_id, name, new_state, pid, now, now)
                self.log.info("создана на лету: %s (%s) state=%d", session_id, name, new_state)
                return self._dirty_set()

            sess.last_event = now
            if cwd:
                sess.name = name
            if pid is not None:
                sess.pid = pid
            if sess.state != new_state:
                sess.state = new_state
                return self._dirty_set()
            return False

    def _name(self, cwd: str) -> str:
        return display_name(cwd, self.cfg.name_max)

    def _dirty_set(self) -> bool:
        self._dirty.set()
        return True

    def mark_dirty(self) -> None:
        self._dirty.set()

    # -- удаление сессий с мёртвым PID; возвращает список удалённых id --
    def reap(self) -> list[str]:
        removed: list[str] = []
        with self.lock:
            for sid, sess in list(self.sessions.items()):
                if sess.pid is not None and not self._is_alive(sess.pid):
                    del self.sessions[sid]
                    removed.append(sid)
                    self.log.info("reaper: сессия %s (%s) — процесс мёртв", sid, sess.name)
        if removed:
            self.mark_dirty()
        return removed

    # -- построение снэпшота --
    def build_snapshot(self) -> dict:
        with self.lock:
            ordered = sorted(self.sessions.values(), key=lambda s: s.first_seen)
        limit = self.cfg.max_sessions
        if len(ordered) > limit:
            self.log.warning(
                "сессий больше %d, не влезли: %s", limit, [s.name for s in ordered[limit:]]
            )
        visible = ordered[:limit]
        return {"v": 1, "sessions": self._disambiguate(visible)}

    def _disambiguate(self, visible: list[Session]) -> list[dict]:
        """Готовит карточки; при совпадении имён (несколько сессий в одном репо/
        worktree) добавляет короткий суффикс из session_id, чтобы различать."""
        names = [s.name for s in visible]
        dups = {n for n in names if names.count(n) > 1}
        out = []
        for s in visible:
            name = s.name
            if name in dups:
                suffix = "#" + s.session_id[:4]
                base = shorten_middle(name, max(1, self.cfg.name_max - len(suffix)))
                name = base + suffix
            out.append({"id": s.session_id, "name": name, "state": s.state})
        return out

    def snapshot_line(self) -> str:
        return json.dumps(self.build_snapshot(), separators=(",", ":"), ensure_ascii=False) + "\n"

    def push(self) -> bool:
        return self.sink.send(self.snapshot_line())

    # -- фоновые циклы (тонкая обёртка над ядром; проверяются интеграционно) --
    def sender_loop(self) -> None:  # pragma: no cover
        debounce = self.cfg.debounce_ms / 1000.0
        while not self._stop.is_set():
            triggered = self._dirty.wait(timeout=self.cfg.heartbeat_sec)
            if self._stop.is_set():
                break
            if triggered:
                # дебаунс: даём серии быстрых событий схлопнуться в один пуш
                self._dirty.clear()
                time.sleep(debounce)
                self._dirty.clear()
            self.push()

    def reaper_loop(self) -> None:  # pragma: no cover
        while not self._stop.wait(self.cfg.reaper_sec):
            self.reap()

    def mock_loop(self) -> None:  # pragma: no cover
        """Генерит 6 фейковых сессий и крутит их состояния — для проверки моста→ESP."""
        names = ["bitrixpet", "worktree-4", "core", "api-refactor", "docs", "hotfix"]
        now = self._clock()
        with self.lock:
            for i, name in enumerate(names):
                sid = f"mock{i}"
                self.sessions[sid] = Session(sid, name, IDLE, None, now + i * 0.001, now)
        self.mark_dirty()
        cycle = [WORKING, WAITING, IDLE, ERROR]
        step = 0
        while not self._stop.wait(1.5):
            with self.lock:
                for i in range(len(names)):
                    sid = f"mock{i}"
                    if sid in self.sessions:
                        self.sessions[sid].state = cycle[(step + i) % len(cycle)]
            step += 1
            self.mark_dirty()

    def start_background(self) -> None:  # pragma: no cover
        threading.Thread(target=self.sender_loop, name="sender", daemon=True).start()
        if self.cfg.mock:
            threading.Thread(target=self.mock_loop, name="mock", daemon=True).start()
        else:
            threading.Thread(target=self.reaper_loop, name="reaper", daemon=True).start()

    def stop(self) -> None:  # pragma: no cover
        self._stop.set()
        self._dirty.set()


# --- HTTP-сервер --------------------------------------------------------------
def parse_event_body(raw: bytes) -> dict:
    """Разбор тела POST /event. Пустое тело → {}. Кидает на невалидном JSON."""
    return json.loads(raw.decode("utf-8")) if raw else {}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    @property
    def bridge(self) -> Bridge:
        return self.server.bridge  # type: ignore[attr-defined]

    def _respond(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        if self.path != "/event":
            self._respond(404, {"ok": False, "error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length else b""
            self.bridge.handle_event(parse_event_body(raw))
        except Exception as exc:
            # хук никогда не должен «зависнуть» — логируем и всё равно 200
            self.bridge.log.warning("ошибка обработки /event: %s", exc)
        self._respond(200, {"ok": True})

    def do_GET(self) -> None:
        if self.path in ("/", "/status"):
            self._respond(200, self.bridge.build_snapshot())
        else:
            self._respond(404, {"ok": False})

    def log_message(self, fmt, *args):  # заглушаем дефолтный шумный лог
        logging.getLogger("octo.http").debug(fmt, *args)


def bind_singleton(cfg: Config) -> ThreadingHTTPServer | None:
    """Биндим порт. Успех → мы единственный инстанс. EADDRINUSE → мост уже есть."""
    # НЕ включаем SO_REUSEADDR: иначе на Windows второй инстанс тоже забиндится
    ThreadingHTTPServer.allow_reuse_address = False
    # daemon-потоки запросов: не копятся и не блокируют завершение за долгую сессию
    ThreadingHTTPServer.daemon_threads = True
    try:
        return ThreadingHTTPServer((cfg.host, cfg.port), Handler)
    except OSError as exc:
        in_use = exc.errno in (
            socket.errno.EADDRINUSE,
            getattr(socket.errno, "WSAEADDRINUSE", 10048),
        )
        if in_use:
            return None
        raise


def main() -> int:  # pragma: no cover
    logging.basicConfig(
        level=logging.DEBUG if os.environ.get("OCTO_DEBUG") else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    log = logging.getLogger("octo")
    cfg = Config()

    server = bind_singleton(cfg)
    if server is None:
        log.info("мост уже запущен на %s:%d — выходим", cfg.host, cfg.port)
        return 0

    sink = SerialSink(cfg.serial_port, cfg.serial_baud)
    bridge = Bridge(cfg, sink=sink)
    server.bridge = bridge  # type: ignore[attr-defined]
    bridge.start_background()

    mode = "MOCK" if cfg.mock else "live"
    log.info(
        "мост слушает %s:%d (%s), serial=%s @ %d",
        cfg.host, cfg.port, mode, cfg.serial_port, cfg.serial_baud,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("остановка по Ctrl-C")
    finally:
        bridge.stop()
        server.shutdown()
        server.server_close()
        sink.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
