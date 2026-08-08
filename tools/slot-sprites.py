#!/usr/bin/env python3
"""Символы барабанов автомата: единственный источник картинок.

Рисуем ASCII-артом, отсюда генерятся таблицы и для прошивки (C), и для эмулятора (JS).
Двух копий картинок быть не должно: они уже расходились.

Размер намеренно 8x8. Пробовали 16x16 (масштаб 3, 48px в окне 56) — символы вышли
крупными и грубыми, окно забивалось целиком, а мелкая клетка на таком масштабе читается
хуже крупной. Вернулись к 8x8 при масштабе 5.

Формат клетки:
    .  пусто        1  тень (тёмный слой)
    2  основной     3  блик (светлый слой)

Слои рисуются по порядку 1→2→3, каждый следующий поверх предыдущего.

    python tools/slot-sprites.py            # показать все символы в консоли
    python tools/slot-sprites.py --emit     # выдать таблицы для .ino и .html
"""
import sys

SIZE = 8

SYMBOLS = [
    ("ОСЬМИНОГ", (0x5A94, 0x947C, 0xFFFF), """
..2222..
.222222.
22322322
22222222
.222222.
.2.22.2.
2..2.2..
.2..2..2
"""),
    ("КОФЕ", (0x7AC7, 0xD5F2, 0xFFFF), """
...111..
........
22222222
22222223
22222.23
.222222.
..2222..
.111111.
"""),
    # Краб вместо рыбки: рыбка на восьми клетках читалась как безымянное пятно.
    # У краба силуэт узнаваемый — клешни по углам сверху и ноги снизу.
    ("КРАБ", (0x9084, 0xF146, 0xFFFF), """
.1....1.
11....11
.11..11.
..2222..
.232232.
22222222
.222222.
1.1..1.1
"""),
    ("СИГАРЕТА", (0xA515, 0xEF5D, 0xFBC2), """
......1.
.....1..
......3.
.....33.
...222..
..222...
.22.....
22......
"""),
    ("ПУЗЫРЬ", (0x2B6F, 0x6EFD, 0xFFFF), """
..2222..
.22..22.
22.3..22
2......2
2......2
22....22
.22..22.
..2222..
"""),
    ("СЕМЁРКА", (0x93C3, 0xFE88, 0xFFFF), """
22222222
22222222
.....22.
....22..
...22...
..22....
..22....
..22....
"""),
]


def parse(art: str) -> list[list[int]]:
    rows = [r for r in art.strip("\n").split("\n") if r.strip() != ""]
    assert len(rows) == SIZE, f"строк {len(rows)}, а надо {SIZE}"
    layers = [[0] * SIZE for _ in range(3)]
    for y, row in enumerate(rows):
        assert len(row) == SIZE, f"строка {y}: {len(row)} клеток, а надо {SIZE}"
        for x, ch in enumerate(row):
            if ch == ".":
                continue
            assert ch in "123", f"строка {y}: непонятный символ {ch!r}"
            layers[int(ch) - 1][y] |= 1 << (SIZE - 1 - x)
    return layers


def show():
    for name, _, art in SYMBOLS:
        layers = parse(art)
        print(name)
        for y in range(SIZE):
            line = ""
            for x in range(SIZE):
                m = 1 << (SIZE - 1 - x)
                if layers[2][y] & m:   line += "██"
                elif layers[1][y] & m: line += "▓▓"
                elif layers[0][y] & m: line += "░░"
                else:                  line += "  "
            print("   " + line)
        print()


def emit():
    print("// --- для octodash.ino -----------------------------------------------------")
    print(f"static const uint8_t SLOT_SPRITE[SLOT_SYMS][3][{SIZE}] = {{")
    for name, _, art in SYMBOLS:
        layers = parse(art)
        body = ", ".join("{" + ", ".join(f"0x{v:02X}" for v in L) + "}" for L in layers)
        print(f"  {{{body}}},   // {name}")
    print("};")
    print("static const uint16_t SLOT_PAL[SLOT_SYMS][3] = {")
    for name, pal, _ in SYMBOLS:
        print("  {" + ", ".join(f"0x{c:04X}" for c in pal) + f"}},   // {name}")
    print("};")

    print("\n// --- для octodash-preview.html --------------------------------------------")
    print("  const SLOT_SPRITES = [")
    for name, _, art in SYMBOLS:
        rows = [r for r in art.strip("\n").split("\n") if r.strip()]
        print("    [" + ",".join(f'"{r}"' for r in rows) + "],")
    print("  ];")
    print("  const SLOT_PALS = [")
    for name, pal, _ in SYMBOLS:
        print("    [" + ",".join(str(c) for c in pal) + "],")
    print("  ];")
    print("  const SLOT_NAMES = [" + ",".join(f'"{n}"' for n, _, _ in SYMBOLS) + "];")


if __name__ == "__main__":
    (emit if "--emit" in sys.argv else show)()
