# OctoDash bridge

Мост между сессиями Claude Code и настольным TFT-экраном. Единственный
долгоживущий процесс: держит правду о состоянии всех сессий, принимает события от
хуков по HTTP и шлёт агрегированный снэпшот на ESP по USB serial.

```
сессии claude ──(хуки)──▶ bridge.py ──(USB serial)──▶ ESP + TFT
```

Прошивка ESP лежит в `../firmware/octodash/`.

## Установка

```bash
# из директории bridge/
python -m venv ../.venv            # если ещё нет
../.venv/Scripts/python.exe -m pip install -r requirements.txt   # Windows
# ../.venv/bin/python -m pip install -r requirements.txt          # Linux/macOS
```

Зависимости: `pyserial` (обязателен), `psutil` (опционально — иначе используется
`ctypes`/`os.kill` фолбэк для проверки живости PID).

## Запуск

```bash
python bridge.py                   # боевой режим
OCTO_MOCK=1 python bridge.py       # mock: 6 фейковых сессий, без реальных хуков
OCTO_DEBUG=1 python bridge.py      # подробный лог (каждый пуш в serial)
```

Второй запущенный экземпляр молча завершится (singleton через бинд порта).

### Имена карточек

`name = basename(cwd)` — последний сегмент рабочей директории (для worktree это
имя папки/ветки). Перед отправкой на ESP имя проходит две обработки в мосту
(`display_name`), т.к. экран «тупой» и с ограничениями:

- **Транслитерация RU → латиница** (`каталог → katalog`). Классический шрифт
  Adafruit GFX — CP437, кириллицу не умеет (рисует мусор), поэтому переводим в ASCII.
- **Обрезка по центру** до `OCTO_NAME_MAX` символов с маркером `~`
  (`feature-knowledgebase-migration-v2 → feature-~tion-v2`). Серединная, а не с
  головы — чтобы worktree с общим префиксом оставались различимыми по хвосту.

Практический предел читаемости на карточке ~16–17 символов (ширина ячейки 102px,
шрифт 6px/символ). Если нужен настоящий кириллический текст — это правка прошивки
(UTF-8-декод + шрифт с кириллическими глифами, напр. U8g2).

### Serial-порт: автоопределение

По умолчанию `OCTO_SERIAL_PORT=auto` — мост сам ищет ESP по USB VID:PID известных
UART-чипов (CH340 у WeMos D1 mini, CH9102, CP210x, FTDI). Если совпадений нет, но
доступен ровно один порт — берётся он. Если портов несколько и ни один не опознан —
задайте порт явно через `OCTO_SERIAL_PORT`. Поиск повторяется при каждом
переоткрытии, поэтому переподключение USB подхватывается автоматически.

## Конфигурация (env-переменные)

| Переменная | Дефолт | Назначение |
|---|---|---|
| `OCTO_BRIDGE_HOST` | `127.0.0.1` | адрес HTTP-приёма хуков |
| `OCTO_BRIDGE_PORT` | `8787` | порт (он же арбитр singleton) |
| `OCTO_SERIAL_PORT` | `auto` | `auto` — автоопределение по USB VID:PID; иначе явный порт (`COM3`, `/dev/ttyUSB0`) |
| `OCTO_SERIAL_BAUD` | `115200` | скорость serial |
| `OCTO_HEARTBEAT_SEC` | `5` | период heartbeat-пуша |
| `OCTO_REAPER_SEC` | `2` | период проверки живости PID |
| `OCTO_DEBOUNCE_MS` | `100` | дебаунс пушей при серии событий |
| `OCTO_MAX_SESSIONS` | `6` | максимум карточек (сетка 3×2) |
| `OCTO_NAME_MAX` | `16` | лимит длины имени карточки (транслит + обрезка по центру) |
| `OCTO_MOCK` | `0` | `1` — режим фейковых сессий |
| `OCTO_HOOK_TIMEOUT` | `0.5` | таймаут запроса в `octo-notify.py` |
| `OCTO_DEBUG` | — | любой непустой → уровень лога DEBUG |

## Установка хуков Claude Code

1. Скопировать обёртку в каталог хуков:
   - Linux/macOS: `cp octo-notify.py ~/.claude/hooks/`
   - Windows: `copy octo-notify.py %USERPROFILE%\.claude\hooks\`
2. Добавить блок `hooks` в `~/.claude/settings.json` (user-level — работает во всех
   проектах и worktree). Готовые примеры для Linux и Windows — в
   `hooks-settings.example.json`. Путь к скрипту в `command` заменить на реальный.

Маппинг событий: `SessionStart→start` (+pid), `UserPromptSubmit→working`,
`Notification→waiting`, `Stop→idle`, `SessionEnd→end`, `StopFailure→error`.
`PreToolUse`/`PostToolUse` намеренно не используются (иначе хук спамил бы на каждый ход).

## Команда запуска

Обёртки идемпотентно поднимают мост, затем запускают `claude`:

- Linux/macOS: `./octo-run.sh [аргументы claude...]`
- Windows: `.\octo-run.ps1 [аргументы claude...]`

Удобно завести алиас `claude` → соответствующий скрипт.

## Тесты

Автотесты (pytest). Ядро моста спроектировано под тестируемость: время, проверка
PID и «сток» снэпшотов внедряются в `Bridge(...)`, поэтому автомат, порядок карточек,
reaper, формат JSON и автоопределение порта проверяются синхронно — без железа и sleep.

```bash
python -m pip install -r requirements-dev.txt
python -m pytest                 # из директории bridge/ — 66 тестов + покрытие ≥90%
python -m pytest tests/test_bridge.py::test_reap_removes_dead_pids   # один тест
python -m pytest -k autodetect   # по имени
```

Ручные проверки на живой системе:

```bash
# singleton — второй инстанс молча выходит
python bridge.py &            # первый
python bridge.py; echo $?     # → 0, сразу выходит

# приём события руками
curl -s -XPOST localhost:8787/event \
  -d '{"session_id":"t1","event":"start","cwd":"/home/e/proj","pid":'$$'}'
curl -s localhost:8787/status   # посмотреть текущий снэпшот
```

Смотреть, что уходит на ESP, без железа: запустить с `OCTO_DEBUG=1` — каждый
снэпшот логируется строкой `→ ESP: {...}`.
