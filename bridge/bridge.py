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
import random
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
    # Диагностика: подробный лог пушей/переоткрытий serial + чтение обратного
    # канала от ESP. Отдельно от OCTO_DEBUG (тот только сыпет снэпшоты в DEBUG).
    diag: bool = field(default_factory=lambda: _env_bool("OCTO_DIAG"))
    # Открывать serial, НЕ дёргая DTR/RTS — чтобы открытие/переоткрытие порта не
    # ресетило ESP (авто-reset схема Wemos/NodeMCU). Дефолт ВКЛ: дисплею незачем
    # ребутиться при (пере)подключении моста. Отключить: OCTO_SERIAL_NO_RESET=0.
    serial_no_reset: bool = field(
        default_factory=lambda: _env("OCTO_SERIAL_NO_RESET", "1") not in ("0", "false", "False"))
    # Дублировать логи в файл (чтобы «сыпались локально» и переживали сессию).
    # Пусто → дефолтный путь (см. default_log_path); "-" → не писать в файл.
    log_file: str = field(default_factory=lambda: _env("OCTO_LOG_FILE", ""))
    log_max_bytes: int = field(default_factory=lambda: int(_env("OCTO_LOG_MAX_BYTES", "2000000")))
    log_backups: int = field(default_factory=lambda: int(_env("OCTO_LOG_BACKUPS", "3")))
    # Сессия в IDLE, не подававшая признаков жизни столько часов, уходит с экрана:
    # она жива (терминал открыт), но брошена — и не должна вытеснять работающие.
    # WAITING/ERROR/WORKING не скрываются никогда (WAITING законно молчит — см. CLAUDE.md).
    stale_idle_hours: float = field(
        default_factory=lambda: float(_env("OCTO_STALE_IDLE_HOURS", "4")))
    # Второй порог — на любое состояние, включая WORKING/WAITING. Нужен потому, что
    # статус тоже протухает: сессия может висеть в WORKING сутки (реестр Claude Code
    # держит busy, последний хук был давно). Скрытие ≠ удаление: сессия остаётся в
    # мосту и вернётся на экран с первым же признаком жизни.
    stale_any_hours: float = field(
        default_factory=lambda: float(_env("OCTO_STALE_ANY_HOURS", "24")))
    # Сверка состава с реестром сессий Claude Code (~/.claude): 0 = выключить.
    registry_sec: float = field(default_factory=lambda: float(_env("OCTO_REGISTRY_SEC", "10")))
    registry_root: str = field(default_factory=lambda: _env("OCTO_REGISTRY_ROOT", ""))
    # Сколько секунд молодая сессия защищена от удаления сверкой (реестр мог отстать).
    reconcile_grace_sec: float = field(
        default_factory=lambda: float(_env("OCTO_RECONCILE_GRACE_SEC", "30")))
    # Тишина во всех сессиях столько минут — гасим панель (0 = не гасить).
    # Сном управляет мост: он один знает, есть ли жизнь, и умеет разбудить экран.
    sleep_min: float = field(default_factory=lambda: float(_env("OCTO_SLEEP_MIN", "20")))
    # Куда ходим обедать (экран рулетки). Пусто → lunch-places.json рядом с мостом.
    places_file: str = field(default_factory=lambda: _env("OCTO_PLACES_FILE", ""))


def default_log_path() -> str:
    """Куда писать лог, если OCTO_LOG_FILE не задан.

    Мост живёт неделями; без файла диагностика уходит в никуда (запуск из
    octo-run.sh отвязывает процесс, stdout → /dev/null). Поэтому файл включён по
    умолчанию, с ротацией. Отключить: OCTO_LOG_FILE=-
    """
    # XDG_STATE_HOME уважаем на любой ОС: если его выставили явно, это осознанный
    # выбор пользователя, а не догадка платформы (и тест не зависит от того, где
    # запущен). Иначе — платформенный дефолт.
    base = os.environ.get("XDG_STATE_HOME")
    if not base:
        base = (os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")) if os.name == "nt" \
            else os.path.expanduser("~/.local/state")
    return os.path.join(base, "octodash", "bridge.log")


# --- Модель сессии ------------------------------------------------------------
@dataclass
class Session:
    session_id: str
    name: str
    state: int
    pid: int | None
    first_seen: float
    last_event: float
    subagents: int = 0   # число активных суб-агентов (Task) в этой сессии
    # Абсолютное (wall, мс) время последней активности. В отличие от монотонных
    # first_seen/last_event переживает рестарт моста и сравнимо со временем из
    # реестра Claude Code — по нему считается «брошенность» (stale_idle_hours).
    last_active_ms: float = 0.0
    started_ms: float | None = None   # старт сессии из реестра (стабильный порядок карточек)
    # Уровень активности на момент ручного «скрыть» (см. Bridge.mute): пока сессия
    # не проявит новой активности, на экран она не возвращается.
    muted_ms: float = 0.0
    source: str = "hook"              # откуда узнали: hook | registry (для /debug)
    reg_name: str | None = None       # имя сессии по версии Claude Code (справочно)
    transcript: str = ""              # путь к файлу транскрипта (из конверта хука)
    size_mb: float = 0.0              # его размер: «вес» сессии, копоть на карточке


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


# --- рулетка обеда ------------------------------------------------------------
# Третий экран. Список мест и выбор победителя держит МОСТ: список — это состояние
# (правится без перепрошивки), а «не повторять прошлого» — правило, которому нужна
# память. Прошивке остаётся физика барабана и рисование.
PLACES_MAX = 12               # больше на барабан не нужно, а строка снэпшота дорога
PLACE_NAME_MAX = 20           # столько влезает в окно барабана
PLACES_FILE = "lunch-places.json"


def prepare_place(name: str) -> str:
    """Готовое к выводу имя места: верхний регистр → '№'→'N' → обрезка по центру.

    Верхний регистр потому, что растровый шрифт ESP содержит только заглавные:
    5x7 в нижнем регистре по-русски читается плохо, а интерфейс и так капсом.
    '№' заменяется, потому что лигатурный глиф в пяти пикселях выходит кривым.
    Обрезка по СЕРЕДИНЕ — та же конвенция, что у имён карточек: «СТОЛОВАЯ 5» и
    «СТОЛОВАЯ 7» при обрезке с конца дали бы одинаковые огрызки.
    """
    s = " ".join(str(name or "").split()).upper().replace("№", "N")
    return shorten_middle(s, PLACE_NAME_MAX)


def load_places(path: str) -> list[str] | None:
    """Читает список мест. None — файла нет или он не разобрался.

    None и пустой список различаются намеренно: на None показываем прошлый список
    (файл могли править в момент чтения), а не пустой барабан.
    """
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    raw = data.get("places") if isinstance(data, dict) else data
    if not isinstance(raw, list):
        return None
    out = []
    for item in raw[:PLACES_MAX]:
        name = prepare_place(item if isinstance(item, str) else "")
        if name:
            out.append(name)
    return out


# --- расписание кофейни -------------------------------------------------------
# Второй экран гаджета. Расписание и статус считает МОСТ: он один знает время и
# умеет менять часы без перепрошивки, а на ESP уезжает готовый код состояния.
CAFE_OPEN, CAFE_BREAK, CAFE_LUNCH, CAFE_SHUT, CAFE_CLEAN = 0, 1, 2, 3, 4
# Подписи по-русски: у прошивки появился растровый шрифт кириллицы (он делался для
# рулетки), и держать англицизмы на домашнем гаджете больше незачем. Слова короткие
# намеренно — статус рисуется кеглем 3, это 18 px на символ.
CAFE_LABEL = {CAFE_OPEN: "ОТКРЫТО", CAFE_BREAK: "ПЕРЕРЫВ", CAFE_LUNCH: "ОБЕД",
              CAFE_SHUT: "ЗАКРЫТО", CAFE_CLEAN: "УБОРКА"}

# Перерывы одни и те же каждый день; в силе только попадающие в часы дня.
CAFE_BREAKS = [(620, 630), (670, 680), (870, 880), (970, 980)]
# Ключ — день недели по датовскому weekday(): 0 = понедельник.
CAFE_WEEK: dict[int, dict] = {
    0: {"om": 540, "cm": 1020, "lunch": (720, 780)},
    1: {"om": 540, "cm": 1020, "lunch": (720, 780)},
    2: {"om": 540, "cm": 780},                                  # среда — короткий, без обеда
    3: {"om": 540, "cm": 1020, "lunch": (720, 780)},
    4: {"om": 540, "cm": 1020, "lunch": (720, 780), "clean": (960, 1020)},   # пт — уборка
}


def cafe_day(dow: int, week: dict[int, dict] | None = None) -> dict | None:
    """План дня: часы, обед, уборка и действующие перерывы. None — выходной.

    Перерыв, накрытый обедом или уборкой, выбрасывается: иначе он рисуется поверх
    на полосе дня и второй раз вычитается из чистого времени (в пятницу 16:10
    приходится ровно на уборку).
    """
    day = (week or CAFE_WEEK).get(dow)
    if not day:
        return None
    om, cm = day["om"], day["cm"]
    big = [seg for seg in (day.get("lunch"), day.get("clean")) if seg]
    breaks = [(f, t) for f, t in CAFE_BREAKS
              if f >= om and t <= cm and not any(f < bt and bf < t for bf, bt in big)]
    return {"om": om, "cm": cm, "lunch": day.get("lunch"), "clean": day.get("clean"),
            "breaks": breaks}


def cafe_net_minutes(day: dict | None) -> int:
    """Чистое рабочее время за вычетом обеда, уборки и перерывов."""
    if not day:
        return 0
    net = day["cm"] - day["om"]
    for seg in [*day["breaks"], day["lunch"], day["clean"]]:
        if seg:
            net -= seg[1] - seg[0]
    return net


def cafe_status(dow: int, minute: int, week: dict[int, dict] | None = None) -> dict:
    """Что с кофейней прямо сейчас: код состояния и время следующей смены."""
    week = week or CAFE_WEEK
    day = cafe_day(dow, week)
    if day is None or minute >= day["cm"]:
        nxt = next((week[(dow + i) % 7] for i in range(1, 8) if (dow + i) % 7 in week), None)
        return {"st": CAFE_SHUT, "till": nxt["om"] if nxt else 0, "day": day}
    if minute < day["om"]:
        return {"st": CAFE_SHUT, "till": day["om"], "day": day}
    for seg, code in ((day["clean"], CAFE_CLEAN), (day["lunch"], CAFE_LUNCH)):
        if seg and seg[0] <= minute < seg[1]:
            return {"st": code, "till": seg[1], "day": day}
    for f, t in day["breaks"]:
        if f <= minute < t:
            return {"st": CAFE_BREAK, "till": t, "day": day}
    edges = [f for f, _ in day["breaks"]]
    edges += [seg[0] for seg in (day["lunch"], day["clean"]) if seg]
    till = min([e for e in edges if e > minute] + [day["cm"]])
    return {"st": CAFE_OPEN, "till": till, "day": day}


# Свет в аквариуме привязан к рабочему дню, а не к астрономическому вечеру:
# днём в полную силу, после работы гаснет. Считает мост — часов у прошивки нет.
WORK_FROM, WORK_TO = 9 * 60, 18 * 60
DUSK_MIN, DAWN_MIN = 180, 120


def night_level(minute: int) -> int:
    """0 — день, 100 — ночь. Сумерки плавные, чтобы свет не щёлкал."""
    if WORK_FROM <= minute < WORK_TO:
        return 0
    if WORK_TO <= minute < WORK_TO + DUSK_MIN:
        return round((minute - WORK_TO) / DUSK_MIN * 100)
    if WORK_FROM - DAWN_MIN <= minute < WORK_FROM:
        return round((1 - (minute - WORK_FROM + DAWN_MIN) / DAWN_MIN) * 100)
    return 100


def find_transcript(session_id: str, root: str = "", lister=None) -> str:
    """Ищет транскрипт сессии: <реестр>/projects/<слаг>/<session_id>.jsonl.

    Полагаться только на путь из конверта хука нельзя: установленная обёртка может
    быть старой версии (у неё этого поля нет), а сессии, поднятые сверкой с
    реестром, хуков не присылают вообще. Формат чужой, поэтому не нашли — ноль.
    """
    if not session_id:
        return ""
    import glob as _glob

    lister = lister or _glob.glob
    hits = lister(os.path.join(registry_root(root), "projects", "*", f"{session_id}.jsonl"))
    return hits[0] if hits else ""


def transcript_size_mb(path: str, stat_fn: Callable[[str], object] | None = None) -> float:
    """Вес сессии в мегабайтах по файлу транскрипта.

    Разбирать чужой формат не нужно — достаточно размера: контекст сбрасывает
    автокомпакт, а файл растёт монотонно, и по нему видно, когда сессию проще
    пересоздать. Файла нет или нет прав — ноль, это не повод шуметь в лог.
    """
    if not path:
        return 0.0
    try:
        size = (stat_fn or os.stat)(path).st_size      # type: ignore[union-attr]
    except OSError:
        return 0.0
    return round(size / (1024 * 1024), 2)


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


# --- Реестр сессий Claude Code ------------------------------------------------
# Claude Code сам ведёт на диске список активных сессий — это точнее любой
# эвристики по PID (в конверте хука PID нет вообще, см. octo-notify.py):
#   ~/.claude/sessions/<pid>.json   интерактивные; имя файла = PID, внутри
#                                   sessionId/cwd/status/name/statusUpdatedAt/procStart
#   ~/.claude/daemon/roster.json    фоновые воркеры (workers.<short>: pid, sessionId, cwd)
#   ~/.claude/jobs/<short>/state.json  фоновые задания (state: blocked/active, name)
# Формат недокументирован и может уехать с версией CLI, поэтому весь разбор —
# best-effort: непонятная запись пропускается, полностью непрочитанный реестр даёт
# None, и мост продолжает работать по-старому (хуки + liveness по PID).
REGISTRY_STATUS_TO_STATE = {
    "busy": WORKING,
    "active": WORKING,
    "working": WORKING,
    "blocked": WAITING,
    "waiting": WAITING,
    "idle": IDLE,
    "done": IDLE,        # фоновое задание отработало, сессия ждёт нового ввода
    "error": ERROR,
    "failed": ERROR,
}


def registry_root(cfg_root: str = "") -> str:
    return cfg_root or os.path.join(os.path.expanduser("~"), ".claude")


def _read_json(path: str) -> dict | None:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None      # файл переписывается на ходу / битый / нет прав — не наша забота
    return data if isinstance(data, dict) else None


def proc_start_ticks(pid: int) -> str | None:
    """Поле starttime (22-е) из /proc/<pid>/stat — тики старта процесса.

    Реестр Claude Code пишет то же значение в `procStart`; совпадение защищает от
    переиспользования PID. Вне Linux возвращает None — тогда проверка пропускается.
    """
    try:
        with open(f"/proc/{pid}/stat", "rb") as f:
            raw = f.read()
        tail = raw[raw.rindex(b")") + 2:].split()
        return tail[19].decode()
    except Exception:
        return None


def _entry(session_id, *, pid=None, cwd="", kind="", name=None,
           status=None, status_ms=0.0, started_ms=None, proc_start=None) -> dict:
    return {
        "session_id": session_id, "pid": pid, "cwd": cwd, "kind": kind, "name": name,
        "status": status, "status_ms": status_ms, "started_ms": started_ms,
        "proc_start": proc_start,
    }


def read_session_registry(root: str = "", *, lister=None, reader=None) -> list[dict] | None:
    """Читает реестр и возвращает список записей (или None, если реестра нет).

    `lister`/`reader` (glob и чтение JSON) внедряются ради тестов без файловой
    системы. None означает «реестр недоступен» — принципиально иное, чем пустой
    список: сверка на None ничего не удаляет.
    """
    import glob as _glob

    base = registry_root(root)
    lister = lister or _glob.glob
    reader = reader or _read_json

    found = False
    out: dict[str, dict] = {}

    # 1) интерактивные сессии: sessions/<pid>.json
    paths = lister(os.path.join(base, "sessions", "*.json"))
    if paths:
        found = True
    for path in paths:
        d = reader(path)
        if not d or not d.get("sessionId"):
            continue
        out[str(d["sessionId"])] = _entry(
            str(d["sessionId"]),
            pid=coerce_pid(d.get("pid")),
            cwd=str(d.get("cwd") or ""),
            kind=str(d.get("kind") or "interactive"),
            name=d.get("name"),
            status=str(d.get("status") or "") or None,
            status_ms=float(d.get("statusUpdatedAt") or d.get("updatedAt") or 0),
            started_ms=float(d["startedAt"]) if d.get("startedAt") else None,
            proc_start=str(d["procStart"]) if d.get("procStart") else None,
        )

    # 2) фоновые воркеры: daemon/roster.json (настоящий PID воркера — у фоновых
    #    сессий в agents --json pid=None, а хук резолвит PID демона-супервизора).
    #    Дополняет запись из sessions/, а не перетирает её: status есть только там.
    roster = reader(os.path.join(base, "daemon", "roster.json"))
    if roster:
        found = True
        workers = roster.get("workers")
        for w in (workers or {}).values():
            if not isinstance(w, dict) or not w.get("sessionId"):
                continue
            sid = str(w["sessionId"])
            started = float(w["startedAt"]) if w.get("startedAt") else None
            prev = out.get(sid)
            if prev is None:
                out[sid] = _entry(
                    sid,
                    pid=coerce_pid(w.get("pid")),
                    cwd=str(w.get("cwd") or ""),
                    kind="background",
                    status_ms=started or float(roster.get("updatedAt") or 0),
                    started_ms=started,
                    proc_start=str(w["procStart"]) if w.get("procStart") else None,
                )
                continue
            prev["kind"] = "background"
            if prev.get("pid") is None:
                prev["pid"] = coerce_pid(w.get("pid"))
                prev["proc_start"] = str(w["procStart"]) if w.get("procStart") else None
            if prev.get("started_ms") is None:
                prev["started_ms"] = started

    # 3) фоновые задания: jobs/<short>/state.json — только обогащение статусом и
    #    именем. Самостоятельным источником сессий быть НЕ может: там лежат и
    #    завершённые задания (state=done, встречаются недельной давности), и
    #    отложенные (blocked без воркера) — процесса за ними нет, показывать нечего.
    for path in lister(os.path.join(base, "jobs", "*", "state.json")):
        d = reader(path)
        if not d or not d.get("sessionId"):
            continue
        entry = out.get(str(d["sessionId"]))
        if entry is None:
            continue
        entry["status"] = str(d.get("state") or "") or entry.get("status")
        entry["name"] = entry.get("name") or d.get("name")

    if not found:
        return None      # ни одного источника — реестра тут нет (или другая версия CLI)
    return list(out.values())


def registry_entry_alive(entry: dict, is_alive: Callable[[int | None], bool] = pid_alive) -> bool:
    """Запись реестра актуальна: PID жив и это тот же процесс (procStart совпал).

    Файл реестра может остаться после kill -9, поэтому одного факта «файл есть»
    мало. Запись без PID (фоновая задача в демоне) считается живой — её liveness
    определяет наличие в реестре.
    """
    pid = entry.get("pid")
    if pid is None:
        return True
    if not is_alive(pid):
        return False
    want = entry.get("proc_start")
    if not want:
        return True
    actual = proc_start_ticks(pid)
    return actual is None or actual == want


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
    # Фолбэк только на USB-порты: у них есть VID. Порт без VID — это COM1 на
    # материнке или виртуальный переходник, и он существует всегда. Раньше мост
    # при отключённой плате радостно «находил» COM1, открывал его и молча сыпал
    # снэпшоты в никуда — экран при этом просто не обновлялся.
    usb = [p for p in ports if getattr(p, "vid", None)]
    if len(usb) == 1:
        return usb[0].device
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

    def status(self) -> dict:
        return {"kind": "logging", "connected": False}


class SerialSink:
    """Пишет строки в serial с ленивым открытием и автопереоткрытием при ошибке."""

    def __init__(
        self,
        port: str,
        baud: int,
        logger: logging.Logger | None = None,
        list_ports_fn: Callable[[], list] | None = None,
        no_reset: bool = False,
    ):
        self.port = port
        self.baud = baud
        self.log = logger or logging.getLogger("octo.serial")
        self._list_ports_fn = list_ports_fn
        self.no_reset = no_reset
        self._ser = None
        self._last_warn = 0.0
        self._opens = 0        # сколько раз открывали порт (>1 = переоткрытие)
        self._rx = b""         # буфер обратного канала от ESP (до '\n')

    def _auto(self) -> bool:
        return self.port in ("", "auto", None)

    def status(self) -> dict:
        return {
            "kind": "serial",
            "port": self.port,
            "resolved": getattr(self._ser, "port", None),
            "connected": self._ser is not None and bool(getattr(self._ser, "is_open", False)),
            "opens": self._opens,
        }

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
            self._ser = self._open(port)
            self._opens += 1
            if self._opens == 1:
                self.log.info(
                    "serial открыт: %s @ %d%s%s",
                    port, self.baud, " (auto)" if self._auto() else "",
                    " (no-reset)" if self.no_reset else "",
                )
            else:
                # Переоткрытие подозрительно: если не no_reset, оно ресетит ESP
                # (DTR/RTS) → чёрный экран ~3с на время ребута. Кричим погромче.
                self.log.warning(
                    "SERIAL (RE)OPEN #%d: %s%s — ESP МОГ СБРОСИТЬСЯ (это причина моргания?)",
                    self._opens, port, " [no-reset]" if self.no_reset else " [DTR/RTS reset!]",
                )
            return self._ser
        except Exception as exc:  # порт недоступен — не падаем
            self._warn_throttled("не удалось открыть serial %s: %s", port, exc)
            self._ser = None
            return None

    def _open(self, port: str):  # pragma: no cover - железо
        """Открыть порт. При no_reset — не дёргать DTR/RTS, чтобы не ресетить ESP.

        Классическая авто-reset схема Wemos/NodeMCU завязана на DTR+RTS от
        USB-UART. Обычный serial.Serial(...) на открытии их дёргает и может
        перезагрузить плату. Открываем закрытый порт, гасим dtr/rts, потом open().
        """
        if not self.no_reset:
            return serial.Serial(port, self.baud, timeout=1, write_timeout=1)
        s = serial.Serial()
        s.port = port
        s.baudrate = self.baud
        s.timeout = 1
        s.write_timeout = 1
        s.dtr = False
        s.rts = False
        s.open()
        return s

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

    def read_lines(self) -> list[str]:  # pragma: no cover - железо
        """Слить обратный канал от ESP и вернуть завершённые строки (без '\\n').

        ESP печатает в serial свои маркеры (boot/reason/heap). Мост их читает,
        чтобы диагностировать ребуты. Никогда не роняет пуш: любые ошибки глотаем.
        """
        ser = self._ser
        if ser is None or not getattr(ser, "is_open", False):
            return []
        try:
            waiting = ser.in_waiting
            if not waiting:
                return []
            self._rx += ser.read(waiting)
        except Exception:
            return []
        lines: list[str] = []
        while b"\n" in self._rx:
            raw, self._rx = self._rx.split(b"\n", 1)
            text = raw.decode("utf-8", "replace").strip("\r ")
            if text:
                lines.append(text)
        if len(self._rx) > 4096:   # защита от мусора без переводов строк
            self._rx = b""
        return lines

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
        wall_clock: Callable[[], float] = time.time,
        registry_probe: Callable[[], list[dict] | None] | None = None,
        size_probe: Callable[[str], float] = transcript_size_mb,
        transcript_probe: Callable[[str], str] | None = None,
        rng: random.Random | None = None,
    ):
        self.cfg = cfg
        self.log = logger or logging.getLogger("octo.bridge")
        self._clock = clock
        self._wall = wall_clock
        self._is_alive = is_alive
        self.sink: Sink = sink or LoggingSink(self.log)
        # Сверка состава с реестром Claude Code; None → читать реальный реестр.
        self._registry_probe = registry_probe or (
            lambda: read_session_registry(self.cfg.registry_root))
        self._size_probe = size_probe
        # Случайность внедряется: иначе выбор победителя рулетки нечем проверить.
        self._rng = rng or random.Random()
        self._transcript_probe = transcript_probe or (
            lambda sid: find_transcript(sid, self.cfg.registry_root))

        self.lock = threading.Lock()
        self.sessions: dict[str, Session] = {}

        self._dirty = threading.Event()
        self._stop = threading.Event()

        self._push_n = 0          # диагностика: счётчик пушей
        self._last_push = 0.0     # монотонное время предыдущего пуша (для dt)
        self._started_ms = self._wall() * 1000
        self._reconcile_n = 0
        self._last_reconcile = 0.0
        self._registry_ok: bool | None = None   # None = ещё не сверялись
        self._registry_n = 0
        self._last_hidden_key: tuple[str, ...] | None = None

        # Что сейчас на экране. Страницу и экран держит МОСТ, а не прошивка: ESP
        # присылает только событие энкодера и получает готовый снэпшот.
        self.page = 0
        self.screen = 0                       # 0 — аквариум, 1 — кофейня, 2 — рулетка
        self.sleeping = False
        self._last_activity = self._clock()
        self._enc_n = 0                       # диагностика: сколько событий пришло с ручки
        self.shot = ShotCollector()           # отладочные снимки с платы

        # Рулетка обеда: список мест и победитель. Перечитывается по mtime, поэтому
        # файл можно править на живом мосту.
        self.places: list[str] = []
        self._places_mtime: float | None = None
        self.roul_win = -1                    # индекс победителя текущего запуска
        self.roul_spin = 0                    # номер запуска: по нему прошивка видит новый ответ
        self.roul_last = -1                   # прошлый победитель — его не повторяем

    # -- обработка события от хука; возвращает True, если снэпшот стал грязным --
    def handle_event(self, data: dict) -> bool:
        event = str(data.get("event", "")).lower()
        session_id = data.get("session_id")
        if not session_id:
            self.log.warning("событие без session_id: %r", data)
            return False

        now = self._clock()
        now_ms = self._wall() * 1000
        cwd = data.get("cwd", "")
        name = self._name(cwd)
        pid = coerce_pid(data.get("pid"))
        transcript = str(data.get("transcript") or "")
        # Любой хук — признак жизни: сдвигает автосон и будит погашенный экран.
        # WAITING обязан будить, иначе просмотришь, что у тебя спрашивают.
        self.touch_activity(wake=True)
        try:
            # абсолютное число работающих субагентов из background_tasks конверта
            subs = max(0, int(data["subs"])) if "subs" in data else None
        except (TypeError, ValueError):
            subs = None

        with self.lock:
            sess = self.sessions.get(session_id)
            if sess is not None:
                self._note_transcript(sess, transcript)

            if event == "end":
                if self.sessions.pop(session_id, None) is not None:
                    self.log.info("end: удалена сессия %s", session_id)
                    return self._dirty_set()
                return False

            if event == "start":
                if sess is None:
                    sess = Session(session_id, name, IDLE, pid, now, now, last_active_ms=now_ms)
                    self.sessions[session_id] = sess
                    self._note_transcript(sess, transcript)
                    self.log.info("start: %s (%s) pid=%s", session_id, name, pid)
                else:
                    sess.state = IDLE
                    sess.last_event = now
                    sess.last_active_ms = now_ms
                    sess.subagents = 0
                    if pid is not None:
                        sess.pid = pid
                    if cwd:
                        sess.name = name
                return self._dirty_set()

            # суб-агенты: PreToolUse matcher Task|Agent (+1) / SubagentStop (sync или -1)
            if event in ("subagent", "subagent_done"):
                if sess is None:
                    # спавн суб-агента у незнакомой сессии → создаём (родитель активен)
                    sess = Session(session_id, name, WORKING, pid, now, now, last_active_ms=now_ms)
                    self.sessions[session_id] = sess
                    self._note_transcript(sess, transcript)
                if event == "subagent":
                    sess.subagents += 1
                elif subs is not None:
                    # SubagentStop срабатывает на КАЖДУЮ остановку субагента (в т.ч.
                    # промежуточную, с живыми фоновыми детьми) — ±1 занижает счётчик.
                    # Конверт несёт background_tasks; синхронизируемся по абсолюту.
                    sess.subagents = subs
                else:
                    sess.subagents = max(0, sess.subagents - 1)   # старый хук без subs
                sess.last_event = now
                sess.last_active_ms = now_ms
                return self._dirty_set()

            new_state = EVENT_TO_STATE.get(event)
            if new_state is None:
                self.log.warning("неизвестный event %r для %s — игнор", event, session_id)
                return False

            if sess is None:
                # событие для незнакомой сессии — создаём на лету (мост мог рестартнуть)
                sess = Session(session_id, name, new_state, pid, now, now, last_active_ms=now_ms)
                self.sessions[session_id] = sess
                self._note_transcript(sess, transcript)
                self.log.info("создана на лету: %s (%s) state=%d", session_id, name, new_state)
                return self._dirty_set()

            sess.last_event = now
            sess.last_active_ms = now_ms
            if cwd:
                sess.name = name
            if pid is not None:
                sess.pid = pid
            # NB: на IDLE счётчик суб-агентов НЕ сбрасываем: субагенты фоновые,
            # конец хода родителя не означает их завершения. Правда — в subs
            # (background_tasks из конверта Stop): синхронизируемся, если прислали.
            dirty = False
            if subs is not None and sess.subagents != subs:
                sess.subagents = subs
                dirty = True
            if sess.state != new_state:
                sess.state = new_state
                dirty = True
            return self._dirty_set() if dirty else False

    def _note_transcript(self, sess: Session, path: str) -> None:
        """Запоминает путь к транскрипту и обновляет вес сессии.

        Считается на хуке: файл растёт только когда сессия работает, а работающая
        сессия шлёт события. Один os.stat на хук — дёшево. Путь может не прийти
        (старая обёртка) — тогда его найдёт refresh_sizes по session_id.
        """
        if path:
            sess.transcript = path
        if sess.transcript:
            sess.size_mb = self._size_probe(sess.transcript)

    def refresh_sizes(self) -> bool:
        """Пересчитывает вес всех сессий и сам находит транскрипты без пути.

        Дёргается в такт сверке с реестром. Грязным помечаем только при смене
        ЦЕЛЫХ мегабайт — снэпшот всё равно везёт целые, а лишние пуши не нужны.
        """
        with self.lock:
            sessions = list(self.sessions.values())
        changed = False
        for sess in sessions:
            path = sess.transcript or self._transcript_probe(sess.session_id)
            if not path:
                continue
            mb = self._size_probe(path)
            if int(round(mb)) != int(round(sess.size_mb)):
                changed = True
            sess.transcript = path
            sess.size_mb = mb
        if changed:
            self.mark_dirty()
        return changed

    def _name(self, cwd: str) -> str:
        return display_name(cwd, self.cfg.name_max)

    def _dirty_set(self) -> bool:
        self._dirty.set()
        return True

    def mark_dirty(self) -> None:
        self._dirty.set()

    # -- сверка состава с реестром Claude Code --------------------------------
    def reconcile(self, entries: list[dict] | None = None) -> bool:
        """Приводит состав сессий к реестру Claude Code. True, если что-то изменилось.

        Зачем, если есть reaper: liveness по PID не видит двух реальных случаев —
        сессию без PID (фоновая, или хук не сумел его резолвить: тогда карточка
        живёт вечно) и смену session_id внутри живого процесса (`/clear`, resume,
        fork — старый id мёртв, а PID процесса жив). В реестре на один PID ровно
        одна актуальная сессия, поэтому оба случая закрываются точно.

        Осторожность: реестр — недокументированный контракт. Если он не прочитался
        (None) или пуст, НИЧЕГО не удаляем — деградируем к поведению по хукам.
        Молодые сессии (младше reconcile_grace_sec) тоже не трогаем: реестр мог
        ещё не успеть записать файл.
        """
        if entries is None:
            entries = self._registry_probe()
        self._reconcile_n += 1
        self._last_reconcile = self._clock()
        self._registry_ok = entries is not None
        if not entries:
            self._registry_n = 0
            return False

        live = {e["session_id"]: e for e in entries if registry_entry_alive(e, self._is_alive)}
        self._registry_n = len(live)
        by_pid = {e["pid"]: sid for sid, e in live.items() if e.get("pid") is not None}

        now, now_ms = self._clock(), self._wall() * 1000
        changed = False
        with self.lock:
            for sid, sess in list(self.sessions.items()):
                entry = live.get(sid)
                if entry is not None:
                    changed |= self._apply_entry(sess, entry)
                    continue
                if now - sess.first_seen < self.cfg.reconcile_grace_sec:
                    continue                          # только что родилась — реестр отстаёт
                owner = by_pid.get(sess.pid) if sess.pid is not None else None
                if sess.pid is not None and owner is None and self._is_alive(sess.pid):
                    continue    # реестр не знает этот PID (другая версия CLI?) — не трогаем
                del self.sessions[sid]
                changed = True
                why = "PID занят сессией " + owner if owner else "нет в реестре"
                self.log.info("сверка: убрана сессия %s (%s) — %s", sid, sess.name, why)

            for sid, entry in live.items():
                if sid in self.sessions:
                    continue
                state = REGISTRY_STATUS_TO_STATE.get((entry.get("status") or "").lower(), IDLE)
                self.sessions[sid] = Session(
                    sid, self._name(entry.get("cwd") or ""), state, entry.get("pid"),
                    now, now,
                    last_active_ms=entry.get("status_ms") or entry.get("started_ms") or now_ms,
                    started_ms=entry.get("started_ms"),
                    source="registry",
                    reg_name=entry.get("name"),
                )
                changed = True
                self.log.info(
                    "сверка: найдена сессия %s (%s) state=%d — добавлена без хука",
                    sid, self.sessions[sid].name, state,
                )
        if changed:
            self.mark_dirty()
        return changed

    def _apply_entry(self, sess: Session, entry: dict) -> bool:
        """Дополняет известную сессию данными реестра. True, если снэпшот изменился."""
        changed = False
        if sess.started_ms is None and entry.get("started_ms"):
            sess.started_ms = entry["started_ms"]
        if entry.get("name"):
            sess.reg_name = entry["name"]
        if sess.pid is None and entry.get("pid") is not None:
            sess.pid = entry["pid"]         # даёт liveness сессии, чей PID хук не нашёл
        # Статус: правда за самым свежим сигналом. Хуки обычно свежее (реагируют
        # мгновенно), но после рестарта моста или потерянного хука реестр точнее.
        status_ms = entry.get("status_ms") or 0
        if status_ms > sess.last_active_ms:
            state = REGISTRY_STATUS_TO_STATE.get((entry.get("status") or "").lower())
            sess.last_active_ms = status_ms
            if state is not None and state != sess.state:
                sess.state = state
                changed = True
        return changed

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

    # -- отбор карточек на экран ---------------------------------------------
    # Что важнее видеть, когда сессий больше, чем слотов. Экран маленький, поэтому
    # приоритет у того, где сейчас что-то происходит или ждут человека.
    _PRIO = {WORKING: 0, ERROR: 1, WAITING: 2, IDLE: 3}

    def stale_reason(self, sess: Session, now_ms: float) -> str | None:
        """Почему сессию не стоит показывать (или None, если стоит).

        Два порога. Короткий (stale_idle_hours) — для IDLE: терминал открыт, но в
        сессии давно тишина. Длинный (stale_any_hours) — для любого состояния,
        включая WORKING/WAITING: их статус тоже протухает, и «работает» сутками без
        единого события означает, что сессию бросили, а не что она трудится.
        WAITING держится дольше остальных намеренно — он законно молчит, пока ждёт
        человека (см. инвариант в CLAUDE.md), поэтому короткий порог его не касается.
        """
        if sess.muted_ms and sess.last_active_ms <= sess.muted_ms:
            return "скрыта вручную"
        if not sess.last_active_ms:
            return None      # про активность ничего не известно — не наказываем
        quiet_h = (now_ms - sess.last_active_ms) / 3_600_000
        if 0 < self.cfg.stale_any_hours < quiet_h:
            return f"тишина {quiet_h:.1f}ч > {self.cfg.stale_any_hours:g}ч"
        if sess.state == IDLE and 0 < self.cfg.stale_idle_hours < quiet_h:
            return f"idle {quiet_h:.1f}ч > {self.cfg.stale_idle_hours:g}ч"
        return None

    def _order_key(self, sess: Session, now: float, now_ms: float) -> float:
        """Место карточки на экране: стабильное, по времени появления сессии.

        Порядок отбора (что показать) и порядок раскладки (где показать) — разные
        вещи: иначе карточки прыгали бы при каждой смене статуса. Абсолютный
        started_ms из реестра переживает рестарт моста, поэтому предпочтительнее
        монотонного first_seen (тот пересчитывается в wall-время как фолбэк).
        """
        if sess.started_ms:
            return sess.started_ms
        return now_ms - (now - sess.first_seen) * 1000

    def paginate(self) -> tuple[list[list[Session]], list[tuple[Session, str]]]:
        """Все живые сессии, разложенные по страницам, и скрытые как брошенные.

        Отбор и раскладка по-прежнему разные вещи: страницы режутся по приоритету
        (чтобы важное было на первой), а внутри страницы порядок — по времени старта,
        чтобы карточки не прыгали при смене статуса.
        """
        now, now_ms = self._clock(), self._wall() * 1000
        with self.lock:
            all_sessions = list(self.sessions.values())

        fresh, stale = [], []
        for s in all_sessions:
            reason = self.stale_reason(s, now_ms)
            (stale if reason else fresh).append((s, reason))

        fresh.sort(key=lambda p: (self._PRIO.get(p[0].state, 9), -p[0].last_active_ms))
        ranked = [p[0] for p in fresh]

        limit = max(1, self.cfg.max_sessions)
        pages = [ranked[i:i + limit] for i in range(0, len(ranked), limit)] or [[]]
        for page in pages:
            page.sort(key=lambda s: self._order_key(s, now, now_ms))
        return pages, stale

    def page_count(self) -> int:
        return len(self.paginate()[0])

    def select_visible(self) -> tuple[list[Session], list[tuple[Session, str]]]:
        """Текущая страница и всё, чего на ней нет, — с причиной."""
        pages, stale = self.paginate()
        idx = min(self.page, len(pages) - 1)
        if idx != self.page:
            self.page = idx                       # страниц стало меньше — подтянулись
        chosen = pages[idx]
        hidden = [(s, f"стр. {n + 1}") for n, page in enumerate(pages) if n != idx for s in page]
        return chosen, hidden + stale

    # -- энкодер и сон ---------------------------------------------------------
    SCREENS = 3                                   # 0 — аквариум, 1 — кофейня, 2 — рулетка

    def touch_activity(self, wake: bool = False) -> None:
        """Признак жизни: сдвигает точку отсчёта автосна, при wake — будит экран."""
        self._last_activity = self._clock()
        if wake and self.sleeping:
            self.set_sleep(False)

    def set_sleep(self, value: bool) -> bool:
        if self.sleeping == value:
            return False
        self.sleeping = value
        if not value:
            self._last_activity = self._clock()
        self.log.info("экран %s", "уснул" if value else "проснулся")
        self.mark_dirty()
        return True

    def maybe_sleep(self) -> bool:
        """Автосон по тишине. Дёргается из reaper-цикла."""
        if self.sleeping or self.cfg.sleep_min <= 0:
            return False
        if self._clock() - self._last_activity < self.cfg.sleep_min * 60:
            return False
        return self.set_sleep(True)

    def handle_encoder(self, event: str, held: bool = False) -> bool:
        """Событие с ручки. Вращение — страницы, нажатие — экран, удержание — сон.

        Первый щелчок из сна ТОЛЬКО будит и ничего не листает: иначе спросонья
        улетаешь не на ту страницу.
        """
        event = str(event or "").lower()
        self._enc_n += 1
        self._last_activity = self._clock()
        if self.sleeping:
            self.set_sleep(False)
            return True

        if event in ("cw", "ccw"):
            step = 1 if event == "cw" else -1
            if held:
                self.screen = (self.screen + step) % self.SCREENS
            elif self.screen == 0:
                n = self.page_count()
                if n:
                    self.page = (min(self.page, n - 1) + step) % n
            else:
                return False                      # вне аквариума листать нечего
        elif event == "key":
            self.screen = (self.screen + 1) % self.SCREENS
        elif event == "spin":
            # Прошивка накопила скорость барабана и просит результат. Экран не
            # проверяем: событие приходит только с экрана рулетки, а мост мог
            # рассинхронизироваться с прошивкой при рестарте.
            return self.spin_roulette()
        elif event == "hold":
            return self.set_sleep(True)
        else:
            self.log.warning("неизвестное событие энкодера: %r", event)
            return False

        self.mark_dirty()
        return True

    # -- экран кофейни --
    def cafe_now(self) -> tuple[int, int]:
        """День недели и минуты от полуночи по часам моста."""
        tm = time.localtime(self._wall())
        return tm.tm_wday, tm.tm_hour * 60 + tm.tm_min

    # -- экран рулетки ---------------------------------------------------------
    def places_path(self) -> str:
        return self.cfg.places_file or os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                    PLACES_FILE)

    def refresh_places(self) -> bool:
        """Перечитывает список мест, если файл изменился. True — состав поменялся.

        По mtime, а не однократно на старте: список правят руками, и перезапускать
        из-за этого мост незачем. Нечитаемый файл НЕ обнуляет прошлый список —
        его могли править в момент чтения.
        """
        path = self.places_path()
        try:
            mtime = os.stat(path).st_mtime
        except OSError:
            if self._places_mtime is not None:
                return False                   # файл пропал — держим что было
            mtime = None
        if mtime is not None and mtime == self._places_mtime:
            return False
        places = load_places(path)
        if places is None:
            if not self.places:
                self.log.warning("список мест не прочитался: %s", path)
            return False
        self._places_mtime = mtime
        if places == self.places:
            return False
        self.places = places
        self.roul_win = -1                     # состав сменился — прошлый выбор недействителен
        self.log.info("список мест обновлён (%d): %s", len(places), ", ".join(places))
        return True

    def spin_roulette(self) -> bool:
        """Выбирает победителя. Вызывается на событие 'spin' от прошивки.

        Выбор здесь, а не в прошивке: список и правило «не повторять прошлого» —
        это состояние, а состояние держит мост. Прошивка накопила скорость и просит
        результат; барабан доедет ровно до присланного индекса.
        """
        self.refresh_places()
        n = len(self.places)
        if n == 0:
            self.log.warning("рулетка: список мест пуст, крутить нечего")
            return False
        if n == 1:
            pick = 0
        else:
            pick = self._rng.randrange(n - 1)   # выбираем среди всех, кроме прошлого
            if self.roul_last >= 0 and pick >= self.roul_last:
                pick += 1
            if pick >= n:                       # прошлый индекс уехал за границы нового списка
                pick = self._rng.randrange(n)
        self.roul_win = pick
        self.roul_last = pick
        self.roul_spin += 1
        self.log.info("рулетка #%d: %s", self.roul_spin, self.places[pick])
        self.mark_dirty()
        return True

    def build_roulette(self) -> dict:
        self.refresh_places()
        # nm — текущее время: шапка экрана его показывает, а без блока кофейни
        # прошивке взять его негде (стояло 00:00)
        return {"p": list(self.places), "win": self.roul_win, "sp": self.roul_spin,
                "nm": self.cafe_now()[1]}

    def build_cafe(self) -> dict:
        """Готовый статус кофейни для прошивки: она в расписании не разбирается.

        Едут минуты от полуночи и код состояния — печатать HH:MM из числа и рисовать
        полосу дня ESP умеет, а знать, что среда короткая, ей незачем.
        """
        dow, minute = self.cafe_now()
        st = cafe_status(dow, minute)
        day = st["day"]
        segs: list[list[int]] = []
        if day:
            segs = [[f, t, 0] for f, t in day["breaks"]]
            if day["lunch"]:
                segs.append([day["lunch"][0], day["lunch"][1], 1])
            if day["clean"]:
                segs.append([day["clean"][0], day["clean"][1], 2])
            segs.sort()
        return {
            "st": st["st"], "nm": minute, "dow": dow, "till": st["till"],
            "om": day["om"] if day else 0, "cm": day["cm"] if day else 0,
            "net": cafe_net_minutes(day), "br": segs,
        }

    def request_shot(self, path: str, timeout: float = 60.0) -> str | None:
        """Просит плату отдать снимок и складывает PNG. None — команда не ушла.

        Снимок собирается на плате той же композицией, что рисует экран, поэтому
        по нему видно ровно то, что увидит человек. Неполноту НЕ прячем: обрезанный
        снимок выглядит как «пропали осьминоги» и уже один раз сбил с толку.
        """
        self.shot.start()
        if not self.sink.send('{"cmd":"shot"}\n'):
            self.log.warning("снимок: не удалось отправить команду (serial?)")
            return None
        if not self.shot.done.wait(timeout):
            self.log.warning("снимок: плата не досыпала плитки за %gс", timeout)
        if not self.shot.complete:
            self.log.warning("снимок НЕПОЛНЫЙ: плиток %d, не хватило строк %d",
                             self.shot.tiles, self.shot.missing_rows)
        return self.shot.save(path)

    # -- построение снэпшота --
    def build_snapshot(self) -> dict:
        # Спящему экрану состав не нужен: гоняем короткую строку, а не список сессий.
        if self.sleeping:
            return {"v": 1, "slp": 1}
        if self.screen == 1:
            return {"v": 1, "scr": 1, "cafe": self.build_cafe()}
        if self.screen == 2:
            return {"v": 1, "scr": 2, "roul": self.build_roulette()}
        visible, hidden = self.select_visible()
        # heartbeat строит снэпшот раз в 5с — логируем только смену состава скрытых,
        # иначе лог заплывёт одинаковыми строками
        key = tuple(sorted(s.session_id for s, _ in hidden))
        if key != self._last_hidden_key:
            self._last_hidden_key = key
            if hidden:
                self.log.info(
                    "не на экране (%d): %s", len(hidden),
                    ", ".join(f"{s.name}[{r or 'нет слота'}]" for s, r in hidden),
                )
        pn = self.page_count()
        snap = {
            "v": 1,
            "scr": self.screen,
            "p": min(self.page, pn - 1) + 1,      # человеческая нумерация: 1..pn
            "pn": pn,
            "sessions": self._disambiguate(visible),
        }
        nl = night_level(self.cafe_now()[1])
        if nl:
            snap["nl"] = nl                        # днём поле не гоняем — экономим байты
        return snap

    @staticmethod
    def short_ids(visible: list[Session], want: int = 8, limit: int = 20) -> dict[str, str]:
        """Короткие id для снэпшота: прошивке они нужны только чтобы отличать карточки.

        Полный session_id — UUID на 36 символов, а в прошивке буфер id[24]: она бы
        обрезала молча, и diff перестал бы различать сессии. Плюс 36 байт на карточку
        в строке снэпшота — дорого при LINE_MAX и StaticJsonDocument на ESP.
        Длину подбираем по факту: минимальную, при которой все видимые id различимы.
        """
        ids = [s.session_id for s in visible]
        for n in range(want, limit + 1):
            cut = {sid: sid[:n] for sid in ids}
            if len(set(cut.values())) == len(set(ids)):
                return cut
        return {sid: sid[:limit] for sid in ids}

    def _disambiguate(self, visible: list[Session]) -> list[dict]:
        """Готовит карточки; при совпадении имён (несколько сессий в одном репо/
        worktree) добавляет короткий суффикс из session_id, чтобы различать."""
        names = [s.name for s in visible]
        dups = {n for n in names if names.count(n) > 1}
        short = self.short_ids(visible)
        out = []
        for s in visible:
            name = s.name
            if name in dups:
                suffix = "#" + s.session_id[:4]
                base = shorten_middle(name, max(1, self.cfg.name_max - len(suffix)))
                name = base + suffix
            item = {"id": short[s.session_id], "name": name, "state": s.state}
            if s.subagents:
                item["sub"] = min(s.subagents, 5)   # число суб-агентов (кап под экран)
            if s.size_mb >= 1:
                # вес в целых МБ: копоть по краям карточки квантуется всё равно грубо,
                # а на ESP каждый байт снэпшота — это RAM под JSON-документ
                item["mb"] = int(round(s.size_mb))
            out.append(item)
        return out

    # -- управление состоянием (эндпоинты /reset, /sessions/<id>) --------------
    def reset(self) -> int:
        """Забывает все сессии и тут же пересобирает состав из реестра.

        Аварийная кнопка: состояние моста эфемерно, живые сессии всё равно вернутся
        (реестр — сразу же, остальные — с первым хуком), а накопленный мусор уходит.
        """
        with self.lock:
            n = len(self.sessions)
            self.sessions.clear()
        self.log.info("reset: забыто сессий: %d", n)
        self.mark_dirty()
        self.reconcile()
        return n

    def mute(self, session_id: str) -> bool:
        """Убирает одну карточку с экрана до следующей активности сессии.

        Именно скрыть, а не удалить: удалённую сессию сверка вернёт обратно в
        пределах OCTO_REGISTRY_SEC (она же есть в реестре), и кнопка выглядела бы
        сломанной. Заглушка снимается сама, когда сессия подаст признак жизни —
        любой хук или свежий статус в реестре поднимут last_active_ms выше метки.
        Мусор, которого в реестре нет, всё равно исчезнет: его добьёт сверка.
        """
        with self.lock:
            sess = self.sessions.get(session_id)
            if sess is None:
                return False
            sess.muted_ms = max(sess.last_active_ms, self._wall() * 1000)
        self.log.info("скрыта вручную: %s (%s)", session_id, sess.name)
        self.mark_dirty()
        return True

    # -- полный дамп состояния (эндпоинт /debug) ------------------------------
    def build_debug(self) -> dict:
        """Всё, что нужно, чтобы разобраться в поведении моста снаружи.

        Появилось не от скуки: /status отдаёт только те 6 карточек, что и так на
        экране, поэтому «почему видно не то» снаружи было не диагностируемо.
        """
        now, now_ms = self._clock(), self._wall() * 1000
        visible, hidden = self.select_visible()
        shown = {s.session_id for s in visible}
        reasons = {s.session_id: r for s, r in hidden}

        def dump(sess: Session) -> dict:
            return {
                "id": sess.session_id,
                "name": sess.name,
                "registry_name": sess.reg_name,
                "state": sess.state,
                "state_name": {WORKING: "WORKING", WAITING: "WAITING",
                               IDLE: "IDLE", ERROR: "ERROR"}.get(sess.state, "?"),
                "pid": sess.pid,
                "pid_alive": self._is_alive(sess.pid) if sess.pid is not None else None,
                "subagents": sess.subagents,
                "size_mb": sess.size_mb,
                "transcript": sess.transcript,
                "source": sess.source,
                "muted": bool(sess.muted_ms and sess.last_active_ms <= sess.muted_ms),
                "age_sec": round(now - sess.first_seen, 1),
                "last_event_sec_ago": round(now - sess.last_event, 1),
                "idle_hours": (round((now_ms - sess.last_active_ms) / 3_600_000, 2)
                               if sess.last_active_ms else None),
                "visible": sess.session_id in shown,
                "hidden_reason": reasons.get(sess.session_id),
            }

        with self.lock:
            sessions = list(self.sessions.values())
        sink_status = getattr(self.sink, "status", None)
        return {
            "v": 1,
            "bridge": {
                "pid": os.getpid(),
                "uptime_sec": round((now_ms - self._started_ms) / 1000, 1),
                "sessions_total": len(sessions),
                "visible": len(visible),
                "push_n": self._push_n,
                "last_push_sec_ago": (round(now - self._last_push, 1) if self._last_push else None),
                "reconcile_n": self._reconcile_n,
                "last_reconcile_sec_ago": (round(now - self._last_reconcile, 1)
                                           if self._last_reconcile else None),
                "registry_ok": self._registry_ok,
                "registry_sessions": self._registry_n,
                "sink": sink_status() if callable(sink_status) else type(self.sink).__name__,
                # что сейчас на экране и как он себя чувствует
                "screen": self.screen,
                "page": min(self.page, max(0, self.page_count() - 1)) + 1,
                "pages": self.page_count(),
                "sleeping": self.sleeping,
                "quiet_sec": round(now - self._last_activity, 1),
                "encoder_events": self._enc_n,
                # рулетка: чем крутили и что выпало — иначе «почему опять оно»
                # снаружи не разобрать
                "places": list(self.places),
                "roulette_spins": self.roul_spin,
                "roulette_win": (self.places[self.roul_win]
                                 if 0 <= self.roul_win < len(self.places) else None),
            },
            "config": {
                "max_sessions": self.cfg.max_sessions,
                "stale_idle_hours": self.cfg.stale_idle_hours,
                "stale_any_hours": self.cfg.stale_any_hours,
                "registry_sec": self.cfg.registry_sec,
                "registry_root": registry_root(self.cfg.registry_root),
                "reconcile_grace_sec": self.cfg.reconcile_grace_sec,
                "heartbeat_sec": self.cfg.heartbeat_sec,
                "reaper_sec": self.cfg.reaper_sec,
                "log_file": self.cfg.log_file,
                "mock": self.cfg.mock,
                "diag": self.cfg.diag,
                "sleep_min": self.cfg.sleep_min,
            },
            "sessions": [dump(s) for s in sorted(sessions, key=lambda s: -s.last_active_ms)],
            "snapshot": self.build_snapshot(),
        }

    def snapshot_line(self) -> str:
        return json.dumps(self.build_snapshot(), separators=(",", ":"), ensure_ascii=False) + "\n"

    _STATE_CH = {WORKING: "W", WAITING: "?", IDLE: "I", ERROR: "E"}

    def push(self, reason: str = "manual") -> bool:
        line = self.snapshot_line()
        ok = self.sink.send(line)
        # счётчик и метка времени — всегда: /debug показывает их и без OCTO_DIAG
        now = self._clock()
        dt_ms = (now - self._last_push) * 1000.0 if self._last_push else 0.0
        self._last_push = now
        self._push_n += 1
        if self.cfg.diag:
            self._log_push(reason, line, ok, dt_ms)
        return ok

    def _log_push(self, reason: str, line: str, ok: bool, dt_ms: float) -> None:
        """Диаг-строка пуша: интервал, число сессий, статусы, дошло ли до serial.

        Именно здесь ловится «моргнуло всё»: если n внезапно 0/меньше при
        reason=heartbeat/event — это мост урезал снэпшот (reaper?), а не ESP.
        """
        sessions = json.loads(line).get("sessions", [])
        summary = ",".join(
            f"{s['name']}:{self._STATE_CH.get(s['state'], '?')}"
            + (f"+{s['sub']}" if s.get("sub") else "")
            for s in sessions
        )
        self.log.info(
            "PUSH #%d reason=%s dt=%.0fms n=%d serial=%s [%s]",
            self._push_n, reason, dt_ms, len(sessions), "ok" if ok else "FAIL", summary,
        )

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
            self.push("event" if triggered else "heartbeat")

    def reaper_loop(self) -> None:  # pragma: no cover
        next_reconcile = 0.0
        while not self._stop.wait(self.cfg.reaper_sec):
            self.reap()
            self.maybe_sleep()
            # Вес считаем в такт reaper'у, а не сверки: он нужен и когда реестр
            # выключен (OCTO_REGISTRY_SEC=0), а стоит один os.stat на сессию.
            self.refresh_sizes()
            if self.cfg.registry_sec > 0 and self._clock() >= next_reconcile:
                next_reconcile = self._clock() + self.cfg.registry_sec
                try:
                    self.reconcile()
                except Exception:
                    # реестр — чужой недокументированный формат; его поломка не
                    # должна валить поток, мост продолжает жить на хуках
                    self.log.exception("сверка с реестром упала — работаем по хукам")

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
            return
        if self.cfg.registry_sec > 0:
            # сразу поднимаем состав из реестра: после рестарта моста картинка
            # появляется мгновенно, не дожидаясь первого хука в каждой сессии
            try:
                self.reconcile()
            except Exception:
                self.log.exception("стартовая сверка с реестром не удалась")
        threading.Thread(target=self.reaper_loop, name="reaper", daemon=True).start()

    def stop(self) -> None:  # pragma: no cover
        self._stop.set()
        self._dirty.set()


# --- Веб-морда ----------------------------------------------------------------
# Одна самодостаточная страница: никаких внешних CSS/JS/шрифтов — мост не должен
# ходить в сеть и не должен зависеть от неё, чтобы показать своё состояние.
# Данные берёт из того же /debug, что и octoctl, опрашивая его раз в 2с.
UI_HTML = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>OctoDash bridge</title>
<style>
  :root{--bg:#12161c;--panel:#1a2028;--line:#2a3340;--txt:#dfe6ee;--dim:#8494a6;
        --work:#4ec9a0;--wait:#e3c05c;--idle:#6d7f92;--err:#e06c6c;--acc:#7aa7d8}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--txt);
       font:14px/1.5 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
  header{padding:14px 18px;border-bottom:1px solid var(--line);display:flex;
         align-items:baseline;gap:14px;flex-wrap:wrap}
  h1{font-size:15px;margin:0;letter-spacing:.08em;text-transform:uppercase}
  .stamp{color:var(--dim);font-size:12px;margin-left:auto}
  .wrap{padding:18px;max-width:1000px;margin:0 auto}
  .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));
        gap:10px;margin-bottom:18px}
  .box{background:var(--panel);border:1px solid var(--line);border-radius:6px;padding:10px 12px}
  .box b{display:block;font-size:19px;font-weight:600}
  .box span{color:var(--dim);font-size:11px;text-transform:uppercase;letter-spacing:.06em}
  .bar{display:flex;gap:8px;margin-bottom:14px;flex-wrap:wrap}
  button{background:var(--panel);color:var(--txt);border:1px solid var(--line);
         border-radius:5px;padding:7px 13px;cursor:pointer;font:inherit;font-size:13px}
  button:hover{border-color:var(--acc);color:var(--acc)}
  button.danger:hover{border-color:var(--err);color:var(--err)}
  table{width:100%;border-collapse:collapse}
  th{text-align:left;color:var(--dim);font-weight:400;font-size:11px;
     text-transform:uppercase;letter-spacing:.06em;padding:6px 8px;border-bottom:1px solid var(--line)}
  td{padding:7px 8px;border-bottom:1px solid var(--line);vertical-align:middle}
  tr.hidden td{opacity:.45}
  .dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:7px}
  .s0{background:var(--work)}.s1{background:var(--wait)}.s2{background:var(--idle)}.s3{background:var(--err)}
  .st0{color:var(--work)}.st1{color:var(--wait)}.st2{color:var(--idle)}.st3{color:var(--err)}
  .why{color:var(--dim);font-size:12px}
  .sub{color:var(--acc);font-size:12px}
  .dead{color:var(--err)}
  .x{background:none;border:none;color:var(--dim);cursor:pointer;font-size:15px;padding:0 4px}
  .x:hover{color:var(--err)}
  .wire{background:#0d1116;border:1px solid var(--line);border-radius:6px;padding:10px 12px;
        color:var(--dim);font-size:12px;word-break:break-all;margin-top:16px}
  .offline{background:#3a1d1d;border:1px solid var(--err);color:#f0c0c0;
           padding:10px 14px;border-radius:6px;margin-bottom:14px;display:none}
</style></head><body>
<header>
  <h1>OctoDash <span style="color:var(--dim)">bridge</span></h1>
  <div id="meta" class="stamp"></div>
</header>
<div class="wrap">
  <div id="offline" class="offline">Мост не отвечает. Запущен ли bridge.py?</div>
  <div id="boxes" class="grid"></div>
  <div class="bar">
    <button onclick="act('/resync','POST')">Сверить с реестром</button>
    <button class="danger" onclick="reset()">Сбросить состав</button>
    <button onclick="tick()">Обновить</button>
  </div>
  <table>
    <thead><tr><th></th><th>сессия</th><th>статус</th><th>pid</th>
      <th>тишина</th><th>на экране</th><th></th></tr></thead>
    <tbody id="rows"></tbody>
  </table>
  <div id="wire" class="wire"></div>
</div>
<script>
const NAMES = ["WORKING","WAITING","IDLE","ERROR"];
const esc = s => String(s ?? "").replace(/[&<>"']/g, c =>
  ({"&":"&amp;","<":"&lt;",">":"&gt;","\\"":"&quot;","'":"&#39;"}[c]));
const idle = h => h === null || h === undefined ? "—"
  : (h < 1 ? Math.round(h * 60) + "м" : h.toFixed(1) + "ч");

function render(d) {
  const b = d.bridge, c = d.config, sink = typeof b.sink === "object" ? b.sink : {};
  document.getElementById("meta").textContent =
    "pid " + b.pid + " · uptime " + (b.uptime_sec / 3600).toFixed(1) + "ч · обновлено "
    + new Date().toLocaleTimeString("ru-RU");
  document.getElementById("boxes").innerHTML = [
    ["на экране", b.visible + " / " + c.max_sessions],
    ["всего сессий", b.sessions_total],
    ["serial", sink.connected ? esc(sink.resolved || sink.port) : "нет связи"],
    ["реестр", b.registry_ok === null ? "не сверялся"
      : (b.registry_ok ? b.registry_sessions + " сессий" : "недоступен")],
    ["пушей", b.push_n],
    ["скрывать после", c.stale_idle_hours + "ч / " + c.stale_any_hours + "ч"],
  ].map(([k, v]) => "<div class=box><span>" + k + "</span><b>" + v + "</b></div>").join("");

  document.getElementById("rows").innerHTML = d.sessions.map(s => {
    const pid = s.pid === null ? "—"
      : esc(s.pid) + (s.pid_alive === false ? " <span class=dead>†</span>" : "");
    return "<tr class=" + (s.visible ? "" : "hidden") + ">"
      + "<td><span class='dot s" + s.state + "'></span></td>"
      + "<td>" + esc(s.registry_name || s.name)
      + (s.subagents ? " <span class=sub>+" + s.subagents + "</span>" : "")
      + "<br><span class=why>" + esc(s.id.slice(0, 8)) + "</span></td>"
      + "<td class='st" + s.state + "'>" + NAMES[s.state] + "</td>"
      + "<td>" + pid + "</td><td>" + idle(s.idle_hours) + "</td>"
      + "<td class=why>" + (s.visible ? "да" : esc(s.hidden_reason || "нет слота")) + "</td>"
      + "<td><button class=x title='скрыть до следующей активности' onclick=\\"hide('"
      + esc(s.id) + "')\\">✕</button></td></tr>";
  }).join("") || "<tr><td colspan=7 class=why>сессий нет</td></tr>";

  document.getElementById("wire").textContent = "на ESP → " + JSON.stringify(d.snapshot);
}

async function tick() {
  try {
    const r = await fetch("/debug", {cache: "no-store"});
    render(await r.json());
    document.getElementById("offline").style.display = "none";
  } catch (e) {
    document.getElementById("offline").style.display = "block";
  }
}
async function act(path, method) { try { await fetch(path, {method}); } catch (e) {} tick(); }
function hide(id) { act("/sessions/" + encodeURIComponent(id), "DELETE"); }
function reset() {
  if (confirm("Забыть все сессии? Состав сразу пересоберётся из реестра."))
    act("/reset", "POST");
}
tick();
setInterval(tick, 2000);
</script></body></html>
"""


def wants_html(accept: str | None) -> bool:
    """Браузер просит text/html, curl — */*. По этому и различаем на `/`.

    Так `http://localhost:8787` в браузере открывает морду, а привычный
    `curl localhost:8787` продолжает отдавать снэпшот.
    """
    return "text/html" in (accept or "")


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

    def _drain_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", "0"))
        return self.rfile.read(length) if length else b""

    def _do_shot(self) -> None:
        import tempfile
        path = os.path.join(tempfile.gettempdir(), "octodash-shot.png")
        saved = self.bridge.request_shot(path)
        shot = self.bridge.shot
        self._respond(200 if saved else 503,
                      {"ok": bool(saved), "path": saved or "",
                       "w": shot.width, "h": shot.height,
                       "complete": shot.complete, "tiles": shot.tiles,
                       "missing_rows": shot.missing_rows})

    def do_POST(self) -> None:
        if self.path == "/event":
            try:
                self.bridge.handle_event(parse_event_body(self._drain_body()))
            except Exception as exc:
                # хук никогда не должен «зависнуть» — логируем и всё равно 200
                self.bridge.log.warning("ошибка обработки /event: %s", exc)
            self._respond(200, {"ok": True})
            return

        if self.path == "/enc":
            # Ручку иначе не дёрнуть без рук, а страницы, экраны и сон живут
            # только за ней — без этого их нельзя ни проверить, ни отладить.
            body = self._drain_body()
            try:
                data = json.loads(body or b"{}")
            except ValueError:
                data = {}
            ev = str(data.get("enc", ""))
            if ev not in ("cw", "ccw", "key", "hold"):
                self._respond(400, {"ok": False, "error": "enc: cw|ccw|key|hold"})
                return
            self.bridge.handle_encoder(ev, held=bool(data.get("held") or data.get("k")))
            self._respond(200, {"ok": True, "screen": self.bridge.screen,
                                "page": self.bridge.page,
                                "pages": self.bridge.page_count(),
                                "sleeping": self.bridge.sleeping})
            return

        # --- управление (локальный порт, поэтому без авторизации) ---
        body = self._drain_body()
        if self.path == "/shot":
            self._do_shot()
            return
        if self.path == "/cmd":
            # Отладочная команда прямо на плату. Нужна, чтобы проверять то, что
            # иначе воспроизводится только руками (физика барабана рулетки).
            try:
                data = json.loads(body or b"{}")
            except ValueError:
                data = {}
            cmd = str(data.get("cmd", ""))
            if not cmd.isalnum():
                self._respond(400, {"ok": False, "error": "cmd: буквы и цифры"})
                return
            n = max(1, min(60, int(data.get("n", 1) or 1)))
            ok = all(self.bridge.sink.send('{"cmd":"%s"}\n' % cmd) for _ in range(n))
            self._respond(200 if ok else 503, {"ok": ok, "cmd": cmd, "n": n})
            return
        if self.path == "/resync":
            changed = self.bridge.reconcile()
            self._respond(200, {"ok": True, "changed": changed,
                                "sessions": self.bridge.build_debug()["bridge"]})
        elif self.path == "/reset":
            forgotten = self.bridge.reset()
            self._respond(200, {"ok": True, "forgotten": forgotten,
                                "sessions": self.bridge.build_debug()["bridge"]})
        else:
            self._respond(404, {"ok": False, "error": "not found"})

    def _respond_html(self, body: str) -> None:
        raw = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        if self.path == "/ui":
            self._respond_html(UI_HTML)
        elif self.path == "/":
            # браузер → морда, curl → снэпшот (см. wants_html)
            if wants_html(self.headers.get("Accept")):
                self._respond_html(UI_HTML)
            else:
                self._respond(200, self.bridge.build_snapshot())
        elif self.path == "/status":
            self._respond(200, self.bridge.build_snapshot())
        elif self.path == "/debug":
            self._respond(200, self.bridge.build_debug())
        elif self.path == "/favicon.ico":
            # иконки нет, но и 404 в консоли браузера не нужен — там должно быть чисто,
            # чтобы реальная ошибка сразу бросалась в глаза
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
        else:
            self._respond(404, {"ok": False})

    def do_DELETE(self) -> None:
        prefix = "/sessions/"
        if self.path.startswith(prefix) and len(self.path) > len(prefix):
            sid = self.path[len(prefix):]
            if self.bridge.mute(sid):
                self._respond(200, {"ok": True, "hidden": sid})
            else:
                self._respond(404, {"ok": False, "error": "unknown session"})
        else:
            self._respond(404, {"ok": False})

    def log_message(self, fmt, *args):  # заглушаем дефолтный шумный лог
        logging.getLogger("octo.http").debug(fmt, *args)


def bind_singleton(cfg: Config) -> ThreadingHTTPServer | None:
    """Биндим порт. Успех → мы единственный инстанс. EADDRINUSE → мост уже есть."""
    # SO_REUSEADDR только на POSIX. Там он не ломает singleton (два LISTEN на одном
    # порту невозможны и с ним), зато разрешает бинд при висящих TIME_WAIT от
    # закрытых соединений — без этого перезапуск моста падал с EADDRINUSE на минуту.
    # На Windows включать нельзя: там второй инстанс успешно забиндится поверх.
    ThreadingHTTPServer.allow_reuse_address = os.name != "nt"
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


def write_png(path: str, width: int, height: int, rgb565: list[int]) -> str:
    """Пишет PNG из пикселей RGB565. Без внешних зависимостей — только zlib.

    Нужно для отладочных снимков с платы: прошивка не может прочитать панель
    (MISO не разведён), но отдаёт то, что сама собрала в буфер. Картинку смотрит
    человек или ассистент — так визуальные ошибки видно, не заливая прошивку.
    """
    import struct
    import zlib

    raw = bytearray()
    for y in range(height):
        raw.append(0)                                  # фильтр строки: none
        row = rgb565[y * width:(y + 1) * width]
        for c in row:
            r5, g6, b5 = (c >> 11) & 31, (c >> 5) & 63, c & 31
            raw += bytes(((r5 << 3) | (r5 >> 2), (g6 << 2) | (g6 >> 4), (b5 << 3) | (b5 >> 2)))
        if len(row) < width:                           # плитка не пришла целиком
            raw += bytes(3 * (width - len(row)))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xffffffff))

    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(bytes(raw), 6))
           + chunk(b"IEND", b""))
    with open(path, "wb") as f:
        f.write(png)
    return path


class ShotCollector:
    """Собирает отладочный снимок из плиток, которые присылает прошивка.

    Формат: {"esp":"shot","w":..,"h":..} → на каждую плитку
    {"esp":"tile","x":..,"y":..,"w":..,"h":..} и h строк, в конце
    {"esp":"shot_end"}. Строка приходит в одном из трёх видов — прошивка на
    каждую выбирает самый короткий, потому что сырой hex это 307 КБ на экран
    (~27с при 115200, снимок не успевал дойти):
      `<hex>`  — 4 hex-цифры на пиксель;
      `L<hex>` — серии: 2 цифры длина + 4 цифры цвет;
      `#N`     — следующие N строк совпадают с предыдущей.
    Непонятные строки игнорируются: канал общий с диагностикой, мусор — норма.
    """

    def __init__(self):
        self.width = self.height = 0
        self.pixels: list[int] = []
        self.tile: dict | None = None
        self.row = 0
        self.prev: list[int] = []
        self.missing_rows = 0          # строки, которых не хватило плиткам
        self.tiles = 0
        self.done = threading.Event()

    def start(self) -> None:
        self.width = self.height = 0
        self.pixels = []
        self.tile = None
        self.row = 0
        self.prev = []
        self.missing_rows = 0
        self.tiles = 0
        self.done.clear()

    @property
    def complete(self) -> bool:
        """Снимок целый: пришёл финал и все плитки досыпали свои строки.

        Важно отличать неполный снимок от бага отрисовки: обрезанная картинка
        выглядит как «пропали осьминоги», и один раз уже сбила с толку.
        """
        return self.done.is_set() and self.missing_rows == 0 and self.tiles > 0

    def _close_tile(self) -> None:
        if self.tile is not None:
            self.missing_rows += max(0, int(self.tile["h"]) - self.row)

    def feed(self, line: str) -> None:
        line = line.strip()
        if line.startswith("{"):
            try:
                d = json.loads(line)
            except ValueError:
                return
            kind = d.get("esp")
            if kind == "shot":
                self.width, self.height = int(d.get("w", 0)), int(d.get("h", 0))
                self.pixels = [0] * (self.width * self.height)
            elif kind == "tile":
                self._close_tile()
                self.tile = d
                self.tiles += 1
                self.row = 0
                self.prev = []             # повтор строки не переходит между плитками
            elif kind == "shot_end":
                self._close_tile()
                self.tile = None
                self.done.set()
            return
        if self.tile is None or not self.pixels:
            return
        if line.startswith("#"):                       # повтор предыдущей строки
            if not self.prev:
                return
            try:
                n = int(line[1:])
            except ValueError:
                return
            for _ in range(max(0, n)):
                self._put(self.prev)
            return
        try:
            if line.startswith("L"):
                body, vals = line[1:], []
                for i in range(0, len(body) - 5, 6):
                    run, color = int(body[i:i + 2], 16), int(body[i + 2:i + 6], 16)
                    vals += [color] * run
            else:
                vals = [int(line[i:i + 4], 16) for i in range(0, len(line) - 3, 4)]
        except ValueError:
            return
        self._put(vals)

    def _put(self, vals: list[int]) -> None:
        y = int(self.tile["y"]) + self.row
        x0 = int(self.tile["x"])
        if 0 <= y < self.height:
            base = y * self.width + x0
            for i, v in enumerate(vals):
                if x0 + i < self.width:
                    self.pixels[base + i] = v
        self.prev = vals
        self.row += 1

    def save(self, path: str) -> str | None:
        if not self.pixels or not self.width:
            return None
        return write_png(path, self.width, self.height, self.pixels)


def parse_esp_line(line: str) -> dict | None:
    """Разбирает строку обратного канала. Возвращает событие энкодера или None.

    Формат чужой и может меняться, поэтому правило то же, что с реестром сессий:
    непонятное молча пропускаем, мост из-за этого не падает. Диагностические
    маркеры ESP (boot/stat/badjson) сюда попадают и отсеиваются как «не команда».
    """
    line = (line or "").strip()
    if not line.startswith("{"):
        return None
    try:
        data = json.loads(line)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    enc = data.get("enc")
    if not isinstance(enc, str):
        return None
    return {"enc": enc.lower(), "held": bool(data.get("k"))}


def esp_reader_loop(sink: SerialSink, stop: threading.Event, bridge: "Bridge | None" = None,
                    poll_sec: float = 0.05) -> None:  # pragma: no cover
    """Слушает обратный канал: команды энкодера и диагностику ESP.

    Читается ВСЕГДА, а не только в OCTO_DIAG: по этому каналу приходит ручка,
    без него энкодер не работает. Опрос частый — щелчок должен отзываться сразу.
    """
    log = logging.getLogger("octo.esp")
    diag = logging.getLogger("octo").isEnabledFor(logging.DEBUG)
    while not stop.wait(poll_sec):
        for line in sink.read_lines():
            if bridge is not None and not bridge.shot.done.is_set():
                bridge.shot.feed(line)          # идёт сбор снимка — строки его
            cmd = parse_esp_line(line)
            if cmd is None:
                if diag:
                    log.info("← ESP: %s", line)
                continue
            log.info("← ESP: %s%s", cmd["enc"], " (с кнопкой)" if cmd["held"] else "")
            if bridge is not None and bridge.handle_encoder(cmd["enc"], cmd["held"]):
                bridge.push("encoder")


def setup_log_handlers(cfg: Config) -> tuple[list[logging.Handler], str]:
    """Хендлеры логов: stderr + файл с ротацией. Возвращает (хендлеры, путь файла).

    Файл включён по умолчанию: мост живёт неделями отвязанным от терминала, и без
    файла его диагностика теряется целиком (проверено — было ровно так).
    """
    from logging.handlers import RotatingFileHandler

    handlers: list[logging.Handler] = [logging.StreamHandler()]
    path = cfg.log_file or default_log_path()
    if cfg.log_file == "-":
        return handlers, ""
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        handlers.append(RotatingFileHandler(
            path, maxBytes=cfg.log_max_bytes, backupCount=cfg.log_backups, encoding="utf-8"))
    except OSError as exc:      # каталог не создать / нет прав — логи в файл не критичны
        logging.getLogger("octo").warning("не удалось открыть лог-файл %s: %s", path, exc)
        return handlers, ""
    return handlers, path


def main() -> int:  # pragma: no cover
    cfg = Config()
    handlers, log_path = setup_log_handlers(cfg)
    logging.basicConfig(
        level=logging.DEBUG if (os.environ.get("OCTO_DEBUG") or cfg.diag) else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
    )
    log = logging.getLogger("octo")

    server = bind_singleton(cfg)
    if server is None:
        log.info("мост уже запущен на %s:%d — выходим", cfg.host, cfg.port)
        return 0

    sink = SerialSink(cfg.serial_port, cfg.serial_baud, no_reset=cfg.serial_no_reset)
    bridge = Bridge(cfg, sink=sink)
    server.bridge = bridge  # type: ignore[attr-defined]
    bridge.start_background()

    esp_stop = threading.Event()
    if not cfg.mock:
        # Читаем обратный канал всегда: по нему приходит энкодер, а не только диагностика.
        threading.Thread(
            target=esp_reader_loop, args=(sink, esp_stop, bridge), name="esp-reader", daemon=True
        ).start()

    mode = "MOCK" if cfg.mock else "live"
    log.info(
        "мост слушает %s:%d (%s), serial=%s @ %d%s%s, реестр=%s, stale=%gч/%gч",
        cfg.host, cfg.port, mode, cfg.serial_port, cfg.serial_baud,
        " DIAG" if cfg.diag else "", f" log→{log_path}" if log_path else "",
        f"каждые {cfg.registry_sec:g}с" if cfg.registry_sec > 0 else "выключен",
        cfg.stale_idle_hours, cfg.stale_any_hours,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("остановка по Ctrl-C")
    finally:
        esp_stop.set()
        bridge.stop()
        server.shutdown()
        server.server_close()
        sink.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
