#!/usr/bin/env python3
"""Суточный прогон: следит за живучестью моста и платы, копит ряд и кричит на аномалии.

Гаджету положено работать сутками. Заметить, что он не выдержал, легко — а вот понять,
ЧТО именно не выдержало, задним числом нельзя: плата перезагружается за полсекунды и
внешне это неотличимо от нормальной работы. Поэтому пишем ряд и сверяем его правилами.

    python tools/soak.py                 # следить, писать soak.csv рядом с логом моста
    python tools/soak.py --every 30      # опрашивать чаще
    python tools/soak.py --report        # разобрать уже накопленный ряд и выйти

Что считается аномалией:
  * плата перезагрузилась (счётчик boots вырос) — с причиной из ESP.getResetReason();
  * пульс платы пропал дольше чем на 3 минуты (шлётся раз в минуту);
  * куча платы просела относительно первого замера — утечка;
  * худшая итерация loop() подобралась к порогу watchdog;
  * serial переоткрывался — потеря связи;
  * RSS моста вырос в полтора раза — утечка уже на стороне Python.
"""
import argparse
import csv
import json
import os
import sys
import time
import urllib.request

URL = os.environ.get("OCTO_URL", "http://127.0.0.1:8787")
FIELDS = ["ts", "uptime_sec", "rss_mb", "sessions", "visible", "push_n", "serial_opens",
          "serial_ok", "esp_boots", "esp_up", "esp_heap", "esp_frag", "esp_maxloop_us",
          "esp_age_sec", "screen", "fails", "points"]

WDT_US = 3_000_000          # аппаратный watchdog ESP8266 ~3с; ближе 1/3 — уже тревога
HEAP_DROP = 0.90            # куча просела больше чем на 10% от первого замера
PULSE_GONE = 180            # пульс раз в минуту; три минуты тишины — потеря платы


def probe() -> dict:
    with urllib.request.urlopen(f"{URL}/debug", timeout=5) as r:
        d = json.loads(r.read())
    br = d.get("bridge", {})
    esp = br.get("esp") or {}
    sink = br.get("sink") or {}
    seen = esp.get("seen_ms")
    return {
        "ts": int(time.time()),
        "uptime_sec": br.get("uptime_sec"),
        "rss_mb": br.get("rss_mb"),
        "sessions": br.get("sessions_total"),
        "visible": br.get("visible"),
        "push_n": br.get("push_n"),
        "serial_opens": sink.get("opens"),
        "serial_ok": int(bool(sink.get("connected"))),
        "esp_boots": esp.get("boots"),
        "esp_up": esp.get("up"),
        "esp_heap": esp.get("heap"),
        "esp_frag": esp.get("frag"),
        "esp_maxloop_us": esp.get("maxloop_us"),
        "esp_age_sec": None if not seen else int(time.time() - seen / 1000),
        "screen": br.get("screen"),
        "fails": sum((br.get("fails") or {}).values()),
        "points": br.get("points"),
    }


def path_for(name: str) -> str:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~/.local/state")
    return os.path.join(base, "octodash", name)


def check(prev: dict | None, row: dict, first: dict) -> list[str]:
    """Правила аномалий. Сравниваем и с предыдущим замером, и с первым: перезагрузка
    видна только по соседям, а утечка — только на длинной базе."""
    out = []
    if prev:
        if (row["esp_boots"] or 0) > (prev["esp_boots"] or 0):
            out.append(f"ПЛАТА ПЕРЕЗАГРУЗИЛАСЬ (всего {row['esp_boots']})")
        if (row["serial_opens"] or 0) > (prev["serial_opens"] or 0):
            out.append("serial переоткрывался — связь рвалась")
        if (row["uptime_sec"] or 0) < (prev["uptime_sec"] or 0):
            out.append("МОСТ ПЕРЕЗАПУСТИЛСЯ")
        # Пуши идут минимум раз в heartbeat (5с). Замерли при живом мосте — значит
        # поток-отправитель умер: HTTP отвечает, порт открыт, а экран не обновляется.
        elif (row["push_n"] or 0) == (prev["push_n"] or 0):
            out.append("ПУШИ ЗАМЕРЛИ — похоже, умер поток отправки")
        if (row.get("fails") or 0) > (prev.get("fails") or 0):
            out.append(f"шаги циклов падали: всего {row['fails']}")
    if row["esp_age_sec"] is not None and row["esp_age_sec"] > PULSE_GONE:
        out.append(f"пульса платы нет {row['esp_age_sec']}с")
    if first.get("esp_heap") and row.get("esp_heap"):
        if row["esp_heap"] < first["esp_heap"] * HEAP_DROP:
            out.append(f"куча платы просела: {first['esp_heap']} → {row['esp_heap']}")
    if row.get("esp_maxloop_us") and row["esp_maxloop_us"] > WDT_US / 3:
        out.append(f"итерация loop() {row['esp_maxloop_us']}мкс — близко к watchdog")
    if first.get("rss_mb") and row.get("rss_mb") and row["rss_mb"] > first["rss_mb"] * 1.5:
        out.append(f"RSS моста вырос: {first['rss_mb']} → {row['rss_mb']} МБ")
    return out


def report(csv_path: str) -> int:
    with open(csv_path, newline="", encoding="utf-8") as f:
        rows = [{k: (int(v) if v.isdigit() else (float(v) if v.replace('.', '', 1).isdigit()
                                                 else None))
                 for k, v in r.items()} for r in csv.DictReader(f)]
    rows = [r for r in rows if r.get("ts")]
    if not rows:
        print("ряд пуст"); return 1
    span = (rows[-1]["ts"] - rows[0]["ts"]) / 3600
    heaps = [r["esp_heap"] for r in rows if r.get("esp_heap")]
    loops = [r["esp_maxloop_us"] for r in rows if r.get("esp_maxloop_us")]
    boots = (rows[-1].get("esp_boots") or 0) - (rows[0].get("esp_boots") or 0)
    print(f"прогон {span:.1f}ч, замеров {len(rows)}")
    print(f"перезагрузок платы: {boots}")
    if heaps:
        print(f"куча платы: {heaps[0]} → {heaps[-1]}, минимум {min(heaps)}")
    if loops:
        print(f"худшая итерация loop(): {max(loops)}мкс (порог тревоги {WDT_US//3})")
    rss = [r["rss_mb"] for r in rows if r.get("rss_mb")]
    if rss:
        print(f"RSS моста: {rss[0]} → {rss[-1]} МБ, максимум {max(rss)}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--every", type=int, default=60, help="период опроса, секунд")
    ap.add_argument("--csv", default=path_for("soak.csv"))
    ap.add_argument("--report", action="store_true", help="разобрать ряд и выйти")
    args = ap.parse_args()
    if args.report:
        return report(args.csv)

    os.makedirs(os.path.dirname(args.csv), exist_ok=True)
    fresh = not os.path.exists(args.csv)
    prev = first = None
    with open(args.csv, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if fresh:
            w.writeheader()
        print(f"пишу {args.csv}, опрос раз в {args.every}с — Ctrl+C чтобы прекратить")
        while True:
            try:
                row = probe()
            except Exception as e:                      # мост может быть перезапущен
                print(f"{time.strftime('%H:%M:%S')} мост не ответил: {e}")
                time.sleep(args.every)
                continue
            first = first or row
            for msg in check(prev, row, first):
                print(f"{time.strftime('%H:%M:%S')} !! {msg}")
            w.writerow(row)
            f.flush()
            prev = row
            time.sleep(args.every)


if __name__ == "__main__":
    sys.exit(main())
