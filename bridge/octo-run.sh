#!/usr/bin/env bash
# OctoDash — команда запуска (Linux/macOS).
# Идемпотентно поднимает мост fire-and-forget, затем запускает claude.
# Повторные вызовы безвредны: лишний инстанс упрётся в занятый порт и молча выйдет.
#
# Установка: положить рядом с bridge.py или указать OCTO_BRIDGE_PATH.
# Использование: octo-run.sh [аргументы claude...]

OCTO_BRIDGE_PATH="${OCTO_BRIDGE_PATH:-$(cd "$(dirname "$0")" && pwd)/bridge.py}"
PYTHON="${OCTO_PYTHON:-python3}"

octo_ensure_bridge() {
  # setsid отвязывает мост от терминала, чтобы он пережил закрытие сессии.
  setsid "$PYTHON" "$OCTO_BRIDGE_PATH" >/dev/null 2>&1 &
}

octo_ensure_bridge
exec claude "$@"
