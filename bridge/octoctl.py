#!/usr/bin/env python3
"""octoctl — управление запущенным мостом OctoDash.

Мост живёт неделями отвязанным от терминала, поэтому «посмотреть, что внутри» и
«починить состав карточек» нужно снаружи, без перезапуска и без чтения кода:

    octoctl status          что сейчас на экране (короткая сводка)
    octoctl debug           полное состояние: все сессии, PID, почему скрыты
    octoctl debug --json    то же машинно-читаемо
    octoctl resync          форс-сверка с реестром сессий Claude Code
    octoctl reset           забыть все сессии и пересобрать состав из реестра
    octoctl hide <id>       убрать карточку с экрана до её следующей активности
    octoctl restart         перезапустить процесс моста

Никаких зависимостей: только стандартная библиотека.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request

HOST = os.environ.get("OCTO_BRIDGE_HOST", "127.0.0.1")
PORT = os.environ.get("OCTO_BRIDGE_PORT", "8787")
BASE = f"http://{HOST}:{PORT}"
STATE_CH = {0: "WORKING", 1: "WAITING", 2: "IDLE", 3: "ERROR"}


def call(path: str, method: str = "GET", timeout: float = 5.0) -> dict:
    req = urllib.request.Request(f"{BASE}{path}", method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


# Консоль Windows часто cp1251: без этого любой не-ASCII вывод валит команду
# UnicodeEncodeError'ом уже после того, как работа сделана.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):      # не TextIO (перехвачен в тестах) — не важно
        pass


def die(msg: str) -> int:
    print(msg, file=sys.stderr)
    return 1


def fmt_sessions(dbg: dict) -> str:
    rows = [f"{'':1} {'id':<9} {'имя':<17} {'статус':<8} {'pid':>8} {'idle':>7}  причина"]
    for s in dbg.get("sessions", []):
        mark = "•" if s.get("visible") else " "
        pid = s.get("pid")
        alive = s.get("pid_alive")
        pid_txt = "—" if pid is None else f"{pid}{'' if alive else '†'}"
        idle = s.get("idle_hours")
        idle_txt = "—" if idle is None else (f"{idle * 60:.0f}м" if idle < 1 else f"{idle:.1f}ч")
        subs = f" +{s['subagents']}sub" if s.get("subagents") else ""
        rows.append(
            f"{mark} {s['id'][:8]:<9} {str(s.get('registry_name') or s['name'])[:17]:<17} "
            f"{s.get('state_name', '?'):<8} {pid_txt:>8} {idle_txt:>7}  "
            f"{s.get('hidden_reason') or ''}{subs}"
        )
    return "\n".join(rows)


def fmt_head(dbg: dict) -> str:
    b, c = dbg.get("bridge", {}), dbg.get("config", {})
    sink = b.get("sink") if isinstance(b.get("sink"), dict) else {}
    reg = {True: "ok", False: "НЕДОСТУПЕН", None: "не сверялся"}[b.get("registry_ok")]
    return (
        f"мост pid={b.get('pid')} uptime={b.get('uptime_sec', 0) / 3600:.1f}ч  "
        f"сессий={b.get('sessions_total')} на экране={b.get('visible')}/{c.get('max_sessions')}\n"
        f"пушей={b.get('push_n')} (последний {b.get('last_push_sec_ago')}с назад)  "
        f"сверок={b.get('reconcile_n')} реестр={reg} ({b.get('registry_sessions')} сессий)\n"
        f"serial={sink.get('resolved') or sink.get('port') or sink}"
        f" {'подключён' if sink.get('connected') else 'НЕ подключён'}"
        f"  stale_idle={c.get('stale_idle_hours')}ч  лог={c.get('log_file') or '(дефолт)'}"
    )


def resolve_id(dbg: dict, prefix: str) -> str | None:
    hits = [s["id"] for s in dbg.get("sessions", []) if s["id"].startswith(prefix)]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        return None
    print(f"префикс {prefix!r} неоднозначен: {', '.join(h[:8] for h in hits)}", file=sys.stderr)
    return None


def find_bridge_pid() -> int | None:
    """PID процесса, слушающего порт моста.

    Нужен для restart, когда работающий мост — старой версии и про /debug ещё не
    знает (ровно то, что происходит при обновлении кода).
    """
    try:
        import psutil
    except ImportError:
        return None
    try:
        for conn in psutil.net_connections(kind="inet"):
            if (conn.pid and conn.status == psutil.CONN_LISTEN
                    and conn.laddr and conn.laddr.port == int(PORT)):
                return conn.pid
    except Exception:
        return None
    return None


def bridge_pid() -> int | None:
    """PID моста: сначала спрашиваем сам мост, иначе ищем слушателя порта."""
    try:
        return call("/debug").get("bridge", {}).get("pid") or find_bridge_pid()
    except urllib.error.HTTPError:
        return find_bridge_pid()      # мост живой, но без /debug — старая версия


def spawn_bridge() -> int:
    """Запускает мост отвязанным от терминала (как это делает octo-run.sh)."""
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bridge.py")
    if os.name == "nt":  # pragma: no cover - Windows
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        subprocess.Popen([sys.executable, script], creationflags=flags,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        subprocess.Popen([sys.executable, script], start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return 0


def cmd_status() -> int:
    snap = call("/status")
    sessions = snap.get("sessions", [])
    print(f"на экране {len(sessions)} карточек:")
    for s in sessions:
        subs = f" +{s['sub']}sub" if s.get("sub") else ""
        print(f"  {s['name']:<17} {STATE_CH.get(s['state'], '?')}{subs}")
    return 0


def cmd_debug(as_json: bool) -> int:
    dbg = call("/debug")
    if as_json:
        print(json.dumps(dbg, ensure_ascii=False, indent=2))
        return 0
    print(fmt_head(dbg))
    print()
    print(fmt_sessions(dbg))
    print("\n(• = на экране; † = PID мёртв)")
    return 0


def cmd_resync() -> int:
    r = call("/resync", "POST")
    print(f"сверка выполнена, изменения: {'да' if r.get('changed') else 'нет'}")
    return cmd_status()


def cmd_reset() -> int:
    r = call("/reset", "POST")
    print(f"забыто сессий: {r.get('forgotten')}; состав пересобран из реестра")
    return cmd_status()


def cmd_lunch(action: str | None) -> int:
    """Журнал обедов: что уже пройдено в круге, сброс и отмена последнего результата.

    Ручкой журнал не сбрасывается намеренно — случайный двойной клик не должен
    стирать историю за две недели, поэтому это живёт только тут и в веб-морде.
    """
    if action == "reset":
        r = call("/lunch/reset", "POST")
        print(f"журнал обедов сброшен ({r.get('forgotten')} мест), "
              f"в круге снова {r.get('left')}")
        return 0
    if action == "undo":
        r = call("/lunch/undo", "POST")
        if not r.get("ok"):
            return die("отменять нечего: журнал пуст")
        print(f"{r.get('place')} снова в круге, осталось {r.get('left')}")
        return 0
    dbg = call("/debug").get("bridge", {})
    places = dbg.get("places") or []
    visited = dbg.get("lunch_visited") or []
    pending = dbg.get("lunch_pending")
    print(f"круг: осталось {dbg.get('lunch_left')} из {len(places)}")
    if pending:
        print(f"сегодня ({dbg.get('lunch_day')}): {pending}")
    print("пройдено: " + (", ".join(visited) if visited else "ничего"))
    left = [x for x in places if x not in visited and x != pending]
    print("впереди: " + (", ".join(left) if left else "круг закрыт, сбросится сам"))
    return 0


def cmd_hide(prefix: str) -> int:
    sid = resolve_id(call("/debug"), prefix)
    if sid is None:
        return die(f"сессия {prefix!r} не найдена")
    call(f"/sessions/{sid}", "DELETE")
    print(f"скрыта {sid[:8]} — вернётся на экран, когда сессия проявит активность")
    return 0


def cmd_shot() -> int:
    """Снимок экрана с платы: плата собирает картинку и высыпает её в serial.

    Панель по SPI не читается (MISO не разведён), поэтому «фотографирует» себя
    сама прошивка — тем же кодом композиции, что рисует экран.
    """
    r = call("/shot", "POST", timeout=90)
    if not r.get("ok"):
        return die("снимок не получился — подключена ли плата и свежая ли прошивка?")
    print(f"снимок {r['w']}x{r['h']}: {r['path']}")
    if not r.get("complete", True):
        # неполный снимок выглядит как пропавшие карточки — молчать об этом нельзя
        print(f"ВНИМАНИЕ: снимок НЕПОЛНЫЙ (плиток {r.get('tiles')}, "
              f"не хватило строк {r.get('missing_rows')}) — это обрыв связи, не баг отрисовки")
        return 2
    return 0


def cmd_restart() -> int:
    try:
        pid = bridge_pid()
    except urllib.error.URLError:        # HTTPError разобран внутри bridge_pid
        print("мост не отвечает — просто запускаю новый")
        return spawn_bridge()
    if not pid:
        return die("мост слушает порт, но PID не определить (нет psutil?) — "
                   "останови процесс bridge.py вручную и запусти заново")
    os.kill(pid, signal.SIGTERM)
    for _ in range(50):                      # ждём освобождения порта (singleton по бинду)
        time.sleep(0.1)
        try:
            os.kill(pid, 0)
        except OSError:
            break
    spawn_bridge()
    time.sleep(1.0)
    try:
        new_pid = call("/debug").get("bridge", {}).get("pid")
    except urllib.error.URLError:
        return die("новый мост не поднялся — посмотри лог")
    print(f"мост перезапущен: {pid} -> {new_pid}")
    return cmd_status()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="octoctl", description="управление мостом OctoDash")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("status", help="что сейчас на экране")
    d = sub.add_parser("debug", help="полное состояние моста")
    d.add_argument("--json", action="store_true", help="машинно-читаемый вывод")
    sub.add_parser("resync", help="форс-сверка с реестром сессий Claude Code")
    sub.add_parser("reset", help="забыть все сессии и пересобрать из реестра")
    hide = sub.add_parser("hide", aliases=["rm"], help="убрать карточку с экрана")
    hide.add_argument("session", help="id сессии или его префикс")
    lunch = sub.add_parser("lunch", help="журнал обедов: что пройдено, сброс, отмена")
    lunch.add_argument("action", nargs="?", choices=["reset", "undo"],
                       help="reset — начать круг заново; undo — отменить последний результат")
    sub.add_parser("shot", help="снять экран платы в PNG (нужна прошивка с cmd shot)")
    sub.add_parser("restart", help="перезапустить процесс моста")
    args = p.parse_args(argv)

    try:
        if args.cmd == "status":
            return cmd_status()
        if args.cmd == "debug":
            return cmd_debug(args.json)
        if args.cmd == "resync":
            return cmd_resync()
        if args.cmd == "reset":
            return cmd_reset()
        if args.cmd == "lunch":
            return cmd_lunch(args.action)
        if args.cmd in ("hide", "rm"):
            return cmd_hide(args.session)
        if args.cmd == "shot":
            return cmd_shot()
        if args.cmd == "restart":
            return cmd_restart()
        p.print_help()
        return 1
    except urllib.error.HTTPError as exc:
        if exc.code == 404 and args.cmd in ("debug", "resync", "reset", "shot"):  # noqa: E501
            return die(f"мост не знает {args.cmd} — вероятно, запущен старой версией. "
                       f"Обнови его: octoctl restart")
        return die(f"мост ответил {exc.code}: {exc.reason}")
    except urllib.error.URLError as exc:
        return die(f"мост не отвечает на {BASE} ({exc.reason}). Запущен ли он?")


if __name__ == "__main__":
    sys.exit(main())
