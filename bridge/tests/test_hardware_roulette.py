"""Прогон режима выбывания НА ЖЕЛЕЗЕ: мост + плата, полный круг.

Зачем отдельный набор. Логика выбывания живёт по обе стороны провода: мост решает,
кто вылетает, плата это проигрывает. Ни pytest моста, ни компилятор Arduino не видят
их вместе — и оба бага первого переноса были ровно в шве:

- снэпшот-heartbeat затирал цель доводки (`roulWin`) чемпионом, то есть −1 посреди
  круга, и барабан доезжал не до жертвы: «выпал уже выбывший»;
- строка состояния не знала про паузу показа и застревала на первом вылетевшем.

Оба видны только тогда, когда плата реально крутит. Поэтому тест гоняет круг через
живой мост и сверяет состояние ПЛАТЫ (`{"cmd":"roul"}` → `/debug.roulette_esp`) с тем,
что думает мост.

Без запущенного моста или без платы тест пропускается — он не про чистую логику.
Запуск:  OCTO_HW_TEST=1 python -m pytest tests/test_hardware_roulette.py -v
"""
import json
import os
import time
import urllib.error
import urllib.request

import pytest

BASE = "http://127.0.0.1:8787"
R_IDLE, R_CHARGE, R_SPIN, R_LAND, R_WON, R_CULL = range(6)
BUSY = {R_CHARGE, R_SPIN, R_LAND, R_CULL}


def _get(path: str) -> dict:
    with urllib.request.urlopen(BASE + path, timeout=3) as r:
        return json.loads(r.read())


def _post(path: str, payload: dict) -> dict:
    req = urllib.request.Request(BASE + path, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read())


def _bridge() -> dict:
    return _get("/debug")["bridge"]


def board_state(timeout: float = 3.0) -> dict:
    """Состояние барабана глазами платы. Ответ приходит обратным каналом, поэтому
    спрашиваем и ждём, пока мост его разберёт."""
    _post("/cmd", {"cmd": "roul"})
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = _bridge().get("roulette_esp") or {}
        if st:
            return st
        time.sleep(0.1)
    raise AssertionError("плата не ответила на {\"cmd\":\"roul\"} — прошивка старая?")


def wait_idle(timeout: float = 60.0) -> dict:
    """Ждёт, пока плата доиграет серию. Возвращает её состояние."""
    deadline = time.time() + timeout
    last = {}
    while time.time() < deadline:
        last = board_state()
        if last["st"] not in BUSY:
            return last
        time.sleep(0.5)
    raise AssertionError(f"барабан не остановился за {timeout}с: {last}")


@pytest.fixture(scope="module")
def hardware():
    if not os.environ.get("OCTO_HW_TEST"):
        pytest.skip("прогон на железе: включается OCTO_HW_TEST=1")
    try:
        dbg = _bridge()
    except (urllib.error.URLError, OSError) as exc:
        pytest.skip(f"мост не отвечает на {BASE}: {exc}")
    if not (dbg.get("sink") or {}).get("connected"):
        pytest.skip("плата не подключена (serial закрыт)")
    before = (dbg["screen"], dbg["roulette_mode"])
    yield dbg
    # вернуть экран и режим как было: тест гоняет живой гаджет на столе у человека
    while _bridge()["screen"] != before[0]:
        _post("/enc", {"enc": "cw", "held": True})
        time.sleep(0.3)
    if _bridge()["roulette_mode"] != before[1]:
        _post("/enc", {"enc": "key"})


def enter_cull_mode():
    while _bridge()["screen"] != 2:
        _post("/enc", {"enc": "cw", "held": True})
        time.sleep(0.3)
    if _bridge()["roulette_mode"] != 1:
        _post("/enc", {"enc": "key"})
    _post("/enc", {"enc": "dbl"})            # круг с чистого листа
    time.sleep(1.0)


def test_full_round_on_hardware(hardware):
    """Полный круг: отсев до пятёрки, финал по одному, победитель — и ни одного
    повторного вылета. Именно это и было сломано швом мост↔плата."""
    enter_cull_mode()
    places = len(_bridge()["places"])
    assert places >= 3, "для прогона нужен список хотя бы из трёх мест"

    seen_out: set[str] = set()
    guard = 0
    while True:
        guard += 1
        assert guard <= places + 2, "круг не сходится: слишком много раскруток"
        _post("/cmd", {"cmd": "kick", "n": 6})       # раскрутка «руками моста»
        board = wait_idle()
        br = _bridge()

        out = set(br["roulette_out"])
        assert seen_out <= out, f"выбывший вернулся в игру: {seen_out - out}"
        seen_out = out
        # плата и мост обязаны сходиться в числе живых — иначе барабан крутит не то,
        # что мост считает составом
        assert board["alive"] == places - len(out), (
            f"плата: {board['alive']} живых, мост: {places - len(out)}")
        assert board["i"] >= board["seq"], "серия не доиграна, а барабан уже стоит"
        # Аварийный предохранитель доводки в норме не срабатывает НИКОГДА. Каждое
        # срабатывание человек видит как «доехало, встало на чужом месте, потом ещё
        # оборот и вычеркнуло» — именно так и проявился закрытый переход в доводку.
        # Счётчик в шапке обязан совпадать с фактом: он лежит вне полосы кадра и
        # обновляется событием, а не сам собой — и однажды всю серию показывал состав
        # на начало круга («13 ИЗ 13», пока выбывала половина).
        assert board.get("head") == board["alive"], (
            f"в шапке нарисовано {board.get('head')}, а живых {board['alive']}")
        assert board.get("boost", 0) == 0, (
            "скорость на доводке росла — барабан рывком ускорялся, чтобы доехать")
        assert board.get("stall", 0) == 0, (
            f"доводку {board['stall']} раз запускал предохранитель — барабан вставал сам")
        if len(out) >= places - 1:
            break
        # пока круг не сыгран, победителя быть не должно
        assert board["champ"] == -1, "чемпион появился раньше времени"

    board = board_state()
    assert board["alive"] == 1, "в конце круга обязан остаться ровно один"
    assert board["champ"] >= 0, "плата не знает победителя"
    assert board["st"] == R_WON, "барабан обязан встать на победителе"


def test_double_click_resets_round_on_hardware(hardware):
    enter_cull_mode()
    _post("/cmd", {"cmd": "kick", "n": 6})
    wait_idle()
    assert _bridge()["roulette_out"], "круг не начался — нечего сбрасывать"
    _post("/enc", {"enc": "dbl"})
    time.sleep(1.0)
    assert not _bridge()["roulette_out"], "двойной клик не сбросил круг"
    assert board_state()["alive"] == len(_bridge()["places"])
