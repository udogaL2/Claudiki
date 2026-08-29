#!/usr/bin/env python3
"""OctoDash hook wrapper.

Тонкая обёртка-хук Claude Code. Читает JSON-конверт хука со stdin, определяет
событие по hook_event_name, шлёт POST /event на мост.

Для SessionStart один раз за сессию определяется PID процесса claude (для liveness
в мосту): обходим дерево процессов вверх и берём ближайшего предка, чья командная
строка содержит "claude" (устойчиво к промежуточному шеллу). Фолбэк — os.getppid().
psutil импортируется лениво и только на SessionStart, чтобы частые события
(working/waiting/idle) оставались лёгкими.

Требования: быстрый, неблокирующий, короткий таймаут, ЛЮБЫЕ ошибки глотаются,
скрипт ВСЕГДА завершается кодом 0 — хук не должен мешать Claude.

Установка: положить в ~/.claude/hooks/octo-notify.py и зарегистрировать в
~/.claude/settings.json (см. README.md).
"""

import json
import os
import sys
import urllib.request

# hook_event_name Claude Code -> event моста
HOOK_TO_EVENT = {
    "SessionStart": "start",
    "UserPromptSubmit": "working",
    "PostToolUse": "working",   # тул отработал → снова активны (снимает «застревание» на WAITING)
    "Notification": "waiting",
    "Stop": "idle",
    "SessionEnd": "end",
    "StopFailure": "error",
    "SubagentStop": "subagent_done",   # суб-агент (Task) завершился → −1
}

HOST = os.environ.get("OCTO_BRIDGE_HOST", "127.0.0.1")
PORT = os.environ.get("OCTO_BRIDGE_PORT", "8787")
TIMEOUT = float(os.environ.get("OCTO_HOOK_TIMEOUT", "0.5"))


def _looks_like_claude(proc) -> bool:
    """True, если процесс — это долгоживущий claude, а не транзиентный шелл/хук.

    ВАЖНО: нельзя матчить по подстроке "claude" во всей cmdline — тогда ложно
    срабатывают предки, у которых в аргументах есть путь к каталогу конфига
    (~/.claude/shell-snapshots/..., ~/.claude/hooks/octo-notify.py). Именно такой
    шелл запускает хук и умирает сразу после — reaper убил бы карточку через ~2с.
    Поэтому смотрим на ИМЯ процесса и basename исполняемого файла/CLI-скрипта.
    """
    try:
        name = (proc.name() or "").lower()
    except Exception:
        name = ""
    if name in ("claude", "claude.exe"):
        return True  # нативный бинарь (Windows/standalone)
    try:
        argv = proc.cmdline()
    except Exception:
        argv = []
    for tok in argv:
        low = tok.lower().replace("\\", "/")
        base = low.rsplit("/", 1)[-1]
        if base in ("claude", "claude.exe"):
            return True  # запуск скрипта claude напрямую
        if "claude-code" in low:
            return True  # node/bun: .../@anthropic-ai/claude-code/cli.js
    return False


def resolve_claude_pid() -> int | None:
    """PID долгоживущего процесса claude через обход дерева вверх (psutil).

    claude живёт всю сессию; промежуточный шелл, запустивший хук, — нет. Ищем
    ближайшего предка, который действительно является claude (см. _looks_like_claude).
    Если уверенно найти не удалось — возвращаем None: мост трактует None как
    «PID неизвестен» и НЕ реапит сессию по liveness (лучше, чем вернуть PID
    транзиентного шелла и гарантированно убить карточку через пару секунд).
    """
    try:
        import psutil
    except Exception:
        return None  # без psutil дерево не обойти; None безопаснее, чем getppid()
    try:
        proc = psutil.Process(os.getppid())
        for _ in range(12):  # не более 12 уровней вверх
            if _looks_like_claude(proc):
                return proc.pid
            parent = proc.parent()
            if parent is None:
                break
            proc = parent
    except Exception:
        pass
    return None


def main() -> None:
    try:
        raw = sys.stdin.read()
        envelope = json.loads(raw) if raw.strip() else {}
    except Exception:
        return  # нет валидного stdin — молча выходим

    hook_name = envelope.get("hook_event_name", "")
    if hook_name == "PreToolUse":
        # Хук вешается с matcher "Task|Agent", но перепроверяем — спавн суб-агента.
        # "Task" — историческое имя тула, "Agent" — текущее (Claude Code переименовал).
        if envelope.get("tool_name") not in ("Task", "Agent"):
            return
        event = "subagent"
    else:
        event = HOOK_TO_EVENT.get(hook_name)
        if event is None:
            return  # неизвестный/ненужный хук — игнор

    payload = {
        "session_id": envelope.get("session_id"),
        "event": event,
        "cwd": envelope.get("cwd", ""),
        # Путь к транскрипту: мост по нему считает вес сессии (os.stat, без разбора
        # чужого формата). Контекст держит автокомпакт, а файл растёт всегда — по
        # весу и видно, когда сессию проще пересоздать через /clear.
        "transcript": envelope.get("transcript_path", ""),
    }
    if event == "start":
        payload["pid"] = resolve_claude_pid()  # захват PID claude один раз за сессию

    # Мета-оркестрация: роль и имя сессии видны прямо в ОКРУЖЕНИИ хука — плагин
    # MetaJetCore ставит их вкладке агента, а claude передаёт своим детям. Это
    # быстрее и надёжнее сверки с реестром: работает с первого же события и не
    # зависит от того, включена ли сверка вообще.
    # MJC_PARENT — имя оркестратора, который завёл этого агента. Плагин знает его
    # только в момент спавна и кладёт во вкладку; в реестре Claude Code такого поля
    # нет. Без него состав команды приходится угадывать по префиксу имени.
    for key, env in (("agent", "CLAUDE_CODE_AGENT"), ("sess_name", "CLAUDE_CODE_SESSION_NAME"),
                     ("parent", "MJC_PARENT")):
        val = os.environ.get(env, "").strip()
        if val:
            payload[key] = val[:64]

    # Stop/SubagentStop несут background_tasks — истинный список фоновой работы
    # сессии. Шлём абсолютное число работающих субагентов для синхронизации
    # счётчика в мосте: SubagentStop срабатывает на каждую остановку субагента
    # (в т.ч. промежуточную), поэтому чистый ±1 занижает счётчик.
    # Считаем строго type=="subagent": активно работающий субагент в списке есть.
    # Известный трейд-офф: субагент, приостановленный с живым фоновым процессом,
    # из списка исчезает (остаётся его дочерний shell) — его пузырёк погаснет до
    # резюма. Считать все running-задачи нельзя: собственный фоновый shell сессии
    # (dev-сервер и т.п.) давал бы вечный ложный пузырёк.
    if hook_name in ("Stop", "SubagentStop"):
        tasks = envelope.get("background_tasks")
        if isinstance(tasks, list):
            payload["subs"] = sum(
                1 for t in tasks
                if isinstance(t, dict)
                and t.get("type") == "subagent"
                and t.get("status") == "running"
            )

    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"http://{HOST}:{PORT}/event",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=TIMEOUT).close()
    except Exception:
        pass  # мост не запущен / таймаут / любая ошибка — глотаем


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)  # ВСЕГДА успех
