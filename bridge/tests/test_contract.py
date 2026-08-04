"""Тесты контракта мост ↔ прошивка.

Скетч и мост живут в одном репозитории, но собираются разными инструментами:
рассинхрон протокола не поймает ни pytest моста, ни компилятор Arduino. Поэтому
тесты здесь ЧИТАЮТ octodash.ino и сверяют его с тем, что мост реально отдаёт.

Ломаются в обе стороны: и если мост перестал слать поле, которое читает прошивка,
и если мост шлёт то, чего прошивка не разбирает.
"""
import json
import os
import pathlib
import re

import pytest

import bridge as b

INO = os.path.join(os.path.dirname(__file__), "..", "..",
                   "firmware", "octodash", "octodash.ino")


@pytest.fixture(scope="module")
def sketch():
    with open(INO, encoding="utf-8") as f:
        src = f.read()
    # комментарии выкидываем: примеры снэпшотов в шапке — не код
    src = re.sub(r"//[^\n]*", "", src)
    return re.sub(r"/\*.*?\*/", "", src, flags=re.S)


def keys_read_from(sketch: str, var: str) -> set[str]:
    """Какие поля JSON прошивка читает у переменной var: doc["x"], s["x"], c["x"]."""
    return set(re.findall(rf'\b{var}\[\s*"([^"]+)"\s*\]', sketch))


class Snap:
    """Снэпшоты моста во всех трёх режимах — на них и проверяем контракт."""

    def __init__(self):
        cfg = b.Config(max_sessions=6)
        self.br = b.Bridge(cfg, sink=None, clock=lambda: 1000.0,
                           is_alive=lambda pid: True, wall_clock=lambda: 1_700_000_000.0,
                           registry_probe=lambda: None,
                           size_probe=lambda path: 17.0)
        # две сессии с полным набором признаков: субагенты и вес
        for i in range(2):
            self.br.handle_event({"event": "start", "session_id": f"s{i}",
                                  "cwd": f"/work/p{i}", "pid": 100 + i,
                                  "transcript": f"/t/{i}.jsonl"})
            self.br.handle_event({"event": "subagent", "session_id": f"s{i}"})
        # ночь: чтобы поле nl появилось
        self.br.cafe_now = lambda: (0, 22 * 60)
        self.aquarium = self.br.build_snapshot()
        self.br.screen = 1
        self.cafe = self.br.build_snapshot()
        self.br.screen = 2
        self.br.places = ["НАПОЛИ", "СКАЗКА"]
        self.roulette = self.br.build_snapshot()
        self.br.screen = 0
        self.br.sleeping = True
        self.sleep = self.br.build_snapshot()
        self.br.sleeping = False


@pytest.fixture(scope="module")
def snap():
    return Snap()


# "cmd" — не снэпшот, а отладочная команда мосту→плате (снимок экрана);
# проверяется отдельным тестом ниже, поэтому из сверки снэпшота исключён.
COMMAND_KEYS = {"cmd"}


def test_firmware_top_level_keys_are_sent(sketch, snap):
    read = keys_read_from(sketch, "doc") - COMMAND_KEYS
    available = set(snap.aquarium) | set(snap.cafe) | set(snap.sleep) | set(snap.roulette)
    missing = read - available
    assert not missing, f"прошивка читает, а мост не шлёт: {sorted(missing)}"


def test_shot_command_matches_firmware(sketch):
    """Команда снимка и рамка ответа — тоже контракт, и он тоже молча ломается."""
    assert '"cmd"' in sketch, "прошивка перестала разбирать команды от моста"
    m = re.search(r'strcmp\(\s*cmd\s*,\s*"(\w+)"\s*\)', sketch)
    assert m, "не нашёл, какую команду ждёт прошивка"
    assert m.group(1) == "shot", f"прошивка ждёт cmd={m.group(1)!r}, мост шлёт 'shot'"
    src = pathlib.Path(b.__file__).read_text(encoding="utf-8")
    assert '{"cmd":"shot"}' in src, "мост шлёт не ту команду"
    # в скетче кавычки экранированы (Serial.print(F("{\"esp\"..."))) — снимаем слэши
    plain = sketch.replace("\\", "")
    for tag in ("shot", "tile", "shot_end"):          # рамка ответа
        assert f'"{tag}"' in plain, f"прошивка не печатает метку {tag}"
        assert f'"{tag}"' in src, f"мост не разбирает метку {tag}"


def test_bridge_top_level_keys_are_understood(sketch, snap):
    read = keys_read_from(sketch, "doc")
    sent = set(snap.aquarium) | set(snap.cafe) | set(snap.sleep) | set(snap.roulette)
    # "v" прошивка намеренно игнорирует: версия нужна людям и логам
    unread = sent - read - {"v"}
    assert not unread, f"мост шлёт, а прошивка не разбирает: {sorted(unread)}"


def test_session_card_fields_match(sketch, snap):
    read = keys_read_from(sketch, "s")
    card = snap.aquarium["sessions"][0]
    assert read, "в скетче не нашлось чтения полей карточки — регулярка устарела?"
    missing = read - set(card)
    assert not missing, f"в карточке не хватает полей: {sorted(missing)}"
    assert not (set(card) - read), f"мост шлёт в карточке лишнее: {sorted(set(card) - read)}"


def test_cafe_fields_match(sketch, snap):
    read = keys_read_from(sketch, "c")
    cafe = snap.cafe["cafe"]
    missing = read - set(cafe)
    assert not missing, f"в блоке cafe не хватает полей: {sorted(missing)}"
    assert not (set(cafe) - read), f"в cafe лишние поля: {sorted(set(cafe) - read)}"


def test_cafe_segment_tuple_is_three_numbers(snap):
    # прошивка читает seg[0], seg[1], seg[2] — от/до/тип
    for seg in snap.cafe["cafe"]["br"]:
        assert len(seg) == 3 and all(isinstance(v, int) for v in seg)


def test_state_codes_match_firmware_enum(sketch):
    # enum State { WORKING, WAITING, IDLE, ERR } — порядок задаёт коды в снэпшоте
    order = re.search(r"enum\s+State\s*\{([^}]*)\}", sketch).group(1)
    names = [n.strip() for n in order.split(",") if n.strip()]
    assert names == ["WORKING", "WAITING", "IDLE", "ERR"]
    assert (b.WORKING, b.WAITING, b.IDLE, b.ERROR) == (0, 1, 2, 3)


def test_cafe_status_codes_match_firmware_labels(sketch):
    # cafeLabel(): case 0..4 → мост должен использовать те же номера
    # подписи по-русски: [A-Z] их не поймает, а тест молча позеленел бы
    cases = dict(re.findall(r'case\s+(\d+):\s*return\s+"([^"]+)"', sketch))
    for code, label in cases.items():
        assert b.CAFE_LABEL[int(code)] == label, f"код {code}: мост {b.CAFE_LABEL[int(code)]}, прошивка {label}"


def test_encoder_events_from_sketch_are_accepted_by_bridge(sketch):
    """Строки, которые скетч реально отправляет, мост обязан понимать."""
    events = set(re.findall(r'sendEnc\(\s*(?:[^,]*\?\s*)?"(\w+)"\s*:?\s*"?(\w+)?"?', sketch))
    sent = {e for pair in events for e in pair if e}
    assert {"cw", "ccw", "key", "hold"} <= sent, f"в скетче нашлось только {sorted(sent)}"

    cfg = b.Config(max_sessions=6)
    br = b.Bridge(cfg, sink=None, clock=lambda: 1.0, is_alive=lambda p: True,
                  wall_clock=lambda: 1_700_000_000.0, registry_probe=lambda: None)
    for ev in sorted(sent):
        parsed = b.parse_esp_line(json.dumps({"enc": ev}))
        assert parsed == {"enc": ev, "held": False}, f"мост не разобрал {ev}"
        br.sleeping = False
        br.screen = 0
        assert br.handle_encoder(ev) in (True, False)      # не падает и не кидает


def test_held_flag_key_matches(sketch):
    # прошивка помечает «крутил с зажатой кнопкой» полем k — мост читает то же
    assert '\\"k\\":1' in sketch or '"k\\":1' in sketch or '\\"k\\"' in sketch
    assert b.parse_esp_line('{"enc":"cw","k":1}') == {"enc": "cw", "held": True}


def test_screen_count_matches(sketch):
    """Число экранов в мосту и в прошивке должно совпадать, иначе кнопка уводит
    на экран, которого прошивка не умеет рисовать."""
    # в скетче экраны различаются по curScreen == N
    for n in range(1, b.Bridge.SCREENS):
        assert f"curScreen == {n}" in sketch, f"прошивка не знает экран {n}"
    assert b.Bridge.SCREENS == 3
    # экрана, которого прошивка не умеет, у моста быть не должно
    assert f"curScreen == {b.Bridge.SCREENS}" not in sketch


def test_max_sessions_matches_grid(sketch):
    cols = int(re.search(r"COLS\s*=\s*(\d+)", sketch).group(1))
    rows = int(re.search(r"ROWS\s*=\s*(\d+)", sketch).group(1))
    assert cols * rows == b.Config().max_sessions == 6


def test_name_limit_fits_firmware_buffer(sketch):
    """Имя карточки должно влезать в char name[N] вместе с '\\0'."""
    size = int(re.search(r"char\s+name\[(\d+)\]", sketch).group(1))
    assert b.Config().name_max < size, f"OCTO_NAME_MAX={b.Config().name_max} не влезет в name[{size}]"


def test_id_prefix_fits_firmware_buffer(sketch):
    size = int(re.search(r"char\s+id\[(\d+)\]", sketch).group(1))
    cfg = b.Config()
    br = b.Bridge(cfg, sink=None, clock=lambda: 1.0, is_alive=lambda p: True,
                  wall_clock=lambda: 1_700_000_000.0, registry_probe=lambda: None)
    br.handle_event({"event": "start", "session_id": "x" * 64, "cwd": "/w/a"})
    sent_id = br.build_snapshot()["sessions"][0]["id"]
    # strlcpy обрежет молча, но тогда diff по id перестанет различать сессии
    assert len(sent_id) < size, f"id длиной {len(sent_id)} не влезает в id[{size}]"


def test_snapshot_line_fits_serial_buffer(sketch, snap):
    """Снэпшот должен влезать в LINE_MAX прошивки, иначе строка отбрасывается."""
    line_max = int(re.search(r"LINE_MAX\s*=\s*(\d+)", sketch).group(1))
    doc_size = int(re.search(r"StaticJsonDocument<(\d+)>", sketch).group(1))

    cfg = b.Config(max_sessions=6, name_max=16)
    br = b.Bridge(cfg, sink=None, clock=lambda: 1.0, is_alive=lambda p: True,
                  wall_clock=lambda: 1_700_000_000.0, registry_probe=lambda: None,
                  size_probe=lambda p: 199.0)
    for i in range(6):                                  # худший случай: всё заполнено
        sid = f"session-{i}-{'x' * 12}"
        br.handle_event({"event": "start", "session_id": sid,
                         "cwd": "/work/" + "n" * 30, "pid": 1000 + i,
                         "transcript": "/t/x.jsonl"})
        for _ in range(5):
            br.handle_event({"event": "subagent", "session_id": sid})
    line = br.snapshot_line()
    assert len(line) < line_max, f"снэпшот {len(line)} байт при LINE_MAX={line_max}"
    assert len(line) * 2 < doc_size, (
        f"снэпшот {len(line)} байт: StaticJsonDocument<{doc_size}> может не хватить")


def test_shot_row_encodings_match(sketch):
    """Маркеры сжатия строк снимка — контракт: разойдутся молча, картинкой-мусором."""
    plain = sketch.replace("\\", "")
    assert "Serial.print('L')" in plain, "прошивка перестала помечать RLE-строки"
    assert "Serial.print('#')" in plain, "прошивка перестала помечать повтор строки"
    src = pathlib.Path(b.__file__).read_text(encoding="utf-8")
    assert 'startswith("L")' in src and 'startswith("#")' in src, \
        "мост не разбирает сжатые строки снимка"
    # длина серии — 2 hex-цифры, значит серия не длиннее 255: прошивка обязана резать
    assert "run < 255" in plain, "серия в прошивке не ограничена 255 — мост её не разберёт"


def test_roulette_fields_match(sketch, tmp_path):
    """Поля блока roul сверяются в обе стороны — иначе барабан молча пуст."""
    read = keys_read_from(sketch, "r")
    cfg = b.Config(max_sessions=6, places_file=str(tmp_path / "нет.json"))
    br = b.Bridge(cfg, sink=None, clock=lambda: 1.0, is_alive=lambda p: True,
                  wall_clock=lambda: 1_700_000_000.0, registry_probe=lambda: None)
    br.places = ["НАПОЛИ", "СКАЗКА"]
    sent = set(br.build_roulette())
    assert read, "в скетче не нашлось чтения полей рулетки — регулярка устарела?"
    assert not (read - sent), f"прошивка читает, а мост не шлёт: {sorted(read - sent)}"
    assert not (sent - read), f"мост шлёт, а прошивка не разбирает: {sorted(sent - read)}"


def test_roulette_limits_match_firmware(sketch):
    """Список мест должен влезать в буферы прошивки — иначе имена обрежутся молча."""
    places_max = int(re.search(r"#define R_PLACES_MAX\s+(\d+)", sketch).group(1))
    name_buf = int(re.search(r"#define R_NAME_MAX\s+(\d+)", sketch).group(1))
    assert b.PLACES_MAX <= places_max, "мост шлёт больше мест, чем помещается"
    # кириллица в UTF-8 — два байта на символ, плюс завершающий ноль
    assert b.PLACE_NAME_MAX * 2 + 1 <= name_buf, "имя места не влезает в буфер прошивки"


def test_roulette_spin_event_understood_by_bridge(sketch):
    """Событие раскрутки, которое печатает прошивка, мост обязан понимать."""
    plain = sketch.replace("\\", "")
    assert '"enc":"spin"' in plain, "прошивка перестала слать событие раскрутки"
    parsed = b.parse_esp_line('{"enc":"spin","v":12.4}')
    assert parsed == {"enc": "spin", "held": False}
    src = pathlib.Path(b.__file__).read_text(encoding="utf-8")
    assert 'event == "spin"' in src, "мост не разбирает событие раскрутки"


def test_cyrillic_font_covers_place_names(sketch, tmp_path):
    """Каждая буква имён мест обязана иметь начертание, иначе на экране «?»."""
    # у прошивки таблица RU_MAP: 32 буквы А..Я по порядку Юникода плюс Ё
    assert re.search(r"RU_MAP\[33\]", sketch), "таблица начертаний изменилась"
    real = b.load_places(str(pathlib.Path(b.__file__).parent / b.PLACES_FILE))
    assert real, "боевой список мест не прочитался"
    allowed = set("АБВГДЕЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯЁ") | set(
        "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 -.,:'\"~()!?/N&+")
    for name in real:
        bad = set(name) - allowed
        assert not bad, f"в «{name}» нет начертаний для: {sorted(bad)}"
