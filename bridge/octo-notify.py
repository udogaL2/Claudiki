#!/usr/bin/env python3
"""OctoDash hook wrapper.

Тонкая обёртка-хук Claude Code. Читает JSON-конверт хука со stdin, определяет
событие по hook_event_name, шлёт POST /event на мост. Для SessionStart добавляет
pid = os.getppid() (родитель обёртки = процесс claude) — единственный захват PID
за сессию, нужен для liveness в мосту.

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
    "Notification": "waiting",
    "Stop": "idle",
    "SessionEnd": "end",
    "StopFailure": "error",
}

HOST = os.environ.get("OCTO_BRIDGE_HOST", "127.0.0.1")
PORT = os.environ.get("OCTO_BRIDGE_PORT", "8787")
TIMEOUT = float(os.environ.get("OCTO_HOOK_TIMEOUT", "0.5"))


def main() -> None:
    try:
        raw = sys.stdin.read()
        envelope = json.loads(raw) if raw.strip() else {}
    except Exception:
        return  # нет валидного stdin — молча выходим

    hook_name = envelope.get("hook_event_name", "")
    event = HOOK_TO_EVENT.get(hook_name)
    if event is None:
        return  # неизвестный/ненужный хук — игнор

    payload = {
        "session_id": envelope.get("session_id"),
        "event": event,
        "cwd": envelope.get("cwd", ""),
    }
    if event == "start":
        # родитель обёртки — процесс claude; ловим его PID один раз за сессию
        payload["pid"] = os.getppid()

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
