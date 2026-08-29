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
        # второй режим того же экрана — на выбывание: у него свои поля (маска и
        # порядок вылета), и контракт обязан покрывать оба
        self.br.roul_mode = 1
        self.br.roul_out = {0}
        self.br.roul_seq = [0]
        self.cull = self.br.build_snapshot()
        self.br.roul_mode = 0
        self.br.screen = 3
        self.br._points_loaded = True
        self.slot = self.br.build_snapshot()
        self.br.screen = 0
        self.br.sleeping = True
        self.sleep = self.br.build_snapshot()
        self.br.sleeping = False


@pytest.fixture(scope="module")
def snap():
    return Snap()


# "cmd" — не снэпшот, а отладочная команда мосту→плате (снимок экрана);
# "pl" — порция списка мест: он любой длины и в строку снэпшота не влезает, поэтому
# едет отдельными строками. Обе проверяются своими тестами ниже.
COMMAND_KEYS = {"cmd", "pl"}


def test_firmware_top_level_keys_are_sent(sketch, snap):
    read = keys_read_from(sketch, "doc") - COMMAND_KEYS
    available = set(snap.aquarium) | set(snap.cafe) | set(snap.sleep) | set(snap.roulette) | set(snap.cull) | set(snap.slot)
    missing = read - available
    assert not missing, f"прошивка читает, а мост не шлёт: {sorted(missing)}"


def test_shot_command_matches_firmware(sketch):
    """Команда снимка и рамка ответа — тоже контракт, и он тоже молча ломается."""
    assert '"cmd"' in sketch, "прошивка перестала разбирать команды от моста"
    # команд у прошивки несколько (снимок, щелчок ручки, телеметрия) — важно, что
    # снимок среди них есть; проверять «первую попавшуюся» значило ломать тест
    # каждый раз, когда добавится отладочная команда
    known = set(re.findall(r'strcmp\(\s*cmd\s*,\s*"(\w+)"\s*\)', sketch))
    assert "shot" in known, f"прошивка не знает команду снимка, знает: {sorted(known)}"
    src = pathlib.Path(b.__file__).read_text(encoding="utf-8")
    assert '{"cmd":"shot"}' in src, "мост шлёт не ту команду"
    # в скетче кавычки экранированы (Serial.print(F("{\"esp\"..."))) — снимаем слэши
    plain = sketch.replace("\\", "")
    for tag in ("shot", "tile", "shot_end"):          # рамка ответа
        assert f'"{tag}"' in plain, f"прошивка не печатает метку {tag}"
        assert f'"{tag}"' in src, f"мост не разбирает метку {tag}"


def test_bridge_top_level_keys_are_understood(sketch, snap):
    read = keys_read_from(sketch, "doc")
    sent = set(snap.aquarium) | set(snap.cafe) | set(snap.sleep) | set(snap.roulette) | set(snap.cull) | set(snap.slot)
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
    # "hold" из жестов убран намеренно: ручной сон гасил подсвеченный экран и только
    # занимал ручку. Клик отдан режиму экрана обеда, двойной клик — сбросу круга.
    assert {"cw", "ccw", "key", "dbl"} <= sent, f"в скетче нашлось только {sorted(sent)}"
    assert "hold" not in sent, "удержание вернулось в прошивку — жест убран намеренно"

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
    assert b.Bridge.SCREENS == 4
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
    # Память документа теперь берётся из статической арены: в ArduinoJson 7
    # StaticJsonDocument — обёртка над кучей, а куче на плате свободно ~12 КБ, и
    # возня в ней каждые пять секунд означала фрагментацию за сутки работы.
    doc_size = int(re.search(r"CAP\s*=\s*(\d+)", sketch).group(1))

    cfg = b.Config(max_sessions=6, name_max=16)
    br = b.Bridge(cfg, sink=None, clock=lambda: 1.0, is_alive=lambda p: True,
                  wall_clock=lambda: 1_700_000_000.0, registry_probe=lambda: None,
                  size_probe=lambda p: 199.0)
    for i in range(6):                                  # худший случай: всё заполнено
        sid = f"session-{i}-{'x' * 12}"
        # имя кириллицей — оно вдвое тяжелее латиницы в UTF-8, это и есть худший случай
        br.handle_event({"event": "start", "session_id": sid,
                         "cwd": "/work/" + "щ" * 30, "pid": 1000 + i,
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
    br.roul_mode = 1
    sent |= set(br.build_roulette())
    assert read, "в скетче не нашлось чтения полей рулетки — регулярка устарела?"
    assert not (read - sent), f"прошивка читает, а мост не шлёт: {sorted(read - sent)}"
    assert not (sent - read), f"мост шлёт, а прошивка не разбирает: {sorted(sent - read)}"


def test_button_gestures_are_blocked_while_drum_spins(sketch):
    """Кнопка обязана молчать, пока барабан крутится.

    Блокировка живёт в ПРОШИВКЕ: только она знает, крутится ли барабан. Через мост
    это не проверить (POST /enc обходит плату), поэтому проверяем сам обработчик:
    в нём должен стоять roulBusy(), а сам roulBusy — покрывать все ходовые состояния.
    """
    press = sketch[sketch.index("void pollEncoder"):]
    press = press[:press.index(chr(10) + "}")]
    assert "roulBusy()" in press, "жесты кнопки перестали блокироваться на ходу"
    busy = sketch[sketch.index("bool roulBusy() {"):]      # тело, а не прототип
    busy = busy[:busy.index(chr(10) + "}")]
    for st in ("R_CHARGE", "R_SPIN", "R_LAND", "R_CULL"):
        assert st in busy, f"roulBusy не считает {st} движением"


def test_board_reports_drum_state(sketch):
    """Плата обязана уметь рассказать о барабане: иначе логику выбывания не проверить
    ничем, кроме глаз у стола — так оба бага первого переноса и дожили до железа."""
    plain = sketch.replace("\\", "")
    assert '"esp":"roul"' in plain, "прошивка не отдаёт состояние барабана"
    for field in ('"md"', '"st"', '"alive"', '"champ"', '"out"'):
        assert field in plain, f"в отчёте о барабане нет поля {field}"
    src = pathlib.Path(b.__file__).read_text(encoding="utf-8")
    assert 'kind == "roul"' in src, "мост не разбирает отчёт о барабане"
    assert "roulette_esp" in src, "состояние барабана не попадает в /debug"


def test_places_chunk_fields_match(sketch):
    """Поля строки-порции сверяются в обе стороны: иначе барабан останется пустым.

    Порцию прошивка разбирает своим сканером, а не ArduinoJson (в 2-килобайтную арену
    документа порция не влезает), поэтому имена полей ищем в её литералах: `"rev":`,
    `"n":`, `"b":`, `"i":`, `"p":[`.
    """
    body = sketch[sketch.index("bool roulTakePlaces"):]
    body = body[:body.index("\n}")]
    read = set(re.findall(r'\\"(\w+)\\":', body)) - {"pl"}   # "pl" — имя самого блока
    sent = set(json.loads(b.places_chunks(["НАПОЛИ", "СКАЗКА"])[0])["pl"])
    assert read, "в скетче не нашлось чтения полей порции — регулярка устарела?"
    assert not (read - sent), f"прошивка читает, а мост не шлёт: {sorted(read - sent)}"
    assert not (sent - read), f"мост шлёт, а прошивка не разбирает: {sorted(sent - read)}"


def test_places_request_understood_by_bridge(sketch):
    """Плата просит список при чужой ревизии — мост обязан понимать этот запрос."""
    plain = sketch.replace("\\", "")
    assert '"enc":"places"' in plain, "прошивка перестала просить список мест"
    assert b.parse_esp_line('{"enc":"places"}') == {"enc": "places", "held": False}
    src = pathlib.Path(b.__file__).read_text(encoding="utf-8")
    assert 'event == "places"' in src, "мост не разбирает запрос списка мест"
    # ответ платы о принятом списке: по нему мост видит, что она уместила не всё
    assert '"esp":"places"' in plain, "прошивка не отчитывается о принятом списке"
    assert 'kind == "places"' in src, "мост не разбирает отчёт платы о списке"


def test_places_chunks_fit_firmware_buffers(sketch):
    """Порции обязаны влезать и в приёмную строку прошивки, и в её арену JSON."""
    line_max = int(re.search(r"LINE_MAX\s*=\s*(\d+)", sketch).group(1))
    arena = int(re.search(r"CAP\s*=\s*(\d+)", sketch).group(1))
    name_buf = int(re.search(r"#define R_NAME_MAX\s+(\d+)", sketch).group(1))
    # кириллица в UTF-8 — два байта на символ, плюс завершающий ноль
    assert b.PLACE_NAME_MAX * 2 + 1 <= name_buf, "имя места не влезает в буфер прошивки"
    assert b.PLACES_LINE_MAX < line_max, "порция не влезет в приёмную строку прошивки"
    assert b.PLACES_LINE_MAX * 2 <= arena, "порции негде разобраться: мала арена JSON"
    # худший случай: имена максимальной длины, список заведомо длиннее одной порции
    names = [b.prepare_place("Я" * b.PLACE_NAME_MAX) + str(i) for i in range(50)]
    for line in b.places_chunks(names):
        assert len(line.encode("utf-8")) <= b.PLACES_LINE_MAX + 1


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


def test_cyrillic_font_covers_sketch_literals(sketch):
    """Каждая русская надпись в скетче должна быть рисуемой.

    Начертания есть только для ПРОПИСНЫХ: строчные буквы декодер молча меняет на «?»,
    и надпись превращается в частокол вопросов. Компилятор про это ничего не скажет,
    а на экран смотрят не после каждой правки — поэтому проверяем текстом.
    """
    allowed = set("АБВГДЕЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯЁ")
    for lit in re.findall(r'"((?:[^"\\\n]|\\.)*)"', sketch):
        cyr = [c for c in lit if "Ѐ" <= c <= "ӿ"]
        bad = sorted({c for c in cyr if c not in allowed})
        assert not bad, f"в надписи «{lit}» нет начертаний для: {bad} (строчные не рисуются)"


def test_slot_fields_match(sketch, tmp_path):
    """Поля блока slot сверяются в обе стороны — иначе автомат молча пуст."""
    read = keys_read_from(sketch, "sl")
    cfg = b.Config(max_sessions=6, points_file=str(tmp_path / "p.json"))
    br = b.Bridge(cfg, sink=None, clock=lambda: 1.0, is_alive=lambda p: True,
                  wall_clock=lambda: 1_700_000_000.0, registry_probe=lambda: None)
    br._points_loaded = True
    sent = set(br.build_slot())
    assert read, "в скетче не нашлось чтения полей автомата — регулярка устарела?"
    assert not (read - sent), f"прошивка читает, а мост не шлёт: {sorted(read - sent)}"
    assert not (sent - read), f"мост шлёт, а прошивка не разбирает: {sorted(sent - read)}"


def test_slot_symbol_count_matches(sketch):
    """Число символов должно совпадать: мост шлёт индексы, прошивка их берёт по модулю."""
    syms = int(re.search(r"#define SLOT_SYMS\s+(\d+)", sketch).group(1))
    assert syms == b.SLOT_SYMS, f"символов в прошивке {syms}, у моста {b.SLOT_SYMS}"


def test_slot_event_understood(sketch):
    plain = sketch.replace("\\", "")
    assert '"enc":"slot"' in plain, "прошивка перестала слать событие автомата"
    assert b.parse_esp_line('{"enc":"slot"}') == {"enc": "slot", "held": False}
    src = pathlib.Path(b.__file__).read_text(encoding="utf-8")
    assert 'event == "slot"' in src, "мост не разбирает событие автомата"


def sketch_defines(sketch: str) -> dict:
    """Числовые #define скетча — чтобы считать координаты прямоугольников."""
    d = {}
    for name, val in re.findall(r"^#define\s+(\w+)\s+(-?\d+)\s*(?://.*)?$", sketch, re.M):
        d[name] = int(val)
    d.setdefault("W", 320)
    d.setdefault("H", 240)
    return d


def test_status_line_is_inside_a_rect_that_redraws_it(sketch):
    """Строку состояния обязан накрывать прямоугольник, который её перерисовывает.

    Это ловушка, на которую тут наступали не раз: объект живёт по одним координатам,
    а обновляется полосой, посчитанной по другим. Строка автомата сидит на S_STATUS_Y,
    а перерисовывалась полосой (0, H-16) от рулетки — надпись застревала, а мигающий
    отказ перерисовывался огрызком из соседней полосы и «накладывался». Ни компилятор,
    ни pytest моста этого не видят: координаты сходятся только на экране.
    """
    d = sketch_defines(sketch)
    rects = []
    for m in re.finditer(r"redrawRect\(([^;]+?)\);", sketch):
        args = [a.strip() for a in m.group(1).split(",")]
        if len(args) != 4:
            continue
        try:
            rects.append([int(eval(a, {"__builtins__": {}}, d)) for a in args])
        except Exception:
            continue                      # аргумент не арифметика по константам — пропускаем
    assert rects, "в скетче не нашлось ни одного redrawRect — регулярка устарела?"
    # Полноэкранная перерисовка накрывает вообще всё и удовлетворила бы проверку
    # всегда — она и есть та редкая заплатка, из-за которой баг доживал до экрана.
    # Спрашиваем про полосы, которые обновляют строку КАЖДЫЙ кадр.
    rects = [r for r in rects if r[3] < d["H"] // 2]

    # y каждой строки состояния: у автомата своя константа, у рулетки — отступ от низа
    for label, y in (("автомат", d["S_STATUS_Y"]), ("рулетка", d["H"] - 13)):
        ok = [r for r in rects
              if r[0] <= 0 and r[0] + r[2] >= d["W"]          # во всю ширину
              and r[1] <= y and r[1] + r[3] >= y + 8]         # и накрывает все 8 строк букв
        assert ok, (f"строку состояния ({label}, y={y}) не накрывает ни один "
                    f"redrawRect во всю ширину — она застрянет на экране")


def test_slot_sprites_come_from_the_generator(sketch):
    """Спрайты барабанов в прошивке и в эмуляторе обязаны совпадать с генератором.

    Картинки уже расходились: на плате оказывался не тот символ, что в превью, и
    заметно это было только глазами после заливки. Генератор `tools/slot-sprites.py` —
    единственный источник, тест сверяет с ним обе копии.
    """
    import importlib.util
    root = pathlib.Path(b.__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location("slotgen", root / "tools" / "slot-sprites.py")
    gen = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gen)

    assert len(gen.SYMBOLS) == b.SLOT_SYMS, "символов в генераторе не столько, сколько у моста"
    html = (root / "tools" / "octodash-preview.html").read_text(encoding="utf-8")
    for name, pal, art in gen.SYMBOLS:
        layers = gen.parse(art)
        c_row = "{" + ", ".join(f"0x{v:02X}" for v in layers[1]) + "}"
        assert c_row in sketch, f"основной слой «{name}» не совпал с генератором"
        assert "{" + ", ".join(f"0x{c:04X}" for c in pal) + "}" in sketch, f"палитра «{name}»"
        for row in [r for r in art.strip("\n").split("\n") if r.strip()]:
            assert f'"{row}"' in html, f"строка «{name}» не совпала в эмуляторе"


def test_slot_payouts_match_firmware_hint(sketch):
    """Подпись на экране обязана совпадать с реальной таблицей выплат."""
    # Числа берутся из моста, а не дублируются здесь: тест обязан ловить расхождение
    # подписи с выплатой, а не фиксировать конкретный коэффициент. Иначе правка
    # выплат требует трёх согласованных правок вместо двух, и третью легко забыть.
    for pay in (b.SLOT_PAY_TRIPLE, b.SLOT_PAY_PAIR):
        want = f"x{pay:g}"
        assert want in sketch, f"мост платит {want}, а на экране такой подписи нет"


def test_visited_mask_is_drawn_in_both_modes(sketch):
    """Журнал обедов виден на экране только если маску разбирают и рисуют в обоих
    режимах. Мост шлёт `out` всегда — прошивка обязана её брать и в рулетке, иначе
    посещённые места молча перестают выпадать без единого следа на экране."""
    plain = sketch.replace(chr(92), "")
    body = sketch[sketch.index("if (scr == 2) {"):]
    body = body[:body.index("if (scr == 1) {")]
    roulette = body[body.index("} else {"):]                 # ветка режима рулетки
    assert 'roulSetMask(r["out"]' in roulette, \
        "в рулетке маска вычеркнутых не применяется — журнал обедов не виден"
    assert 'r["k"]' in body, "прошивка не берёт знаменатель шапки"
    drum = sketch[sketch.index("bool drumVis"):]
    drum = drum[:drum.index("void composeSlot")]
    assert "bool dead = roulDead(idx);" in drum, \
        "барабан гасит вычеркнутых только в выбывании — в рулетке журнал не видно"
    assert "ОСТАЛОСЬ %d ИЗ %d" in plain and "В ИГРЕ %d ИЗ %d" in plain, \
        "шапка перестала различать круг обедов и партию отсева"


def test_visited_mask_survives_heartbeat_without_redraw(sketch):
    """Маска приезжает КАЖДЫМ снэпшотом, в том числе heartbeat раз в пять секунд.
    Если roulSetMask отвечает «изменилась» просто потому, что она непустая, экран
    будет полностью перерисовываться каждые пять секунд — это заметно глазом."""
    fn = sketch[sketch.index("bool roulSetMask(const char *hex) {"):]
    fn = fn[:fn.index(chr(10) + "}")]
    assert "memcmp" in fn, "roulSetMask не сравнивает «было/стало» — лишние перерисовки"


def test_full_redraw_never_happens_mid_flight(sketch):
    """Полная перерисовка экрана обеда стоит 72мс (замер telon на живой плате), а шаг
    физики зажат сверху 0.05с. Дёрнуть её посреди полёта — значит уронить пару кадров
    и часть хода барабана. Маска в рулетке меняется РОВНО в полёте (ответ на раскрутку
    приносит нового победителя и вычёркивает прошлого), поэтому такие просьбы обязаны
    откладываться до остановки."""
    body = sketch[sketch.index("if (scr == 2) {"):]
    body = body[:body.index("if (scr == 1) {")]
    # Ветка смены режима (выше по телу) перерисовывает сразу, и это законно: кнопка
    # на ходу не принимается вовсе. Проверяем то, что приходит КАЖДЫМ снэпшотом.
    assert "roulDirty = true" not in body.split("if (md == 1)")[-1], \
        "снэпшот дёргает полную перерисовку напрямую — она попадёт в полёт барабана"
    fn = sketch[sketch.index("void roulNeedRedraw() {"):]
    fn = fn[:fn.index(chr(10) + "}")]
    assert "roulBusy()" in fn and "roulDirtyLate" in fn, \
        "перерисовка перестала откладываться на время движения барабана"
    loop = sketch[sketch.index("if (roulDirtyLate"):]
    assert "!roulBusy()" in loop[:120], "отложенную перерисовку никто не забирает"


def test_landing_retargets_when_answer_changes_winner(sketch):
    """Доводка считает траекторию ОДИН раз и в конце делает `roulPos = roulWin`. Если
    победитель сменился уже на торможении, барабан доедет до старой цели, встанет и
    прыгнет на новую — «остановился не на том месте, потом доехал». Значит смена цели
    обязана сбрасывать признак доводки, чтобы траектория пересчиталась."""
    body = sketch[sketch.index("if (scr == 2) {"):]
    body = body[:body.index("if (scr == 1) {")]
    assert "roulState == R_LAND && roulWin != was" in body, \
        "смена победителя на доводке не пересчитывает траекторию"
    land = sketch[sketch.index("if (roulState == R_LAND) {"):]
    assert "roulPos = roulWin;" in land[:land.index("roulBubblesStep")], \
        "доводка больше не доснапывает на цель — проверка выше устарела"


def test_bridge_spin_animates_the_drum_in_both_modes(sketch):
    """`POST /enc spin` обязан крутить барабан и в рулетке, а не только в выбывании:
    иначе анимацию рулетки нельзя проверить ничем, кроме руки на ручке — ровно поэтому
    «замирание в полёте» дожило до живого прогона."""
    body = sketch[sketch.index("if (scr == 2) {"):]
    body = body[:body.index("if (scr == 1) {")]
    roulette = body[body.index("} else {"):]
    assert "roulState = R_LAND" in roulette, \
        "ответ моста без нашей раскрутки не двигает барабан в рулетке"
    assert "roulSpSynced" in roulette, \
        "первый снэпшот после включения крутанёт барабан сам — нужен признак синхронизации"
