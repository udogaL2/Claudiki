// OctoDash firmware — ESP8266 (WeMos D1 mini) + ILI9341 320x240 + энкодер.
//
// Рендерер «аквариума» осьминогов. Вся логика (какие сессии живы, какая страница,
// какой экран, спать или нет, статус кофейни) — на стороне моста; прошивка рисует
// и отправляет события ручки. Контракты — CLAUDE.md, бюджет кадра — RENDERING.md,
// распиновка — WIRING.md, сборка — BUILD.md.
//
// Снэпшот (полный, одна строка + '\n'):
//   {"v":1,"scr":0,"p":2,"pn":3,"nl":40,"sessions":[{"id","name","state","sub","mb"}]}
//   {"v":1,"scr":1,"cafe":{"st","nm","dow","till","om","cm","net","br":[[от,до,тип]]}}
//   {"v":1,"slp":1}                                   — экран спит
// Обратно (событие ручки):
//   {"enc":"cw"} {"enc":"ccw"} {"enc":"key"} {"enc":"hold"} {"enc":"cw","k":1}
//
// Зависимости: Adafruit GFX, Adafruit ILI9341, ArduinoJson (v6).

#include <SPI.h>
#include <math.h>
#include <Adafruit_GFX.h>
#include <Adafruit_ILI9341.h>
#include <ArduinoJson.h>

#define ESP_DIAG 0   // 1 = телеметрия boot/stat в serial (мост её логирует)
// Снимок экрана (octoctl shot). ОСТАВЛЕН ВКЛЮЧЁННЫМ в проде намеренно: это
// единственный способ увидеть, что на панели, не стоя рядом с ней, и именно он нашёл
// три визуальных бага. Цена — 1.9 КБ статической памяти на буфер строки. Флаг есть
// на случай, если эти байты понадобятся; отдельной отладочной ВЕРСИИ прошивки нет
// намеренно: два пути отрисовки в этом проекте уже расходились и стоили дня работы.
#define ESP_SHOT 1
#define FW_VER   70 // бампать при каждой заливке — видно в диаг-логе

// --- пины --------------------------------------------------------------------
#define TFT_CS   D8
// DC и RST поменяны местами относительно первой сборки — так удобнее паять. Оба
// пина страппинговые и оба подтянуты на плате к питанию, поэтому перестановка
// безопасна: GPIO0 (D3) и GPIO2 (D4) обязаны быть HIGH при старте, а входы дисплея
// их не перетягивают. Побочный эффект приятный: встроенный светодиод сидит на GPIO2,
// и в роли RST он почти всегда HIGH — плата перестала мигать (в роли DC он дёргался
// на каждой команде SPI).
#define TFT_DC   D3
#define TFT_RST  D4
#define ENC_A    D1   // S1 (CLK) — прерывание
#define ENC_B    D2   // S2 (DT)  — прерывание
#define ENC_SW   D0   // KEY — опрос: у GPIO16 нет прерываний и внутренней подтяжки

Adafruit_ILI9341 tft = Adafruit_ILI9341(TFT_CS, TFT_DC, TFT_RST);

// --- палитра (RGB565) --------------------------------------------------------
#define BG        0x0000
#define GRIDLINE  0x0861
#define BODY_LIGHT 0xEF1F
#define BODY_BOT   0x410F
#define BODY_DK    0x28CC
#define TENT_TIP   0xC4FF
#define EYE_LIGHT 0xF7BF
#define EYE_DARK  0x10C4
#define GLINT     0x8FBD
#define C_WORKING 0x37E7
#define C_WAITING 0xFEA0
#define C_IDLE    0x5AEB
#define C_ERROR   0xF8AC
#define SUBA      0x475B
#define SUBA_D    0x1BD2
#define SMOKE     0x9CD3
#define SMOG      0x8410   // копоть по краям карточки (вес сессии)
#define SMOG_HI   0xC5AC   // тяжёлая сессия: копоть светлее и с желтизной
#define ACCENT    0x35FB   // всплывашка страницы
#define PLATE     0x0861
#define CIG_BODY  0xF7BF
#define CIG_HI    0xFFFF
#define FILTERC   0xCD0B
#define EMBER     0xFC00
#define EMBER_HOT 0xFF0C
#define FISH_B    0xFAC7
#define FISH_D    0xB9A3
#define CRAB_B    0xE28B
#define CRAB_D    0x9146
#define BUBBLE    0x7DFB
#define CREAMC    0xE73C
#define COFFEEC   0x9B26
#define COFFEE_DK 0x4A44

// --- общий off-screen буфер (см. RENDERING.md) -------------------------------
// Один на оба экрана, выделяется однажды: пересоздание канвы фрагментирует кучу.
#define BUF_W 68
#define BUF_H 76
#define OCTO_H 72          // окно осьминога аквариума — верхние строки буфера
#define LCX   32
#define LCY   30
GFXcanvas16 octoBuf(BUF_W, BUF_H);

// Полоса на всю ширину экрана: в неё собирается ЦЕЛЫЙ экран (сетка, рамки, имена,
// осьминоги, копоть) и выливается одним блитом. 320x16x2 = 10 КБ — столько же, сколько
// занимал прежний буфер снимка, поэтому память не выросла. Одна полоса — один буфер
// и для экрана, и для снимка: снимок физически не может разойтись с картинкой.
#define STRIP_H 16

// --- канва со смещением (пока только для отладочных снимков) ------------------
// Позволяет рисовать в АБСОЛЮТНЫХ координатах экрана, складывая картинку плитками.
//
// Переопределён ТОЛЬКО drawPixel, а заливки реализованы циклом по нему. Так сделано
// намеренно: если переопределить fillRect и позвать из него базовую версию, та
// внутри вызовет drawFastVLine — тоже переопределённый — и смещение вычтется
// ДВАЖДЫ. Именно на этом сломалась первая попытка: тело осьминога рисуется
// через drawPixel и оставалось на месте, а щупальца, сигарета и буквы идут
// прямоугольниками и уезжали за пределы окна.
class OffsetCanvas : public GFXcanvas16 {
 public:
  OffsetCanvas(int16_t w, int16_t h) : GFXcanvas16(w, h) {}
  int16_t offX = 0, offY = 0;
  void moveTo(int x, int y) { offX = x; offY = y; }
  // Ограничение области в ЭКРАННЫХ координатах. Нужно, чтобы осьминог в полосе
  // занимал ровно то же окно, что перерисовывает анимация: иначе клубы и щупальца
  // выходят за окно, анимация их больше никогда не трогает — и они остаются
  // мусором у имени карточки.
  int16_t clipL = -32768, clipT = -32768, clipR = 32767, clipB = 32767;
  // База — прямоугольник, который сейчас перерисовывается. clipOff возвращает к ней,
  // а не «в бесконечность»: иначе рисование за пределами прямоугольника снова стоило
  // бы полную ширину полосы.
  int16_t baseL = -32768, baseT = -32768, baseR = 32767, baseB = 32767;
  void clipTo(int l, int t, int r, int b) { clipL = l; clipT = t; clipR = r; clipB = b; }
  void clipBase(int l, int t, int r, int b) {
    baseL = l; baseT = t; baseR = r; baseB = b;
    clipTo(l, t, r, b);
  }
  void clipOff() { clipTo(baseL, baseT, baseR, baseB); }
  static int16_t clampClip(int32_t v) {
    return v < -32768 ? -32768 : (v > 32767 ? 32767 : (int16_t)v);
  }
  // Сдвиг начала координат. Бариста и реквизит кофейни нарисованы в ЛОКАЛЬНЫХ
  // координатах своего окна — так их рисует кадр анимации в буфер. Чтобы теми же
  // функциями собирать их в полосу целого экрана, цель сама переносит координаты:
  // рисование остаётся одно, а куда оно ляжет, решает получатель.
  int16_t trX = 0, trY = 0;
  void originAt(int x, int y) { trX = x; trY = y; }
  void originReset() { trX = 0; trY = 0; }
  void drawPixel(int16_t x, int16_t y, uint16_t c) override {
    x += trX; y += trY;
    if (x < clipL || x > clipR || y < clipT || y > clipB) return;
    GFXcanvas16::drawPixel(x - offX, y - offY, c);
  }
  void drawFastHLine(int16_t x, int16_t y, int16_t w, uint16_t c) override { fillRect(x, y, w, 1, c); }
  void drawFastVLine(int16_t x, int16_t y, int16_t h, uint16_t c) override { fillRect(x, y, 1, h, c); }
  // Пересечение с полосой и областью обрезки считается ДО циклов. Наивный обход
  // «перебрать все пиксели и выбросить лишние» стоил дорого там, где заливки во всю
  // ширину экрана: статика кофейни собиралась 527мс, потому что каждая из 15 полос
  // честно перебирала полосы шапки и таблицы целиком.
  // Приглушить прямоугольник решетом цвета фона. Пересечение с полосой считается
  // ДО циклов: наивная версия перебирала весь прямоугольник на КАЖДОЙ полосе и
  // стоила 198мс из 230мс кадра рулетки — 4 кадра в секунду.
  void dimRect(int16_t x, int16_t y, int16_t w, int16_t h) {
    int ax = x + trX, ay = y + trY;
    int x0 = max(max(ax, (int)clipL), (int)offX);
    int y0 = max(max(ay, (int)clipT), (int)offY);
    int x1 = min(min(ax + w - 1, (int)clipR), offX + width() - 1);
    int y1 = min(min(ay + h - 1, (int)clipB), offY + height() - 1);
    if (x1 < x0 || y1 < y0) return;
    uint16_t *buf = getBuffer();
    const int stride = width();
    for (int yy = y0; yy <= y1; yy++) {
      uint16_t *row = buf + (yy - offY) * stride - offX;
      for (int xx = x0 + ((yy - ay) & 1); xx <= x1; xx += 2) row[xx] = BG;
    }
  }
  void fillRect(int16_t x, int16_t y, int16_t w, int16_t h, uint16_t c) override {
    int ax = x + trX, ay = y + trY;                   // экранные координаты
    int x0 = max(max(ax, (int)clipL), (int)offX);
    int y0 = max(max(ay, (int)clipT), (int)offY);
    int x1 = min(min(ax + w - 1, (int)clipR), offX + width() - 1);
    int y1 = min(min(ay + h - 1, (int)clipB), offY + height() - 1);
    if (x1 < x0 || y1 < y0) return;
    // Пишем в буфер строками, а не через drawPixel: тот на каждый пиксель проверяет
    // границы заново, и заливки корпуса автомата стоили 21мс из 37мс кадра. Границы
    // уже посчитаны выше и зажаты по холсту, так что проверять нечего.
    uint16_t *buf = getBuffer();
    const int stride = width();
    for (int yy = y0; yy <= y1; yy++) {
      uint16_t *row = buf + (yy - offY) * stride + (x0 - offX);
      for (int i = 0, n = x1 - x0; i <= n; i++) row[i] = c;
    }
  }
  // Текст особый случай: drawChar НЕ виртуальный, а отсекает по размеру холста.
  // Экранная координата имени (y≈108) для холста 68x76 всегда «за краем», поэтому
  // print() молча ничего не рисовал. Через write() (он виртуальный) переводим
  // координаты сами, обнуляя смещение — внутри drawChar они уже холстовые.
  size_t write(uint8_t c) override {
    if (c == '\r') return 1;
    if (c == '\n') { cursor_x = 0; cursor_y += textsize_y * 8; return 1; }
    // Внутри drawChar координаты уже холстовые, поэтому и область обрезки нужно
    // перевести в холстовые. Пока этого не было, имя карточки исчезало с ПАНЕЛИ, а в
    // снимке оставалось: снимок не выставлял базовую обрезку и потому врал.
    int16_t sx = offX, sy = offY;
    int16_t cl = clipL, ct = clipT, cr = clipR, cb = clipB;
    offX = 0; offY = 0;
    clipL = clampClip((int32_t)cl - sx); clipT = clampClip((int32_t)ct - sy);
    clipR = clampClip((int32_t)cr - sx); clipB = clampClip((int32_t)cb - sy);
    GFXcanvas16::drawChar(cursor_x - sx, cursor_y - sy, c, textcolor, textbgcolor,
                          textsize_x, textsize_y);
    offX = sx; offY = sy;
    clipL = cl; clipT = ct; clipR = cr; clipB = cb;
    cursor_x += textsize_x * 6;          // перенос строки не нужен: рисуем в плитку
    return 1;
  }
};



// =============================================================================
// Кириллица: у ILI9341 её нет вовсе
// =============================================================================
// Заглавная кириллица 5x8, формат glcdfont (байт = столбец, бит 0 = верх).
// 21 глифов x 5 байт = 105 байт флеша. Ещё 12 букв
// (АВЕКМНОРСТУХ) совпадают с латинскими начертаниями — их рисует встроенный
// шрифт, и начертания заведомо согласованы.
static const uint8_t RU_FONT[][5] = {
  {0xFE, 0x92, 0x92, 0x92, 0x62},   // Б
  {0xFE, 0x02, 0x02, 0x02, 0x02},   // Г
  {0xC0, 0x7C, 0x42, 0x42, 0xFE},   // Д
  {0xFE, 0x10, 0xFE, 0x10, 0xFE},   // Ж
  {0x44, 0x82, 0x92, 0x92, 0x6C},   // З
  {0xFE, 0x20, 0x10, 0x08, 0xFE},   // И
  {0x7E, 0x11, 0x09, 0x05, 0x7E},   // Й
  {0xC0, 0x3C, 0x02, 0x02, 0xFE},   // Л
  {0xFE, 0x02, 0x02, 0x02, 0xFE},   // П
  {0x18, 0x24, 0xFE, 0x24, 0x18},   // Ф
  {0x7E, 0x40, 0x40, 0x40, 0xFE},   // Ц
  {0x0E, 0x10, 0x10, 0x10, 0xFE},   // Ч
  {0xFE, 0x80, 0xFE, 0x80, 0xFE},   // Ш
  {0x7E, 0x40, 0x7E, 0x40, 0xFE},   // Щ
  {0x02, 0xFE, 0x90, 0x90, 0x60},   // Ъ
  {0xFE, 0x90, 0x90, 0x60, 0xFE},   // Ы
  {0xFE, 0x90, 0x90, 0x90, 0x60},   // Ь
  {0x44, 0x82, 0x92, 0x92, 0x7C},   // Э
  {0xFE, 0x10, 0xFE, 0x82, 0xFE},   // Ю
  {0x8C, 0x52, 0x32, 0x12, 0xFE},   // Я
  {0x7E, 0x4B, 0x4A, 0x4B, 0x42},   // Ё
};

// Перевод буквы в начертание. Индекс: 0..31 = А..Я в порядке Юникода, 32 = Ё.
// Значение: < 0x80 — рисовать этим ASCII-символом встроенным шрифтом;
// >= 0x80 — индекс в RU_FONT (значение минус 0x80).
static const uint8_t RU_MAP[33] = {
       'A',   0x80+0,      'B',   0x80+1,   0x80+2,      'E',
    0x80+3,   0x80+4,   0x80+5,   0x80+6,      'K',   0x80+7,
       'M',      'H',      'O',   0x80+8,      'P',      'C',
       'T',      'Y',   0x80+9,      'X',  0x80+10,  0x80+11,
   0x80+12,  0x80+13,  0x80+14,  0x80+15,  0x80+16,  0x80+17,
   0x80+18,  0x80+19,  0x80+20,
};

// Символы слот-машины 8x8: бит на пиксель в трёх слоях (тусклый/основной/блик),
// по 8 байт на слой. Один источник с эмулятором (scratchpad/slot-sprites.py).
#define SLOT_SYMS 6
static const uint8_t SLOT_SPRITE[SLOT_SYMS][3][8] = {
  {{0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00}, {0x3C, 0x7E, 0xDB, 0xFF, 0x7E, 0x5A, 0x94, 0x49}, {0x00, 0x00, 0x24, 0x00, 0x00, 0x00, 0x00, 0x00}},   // ОСЬМИНОГ
  {{0x1C, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x7E}, {0x00, 0x00, 0xFF, 0xFE, 0xFA, 0x7E, 0x3C, 0x00}, {0x00, 0x00, 0x00, 0x01, 0x01, 0x00, 0x00, 0x00}},   // КОФЕ
  {{0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00}, {0x00, 0x3D, 0x7B, 0xFF, 0xFF, 0x7B, 0x3D, 0x00}, {0x00, 0x00, 0x04, 0x00, 0x00, 0x04, 0x00, 0x00}},   // РЫБКА
  {{0x02, 0x04, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00}, {0x00, 0x00, 0x00, 0x00, 0x1C, 0x38, 0x60, 0xC0}, {0x00, 0x00, 0x02, 0x06, 0x00, 0x00, 0x00, 0x00}},   // СИГАРЕТА
  {{0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00}, {0x3C, 0x66, 0xC3, 0x81, 0x81, 0xC3, 0x66, 0x3C}, {0x00, 0x00, 0x10, 0x00, 0x00, 0x00, 0x00, 0x00}},   // ПУЗЫРЬ
  {{0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00}, {0xFF, 0xFF, 0x06, 0x0C, 0x18, 0x30, 0x30, 0x30}, {0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00}},   // СЕМЁРКА
};
static const uint16_t SLOT_PAL[SLOT_SYMS][3] = {
  {0x5A94, 0x947C, 0xFFFF},   // ОСЬМИНОГ
  {0x7AC7, 0xD5F2, 0xFFFF},   // КОФЕ
  {0x2C11, 0x5E9B, 0xFFFF},   // РЫБКА
  {0xA515, 0xEF5D, 0xFBC2},   // СИГАРЕТА
  {0x2B6F, 0x6EFD, 0xFFFF},   // ПУЗЫРЬ
  {0x93C3, 0xFE88, 0xFFFF},   // СЕМЁРКА
};

// Порог решета копоти. Была формула (x*3 + y*5) & 3, но при dx=dy=1 это
// 3+5=8 ≡ 0 (mod 4): порог ПОСТОЯНЕН вдоль диагонали 45°, и копоть читалась как
// ровная штриховка, а не как дым. Таблица 8x8 (по 16 значений каждого) ломает
// эту регулярность одним чтением из флеша, без арифметики на пиксель.
// Шум границы копоти таблицей 64x64. Раньше это были три синуса НА ПИКСЕЛЬ, и вместе
// с двумя float-делениями они съедали 989мс из 1157мс полной перерисовки (замерено).
// Частоты выбраны кратными 2π/64, чтобы плитка стыковалась сама с собой без шва.
#define SMOG_NOISE_MAX 86              // |шум| в 1/256, нужен для раннего отсева
int8_t smogNoise[64 * 64];

static const uint8_t SMOG_DITHER[64] = {
  2, 0, 3, 1, 2, 3, 0, 1,
  1, 3, 0, 2, 0, 1, 3, 2,
  3, 1, 2, 0, 3, 2, 1, 0,
  0, 2, 1, 3, 1, 0, 2, 3,
  1, 0, 2, 3, 2, 1, 3, 0,
  3, 2, 0, 1, 0, 3, 1, 2,
  0, 3, 1, 2, 3, 0, 2, 1,
  2, 1, 3, 0, 1, 2, 0, 3,
};

// --- предрасчёт: сфера тела и синус ------------------------------------------
#define SPH_R 17
#define SPH_D (2 * SPH_R + 1)
uint16_t sphereTile[SPH_D * SPH_D];
uint8_t  sphRowX0[SPH_D];
uint8_t  sphRowLen[SPH_D];
int8_t   sphereNight = -1;      // для какого уровня ночи собран тайл

#define TENT_SEG 9
float    sinLut[256];
uint16_t tentCol[TENT_SEG];
float    tentTaper[TENT_SEG];

// Кривая щупальца: веса Безье ×256 и цвета сегментов — считаются один раз.
#define ARM_SEG 12
uint16_t armW0[ARM_SEG], armW1[ARM_SEG], armW2[ARM_SEG];
uint16_t armCol[ARM_SEG];
uint8_t  armWidth[ARM_SEG];

enum State { WORKING, WAITING, IDLE, ERR };

// Параметры копоти карточки. Объявление ОБЯЗАНО быть здесь, выше функций: Arduino
// вставляет автоматические прототипы в начало файла, и тип из сигнатуры должен быть
// к тому моменту известен. Считается один раз на карточку, дальше строка рисуется
// отдельно — это нужно для обхода «строка → все карточки», иначе копоть проявляется
// волной, переползая с карточки на карточку (~6000 точек и 19 000 синусов на карточку).
// Приёмник пикселя: на экране — пакетная запись внутри транзакции, в снимке —
// канва. Формула копоти при этом одна, дублировать её нельзя.
struct PixelSink { virtual void px(int x, int y, uint16_t c) = 0; };
struct TftSink : PixelSink {           // только внутри startWrite/endWrite
  void px(int x, int y, uint16_t c) override { tft.writePixel(x, y, c); }
};
struct CanvasSink : PixelSink {
  Adafruit_GFX &g;
  CanvasSink(Adafruit_GFX &t) : g(t) {}
  void px(int x, int y, uint16_t c) override { g.drawPixel(x, y, c); }
};

// Параметры копоти. Всё, что можно, посчитано ОДИН раз на карточку: три синуса и
// два float-деления на пиксель стоили 989мс из 1157мс полной перерисовки (замерено,
// а не угадано), а деление у ESP8266 программное. Внутри строки теперь только
// чтения из таблиц и целые умножения.
struct SmogP {
  bool on;
  int x0, y0, x1, y1, leftTo, rightFrom;
  int reachHi, reachVi;              // запас вбок и по вертикали, целые
  int smq;                           // вес сессии в 1/256
  uint16_t hq[24];                   // (i*256)/reachH — доля запаса вбок по столбцу
  uint16_t base;
};

struct Session {
  bool  active;
  char  id[24];
  char  name[20];
  State state;
  int   sub;          // активных суб-агентов
  int   mb;           // вес транскрипта, МБ — копоть по краям карточки
  uint32_t seed;      // хеш id: характер (темп, размах, фаза, моргание)
  unsigned long poke; // до какого millis() «вздрагивает» после промпта
  unsigned long born; // когда карточка появилась (всплытие после /clear)
};

const int COLS = 3, ROWS = 2;
const int MAX_SESSIONS = COLS * ROWS;
const int W = 320, H = 240;
// замер: что именно стоит дорого в полной перерисовке (гадать уже пробовал)
unsigned long usSmog = 0, usOcto = 0, usBlit = 0, lastRedrawAt = 0; int nOcto = 0;
OffsetCanvas stripBuf(W, STRIP_H);   // полоса сборки экрана и снимка
int cellW, cellH;

Session sessions[MAX_SESSIONS];
Session incoming[MAX_SESSIONS];

// Что сейчас на экране — приезжает от моста, прошивка ничего не решает сама.
int  curScreen = 0, curPage = 1, curPages = 1;
int  nightLevel = 0;          // 0..100, свет по рабочему дню (считает мост)
bool sleeping = false;
bool fullRedraw = false;      // сменилась страница/экран — рисуем всё одним заходом

// Кофейня
struct CafeSeg { int from, to, kind; };   // kind: 0 перерыв, 1 обед, 2 уборка
int cafeSt = 3, cafeNm = 0, cafeDow = 0, cafeTill = 0;
int cafeOm = 0, cafeCm = 0, cafeNet = 0, cafeSegN = 0;
CafeSeg cafeSegs[8];
bool cafeDirty = true;

unsigned long lastTick[MAX_SESSIONS] = {0};
unsigned long lastFrame = 0;      // номер последнего отрисованного кадра общей сетки
unsigned long popupUntil = 0;
int  popupPage = 0, popupPages = 0;
bool popupDrawn = false;

// Вбросы: чисто декоративные сценки. Единственное, что прошивка заводит сама —
// они ничего не сообщают и никаких решений не принимают, гонять их через мост
// было бы шумом ради шума.
unsigned long evFishUntil = 0, evBubUntil = 0, evCrabUntil = 0, evNext = 0;
int evFishCell = -1, evCrabCell = -1;

// --- serial ------------------------------------------------------------------
static const size_t LINE_MAX = 1024;
char lineBuf[LINE_MAX];
size_t lineLen = 0;

#if ESP_DIAG
unsigned long lastStat = 0, maxFrameUs = 0, maxDrawUs = 0, maxBlitUs = 0;
uint16_t diagSnaps = 0, diagBadJson = 0, diagCells = 0;
#endif

// --- прототипы (Arduino их генерит сам, но с явными надёжнее) ----------------
void buildSphere(int night);
void redrawAll();
void redrawRect(int rx, int ry, int rw, int rh);
void composeCurrentScreen(OffsetCanvas &g, int top, int bot, int left, int right, float tt);
void sendShot();
void redrawCell(int i);
void cardFrame(Adafruit_GFX &g, int col, int row, Session &s);
void smogRow(PixelSink &sink, const SmogP &p, int y);
void octoWindow(int col, int row, int &wx, int &wy, int &wcx, int &wcy);
void updateOctopusArea(int col, int row, Session &s, float tt);
void drawOctopus(Adafruit_GFX &g, int cx, int cy, Session &s, float tt);
void applySnapshot();
void handleLine(const char *line);
void composeCafe(Adafruit_GFX &g);
void composeCafeScene(OffsetCanvas &g, float tt);
void cafeTime(int minutes, char *out);
void composeRoulette(OffsetCanvas &g, int top, int bot, int left, int right, float tt);
void composeSlots(OffsetCanvas &g, int top, int bot, int left, int right, float tt);
void slotKick();
void slotPhysics(unsigned long now);
bool slotHot(int i);
void roulKick();
void roulPhysics(unsigned long now);
void roulBubblesStep(float dt, float spin);
void animateCafeScene(float tt);
void baristaArt(Adafruit_GFX &g, float tt);
void animateBarista(float tt);
void propsArt(Adafruit_GFX &g, float tt, float fill, bool steam, bool pouring);
void animateProps(float tt, float fill, bool steam, bool pouring);
void cafePour(float tt, float &fill, bool &steam, bool &pouring);
void drawPopup();
void hidePopup();
void enterSleep();
void leaveSleep();

// =============================================================================
// Мелкая математика
// =============================================================================
static inline float fastSin(float x) { return sinLut[(int32_t)(x * 40.7436f) & 255]; }
static inline float fastCos(float x) { return sinLut[((int32_t)(x * 40.7436f) + 64) & 255]; }
static inline int   iround(float x)  { return (int)(x < 0 ? x - 0.5f : x + 0.5f); }

// Целочисленный вариант: q в 1/256. Нужен там, где смешивание идёт на пиксель —
// float-деления и умножения в таком цикле у ESP8266 программные и стоят дорого.
// SPI.writeBytes на ESP8266 читает буфер 32-битными словами, поэтому адрес обязан
// быть выровнен на 4. Прямоугольник может начинаться на ЛЮБОМ x (место всплывашки,
// реквизит кофейни) — и строка внутри буфера оказывалась выровненной лишь на 2:
// Exception (9), ребут, снова всплывашка, снова ребут. Поэтому строка всегда
// копируется в выровненный буфер; заодно исходный буфер не портится свапом.
static uint16_t blitLine[W] __attribute__((aligned(4)));

void blitRow(const uint16_t *src, int n) {
  for (int i = 0; i < n; i++) { uint16_t v = src[i]; blitLine[i] = (uint16_t)((v << 8) | (v >> 8)); }
  SPI.writeBytes((uint8_t *)blitLine, n * 2);
}

uint16_t lerp565q(uint16_t a, uint16_t b, int q) {
  if (q < 0) q = 0;
  if (q > 256) q = 256;
  int r = (a >> 11) & 31, g = (a >> 5) & 63, bl = a & 31;
  int r2 = (b >> 11) & 31, g2 = (b >> 5) & 63, b2 = b & 31;
  r += ((r2 - r) * q) >> 8; g += ((g2 - g) * q) >> 8; bl += ((b2 - bl) * q) >> 8;
  return (uint16_t)((r << 11) | (g << 5) | bl);
}


uint16_t lerp565(uint16_t a, uint16_t b, float t) {
  if (t < 0) t = 0;
  if (t > 1) t = 1;
  int r = (a >> 11) & 31, g = (a >> 5) & 63, bl = a & 31;
  int r2 = (b >> 11) & 31, g2 = (b >> 5) & 63, b2 = b & 31;
  r += (int)((r2 - r) * t); g += (int)((g2 - g) * t); bl += (int)((b2 - bl) * t);
  return (uint16_t)((r << 11) | (g << 5) | bl);
}

// Рисует строку с кириллицей. Латиница и цифры идут встроенным шрифтом через
// write() (он виртуальный, поэтому холст со смещением их перехватывает), русские
// буквы — своей таблицей. Шаг 6*scale на символ, как у GFX. Возвращает ширину.
//
// Разбор UTF-8 прямо здесь: русские заглавные — это 0xD0 0x90..0x9F (А..П),
// 0xD1 0x80..0x8F (Р..Я) и 0xD0 0x81 (Ё). Перекодировать на мосту в однобайтовую
// кодировку было бы дешевле по трафику, но JSON по спецификации UTF-8, и лишний
// слой перекодировки — это ещё одно место, где стороны разъезжаются.
int textWidthRu(const char *s, int scale) {
  int n = 0;
  for (const uint8_t *p = (const uint8_t *)s; *p; p++) {
    if (*p >= 0xC0) { if (p[1]) p++; }          // ведущий байт двухбайтовой буквы
    n++;
  }
  return n * 6 * scale;
}

void drawTextRu(Adafruit_GFX &g, int x, int y, const char *s, uint16_t col, int scale) {
  g.setTextColor(col);
  g.setTextSize(scale);
  for (const uint8_t *p = (const uint8_t *)s; *p; p++) {
    int glyph = -1;                             // >=0 — своё начертание
    uint8_t ascii = 0;
    if (*p < 0x80) {
      ascii = *p;
    } else if ((*p == 0xD0 || *p == 0xD1) && p[1]) {
      uint8_t lead = *p, b = *++p;
      int idx = -1;
      if (lead == 0xD0 && b == 0x81) idx = 32;             // Ё
      else if (lead == 0xD0 && b >= 0x90 && b <= 0xBF) idx = b - 0x90;
      else if (lead == 0xD1 && b >= 0x80 && b <= 0x8F) idx = 16 + (b - 0x80);
      if (idx >= 0 && idx <= 32) {
        uint8_t v = RU_MAP[idx];
        if (v & 0x80) glyph = v & 0x7F; else ascii = v;
      } else {
        ascii = '?';                            // строчные и прочее не рисуем
      }
    } else {
      continue;                                 // хвостовой байт — уже съеден
    }

    if (glyph >= 0) {
      for (int cx = 0; cx < 5; cx++) {
        uint8_t bits = RU_FONT[glyph][cx];
        if (!bits) continue;
        for (int cy = 0; cy < 8; cy++) {
          if (!(bits & (1 << cy))) continue;
          if (scale == 1) g.drawPixel(x + cx, y + cy, col);
          else            g.fillRect(x + cx * scale, y + cy * scale, scale, scale, col);
        }
      }
    } else if (ascii) {
      g.setCursor(x, y);
      g.write(ascii);
    }
    x += 6 * scale;
  }
}


// FNV-1a: характер сессии выводится из её id, поэтому не меняется при переезде
// карточки между страницами.
uint32_t hash32(const char *s) {
  uint32_t h = 2166136261UL;
  for (; *s; ++s) { h ^= (uint8_t)*s; h *= 16777619UL; }
  return h;
}

uint16_t stateColor(State s) {
  switch (s) {
    case WORKING: return C_WORKING;
    case WAITING: return C_WAITING;
    case IDLE:    return C_IDLE;
    case ERR:     return C_ERROR;
  }
  return C_IDLE;
}

// Период кадра по статусу: спящему IDLE 25 к/с не нужны, а шина одна на всех.
// Кадр общий для всех карточек, а статус задаёт, через сколько кадров карточка
// обновляется. Раньше у каждой был свой таймер со своим порогом (40/80/160мс), и
// после перелистывания они расходились по фазе: обновления сыпались вразнобой, и
// это читалось как рваная частота кадров. На общей сетке кратные делители всегда
// совпадают — раз в 4 кадра обновляются все.
#define FRAME_MS 40
#define ROUL_FRAME_MS 50   // экран рулетки: свой, стабильный период кадра
uint8_t frameDiv(State s) {
  switch (s) {
    case WORKING: return 1;
    case ERR:     return 1;
    case WAITING: return 2;
    default:      return 4;
  }
}

// =============================================================================
// Энкодер
// =============================================================================
volatile int8_t  encDelta = 0;
volatile uint8_t encPrev  = 0;

void IRAM_ATTR encISR() {
  static const int8_t TBL[16] = {0, -1, 1, 0, 1, 0, 0, -1, -1, 0, 0, 1, 0, 1, -1, 0};
  encPrev = ((encPrev << 2) | (digitalRead(ENC_A) << 1) | digitalRead(ENC_B)) & 0x0f;
  encDelta += TBL[encPrev];
}

bool     swDown = false;
bool     swHandled = false;         // удержание уже отправлено — на отпускании молчим
unsigned long swSince = 0, swChanged = 0;
const unsigned long SW_DEBOUNCE = 30, SW_HOLD = 1000;

void sendEnc(const char *what, bool held) {
  Serial.print(F("{\"enc\":\""));
  Serial.print(what);
  if (held) Serial.print(F("\",\"k\":1}"));
  else      Serial.print(F("\"}"));
  Serial.println();
}

void pollEncoder() {
  // Кнопку опрашиваем ПЕРЕД вращением. Раньше было наоборот, и первый щелчок с
  // зажатой кнопкой видел ещё старое swDown=false: он уходил как листание страниц
  // (а на рулетке и автомате — как рывок барабана), и переключать экраны начинало
  // только со второго щелчка. Порядок внутри одного вызова и есть весь баг.
  bool down = (digitalRead(ENC_SW) == LOW);
  unsigned long now = millis();
  if (down != swDown && now - swChanged > SW_DEBOUNCE) {
    swChanged = now;
    swDown = down;
    if (down) { swSince = now; swHandled = false; }
    else if (!swHandled)      sendEnc("key", false);
  }
  if (swDown && !swHandled && now - swSince >= SW_HOLD) {
    swHandled = true;
    sendEnc("hold", false);
  }

  // вращение: накопитель прерываний делим на 4 — один детент энкодера
  int8_t d;
  noInterrupts();
  d = encDelta;
  if (d >= 4 || d <= -4) encDelta = d % 4; else d = 0;
  interrupts();
  if (d >= 4 || d <= -4) {
    // Знак ЗАВИСИТ ОТ ПАЙКИ: какой канал энкодера попал на D1, а какой на D2. У нашей
    // сборки вышло наоборот — вращение вправо давало «ccw», и экраны листались назад.
    // Инвертируем здесь, у источника, чтобы «вправо = вперёд» было верно везде:
    // и для экранов с зажатой кнопкой, и для страниц аквариума.
    int steps = -d / 4;
    for (int i = 0; i < abs(steps); i++) {
      // На экране рулетки вращение НЕ уходит мостом как «листание»: барабан
      // обязан отзываться на щелчок мгновенно, а круг через мост это ~50мс.
      // Мосту уйдёт одно событие "spin", когда порог будет перевален.
      if (curScreen == 2 && !swDown) roulKick();
      else if (curScreen == 3 && !swDown) slotKick();
      else sendEnc(steps > 0 ? "cw" : "ccw", swDown);
    }
    if (swDown) swHandled = true;   // это было «крутить с зажатой» — не слать key
  }
}

// =============================================================================
// Предрасчёт
// =============================================================================
void buildSphere(int night) {
  const int R = SPH_R;
  const float k = night / 100.0f * 0.30f;      // ночь — смена палитры ДО цикла
  const uint16_t L = lerp565(BODY_LIGHT, BG, k), D = lerp565(BODY_BOT, BG, k);
  const float lx = -R * 0.34f, ly = -R * 0.42f, spread = 1.5f;
  for (int y = -R; y <= R; y++) {
    for (int x = -R; x <= R; x++) {
      int idx = (y + R) * SPH_D + (x + R);
      if (x * x + y * y > R * R) continue;
      float nx = (x - lx) / (float)R, ny = (y - ly) / (float)R;
      float d = sqrtf(nx * nx + ny * ny) / spread;
      sphereTile[idx] = lerp565(L, D, d);
    }
  }
  for (int yy = 0; yy < SPH_D; yy++) {
    int y = yy - R;
    int dx = (int)floorf(sqrtf((float)(R * R - y * y)));
    sphRowX0[yy]  = (uint8_t)(R - dx);
    sphRowLen[yy] = (uint8_t)(2 * dx + 1);
  }
  for (int s = 0; s < TENT_SEG; s++) {
    uint16_t dk = lerp565(BODY_DK, BG, k), tip = lerp565(TENT_TIP, BG, k);
    tentCol[s]   = lerp565(dk, tip, (s < 3) ? 0.0f : (float)(s - 3) / (TENT_SEG - 3));
    tentTaper[s] = 0.2f + (float)s / TENT_SEG;
  }
  for (int i = 0; i < ARM_SEG; i++) {
    float t = (float)i / (ARM_SEG - 1), u = 1 - t;
    armW0[i] = (uint16_t)(u * u * 256);
    armW1[i] = (uint16_t)(2 * u * t * 256);
    armW2[i] = (uint16_t)(t * t * 256);
    armCol[i] = lerp565(lerp565(BODY_DK, BG, k), lerp565(TENT_TIP, BG, k), t);
    armWidth[i] = (uint8_t)max(1, 4 - iround(t * 3));
  }
  sphereNight = night;
}

void buildSin() {
  for (int i = 0; i < 256; i++) sinLut[i] = sinf(i * (6.2831853f / 256.0f));
}

// Шум копоти: те же три волны разной частоты, что считались на пиксель, но
// посчитанные один раз на старте. Частоты кратны 2π/64 — плитка стыкуется без шва.
void buildSmogNoise() {
  const float k = 6.2831853f / 64.0f;
  for (int y = 0; y < 64; y++)
    for (int x = 0; x < 64; x++) {
      float n = sinf((x * 3 + y * 1) * k) * 2.0f
                + sinf((y * 3 - x * 1) * k + 2.1f) * 1.6f
                + sinf((x + y) * 2 * k + 4.2f) * 1.2f;
      smogNoise[(y << 6) | x] = (int8_t)(n * (SMOG_NOISE_MAX / 4.8f));
    }
}

// =============================================================================
// Примитивы осьминога
// =============================================================================
void sphereBody(Adafruit_GFX &g, int cx, int hy) {
  for (int yy = 0; yy < SPH_D; yy++) {
    int y = hy - SPH_R + yy;
    int sx = sphRowX0[yy], len = sphRowLen[yy];
    const uint16_t *row = &sphereTile[yy * SPH_D + sx];
    int x = cx - SPH_R + sx;
    for (int i = 0; i < len; i++) g.drawPixel(x + i, y, row[i]);
  }
  const int R = SPH_R;
  g.fillCircle(cx - (int)(R * 0.32f), hy - (int)(R * 0.38f), (int)(R * 0.26f),
               lerp565(BODY_LIGHT, 0xFFFF, 0.6f));
  g.fillCircle(cx - (int)(R * 0.30f), hy - (int)(R * 0.36f), (int)(R * 0.12f), 0xFFFF);
}

void tentacles(Adafruit_GFX &g, int cx, int hy, int dir, float tt, float speed, float amp) {
  const int R = SPH_R, legs = 6;
  const int baseHW = iround(R * 0.66f);
  const int rootY = hy + dir * (R - 4);
  const float step = (2.0f * baseHW - 2) / (legs - 1);
  for (int i = 0; i < legs; i++) {
    float bx = cx - baseHW + 1 + i * step;
    float ph = tt * speed + i * 0.7f;
    for (int s = 0; s < TENT_SEG; s++) {
      float sway = fastSin(ph + s * 0.5f) * amp * tentTaper[s];
      int w = (s < 3) ? 3 : (s < 6 ? 2 : 1);
      g.fillRect(iround(bx + sway) - (w >> 1), rootY + dir * (s * 2), w, 2, tentCol[s]);
    }
  }
}

// Гнущееся щупальце: квадратичная кривая на целых числах (у ESP8266 нет FPU).
void arm(Adafruit_GFX &g, int x0, int y0, int x1, int y1, float bend, float tt, float speed) {
  int dx = x1 - x0, dy = y1 - y0;
  int len = abs(dx) + abs(dy);           // манхэттен вместо sqrt: только для нормировки
  if (len < 1) len = 1;
  float b = (bend + fastSin(tt * speed) * 2.0f) / len;
  int ccx = iround((x0 + x1) / 2.0f - dy * b), ccy = iround((y0 + y1) / 2.0f + dx * b);
  // Сегменты СОЕДИНЯЮТСЯ линией. Двенадцать отдельных квадратиков хватало на
  // короткую руку, но на длинном вылете (щупальце до рукояти автомата) между ними
  // появлялись разрывы, и щупальце читалось как пунктир.
  int prevX = 0, prevY = 0;
  for (int k = 0; k < ARM_SEG; k++) {
    int x = (armW0[k] * x0 + armW1[k] * ccx + armW2[k] * x1) >> 8;
    int y = (armW0[k] * y0 + armW1[k] * ccy + armW2[k] * y1) >> 8;
    int w = armWidth[k];
    if (k) {
      g.drawLine(prevX, prevY, x, y, armCol[k]);
      if (w > 2) g.drawLine(prevX, prevY + 1, x, y + 1, armCol[k]);
    }
    g.fillRect(x - (w >> 1), y - (w >> 1), w, w, armCol[k]);
    prevX = x; prevY = y;
  }
}

// Дым — связная струйка, а не россыпь клубов: на 68×72 только так и читается.
void smokeTrail(Adafruit_GFX &g, int x, int y, float tt, int len, float sway, uint16_t col) {
  int w1 = (int)(tt * 7);
  for (int i = 0; i < len; i++) {
    float f = (float)i / len;
    int cx = x + iround(fastSin(tt * 1.5f + i * 0.5f) * sway * (0.3f + f * 1.3f));
    int cy = y - i;
    uint16_t c = lerp565(col, BG, f * f * 0.9f);
    g.drawPixel(cx, cy, c);
    if (f > 0.2f && ((i + w1) & 1) == 0) g.drawPixel(cx + 1, cy, c);
    if (f > 0.55f && ((i * 3 + w1) & 3) == 0) g.drawPixel(cx - 1, cy, c);
  }
}

// Разовый выдох: три расходящиеся струи, распухают и опадают.
void smokeBurst(Adafruit_GFX &g, int x, int y, float p, int rise, int drift, uint16_t col) {
  if (p <= 0 || p >= 1) return;
  float grow = fastSin(p * 3.1416f);
  uint16_t c = lerp565(col, BG, p * 0.55f);
  for (int k = -1; k <= 1; k++) {
    int ax = x + iround(k * grow * 7 + drift * p * 0.4f);
    int ay = y - iround(grow * 2);
    int len = (int)max(4.0f, rise * grow * (k == 0 ? 0.9f : 0.6f));
    for (int i = 0; i < len; i++) {
      float f = (float)i / len;
      int px = ax + iround(k * f * grow * 8 + fastSin(p * 9 + i * 0.6f + k) * 1.6f);
      g.drawPixel(px, ay - i, lerp565(c, BG, f * f * 0.85f));
    }
  }
}

void cigarette(Adafruit_GFX &g, int x, int y, bool hot, int bodyLen) {
  g.fillRect(x, y, 3, 3, FILTERC);
  g.fillRect(x + 3, y, bodyLen, 3, CIG_BODY);
  g.drawFastHLine(x + 4, y, bodyLen - 3, CIG_HI);
  g.fillRect(x + 3 + bodyLen, y, 3, 3, hot ? EMBER_HOT : EMBER);
}

void drawEyes(Adafruit_GFX &g, int cx, int eyY, State state, float tt, bool wide, float blinkT) {
  int exL = cx - 6, exR = cx + 6;
  if (state == ERR) {
    g.drawLine(exL - 3, eyY - 3, exL + 3, eyY + 3, C_ERROR);
    g.drawLine(exL + 3, eyY - 3, exL - 3, eyY + 3, C_ERROR);
    g.drawLine(exR - 3, eyY - 3, exR + 3, eyY + 3, C_ERROR);
    g.drawLine(exR + 3, eyY - 3, exR - 3, eyY + 3, C_ERROR);
    return;
  }
  bool blink = (state != IDLE) && (fmodf(tt, blinkT) < 0.11f);
  if (state == IDLE || blink) {
    g.drawFastHLine(exL - 3, eyY, 6, EYE_LIGHT);
    g.drawFastHLine(exR - 3, eyY, 6, EYE_LIGHT);
    return;
  }
  int look = (state == WORKING) ? iround(fastSin(tt * 2.2f) * 1.4f) : 0;
  int r = wide ? 5 : (state == WAITING ? 4 : 3);
  g.fillCircle(exL, eyY, r, EYE_LIGHT);
  g.fillCircle(exR, eyY, r, EYE_LIGHT);
  g.fillCircle(exL + look, eyY + 1, 1, EYE_DARK);
  g.fillCircle(exR + look, eyY + 1, 1, EYE_DARK);
  g.drawPixel(exL + look - 1, eyY - 1, GLINT);
  g.drawPixel(exR + look - 1, eyY - 1, GLINT);
}

// Вбросы внутри окна ячейки: бесплатны, окно и так перерисовывается каждый тик.
void drawFish(Adafruit_GFX &g, int cx, int hy, float p, unsigned long now) {
  int x = cx + 36 - iround(p * 76);
  int y = hy - 4 + iround(fastSin(p * 6.2832f) * 12);
  g.fillRect(x - 3, y - 2, 7, 4, FISH_B);
  g.fillRect(x - 5, y - 1, 2, 2, FISH_B);
  g.drawFastHLine(x - 3, y - 3, 5, FISH_D);
  g.drawFastHLine(x - 3, y + 2, 5, FISH_D);
  int t = ((now % 400) < 200) ? 1 : 2;
  g.fillRect(x + 4, y - t, 2, 2 * t, FISH_B);
  g.drawPixel(x - 3, y - 1, 0xFFFF);
}

void drawBubbles(Adafruit_GFX &g, int cx, int hy, float p, uint32_t seed) {
  for (int k = 0; k < 7; k++) {
    float f = fmodf(p * 1.6f + ((seed >> k) & 7) / 7.0f, 1.0f);
    int x = cx - 24 + ((k * 37 + (seed & 15)) % 48);
    int y = hy + 26 - iround(f * 54);
    g.drawCircle(x, y, f < 0.5f ? 1 : 2, lerp565(BUBBLE, BG, f * 0.75f));
  }
}

void drawCrab(Adafruit_GFX &g, int cx, int hy, float p, unsigned long now) {
  int x = cx - 38 + iround(p * 76), base = hy + 36;
  g.fillRect(x - 3, base - 4, 7, 4, CRAB_B);
  g.drawPixel(x - 4, base - 5, CRAB_B);
  g.drawPixel(x + 4, base - 5, CRAB_B);
  g.drawPixel(x - 2, base - 5, 0xFFFF);
  g.drawPixel(x + 2, base - 5, 0xFFFF);
  int step = ((now / 120) & 1);
  g.drawPixel(x - 3, base + step, CRAB_D);
  g.drawPixel(x + 3, base + 1 - step, CRAB_D);
}

// =============================================================================
// Осьминог целиком
// =============================================================================
void drawOctopus(Adafruit_GFX &g, int cx, int cy, Session &s, float tt) {
  unsigned long now = millis();
  // характер: свой темп, размах, фаза курения и ритм моргания
  float pSpd = 0.86f + ((s.seed >> 3) & 7) / 24.0f;
  float pAmp = 0.85f + ((s.seed >> 7) & 7) / 20.0f;
  float pPh  = ((s.seed >> 11) & 31) / 31.0f;
  float pBlink = 3.2f + ((s.seed >> 17) & 7) * 0.4f;

  bool flipped = (s.state == ERR);
  int dir = flipped ? -1 : 1;
  float speed = (s.state == WORKING ? 7.5f : s.state == WAITING ? 2.2f
                 : s.state == IDLE ? 1.4f : 5.5f) * pSpd;
  float amp = (s.state == WORKING ? 2.6f : s.state == WAITING ? 1.0f
               : s.state == IDLE ? 0.7f : 2.0f) * pAmp;
  int bob = (s.state == IDLE) ? 0
            : iround(fastSin(tt * (s.state == WORKING ? 4.4f : 2.4f)) * 1.2f);

  // вздрагивание на промпт: видно, что хук дошёл и мост жив
  bool poked = (s.poke > now);
  if (poked) {
    float k = (s.poke - now) / 400.0f;
    bob -= iround(fastSin((s.poke - now) * 0.06f) * 4 * k);
  }
  // после /clear карточка всплывает снизу — награда за уборку
  int riseY = 0;
  if (s.born && now - s.born < 1400) riseY = iround((1 - (now - s.born) / 1400.0f) * 26);

  int hy = cy + bob + riseY + (flipped ? 5 : 0);

  tentacles(g, cx, hy, dir, tt, speed, amp);
  sphereBody(g, cx, hy);
  drawEyes(g, cx, hy - dir * 4, s.state, tt, poked, pBlink);

  if (s.state == WORKING) {
    // Щупальце опускает сигарету и подносит обратно ко рту. Ход ВЕРТИКАЛЬНЫЙ:
    // вбок в ячейке не увезти — справа от тела 18px против 21px сигареты.
    float ph = fmodf(tt / 4.2f + pPh, 1.0f);
    float up = ph < 0.14f ? ph / 0.14f
               : ph < 0.34f ? 1.0f
               : ph < 0.50f ? 1 - (ph - 0.34f) / 0.16f : 0.0f;
    bool drag = (ph >= 0.14f && ph < 0.34f);
    bool exhale = (ph >= 0.52f && ph < 0.78f);
    bool ash = (ph >= 0.82f && ph < 0.90f);
    int gx = cx + 4 - iround(3 * up), gy = hy + 20 - iround(15 * up);
    arm(g, cx + 15, hy + 10, gx + 13, gy + 3, 4, tt, 1.4f);
    cigarette(g, gx, gy, drag, 15);
    int tipx = gx + 6 + 15, tipy = gy - 1;
    if (drag) smokeTrail(g, tipx + 1, tipy - 1, tt, 7, 1.2f, SMOKE);
    // Выдох начинается У РТА (там, где стоял фильтр: cx+1, hy+5), а вбок уходит
    // уже сносом по мере роста. Раньше стартовал в cx+9 — облако висело правее
    // рта и читалось как чужое.
    if (exhale) smokeBurst(g, cx + 2, hy + 5, (ph - 0.52f) / 0.26f, 13, 12, SMOKE);
    if (ash) {
      float f = (ph - 0.82f) / 0.08f;
      for (int k = 0; k < 3; k++)
        g.drawPixel(tipx - 2 + k, tipy + 3 + iround(f * 12) - k * 2, lerp565(SMOKE, BG, 0.35f));
    }
  } else if (s.state == WAITING) {
    int yy = hy - SPH_R - 8;
    g.fillRect(cx + 8, yy, 4, 1, C_WAITING);
    g.drawPixel(cx + 11, yy + 1, C_WAITING);
    g.drawPixel(cx + 10, yy + 2, C_WAITING);
    g.drawPixel(cx + 10, yy + 4, C_WAITING);
  } else if (s.state == IDLE) {
    int zt = ((int)(tt * 1.2f)) % 3;
    for (int z = 0; z <= zt; z++) {
      int zx = cx + 7 + z * 4, zy = hy - SPH_R - 2 - z * 5;
      g.fillRect(zx, zy, 3, 1, C_IDLE);
      g.fillRect(zx, zy + 2, 3, 1, C_IDLE);
      g.drawLine(zx + 2, zy, zx, zy + 2, C_IDLE);
    }
  }

  // суб-агенты: пузырьки-искры на орбите
  int nsub = s.sub > 5 ? 5 : s.sub;
  for (int k = 0; k < nsub; k++) {
    float a = tt * 1.0f + k * (6.2832f / nsub);
    int ox = cx + iround(fastCos(a) * (SPH_R + 9));
    int oy = hy - 2 + iround(fastSin(a) * (SPH_R * 0.62f));
    g.drawPixel(cx + iround(fastCos(a - 0.4f) * (SPH_R + 9)),
                hy - 2 + iround(fastSin(a - 0.4f) * (SPH_R * 0.62f)), SUBA_D);
    g.fillCircle(ox, oy, 2, SUBA);
    g.drawPixel(ox - 1, oy - 1, 0xFFFF);
  }
}

// =============================================================================
// Сетка, карточки, копоть
// =============================================================================
void composeGrid(Adafruit_GFX &g) {
  g.fillScreen(BG);
  for (int i = 1; i < COLS; i++) g.drawFastVLine(i * cellW, 0, H, GRIDLINE);
  for (int j = 1; j < ROWS; j++) g.drawFastHLine(0, j * cellH, W, GRIDLINE);
}

void cardFrame(Adafruit_GFX &g, int col, int row, Session &s) {
  int bw = cellW - 4, bh = cellH - 4;
  int x0 = col * cellW + 2, y0 = row * cellH + 2;
  g.drawRect(x0, y0, bw, bh, stateColor(s.state));
  // Имя с ФОНОМ: у тяжёлой сессии нижний слой копоти доходит до строки имени и
  // оно тонуло в решете. Фон даёт каждой букве чистую клетку, а копоть вокруг
  // остаётся — шкала веса не страдает.
  g.setTextColor(0xCE79, BG);
  g.setTextSize(1);
  g.setCursor(x0 + bw / 2 - (int)(strlen(s.name) * 3), y0 + bh - 10);
  g.print(s.name);
}

// Копоть = вес транскрипта. Ползёт от КРАЁВ внутрь и никогда не касается окна
// осьминога (оно в середине, 68×72). Это статика: рисуется вместе с рамкой,
// в кадре анимации не стоит ничего.
//
void smogParams(int col, int row, int mb, SmogP &p) {
  p.on = (mb >= 1);
  if (!p.on) return;
  // Корень, а не линейка: у сессий предел 15-20 МБ, но живут они в основном
  // в диапазоне 2-8 МБ. При линейной шкале там были бы неразличимые 3 пикселя.
  float sm = sqrtf(min(1.0f, mb / 20.0f));
  p.smq = (int)(sm * 256);
  p.x0 = col * cellW + 3;
  p.y0 = row * cellH + 3;
  p.x1 = col * cellW + cellW - 4;
  p.y1 = row * cellH + cellH - 4;
  // Рост НЕСИММЕТРИЧНЫЙ. Вбок до окна осьминога всего 16px, и симметричная копоть
  // упиралась в них уже к 6 МБ — дальше отличить 6 от 20 было нечем. Сверху и снизу
  // места 22px, поэтому вверх слой растёт втрое сильнее: высота видна боковым
  // зрением, в отличие от плотности решета.
  p.reachHi = 2 + (p.smq * 6 >> 8);   // вбок: 2..8
  p.reachVi = 3 + (p.smq * 13 >> 8);  // вверх и вниз: 3..16
  // доля запаса вбок по столбцу — считается один раз, в строке только чтение
  for (int i = 0; i < 24; i++) p.hq[i] = (uint16_t)((i * 256) / p.reachHi);
  int lim = p.reachHi + 5;
  // Границы полос ЦЕЛЫЕ. Со дробными сравнение x < x1 - lim и присваивание
  // x = (int)(x1 - lim - 1) зацикливались: усечение возвращало x назад, инкремент
  // снова попадал в условие. Плата зависала и уходила по watchdog — в эмуляторе
  // бага не было, там x дробный.
  p.leftTo = p.x0 + lim;
  p.rightFrom = p.x1 - lim;
  p.base = lerp565(SMOKE, SMOG_HI, sm);   // тяжёлая сессия ещё и ярче
}

// Одна строка копоти. Транзакцию открывает вызывающий: каждый drawPixel у Adafruit
// это отдельная SPI-транзакция с дёрганьем CS, и на шесть карточек их выходило
// под 15 тысяч подряд — ESP уходил в перезагрузку.
void smogRow(PixelSink &sink, const SmogP &p, int y) {
  int dvRaw = min(y - p.y0, p.y1 - y);
  int vq = (dvRaw << 8) / p.reachVi;             // единственное деление на строку
  bool rowFar = vq > 256 + SMOG_NOISE_MAX;       // даже максимум шума сюда не дотянет
  for (int x = p.x0; x <= p.x1; x++) {
    // прыжок гарантированно вперёд: x++ доведёт ровно до rightFrom
    if (rowFar && x > p.leftTo && x < p.rightFrom) { x = p.rightFrom - 1; continue; }
    int i = min(x - p.x0, p.x1 - x);
    // t в 1/256: 0 у самого края, 256 на границе облака. Каждая ось нормируется
    // на свой запас, поэтому слой сверху толстый, а сбоку тонкий.
    int tq = min(i < 24 ? (int)p.hq[i] : 1024, vq);
    if (tq > 256 + SMOG_NOISE_MAX) continue;     // до облака далеко — шум не считаем
    tq += smogNoise[((y & 63) << 6) | (x & 63)]; // волнистая граница: облако, не рамка
    if (tq >= 256) continue;
    if (tq < 0) tq = 0;
    // Плюс два признака к высоте: плотность решета и яркость.
    int q = ((256 - tq) * (256 + 3 * p.smq) + 32768) >> 16;
    if (q <= 0 || SMOG_DITHER[(y & 7) * 8 + (x & 7)] >= q) continue;
    int fadeq = 26 + (tq >> 1) + (((256 - p.smq) * 90) >> 8);
    sink.px(x, y, lerp565q(p.base, BG, fadeq));
  }
}

TftSink tftSink;


// Копоть всех карточек РАЗОМ: внешний цикл — строка, внутренний — карточки.
// Копоть наплывает сверху вниз по всему экрану как одно событие, а не переползает
// с карточки на карточку.

// Окно осьминога — ЕДИНСТВЕННОЕ определение на обе отрисовки: и на кадр анимации
// (что она чистит и выливает), и на полную перерисовку (докуда осьминогу можно
// рисовать). Пока границы задавались в двух местах, композиция рисовала шире окна,
// анимация эти пиксели не обновляла, и у имени оставался мусор от клубов дыма.
void octoWindow(int col, int row, int &wx, int &wy, int &wcx, int &wcy) {
  int bw = cellW - 4, bh = cellH - 4;
  int x0 = col * cellW + 2, y0 = row * cellH + 2;
  wcx = x0 + bw / 2;                    // центр осьминога
  wcy = y0 + bh / 2 - 6;
  wx = wcx - LCX;                       // левый верхний угол окна
  wy = wcy - LCY;
}

void updateOctopusArea(int col, int row, Session &s, float tt) {
  int bw = cellW - 4, bh = cellH - 4;
  int x0 = col * cellW + 2, y0 = row * cellH + 2;
  int cx, cy, wx, wy;
  octoWindow(col, row, wx, wy, cx, cy);

#if ESP_DIAG
  unsigned long d0 = micros();
#endif
  octoBuf.fillScreen(BG);
  drawOctopus(octoBuf, LCX, LCY, s, tt);

  int idx = row * COLS + col;
  unsigned long now = millis();
  if (evFishUntil > now && evFishCell == idx)
    drawFish(octoBuf, LCX, LCY, 1 - (evFishUntil - now) / 6500.0f, now);
  if (evCrabUntil > now && evCrabCell == idx)
    drawCrab(octoBuf, LCX, LCY, 1 - (evCrabUntil - now) / 9000.0f, now);
  if (evBubUntil > now)
    drawBubbles(octoBuf, LCX, LCY, 1 - (evBubUntil - now) / 4000.0f, s.seed);

#if ESP_DIAG
  unsigned long d1 = micros();
#endif
  uint16_t *bb = octoBuf.getBuffer();
  uint32_t px = (uint32_t)BUF_W * OCTO_H;
  for (uint32_t i = 0; i < px; i++) { uint16_t v = bb[i]; bb[i] = (uint16_t)((v << 8) | (v >> 8)); }
  tft.startWrite();
  tft.setAddrWindow(wx, wy, BUF_W, OCTO_H);
  SPI.writeBytes((uint8_t *)bb, px * 2);
  tft.endWrite();
  // обратный свап не нужен: следующий кадр начинается с fillScreen
#if ESP_DIAG
  unsigned long d2 = micros();
  if (d1 - d0 > maxDrawUs) maxDrawUs = d1 - d0;
  if (d2 - d1 > maxBlitUs) maxBlitUs = d2 - d1;
#endif

  bool blink = (s.state != WAITING) || (((int)(tt * 3)) & 1);
  tft.fillRect(x0 + 3, y0 + 3, 3, 3, blink ? stateColor(s.state) : BG);
}


void redrawCell(int i) {
  redrawRect((i % COLS) * cellW, (i / COLS) * cellH, cellW, cellH);
  if (sessions[i].active) lastTick[i] = millis();
}

// Два прохода, и порядок здесь важен для восприятия.
// Раньше карточка рисовалась целиком (копоть + рамка + имя), и только потом
// следующая — а копоть тяжёлая, ~10мс на карточку. Из-за этого рамки с именами
// выползали по очереди, и смена страницы читалась как марш слева направо.
// Теперь структура появляется разом, а копоть проявляется следом: она фоновая
// текстура, её постепенное появление глазу не мешает.
// Совсем одновременно нельзя: под полный кадр 320×240 нужно 150 КБ, у ESP их нет.
// =============================================================================
// Экран рулетки обеда
// =============================================================================
// Список мест и победителя держит МОСТ: список — состояние (правится без
// перепрошивки), «не повторять прошлого» — правило, которому нужна память.
// Прошивке остаётся физика барабана и рисование.
//
// Числа физики подобраны моделью и проверены сквозняком в эмуляторе, а не на глаз:
// у KY-040 20 детентов на оборот, а рукой реально провернуть один оборот. Ленивый
// темп (3 щелчка/с) не раскручивает вовсе, спокойный (6/с) — 9 щелчков (полоборота
// ручки), бодрый (10/с) — 8. Полёт 2.8-4.1с, это 3 оборота барабана.
#define R_ROW      20         // высота строки барабана
#define R_WIN_Y    104        // верх окна выбора
// Видимая часть барабана — ±1 строка. Так весь живой ряд влезает в один
// прямоугольник на кадр, и пузыри с крупье перестают дёргаться: раньше полоса
// делилась на три среза, и всё в ней обновлялось на треть частоты.
#define R_TOP      82
#define R_BOT      146
#define R_WX       92         // барабан правее: слева живёт крупье
#define R_WW       216
#define R_SPIN_MIN      2.8f  // порог пуска, строк/с
#define R_FRICTION      2.0f  // трение при накрутке
#define R_SPIN_FRICTION 4.0f  // трение после пуска — оно задаёт длину полёта
#define R_LAUNCH_V     12.0f  // скорость пуска
#define R_KICK          0.6f  // прибавка скорости за щелчок
#define R_PLACES_MAX   12
// 20 символов кириллицы в UTF-8 — это 40 байт, плюс завершающий ноль. Было 24:
// имена резались бы посреди буквы, и поймал это только контракт-тест, сверяющий
// буфер прошивки с лимитом моста.
#define R_NAME_MAX     44

enum RoulState { R_IDLE, R_CHARGE, R_SPIN, R_LAND, R_WON };

char  roulPlaces[R_PLACES_MAX][R_NAME_MAX];
int   roulN = 0;
int   roulWin = -1;           // индекс победителя от моста
int   roulSp = 0;             // номер запуска от моста
int   roulSeenSp = 0;         // какой номер мы уже отработали
float roulPos = 0, roulVel = 0;
// Доводка ограничена по времени — та же причина, что в автомате: скорость,
// пропорциональная остатку, с полом 0.12 строки/с превращала хвост в ползание.
bool  roulLanding = false;
float roulLandA = 0, roulLandEnd = 0;   // трение и конец пути на доводке
unsigned long roulLandT0 = 0;
RoulState roulState = R_IDLE;
unsigned long roulWonAt = 0, roulLastPhys = 0;
bool roulDirty = true;      // состав сменился — нужна полная перерисовка экрана
int roulStatusShown = -1;   // какое состояние уже нарисовано в строке снизу

int roulCount() { return roulN > 0 ? roulN : 1; }

// Щелчок ручки на этом экране: подкрутить барабан. Порог перевален — просим у моста
// победителя и уходим в полёт.
void roulKick() {
  if (roulState == R_LAND) return;                  // доезжает — не мешаем
  if (roulState == R_WON) { roulState = R_IDLE; roulWin = -1; roulLanding = false; }
  roulVel += R_KICK * (roulState == R_SPIN ? 0.6f : 1.0f);
  if (roulVel >= R_SPIN_MIN && roulState != R_SPIN) {
    roulState = R_SPIN;
    // чем сильнее раскрутил сверх порога, тем дольше полёт
    float extra = (roulVel - R_SPIN_MIN) * 1.5f;
    roulVel = R_LAUNCH_V + (extra > 3.0f ? 3.0f : extra);
    Serial.print(F("{\"enc\":\"spin\",\"v\":"));
    Serial.print(roulVel, 1);
    Serial.println(F("}"));
  } else if (roulState != R_SPIN) {
    roulState = R_CHARGE;
  }
}

void roulPhysics(unsigned long now) {
  float dt = roulLastPhys ? (now - roulLastPhys) / 1000.0f : 0.016f;
  if (dt > 0.05f) dt = 0.05f;
  roulLastPhys = now;
  if (roulState == R_IDLE || roulState == R_WON) {
    roulVel = 0;
    roulBubblesStep(dt, 0);          // пузыри всплывают и в покое: иначе экран мёртвый
    return;
  }

  if (!roulLanding) {                       // на доводке трение своё, подогнанное
    roulVel -= (roulState == R_CHARGE ? R_FRICTION : R_SPIN_FRICTION) * dt;
    if (roulVel < 0) roulVel = 0;
    roulPos += roulVel * dt;
  }

  if (roulState == R_CHARGE && roulVel == 0) roulState = R_IDLE;
  // ответ моста пришёл — переходим к доводке
  if (roulState == R_SPIN && roulWin >= 0 && roulSp != roulSeenSp && roulVel < 6.0f) {
    roulSeenSp = roulSp;
    roulState = R_LAND;
  }
  if (roulState == R_LAND) {
    // Доводим ТОЛЬКО вперёд (доводка назад читалась бы как подкрутка результата) и
    // за ФИКСИРОВАННЫЙ срок с кубическим замедлением: скорость, пропорциональная
    // остатку, давала ползание на секунду с лишним в самом конце.
    int n = roulCount();
    if (!roulLanding) {
      // Путь = остаток до цели плюс столько кругов, чтобы он совпал с естественным
      // тормозным путём. Тогда подогнанное трение почти равно обычному и переход
      // не виден: ни скачка скорости, ни обрыва в конце.
      float d = fmodf(fmodf((float)roulWin - roulPos, (float)n) + n, (float)n);
      float natural = roulVel * roulVel / (2 * R_SPIN_FRICTION);
      int k = (int)((natural - d) / n + 0.5f);
      if (k < 0) k = 0;
      float D = d + k * (float)n;
      if (D < 0.5f) D += n;                  // путь строго положительный, иначе NaN
      float vNeed = sqrtf(2 * R_SPIN_FRICTION * D);
      if (roulVel < vNeed) roulVel = vNeed;
      roulLandA = roulVel * roulVel / (2 * D);
      if (!(roulLandA > 0)) roulLandA = R_SPIN_FRICTION;   // страховка от нуля и NaN
      roulLandEnd = roulPos + D;
      roulLandT0 = now;
      roulLanding = true;
    }
    roulVel -= roulLandA * dt;
    if (now - roulLandT0 > 8000) roulVel = 0;   // предохранитель от зависания, не ограничитель
    if (roulVel <= 0) {
      roulPos = roulWin; roulVel = 0; roulLanding = false;
      roulState = R_WON; roulWonAt = now;
    } else {
      // положение из остатка скорости: приезд точен, доснапа нет
      roulPos = roulLandEnd - roulVel * roulVel / (2 * roulLandA);
    }
  }
  int n = roulCount();
  roulPos = fmodf(fmodf(roulPos, (float)n) + n, (float)n);
  float spin = roulVel / 6.0f;
  if (spin > 1) spin = 1;
  roulBubblesStep(dt, spin);        // фазу двигает физика: ОДИН раз на кадр
}

// Пузыри вокруг крупье: слева иначе пустует полэкрана. Позиция — чистая функция
// времени и номера, без random: кадр должен быть воспроизводим, иначе снимок
// экрана перестаёт быть проверкой.
// Пузыри вокруг крупье. Три вещи, на которых уже наступили:
//
// 1. Фаза НАКАПЛИВАЕТСЯ, а не считается как tt*speed. При скорости, зависящей от
//    раскрутки, второй способ телепортирует пузырь при каждом изменении скорости —
//    это и выглядело как дёрганье на отдельных щелчках.
// 2. Пузырь живёт строго внутри полосы крупье. Раньше верх траектории уходил на
//    y=28, выше перерисовываемого прямоугольника, и такие пузыри оставались на
//    панели навсегда — та же болезнь, что копоть за окном осьминога.
// 3. Позиция зависит только от накопленной фазы, поэтому кадр воспроизводим и
//    снимок остаётся проверкой.
#define R_BUB_N   9
#define R_BUB_TOP (R_TOP + 4)
#define R_BUB_BOT (R_BOT - 4)
float roulBubPh[R_BUB_N];

void roulBubblesStep(float dt, float spin) {
  for (int i = 0; i < R_BUB_N; i++) {
    float speed = (9 + (i % 4) * 3 + spin * 14) / (float)(R_BUB_BOT - R_BUB_TOP);
    roulBubPh[i] += speed * dt;
    if (roulBubPh[i] > 1) roulBubPh[i] -= 1;
  }
}

void roulBubbles(Adafruit_GFX &g, int top, int bot, float tt, bool won) {
  for (int i = 0; i < R_BUB_N; i++) {
    float ph = roulBubPh[i];
    int y = R_BUB_BOT - (int)(ph * (R_BUB_BOT - R_BUB_TOP));
    int r = 1 + ((i + (ph > 0.6f ? 1 : 0)) % 3);
    if (y - r < R_BUB_TOP || y + r > R_BUB_BOT) continue;   // за полосу не выходим
    if (y + r < top || y - r > bot) continue;               // вне текущей полосы кадра
    int x = 12 + (i * 9) % 62 + (int)(fastSin(tt * 1.3f + i * 1.7f) * (2 + i % 3));
    uint16_t col = lerp565(won ? C_WORKING : ACCENT, BG, 0.35f + ph * 0.5f);
    if (r <= 1) g.drawPixel(x, y, col);
    else {
      g.drawCircle(x, y, r, col);
      g.drawPixel(x - r + 1, y - r + 1, lerp565(col, 0xFFFF, 0.5f));
    }
  }
}

// Крупье: щупальцем толкает барабан, частота взмаха растёт со скоростью —
// движение читается как ПРИЧИНА вращения, а не как соседняя анимация.
void roulOcto(Adafruit_GFX &g, int top, int bot, float tt, unsigned long now, bool won) {
  // Крупье занимает ~60 строк и попадает в несколько полос: без отсева он считался
  // по четыре раза на каждый срез полосы (10мс из 12мс её цены).
  if (R_WIN_Y + 44 < top || R_WIN_Y - 24 > bot) return;
  float spin = roulVel / 6.0f;
  if (spin > 1) spin = 1;
  const int ox = 52, oy = R_WIN_Y + 4;
  float wob = fastSin(tt * (1.6f + spin * 9)) * (1 + spin * 2.5f);
  int hy = oy + (int)(wob * 0.5f) - (won ? 3 : 0);
  tentacles(g, ox, hy, 1, tt, 1.2f + spin * 4, 1 + spin * 2.5f);
  sphereBody(g, ox, hy);
  int eyY = hy - 5;
  if (won) {                                   // радуется: щёлочки и улыбка
    g.drawFastHLine(ox - 7, eyY, 5, EYE_DARK);
    g.drawFastHLine(ox + 3, eyY, 5, EYE_DARK);
    g.drawFastHLine(ox - 3, eyY + 7, 7, EYE_DARK);
    g.drawPixel(ox - 4, eyY + 6, EYE_DARK);
    g.drawPixel(ox + 4, eyY + 6, EYE_DARK);
  } else {
    drawEyes(g, ox, eyY, roulState == R_SPIN || roulState == R_LAND ? WORKING : IDLE,
             tt, spin > 0.4f, 3.7f);
  }
  arm(g, ox + 14, hy + 3, R_WX - 7, R_WIN_Y + R_ROW / 2, 5 - spin * 4, tt, 1.4f + spin * 5);
  if (roulVel > 1.2f) {                        // штрихи движения у кромки барабана
    for (int i = 0; i < 3; i++) {
      int y = R_TOP + (int)fmodf(now / 26.0f + i * 41, (float)(R_BOT - R_TOP - 6));
      g.drawFastHLine(R_WX + 4, y, 5 + (i * 5) % 7, lerp565(ACCENT, BG, 0.5f));
    }
  }
}

// top/bot/left/right — границы полосы и перерисовываемого прямоугольника. Без них
// каждая из девяти полос пересчитывала крупье, пузыри и надписи целиком: 52мс
// композиции на кадр против 32мс блита.
void composeRoulette(OffsetCanvas &g, int top, int bot, int left, int right, float tt) {
  unsigned long now = millis();
  bool won = (roulState == R_WON);
  g.fillScreen(BG);

  // шапка: сколько мест и время — обед привязан ко времени, это уместно
  bool headVis = (top <= 18);
  if (headVis) {
  g.fillRect(0, 0, W, 18, lerp565(ACCENT, BG, 0.8f));
  g.drawFastHLine(0, 18, W, lerp565(ACCENT, BG, 0.4f));
  drawTextRu(g, 8, 5, "ГДЕ ОБЕДАЕМ", lerp565(CREAMC, BG, 0.05f), 1);
  char head[48];
  char hm[8];
  cafeTime(cafeNm, hm);
  snprintf(head, sizeof(head), "%d МЕСТ  %s", roulN, hm);
  drawTextRu(g, W - 8 - textWidthRu(head, 1), 5, head, lerp565(CREAMC, BG, 0.35f), 1);
  }

  bool drumVis = (right >= R_WX - 8);
  if (drumVis) {
  // корпус барабана: рейки и подсветка окна ПОД текстом
  g.drawFastVLine(R_WX - 6, R_TOP, R_BOT - R_TOP, lerp565(ACCENT, BG, 0.72f));
  g.drawFastVLine(R_WX + R_WW + 5, R_TOP, R_BOT - R_TOP, lerp565(ACCENT, BG, 0.72f));
  if (won) g.fillRect(R_WX + 1, R_WIN_Y - 2, R_WW - 2, R_ROW, lerp565(C_WORKING, BG, 0.86f));

  // строки барабана
  int base = (int)(roulPos + 0.5f);
  float frac = roulPos - base;
  for (int k = -1; k <= 1; k++) {
    int n = roulCount();
    int idx = ((base + k) % n + n) % n;
    if (idx >= roulN) continue;
    int y = R_WIN_Y + k * R_ROW - (int)(frac * R_ROW + 0.5f);
    if (y < R_TOP - 2 || y > R_BOT - 8) continue;
    if (y + 8 < top || y > bot) continue;      // вне полосы — даже не считаем
    int off = y > R_WIN_Y ? y - R_WIN_Y : R_WIN_Y - y;
    bool inWin = off < R_ROW / 2;
    float fade = 0.4f + (off / (float)(R_ROW * 3.2f));
    if (fade > 0.9f) fade = 0.9f;
    uint16_t col = inWin ? (won ? 0xFFFF : CREAMC) : lerp565(CREAMC, BG, fade);
    const char *nm = roulPlaces[idx];
    drawTextRu(g, R_WX + ((R_WW - textWidthRu(nm, 1)) >> 1), y + 3, nm, col, 1);
  }

  // края барабана глуше — окно читается как окно
  g.dimRect(R_WX - 5, R_TOP, R_WW + 10, R_WIN_Y - R_TOP - 3);
  g.dimRect(R_WX - 5, R_WIN_Y + R_ROW + 1, R_WW + 10, R_BOT - (R_WIN_Y + R_ROW + 1));

  // Заряд показываем ЦВЕТОМ РАМКИ: чем ближе к порогу пуска, тем горячее. Раньше
  // тут была шкала внизу экрана, но она вне перерисовываемой полосы и не
  // обновлялась вовсе, а тянуть ради неё лишний блит каждый кадр — расточительно.
  float charge = roulVel / R_SPIN_MIN;
  if (charge > 1) charge = 1;
  uint16_t winCol = won ? C_WORKING
                        : (roulState == R_CHARGE ? lerp565(lerp565(ACCENT, BG, 0.3f), C_WORKING, charge)
                                                 : lerp565(ACCENT, BG, 0.3f));
  g.drawRect(R_WX, R_WIN_Y - 3, R_WW, R_ROW + 2, winCol);
  g.drawRect(R_WX - 1, R_WIN_Y - 4, R_WW + 2, R_ROW + 4, lerp565(winCol, BG, 0.6f));
  g.fillRect(R_WX - 5, R_WIN_Y + R_ROW / 2 - 3, 4, 6, winCol);
  g.fillRect(R_WX + R_WW + 1, R_WIN_Y + R_ROW / 2 - 3, 4, 6, winCol);

  // насечки на рейке двигаются вместе с барабаном — сильнейший признак вращения
  int notch = ((int)(roulPos * R_ROW)) % 8;
  for (int y = R_TOP; y < R_BOT; y += 8) {
    int yy = y + notch;
    if (yy < R_TOP || yy >= R_BOT) continue;
    if (yy < top || yy > bot) continue;
    g.drawPixel(R_WX - 6, yy, lerp565(ACCENT, BG, 0.25f));
    g.drawPixel(R_WX + R_WW + 5, yy, lerp565(ACCENT, BG, 0.25f));
  }

  if (won) {                                   // искры вокруг окна
    unsigned long age = now - roulWonAt;
    for (int i = 0; i < 10; i++) {
      float ph = fmodf(age / 260.0f + i * 0.37f, 1.0f);
      if (ph > 0.75f) continue;
      int sx = R_WX + 6 + (i * 97) % (R_WW - 12);
      int sy = R_WIN_Y + ((i & 1) ? R_ROW + 4 + (int)(ph * 10) : -6 - (int)(ph * 10));
      g.drawPixel(sx, sy, lerp565(0xFFFF, BG, ph));
    }
  }

  }                                      // конец блока барабана

  // Крупье и пузыри живут СЛЕВА от барабана и рисуются независимо от него: пока
  // закрывающая скобка блока барабана стояла ниже, крупье попадал внутрь него и
  // не рисовался вовсе — в прямоугольнике барабана его отсекало по x.
  float spin = roulVel / 6.0f;
  if (spin > 1) spin = 1;
  if (left < R_WX - 8) {
    roulBubbles(g, top, bot, tt, won);
    roulOcto(g, top, bot, tt, now, won);
  }

  // состояние снизу
  if (top <= H - 4 && bot >= H - 14)
  if (won) {
    bool flash = (now - roulWonAt) < 1200 && (((now - roulWonAt) / 130) & 1) == 0;
    drawTextRu(g, 8, H - 13, "ИДЁМ СЮДА!", flash ? 0xFFFF : C_WORKING, 1);
    const char *again = "КРУТНИ ЕЩЁ";
    drawTextRu(g, W - 8 - textWidthRu(again, 1), H - 13, again, lerp565(CREAMC, BG, 0.62f), 1);
  } else if (roulState == R_SPIN || roulState == R_LAND) {
    drawTextRu(g, 8, H - 13, "КРУТИТСЯ...", lerp565(CREAMC, BG, 0.3f), 1);
  } else {
    drawTextRu(g, 8, H - 13, "КРУТИ РУЧКУ ПОБОДРЕЕ", lerp565(CREAMC, BG, 0.45f), 1);
  }
}

// =============================================================================
// Экран автомата на баллы
// =============================================================================
// Баллы капают за отработанные минуты (мост считает только время, когда хотя бы одна
// сессия РАБОТАЕТ) и тратятся здесь. Исход спина считает МОСТ: счёт и случайность —
// состояние, оно обязано переживать перезагрузку платы.
//
// Оформление подводное, а не «абстрактный автомат»: латунный корпус с заклёпками,
// щупальца-завитки по углам, жемчужины-огни, водоросли по дну и пузыри. Корпус —
// статика, живут только пузыри, водоросли и огни: две-три точки на кадр.
#define S_ROW      56
#define S_CAB_X    84
#define S_CAB_Y    22
#define S_CAB_W    230
#define S_CAB_H    166
#define S_WIN_Y    74                 // верх окон барабанов
#define S_BW       56
#define S_GAP      8
#define S_WX       (S_CAB_X + 3 + (((S_CAB_W - 6) - (3*S_BW + 2*S_GAP)) / 2))
#define S_STATUS_Y 196                // строка состояния под корпусом, над дном
#define S_BED_Y    206                // дно: водоросли
#define S_SPIN_MIN      2.8f
#define S_FRICTION      2.0f
#define S_SPIN_FRICTION 4.2f
#define S_LAUNCH_V     13.0f
#define S_KICK          0.6f
#define BRASS     0xABC6
#define BRASS_HI  0xE5ED
#define PEARL     0xEF9E
#define WEED      0x23C9

enum SlotState { S_IDLE, S_CHARGE, S_SPINNING, S_LANDING, S_SHOWN, S_REFUSED };

int   slotPts = 0, slotBet = 5, slotRec = 0, slotPrg = 0;
int   slotEta = -1;                   // минут реального времени до балла, −1 = никто не работает
int   slotTarget[3] = {-1, -1, -1};
int   slotWin = 0;
int   slotSp = 0, slotSeenSp = 0;
float slotPos[3] = {0, 0, 0}, slotVel[3] = {0, 0, 0};
// Доводка — ПОДОГНАННОЕ ТРЕНИЕ, а не отдельная кривая. Две попытки до этого были
// неверны: скорость пропорционально остатку с полом давала ползание на секунду, а
// сглаживание по времени начиналось со скачка скорости (барабан шёл 3 строки/с, а
// кривая стартовала с 9) — это и читалось как телепорт и резкий обрыв.
// Теперь: зная скорость и остаток, считаем трение, при котором барабан встанет РОВНО
// на цель. Дальше он просто едет по физике: замедление постоянное, скорость приходит
// в ноль точно на символе, передачи управления нет вообще.
bool  slotLanding[3] = {false, false, false};
// Отдельный признак «уже приехал»: без него условие начала доводки срабатывало
// снова сразу после её конца, и барабан бесконечно уезжал ещё на символ каждые
// полсекунды — на экране это выглядело как «крутится и не встаёт».
bool  slotLanded[3] = {false, false, false};
float slotLandA[3];            // подогнанное трение на доводке
// Конец пути в НЕПРЕРЫВНЫХ координатах: положение на доводке считается из остатка
// скорости (pos = end - v²/2a), а не накапливается шагами. Так барабан приезжает
// математически точно, и финального «доснапа» на символ не существует — именно он
// читался как смена без анимации.
float slotLandEnd[3];
unsigned long slotLandT0[3];
SlotState slotState = S_IDLE;
unsigned long slotShownAt = 0, slotRefusedAt = 0, slotLastPhys = 0;
float slotLev = 0;          // угол рычага 0..1, ходит плавно
bool slotDirty = true;
int slotStatusShown = -1;

bool slotHot(int i) {
  int a = slotTarget[0], b = slotTarget[1], c = slotTarget[2];
  if (a < 0) return false;
  if (a == b && b == c) return true;
  if (a == b) return i < 2;
  if (b == c) return i > 0;
  if (a == c) return i != 1;
  return false;
}

void slotKick() {
  // Автомат не берёт спин, который не может оплатить. Раньше барабаны крутились
  // впустую, а мост уже потом отвечал «не хватает» — холостая анимация и обман.
  if (slotPts < slotBet) {
    if (slotState != S_REFUSED) slotDirty = true;
    slotState = S_REFUSED;
    slotRefusedAt = millis();
    slotVel[0] = slotVel[1] = slotVel[2] = 0;
    return;
  }
  if (slotState == S_LANDING) return;
  if (slotState == S_SHOWN || slotState == S_REFUSED) {
    slotState = S_IDLE;
    slotTarget[0] = slotTarget[1] = slotTarget[2] = -1;
    slotLanding[0] = slotLanding[1] = slotLanding[2] = false;
    slotLanded[0] = slotLanded[1] = slotLanded[2] = false;
    slotWin = 0;
    slotDirty = true;
  }
  slotVel[0] += S_KICK;
  if (slotVel[0] >= S_SPIN_MIN && slotState != S_SPINNING) {
    slotState = S_SPINNING;
    for (int i = 0; i < 3; i++) slotVel[i] = S_LAUNCH_V + i * 0.8f;
    Serial.println(F("{\"enc\":\"slot\"}"));
  } else if (slotState != S_SPINNING) {
    slotState = S_CHARGE;
  }
}

void slotPhysics(unsigned long now) {
  float dt = slotLastPhys ? (now - slotLastPhys) / 1000.0f : 0.016f;
  if (dt > 0.05f) dt = 0.05f;
  slotLastPhys = now;

  float spin = slotVel[0] / 6.0f;
  if (spin > 1) spin = 1;

  if (slotState == S_REFUSED && now - slotRefusedAt > 1600) { slotState = S_IDLE; slotDirty = true; }
  if (slotState == S_IDLE || slotState == S_SHOWN || slotState == S_REFUSED) {
    slotVel[0] = slotVel[1] = slotVel[2] = 0;
    return;
  }

  float fr = (slotState == S_CHARGE) ? S_FRICTION : S_SPIN_FRICTION;
  bool allStopped = true;
  for (int i = 0; i < 3; i++) {
    if (!slotLanding[i]) {                    // на доводке трение своё, подогнанное
      float myFr = fr * (1 - i * 0.16f);      // следующий барабан тормозит позже
      slotVel[i] -= myFr * dt;
      if (slotVel[i] < 0) slotVel[i] = 0;
      slotPos[i] += slotVel[i] * dt;
    }
    if (slotState != S_CHARGE && slotTarget[i] >= 0 && !slotLanding[i] && !slotLanded[i]
        && slotVel[i] < 6.0f) {
      // Остаток до цели плюс столько оборотов, чтобы путь совпал с естественным
      // тормозным путём: тогда подогнанное трение почти не отличается от обычного,
      // и переход незаметен. Каждому следующему барабану — на оборот больше, отсюда
      // остановка слева направо.
      float d = fmodf(fmodf((float)slotTarget[i] - slotPos[i], (float)SLOT_SYMS) + SLOT_SYMS,
                      (float)SLOT_SYMS);
      float aBase = S_SPIN_FRICTION * (1 - i * 0.16f);
      float natural = slotVel[i] * slotVel[i] / (2 * aBase);
      int k = (int)((natural - d) / SLOT_SYMS + 0.5f);
      if (k < i) k = i;
      if (k > i + 1) k = i + 1;              // дальше двух оборотов доводка тянется зря
      float D = d + k * (float)SLOT_SYMS;
      // Путь обязан быть положительным: если барабан на момент доводки оказался почти
      // на цели, D выходил нулевым, трение считалось как v²/0, положение становилось
      // NaN — и признак «идёт доводка» не снимался никогда: вечный спин и нажатый рычаг.
      if (D < 0.5f) D += SLOT_SYMS;
      float vNeed = sqrtf(2 * aBase * D);
      if (slotVel[i] < vNeed) slotVel[i] = vNeed;     // ответ пришёл поздно — подтолкнуть
      slotLandA[i] = slotVel[i] * slotVel[i] / (2 * D);
      if (!(slotLandA[i] > 0)) slotLandA[i] = aBase;    // страховка от нуля и NaN
      slotLandEnd[i] = slotPos[i] + D;
      slotLandT0[i] = now;
      slotLanding[i] = true;
    }
    if (slotLanding[i]) {
      slotVel[i] -= slotLandA[i] * dt;                // едем по физике до нуля
      // Предохранитель — только от зависания (порядка секунд «сверх любого разумного»),
      // а НЕ ограничитель длительности: у третьего барабана путь длиннее и доводка
      // честно занимает до трёх секунд. На пороге 3с он обрывал движение в ноль и
      // снапал на цель — это и читалось как «последний не докручивается и резко
      // меняется на следующий».
      if (now - slotLandT0[i] > 8000) slotVel[i] = 0;
      if (slotVel[i] <= 0) {
        slotPos[i] = slotTarget[i]; slotVel[i] = 0;
        slotLanding[i] = false; slotLanded[i] = true;
      } else {
        slotPos[i] = slotLandEnd[i] - slotVel[i] * slotVel[i] / (2 * slotLandA[i]);
      }
    }
    if (slotVel[i] > 0 || slotLanding[i]) allStopped = false;
    slotPos[i] = fmodf(fmodf(slotPos[i], (float)SLOT_SYMS) + SLOT_SYMS, (float)SLOT_SYMS);
  }
  if (slotState == S_CHARGE && allStopped) slotState = S_IDLE;
  if (slotState == S_SPINNING && slotSp != slotSeenSp && slotTarget[0] >= 0) {
    slotSeenSp = slotSp;
    slotState = S_LANDING;
  }
  if (slotState == S_LANDING && allStopped) {
    slotState = S_SHOWN;
    slotShownAt = now;
    slotDirty = true;                          // результат меняет шапку и рамки
  }
}

// Символ 8x8 в увеличении, обрезанный по окну барабана: соседний символ должен
// ВЪЕЗЖАТЬ в окно, а не вылезать за рамку.
void slotSprite(Adafruit_GFX &g, int idx, int x, int y, int scale, int clipT, int clipB) {
  int k = ((idx % SLOT_SYMS) + SLOT_SYMS) % SLOT_SYMS;
  for (int layer = 0; layer < 3; layer++) {
    uint16_t col = SLOT_PAL[k][layer];
    for (int ry = 0; ry < 8; ry++) {
      uint8_t bits = SLOT_SPRITE[k][layer][ry];
      if (!bits) continue;
      int py = y + ry * scale;
      int top = py < clipT ? clipT : py;
      int bot = (py + scale) > clipB ? clipB : (py + scale);
      if (bot <= top) continue;
      for (int rx = 0; rx < 8; rx++)
        if (bits & (1 << (7 - rx))) g.fillRect(x + rx * scale, top, scale, bot - top, col);
    }
  }
}

void slotCabinet(Adafruit_GFX &g, int top, int bot, float tt) {
  if (S_CAB_Y + S_CAB_H < top || S_CAB_Y > bot) return;
  g.fillRect(S_CAB_X, S_CAB_Y, S_CAB_W, S_CAB_H, lerp565(BRASS, BG, 0.78f));
  g.drawRect(S_CAB_X, S_CAB_Y, S_CAB_W, S_CAB_H, BRASS);
  g.drawRect(S_CAB_X + 1, S_CAB_Y + 1, S_CAB_W - 2, S_CAB_H - 2, lerp565(BRASS_HI, BG, 0.45f));
  g.fillRect(S_CAB_X + 4, S_CAB_Y + 4, S_CAB_W - 8, S_CAB_H - 8, BG);

  for (int x = S_CAB_X + 8; x < S_CAB_X + S_CAB_W - 6; x += 26) {   // заклёпки
    g.fillRect(x, S_CAB_Y + 2, 2, 2, BRASS_HI);
    g.fillRect(x, S_CAB_Y + S_CAB_H - 4, 2, 2, BRASS_HI);
  }
  for (int y = S_CAB_Y + 10; y < S_CAB_Y + S_CAB_H - 8; y += 24) {
    g.fillRect(S_CAB_X + 2, y, 2, 2, BRASS_HI);
    g.fillRect(S_CAB_X + S_CAB_W - 4, y, 2, 2, BRASS_HI);
  }

  // Жемчужины — СТАТИЧНЫЕ накладки, а не бегущий огонёк. Огонёк тут невозможен:
  // их ряды лежат вне полос, которые обновляются в кадре, поэтому «бегущая» точка
  // стояла на месте и перескакивала только после спина, когда экран перерисовывался
  // целиком. Оживлять ради украшения ещё две полосы — не та цена.
  for (int i = 0; i < 12; i++) {
    int px = (i < 6) ? S_CAB_X + 16 + i * 38 : S_CAB_X + S_CAB_W - 16 - (i - 6) * 38;
    int py = (i < 6) ? S_CAB_Y + 8 : S_CAB_Y + S_CAB_H - 10;
    g.fillCircle(px, py, 2, lerp565(PEARL, BG, 0.55f));
    g.drawPixel(px - 1, py - 1, PEARL);            // блик, чтобы читались как жемчуг
  }

  const char *name = "ОСЬМИ-СЛОТ";
  drawTextRu(g, S_CAB_X + ((S_CAB_W - textWidthRu(name, 1)) >> 1), S_CAB_Y + 12, name,
             lerp565(BRASS_HI, 0xFFFF, 0.35f), 1);
}

void slotWeeds(Adafruit_GFX &g, int top, int bot, float tt) {
  if (S_BED_Y > bot || H < top) return;
  for (int i = 0; i < 22; i++) {
    int bx = 6 + i * 14;
    int h = 10 + (i % 4) * 7;
    // Наклон считается ОДИН раз на стебель, а не на пиксель: синус на пиксель стоил
    // 10мс из 19мс среза дна и рвал период кадра. Стебель кланяется целиком —
    // на такой высоте от волны это не отличить.
    float lean = fastSin(tt * 0.9f + i * 1.3f) * 3;
    for (int k = 0; k < h; k++) {
      int y = H - 2 - k;
      if (y < top || y > bot) continue;
      float f = (float)k / h;
      g.drawPixel(bx + (int)(lean * f), y, lerp565(WEED, BG, 0.15f + f * 0.5f));
    }
  }
}


void slotOcto(Adafruit_GFX &g, int top, int bot, float tt, bool shown, bool jackpot) {
  const int ox = 30, oy = 138;
  if (oy + 46 < top || oy - 56 > bot) return;
  float spin = slotVel[0] / 6.0f;
  if (spin > 1) spin = 1;
  float wob = fastSin(tt * (1.6f + spin * 9)) * (1 + spin * 2.5f);
  int hy = oy + (int)(wob * 0.5f) - (jackpot ? 4 : 0);
  tentacles(g, ox, hy, 1, tt, 1.2f + spin * 4, 1 + spin * 2.5f);
  sphereBody(g, ox, hy);
  int eyY = hy - 5;
  if (slotState == S_REFUSED) {                 // «пусто»: круглые глаза, рот линией
    g.drawCircle(ox - 6, eyY, 2, EYE_LIGHT);
    g.drawCircle(ox + 6, eyY, 2, EYE_LIGHT);
    g.drawFastHLine(ox - 3, eyY + 8, 7, EYE_DARK);
  } else if (shown && slotWin > 0) {
    g.drawFastHLine(ox - 7, eyY, 5, EYE_DARK);
    g.drawFastHLine(ox + 3, eyY, 5, EYE_DARK);
    g.drawFastHLine(ox - 3, eyY + 7, 7, EYE_DARK);
  } else if (shown) {
    g.drawFastHLine(ox - 7, eyY, 5, EYE_DARK);
    g.drawFastHLine(ox + 3, eyY, 5, EYE_DARK);
    g.drawFastHLine(ox - 3, eyY + 8, 7, EYE_DARK);
    g.drawPixel(ox - 4, eyY + 7, EYE_DARK);
    g.drawPixel(ox + 4, eyY + 7, EYE_DARK);
  } else {
    drawEyes(g, ox, eyY, slotState == S_SPINNING || slotState == S_LANDING ? WORKING : IDLE,
             tt, spin > 0.4f, 3.9f);
  }

  // Рычаг НА ОСИ, а не телескоп: рукоять ходит по дуге вокруг кронштейна на корпусе —
  // именно так читается «однорукий бандит». Штанга в два тона (светлая грань слева,
  // тёмная справа) даёт вид хрома, рукоять со блик-пятном и обводкой — вид стекла.
  // Ось на СЕРЕДИНЕ высоты: при оси снизу рукоять уходила в сторону, и это читалось
  // не как «дёрнул вниз». Теперь ход идёт большой дугой вверх → в сторону → вниз,
  // ровно как у однорукого бандита.
  const int px = S_CAB_X - 6, py = 128;        // ось вращения
  const float lenArm = 40;
  float aim = (slotState == S_SPINNING || slotState == S_LANDING) ? 1.0f
              : (slotState == S_CHARGE ? spin : 0.0f);
  slotLev += (aim - slotLev) * 0.28f;          // возврат с замедлением, без рывка
  float ang = 0.17f + slotLev * 2.27f;         // от вертикали вверх до наклона вниз
  int hx = px - (int)(lenArm * fastSin(ang));
  int hy2 = py - (int)(lenArm * fastCos(ang));

  g.fillRect(px - 4, py - 4, 9, 9, lerp565(BRASS, BG, 0.25f));      // кронштейн
  g.drawRect(px - 4, py - 4, 9, 9, lerp565(BRASS_HI, BG, 0.35f));
  g.drawPixel(px, py, lerp565(BG, BRASS_HI, 0.3f));                 // ось
  for (int o = -1; o <= 1; o++) {                                   // штанга в два тона
    uint16_t c = (o < 0) ? lerp565(0xFFFF, BRASS_HI, 0.45f)
               : (o > 0) ? lerp565(BRASS, BG, 0.25f) : BRASS_HI;
    g.drawLine(px + o, py, hx + o, hy2, c);
  }
  uint16_t ball = jackpot ? C_WORKING : C_ERROR;
  g.fillCircle(hx, hy2, 8, ball);
  g.drawCircle(hx, hy2, 8, lerp565(ball, BG, 0.45f));               // обводка-тень
  g.drawCircle(hx, hy2, 5, lerp565(ball, 0xFFFF, 0.25f));           // внутренний блеск
  g.fillCircle(hx - 3, hy2 - 3, 2, lerp565(0xFFFF, ball, 0.15f));   // блик
  // Хомут под шаром был прямоугольником в ЭКРАННЫХ координатах: штанга наклонялась,
  // а он оставался внизу круга и отклеивался от рычага. Шар сидит на штанге и без него.
  if (jackpot) g.drawCircle(hx, hy2, 11, C_WORKING);
  arm(g, ox + 13, hy + 1, hx - 4, hy2 + 3, 6 - spin * 5, tt, 1.4f + spin * 5);
}

void composeSlots(OffsetCanvas &g, int top, int bot, int left, int right, float tt) {
  unsigned long now = millis();
  bool shown = (slotState == S_SHOWN);
  bool jackpot = shown && slotWin >= slotBet * 8;
  g.fillScreen(BG);

  if (top <= 18) {
    g.fillRect(0, 0, W, 18, lerp565(BRASS, BG, 0.7f));
    g.drawFastHLine(0, 18, W, lerp565(BRASS_HI, BG, 0.4f));
    drawTextRu(g, 8, 5, "АВТОМАТ", lerp565(CREAMC, BG, 0.05f), 1);
    char head[48];
    snprintf(head, sizeof(head), "%d Б  СТАВКА %d  РЕКОРД %d", slotPts, slotBet, slotRec);
    // Не хватает на спин — счёт горит красным РОВНО, без мигания: шапку не обновляет
    // ни одна покадровая полоса, и «мигание» тут застывало бы в случайной фазе —
    // то ярким, то тусклым до следующей полной перерисовки. Цвета достаточно.
    bool low = slotPts < slotBet;
    uint16_t hc = low ? C_ERROR : lerp565(CREAMC, BG, 0.25f);
    drawTextRu(g, W - 8 - textWidthRu(head, 1), 5, head, hc, 1);
  }

  unsigned long tc = micros();
  slotWeeds(g, top, bot, tt);
  if (right >= S_CAB_X) slotCabinet(g, top, bot, tt);
  usSmog += micros() - tc;

  if (right >= S_WX - 8) {
    const int winT = S_WIN_Y - 1, winB = S_WIN_Y + S_ROW - 6;
    for (int i = 0; i < 3; i++) {
      int x = S_WX + i * (S_BW + S_GAP);
      int base = (int)(slotPos[i] + 0.5f);
      float frac = slotPos[i] - base;
      for (int k = -1; k <= 1; k++) {
        int y = S_WIN_Y + k * S_ROW - (int)(frac * S_ROW + 0.5f);
        if (y + 40 < top || y > bot) continue;
        unsigned long ts2 = micros();
        slotSprite(g, base + k, x + 8, y + 4, 5, winT, winB);
        usOcto += micros() - ts2;
        nOcto++;
      }
      bool hot = shown && slotWin > 0 && slotHot(i);
      uint16_t col = hot ? (jackpot ? C_WORKING : lerp565(C_WORKING, BG, 0.3f))
                         : lerp565(BRASS_HI, BG, 0.55f);
      g.drawRect(x, S_WIN_Y - 2, S_BW, S_ROW - 4, col);
      g.dimRect(x + 1, S_WIN_Y - 1, S_BW - 2, 8);          // цилиндр: тень сверху
      g.dimRect(x + 1, S_WIN_Y + S_ROW - 14, S_BW - 2, 8); //           и снизу
    }

    // линия выплаты со стрелками — сразу видно, что считается
    int plY = S_WIN_Y + ((S_ROW - 4) / 2) - 2;
    uint16_t plCol = (shown && slotWin > 0) ? C_WORKING : lerp565(BRASS_HI, BG, 0.65f);
    g.drawFastHLine(S_WX - 8, plY, 8, plCol);
    g.drawFastHLine(S_WX + 3 * S_BW + 2 * S_GAP, plY, 8, plCol);
    for (int t = 0; t < 3; t++) {
      g.drawFastVLine(S_WX - 6 + t, plY - t, 2 * t + 1, plCol);
      g.drawFastVLine(S_WX + 3 * S_BW + 2 * S_GAP + 5 - t, plY - t, 2 * t + 1, plCol);
    }

    const char *pay = "ТРОЙКА x15   ПАРА x1.6";
    drawTextRu(g, S_CAB_X + ((S_CAB_W - textWidthRu(pay, 1)) >> 1), S_WIN_Y + S_ROW + 6, pay,
               lerp565(BRASS_HI, BG, 0.5f), 1);

    // прогресс до балла — внутри корпуса, это его же показатель
    const int pbx = S_CAB_X + 16, pbw = S_CAB_W - 32;
    // Надпись живая: балл начисляется за работу КАЖДОЙ сессии, поэтому статичное
    // «до балла» врало — на пяти сессиях полоса идёт впятеро быстрее. Мост считает
    // eta в реальных минутах при нынешнем числе работающих (−1 = никто не работает).
    char eta[24];              // кириллица в UTF-8 по 2 байта: «ЧЕРЕЗ 20М» это 15
    if (slotEta < 0)       strlcpy(eta, "ПАУЗА", sizeof(eta));
    else if (slotEta <= 1) strlcpy(eta, "СКОРО", sizeof(eta));
    else                   snprintf(eta, sizeof(eta), "ЧЕРЕЗ %dМ", slotEta);
    drawTextRu(g, pbx, S_CAB_Y + S_CAB_H - 22, eta,
               lerp565(CREAMC, BG, slotEta < 0 ? 0.75f : 0.6f), 1);
    g.drawRect(pbx + 56, S_CAB_Y + S_CAB_H - 23, pbw - 56, 9, lerp565(BRASS_HI, BG, 0.7f));
    g.fillRect(pbx + 58, S_CAB_Y + S_CAB_H - 21, (pbw - 60) * slotPrg / 100, 5,
               lerp565(BRASS_HI, BG, 0.3f));
  }

  if (left < S_CAB_X) slotOcto(g, top, bot, tt, shown, jackpot);

  if (top <= S_STATUS_Y + 8 && bot >= S_STATUS_Y) {
    if (slotState == S_REFUSED) {
      bool flash = (((now - slotRefusedAt) / 140) & 1) == 0;
      drawTextRu(g, 8, S_STATUS_Y, "БАЛЛОВ НЕ ХВАТАЕТ, ИДИ РАБОТАЙ",
                 flash ? C_ERROR : lerp565(C_ERROR, BG, 0.4f), 1);
    } else if (shown) {
      bool flash = (now - slotShownAt) < 1200 && (((now - slotShownAt) / 130) & 1) == 0;
      char line[48];
      if (slotWin > 0) {
        if (jackpot) snprintf(line, sizeof(line), "ТРОЙКА! +%d БАЛЛОВ", slotWin);
        else         snprintf(line, sizeof(line), "ПАРА, +%d", slotWin);
        drawTextRu(g, 8, S_STATUS_Y, line, flash ? 0xFFFF : C_WORKING, 1);
      } else {
        snprintf(line, sizeof(line), "МИМО, -%d", slotBet);
        drawTextRu(g, 8, S_STATUS_Y, line, lerp565(CREAMC, BG, 0.35f), 1);
      }
      const char *again = "КРУТНИ ЕЩЁ";
      drawTextRu(g, W - 8 - textWidthRu(again, 1), S_STATUS_Y, again, lerp565(CREAMC, BG, 0.62f), 1);
    } else if (slotState == S_SPINNING || slotState == S_LANDING) {
      drawTextRu(g, 8, S_STATUS_Y, "КРУТИТСЯ...", lerp565(CREAMC, BG, 0.3f), 1);
    } else {
      drawTextRu(g, 8, S_STATUS_Y, "КРУТИ РУЧКУ ПОБОДРЕЕ", lerp565(CREAMC, BG, 0.45f), 1);
    }
  }
}

// ЕДИНСТВЕННЫЙ путь отрисовки. Любая перерисовка — это прямоугольник, собранный
// полосами в буфер и вылитый блитом: весь экран, одна карточка, место всплывашки.
// Отдельных путей нет намеренно — раньше каждый рисовал по-своему (сетка отдельно,
// копоть отдельным проходом, осьминоги следующим тиком), и элементы проявлялись
// разными волнами. Экран целиком в буфер не влезает: 320x240x2 = 150 КБ.
void redrawRect(int rx, int ry, int rw, int rh) {
  unsigned long t0 = micros();
  usSmog = usOcto = usBlit = 0; nOcto = 0;
  float tt = millis() / 1000.0f;
  int x1 = rx + rw, y1 = ry + rh;
  for (int y0 = ry; y0 < y1; y0 += STRIP_H) {
    int h = min(STRIP_H, y1 - y0);
    stripBuf.moveTo(0, y0);
    stripBuf.fillScreen(BG);
    stripBuf.clipBase(rx, y0, rx + rw - 1, y0 + h - 1);
    composeCurrentScreen(stripBuf, y0, y0 + h - 1, rx, rx + rw - 1, tt);
    stripBuf.clipBase(-32768, -32768, 32767, 32767);

    uint16_t *b = stripBuf.getBuffer();
    unsigned long tb = micros();
    tft.startWrite();
    tft.setAddrWindow(rx, y0, rw, h);
    for (int r = 0; r < h; r++) blitRow(b + r * W + rx, rw);
    tft.endWrite();
    usBlit += micros() - tb;
    yield();                              // блит длинный, watchdog кормим между полосами
  }
  // Цена перерисовки — в обратный канал безусловно (а не под ESP_DIAG): полные
  // перерисовки редкие, зато по этому числу видно, читается ли она как одно
  // движение или как медленная протяжка. Иначе судить о «плавно» нечем.
  Serial.print(F("{\"esp\":\"redraw\",\"w\":")); Serial.print(rw);
  Serial.print(F(",\"h\":")); Serial.print(rh);
  Serial.print(F(",\"ms\":")); Serial.print((micros() - t0) / 1000);
  Serial.print(F(",\"smog\":")); Serial.print(usSmog / 1000);
  Serial.print(F(",\"octo\":")); Serial.print(usOcto / 1000);
  Serial.print(F(",\"nocto\":")); Serial.print(nOcto);
  Serial.print(F(",\"blit\":")); Serial.print(usBlit / 1000);
  Serial.print(F(",\"dt\":")); Serial.print(lastRedrawAt ? (t0 - lastRedrawAt) / 1000 : 0);
  Serial.println(F("}"));
  lastRedrawAt = t0;
}

// Экран собирается ПОЛОСАМИ в тот же буфер со смещением, которым делается снимок,
// и каждая полоса выливается одним блитом. Раньше это были три отдельные волны —
// сетка с рамками, потом копоть по всему экрану, потом осьминоги следующим тиком, —
// и было видно, как элементы доезжают по очереди. Теперь внутри полосы рамка, имя,
// осьминог и копоть появляются ОДНОВРЕМЕННО, а экран проходит одним движением
// сверху вниз. Экран целиком в буфер не влезает физически: 320x240x2 = 150 КБ.
void redrawAll() {
  redrawRect(0, 0, W, H);
  // осьминоги уже нарисованы в полосах — тик анимации продолжается с этого кадра,
  // без лишней немедленной перерисовки (она и давала «третью волну»)
  unsigned long now = millis();
  for (int i = 0; i < MAX_SESSIONS; i++) lastTick[i] = now;
}

// =============================================================================
// Всплывашка «2/3»
// =============================================================================
static const uint8_t GLYPH[11][5] = {
  {7,5,5,5,7}, {2,6,2,2,7}, {7,1,7,4,7}, {7,1,7,1,7}, {5,5,7,1,1},
  {7,4,7,1,7}, {7,4,7,5,7}, {7,1,1,1,1}, {7,5,7,5,7}, {7,5,7,1,7},
  {1,1,2,4,4},   // '/'
};

void tinyChar(int x, int y, int glyph, int scale, uint16_t col) {
  for (int r = 0; r < 5; r++)
    for (int c = 0; c < 3; c++)
      if (GLYPH[glyph][r] & (1 << (2 - c)))
        tft.fillRect(x + c * scale, y + r * scale, scale, scale, col);
}

void popupGeom(int &x, int &y, int &w, int &h) {
  const int scale = 3;
  int chars = 3;                                 // «p/n» — обе цифры однознач.
  if (popupPage >= 10) chars++;
  if (popupPages >= 10) chars++;
  w = chars * 4 * scale - scale + 24;
  h = 5 * scale + 22;
  x = (W - w) / 2;
  y = (H - h) / 2;
}

void drawPopup() {
  if (popupPages < 2) return;
  int x, y, w, h;
  popupGeom(x, y, w, h);
  tft.fillRect(x, y, w, h, PLATE);
  tft.drawRect(x, y, w, h, ACCENT);
  int cx = x + 12, scale = 3;
  int digits[6], n = 0;
  if (popupPage >= 10) digits[n++] = popupPage / 10;
  digits[n++] = popupPage % 10;
  digits[n++] = 10;                              // '/'
  if (popupPages >= 10) digits[n++] = popupPages / 10;
  digits[n++] = popupPages % 10;
  for (int i = 0; i < n; i++) {
    tinyChar(cx, y + 11, digits[i], scale, ACCENT);
    cx += 4 * scale;
  }
  popupDrawn = true;
}

// Гасим плашку: место под ней пересобирается тем же единым путём. Раньше тут
// вручную чинились фон, гридлайны и рамки задетых карточек — отдельная копия
// логики отрисовки, которая уже расходилась с настоящей (копоть не чинилась).
void hidePopup() {
  if (!popupDrawn) return;
  popupDrawn = false;
  int x, y, w, h;
  popupGeom(x, y, w, h);
  redrawRect(x, y, w, h);
}

// =============================================================================
// Экран кофейни
// =============================================================================
void cafeTime(int minutes, char *out) {
  int hh = (minutes / 60) % 24, mm = minutes % 60;
  out[0] = '0' + hh / 10; out[1] = '0' + hh % 10; out[2] = ':';
  out[3] = '0' + mm / 10; out[4] = '0' + mm % 10; out[5] = 0;
}

const char *cafeLabel(int st) {
  switch (st) {
    case 0: return "ОТКРЫТО";
    case 1: return "ПЕРЕРЫВ";
    case 2: return "ОБЕД";
    case 4: return "УБОРКА";
    default: return "ЗАКРЫТО";
  }
}

uint16_t cafeColor(int st) {
  switch (st) {
    case 0: return C_WORKING;
    case 1: return C_WAITING;
    case 2: return 0xFC80;      // обед — оранжевый
    case 4: return ACCENT;      // уборка — свой цвет
    default: return C_IDLE;
  }
}

// --- бариста -----------------------------------------------------------------
// Живёт в общем буфере (том же, что осьминог аквариума) и блитится одним окном.
// Реквизит — машина, стакан, пролив — рисуется прямо на панель в своей полосе:
// там плоский фон, восстанавливать нечего.
#define CAFE_BOX_X 8
#define CAFE_BOX_Y 44
#define CNT_Y      120           // линия стойки
#define BCX        34            // центр баристы внутри буфера
#define BCY        46
#define MACH_X     96

// Полоса реквизита: своё окно вывода. Раньше я стирал её fillRect'ом прямо
// на панели и рисовал заново каждый кадр — это и был дребезг: между стиранием
// и отрисовкой панель успевала показать чёрное. Теперь кадр собирается в буфере
// и выливается одним окном, как осьминоги в аквариуме.
#define BAND_X 92
#define BAND_Y 60
#define BAND_W 32
#define BAND_H 60

// Выливает прямоугольник общего буфера в окно панели. Байты свопятся построчно
// на месте: буфер всё равно перезаписывается следующим кадром.
void blitCanvasRect(int sx, int sy, int w, int h, int dx, int dy) {
  uint16_t *buf = octoBuf.getBuffer();
  tft.startWrite();
  tft.setAddrWindow(dx, dy, w, h);
  for (int r = 0; r < h; r++) blitRow(&buf[(sy + r) * BUF_W + sx], w);
  tft.endWrite();
}

void cafeCounter() {
  tft.fillRect(0, CNT_Y, 124, 7, lerp565(COFFEE_DK, BG, 0.25f));
  tft.drawFastHLine(0, CNT_Y, 124, lerp565(COFFEEC, CREAMC, 0.25f));
}

// Машина, стакан и пролив — в локальных координатах полосы.
void propsArt(Adafruit_GFX &g, float tt, float fill, bool steam, bool pouring) {
  const int mx = MACH_X - BAND_X, my = 66 - BAND_Y, bottom = CNT_Y - BAND_Y;

  g.fillRect(mx, my, 24, bottom - my, lerp565(0xE73C, BG, 0.62f));
  g.fillRect(mx + 2, my + 2, 20, 9, lerp565(COFFEE_DK, BG, 0.15f));
  g.fillRect(mx + 7, my + 13, 10, 6, lerp565(0xE73C, BG, 0.35f));
  g.fillRect(mx + 10, my + 19, 4, 6, lerp565(0xE73C, BG, 0.5f));
  uint16_t led = cafeColor(cafeSt);
  bool on = (cafeSt == 0) ? true
            : (cafeSt == 3) ? (fmodf(tt, 2.0f) < 0.12f) : (((int)(tt * 1.5f)) & 1) == 0;
  g.fillRect(mx + 18, my + 5, 4, 4, on ? led : lerp565(led, BG, 0.82f));

  const int cx = (MACH_X + 12) - BAND_X;
  const int cw2 = 15, chh = 16, cy = bottom - chh;
  g.fillRect(cx - cw2 / 2, cy, cw2, chh, 0xE73C);
  g.fillRect(cx - cw2 / 2, cy, cw2, 2, lerp565(0xE73C, BG, 0.4f));
  int fh = iround((chh - 5) * fill);
  if (fh > 0) g.fillRect(cx - cw2 / 2 + 2, cy + chh - 2 - fh, cw2 - 4, fh, COFFEEC);

  if (pouring) {
    for (int y = 91 - BAND_Y; y < cy; y += 2)
      g.fillRect(cx + iround(fastSin(tt * 14 + y * 0.7f)), y, 2, 2,
                 lerp565(COFFEEC, CREAMC, 0.2f));
  }
  if (steam) {
    for (int i = 0; i < 10; i++) {
      float f = (float)i / 10;
      g.drawPixel(cx + iround(fastSin(tt * 1.5f + i * 0.5f) * 2 * (0.3f + f)), cy - 3 - i,
                  lerp565(CREAMC, BG, f * f * 0.9f + 0.1f));
    }
  }
}

// Кадр анимации: то же рисование в буфер и один блит окна.
void animateProps(float tt, float fill, bool steam, bool pouring) {
  octoBuf.fillScreen(BG);
  propsArt(octoBuf, tt, fill, steam, pouring);
  blitCanvasRect(0, 0, BAND_W, BAND_H, BAND_X, BAND_Y);
}

// Сцена по статусу. Классы движения намеренно разные: работа — поток предметов,
// перерыв — работа телом, обед — предмет ко рту, уборка — движение вбок по стойке.
void baristaArt(Adafruit_GFX &g, float tt) {
  bool lively = (cafeSt == 0 || cafeSt == 1 || cafeSt == 4);
  int bob = iround(fastSin(tt * (lively ? 1.8f : 1.1f)) * (lively ? 1.5f : 1.0f));
  int hy = BCY + bob + (cafeSt == 3 ? 6 : 0);

  // потягушки на коротком перерыве: щупальца шире, тело чуть выше
  float reach = 0;
  float ph5 = fmodf(tt, 5.0f) / 5.0f;
  if (cafeSt == 1) {
    reach = ph5 < 0.40f ? 0 : ph5 < 0.56f ? (ph5 - 0.40f) / 0.16f
            : ph5 < 0.78f ? 1 : max(0.0f, 1 - (ph5 - 0.78f) / 0.22f);
  }
  float tSpeed = (cafeSt == 0) ? 2.6f : 1.3f;
  float tAmp = (cafeSt == 0) ? 1.8f : (cafeSt == 1 ? 0.8f + reach * 3.6f : 0.8f);
  tentacles(g, BCX, hy, 1, tt, tSpeed, tAmp);
  sphereBody(g, BCX, hy);

  int exL = BCX - 7, exR = BCX + 7, eyY = hy - 5;
  bool sleepy = (cafeSt == 2 || cafeSt == 3) || (cafeSt == 1 && reach > 0.35f);
  if (sleepy) {
    g.drawFastHLine(exL - 4, eyY, 9, EYE_LIGHT);
    g.drawFastHLine(exR - 4, eyY, 9, EYE_LIGHT);
  } else {
    int look = iround(fastSin(tt * 1.4f) * 1.6f);
    g.fillCircle(exL, eyY, 4, EYE_LIGHT);
    g.fillCircle(exR, eyY, 4, EYE_LIGHT);
    g.fillCircle(exL + look, eyY + 1, 2, EYE_DARK);
    g.fillCircle(exR + look, eyY + 1, 2, EYE_DARK);
    g.drawPixel(exL + look - 1, eyY - 1, GLINT);
    g.drawPixel(exR + look - 1, eyY - 1, GLINT);
  }

  if (cafeSt == 0) {                       // работает: курит и следит за струёй
    g.drawFastHLine(BCX - 4, hy + 7, 6, EYE_DARK);
    cigarette(g, BCX + 3, hy + 6, (((int)(tt * 3)) % 2) == 0, 11);
    smokeTrail(g, BCX + 20, hy + 5, tt, 14, 2.0f, SMOKE);
    arm(g, BCX + 13, hy + 9, 62, CNT_Y - CAFE_BOX_Y - 8, 8, tt, 1.6f);   // рука на стойке
  } else if (cafeSt == 1) {                // перерыв: потягивается и зевает
    float yawn = (ph5 >= 0.50f && ph5 < 0.76f) ? fastSin((ph5 - 0.50f) / 0.26f * 3.1416f) : 0;
    if (yawn > 0.15f) g.fillCircle(BCX + 1, hy + 8, 2 + iround(yawn * 3), EYE_DARK);
    else              g.drawFastHLine(BCX - 3, hy + 7, 7, EYE_DARK);
    int ex = iround(reach * 9), ey = iround(reach * 20);
    arm(g, BCX - 12, hy + 8, BCX - 17 - ex, hy - 5 - ey, -6, tt, 1.0f);
    arm(g, BCX + 12, hy + 8, BCX + 17 + ex, hy - 5 - ey, 6, tt, 1.0f);
  } else if (cafeSt == 2) {                // обед: сэндвич ко рту и обратно
    float p = fmodf(tt, 8.0f) / 8.0f;
    float lift = p < 0.12f ? p / 0.12f : p < 0.70f ? 1.0f
                 : p < 0.76f ? 1 - (p - 0.70f) / 0.06f : 0.0f;
    int bites = p < 0.18f ? 0 : p < 0.50f ? 1 : 2;
    bool bite = (p >= 0.16f && p < 0.24f) || (p >= 0.48f && p < 0.56f);
    int plateY = CNT_Y - CAFE_BOX_Y - 18;
    int sx = iround(52 - 9 + (BCX + 7 - (52 - 9)) * lift);
    int sy = iround(plateY + (hy + 1 - plateY) * lift);
    g.fillRect(52 - 12, plateY + 13, 24, 2, lerp565(0xE73C, BG, 0.25f));   // тарелка
    g.fillRect(BCX - 3, hy + 6, 7, bite ? 5 : 1, EYE_DARK);
    arm(g, BCX + 12, hy + 12, sx + 8, sy + 6, 7 - lift * 4, tt, 1.4f);
    int sw = 18 - bites * 5;
    if (sw > 3) {
      g.fillRect(sx, sy, sw, 3, CREAMC);
      g.fillRect(sx, sy + 3, sw, 3, 0x6EC5);
      g.fillRect(sx, sy + 6, sw, 4, 0xAAA9);
      g.fillRect(sx, sy + 10, sw, 3, CREAMC);
    }
  } else if (cafeSt == 4) {                // уборка: водит тряпкой по стойке
    float mop = fmodf(tt, 2.4f) / 2.4f;
    int mopX = BCX + 6 + iround(fastSin(mop * 6.2832f) * 22);
    int mopY = CNT_Y - CAFE_BOX_Y - 7;
    g.drawFastHLine(BCX - 3, hy + 7, 7, EYE_DARK);
    arm(g, BCX + 10, hy + 12, mopX, mopY, 5, tt, 2.2f);
    g.fillRect(mopX - 6, mopY, 12, 4, lerp565(ACCENT, BG, 0.35f));
    g.drawFastHLine(mopX - 6, mopY, 12, lerp565(ACCENT, 0xFFFF, 0.4f));
    for (int k = 0; k < 3; k++) {
      float f = fmodf(tt * 1.6f + k / 3.0f, 1.0f);
      int x = mopX - 10 + iround(f * 20);
      if (((x + k) & 3) == 0) g.drawPixel(x, mopY - 3 - iround(f * 3), lerp565(ACCENT, BG, f * 0.7f));
    }
  } else {                                 // закрыто: спит, «z z»
    g.drawFastHLine(BCX - 3, hy + 7, 7, EYE_DARK);
    arm(g, BCX + 12, hy + 10, 60, CNT_Y - CAFE_BOX_Y - 4, 6, tt, 0.5f);
    int n = ((int)(tt * 0.9f)) % 3;
    for (int z = 0; z <= n; z++) {
      int zx = BCX + 14 + z * 5, zy = hy - SPH_R - 4 - z * 6;
      g.fillRect(zx, zy, 4, 1, CREAMC);
      g.fillRect(zx, zy + 3, 4, 1, CREAMC);
      g.drawLine(zx + 3, zy, zx, zy + 3, CREAMC);
    }
  }

}

// Кадр анимации баристы: буфер + один блит окна.
void animateBarista(float tt) {
  octoBuf.fillScreen(BG);
  baristaArt(octoBuf, tt);
  blitCanvasRect(0, 0, BUF_W, BUF_H, CAFE_BOX_X, CAFE_BOX_Y);
}

// Динамика кофейни: окно баристы + полоса реквизита. Раз в 40мс, как аквариум.
// Стойка НЕ перерисовывается — она статика и живёт между кадрами.
// Фаза стакана — одна функция на оба пути, чтобы кадр и полная перерисовка
// не разошлись в том, сколько налито.
void cafePour(float tt, float &fill, bool &steam, bool &pouring) {
  fill = 0.5f;
  pouring = false;
  if (cafeSt == 0) {
    float cyc = fmodf(tt, 5.0f) / 5.0f;
    fill = min(1.0f, cyc * 1.45f);
    pouring = cyc < 0.72f;
  } else if (cafeSt == 3) fill = 0.15f;
  steam = (cafeSt == 0 && fill > 0.45f);
}

// Сцена в ПОЛОСУ (полная перерисовка и снимок): те же функции рисования, что в кадре
// анимации, но цель переносит их локальные координаты в окна на экране и обрезает
// ровно по этим окнам — иначе кадр анимации потом не стёр бы то, что вылезло.
void composeCafeScene(OffsetCanvas &g, float tt) {
  float fill;
  bool steam, pouring;
  cafePour(tt, fill, steam, pouring);

  g.originAt(CAFE_BOX_X, CAFE_BOX_Y);
  g.clipTo(CAFE_BOX_X, CAFE_BOX_Y, CAFE_BOX_X + BUF_W - 1, CAFE_BOX_Y + BUF_H - 1);
  baristaArt(g, tt);
  g.originAt(BAND_X, BAND_Y);
  g.clipTo(BAND_X, BAND_Y, BAND_X + BAND_W - 1, BAND_Y + BAND_H - 1);
  propsArt(g, tt, fill, steam, pouring);
  g.originReset();
  g.clipOff();
}

// Кадр анимации кофейни: только два окна, остальной экран не трогаем.
void animateCafeScene(float tt) {
  float fill;
  bool steam, pouring;
  cafePour(tt, fill, steam, pouring);
  animateBarista(tt);
  animateProps(tt, fill, steam, pouring);
}

void composeCafe(Adafruit_GFX &g) {
  char buf[8];
  g.fillScreen(BG);
  g.fillRect(0, 0, W, 18, lerp565(COFFEE_DK, BG, 0.35f));
  g.drawFastHLine(0, 18, W, COFFEEC);
  drawTextRu(g, 8, 5, "КОФЕЙНЯ ОСЬМИНОГА", CREAMC, 1);

  static const char *DOW[7] = {"ПН", "ВТ", "СР", "ЧТ", "ПТ", "СБ", "ВС"};
  cafeTime(cafeNm, buf);
  char head[48];
  snprintf(head, sizeof(head), "%s %s", DOW[cafeDow % 7], buf);
  drawTextRu(g, W - 8 - textWidthRu(head, 1), 5, head, CREAMC, 1);

  // статус крупно + до какого времени и сколько осталось
  uint16_t col = cafeColor(cafeSt);
  g.fillRect(126, 38, 4, 26, col);
  drawTextRu(g, 138, 38, cafeLabel(cafeSt), col, 3);
  cafeTime(cafeTill, buf);
  char line[48];        // кириллица в UTF-8 — два байта на символ:
                        // «ОТКРОЕТСЯ В 00:00» это 29 байт, в 24 не влезало
  snprintf(line, sizeof(line), "%s %s", cafeSt == 3 ? "ОТКРОЕТСЯ В" : "ДО", buf);
  drawTextRu(g, 138, 66, line, lerp565(CREAMC, BG, 0.25f), 1);
  int left = (cafeTill - cafeNm + 1440) % 1440;
  if (left >= 60) snprintf(line, sizeof(line), "ЕЩЁ %dЧ %dМ", left / 60, left % 60);
  else            snprintf(line, sizeof(line), "ЕЩЁ %dМ", left);
  drawTextRu(g, 138, 80, line, col, 2);

  // полоса дня: вся длина — рабочее время, вырезы — перерывы и уборка
  const int bx = 12, by = 140, bw = 296, bh = 14;
  g.drawRect(bx - 1, by - 1, bw + 2, bh + 2, lerp565(CREAMC, BG, 0.7f));
  if (cafeCm > cafeOm) {
    int span = cafeCm - cafeOm;
    g.fillRect(bx, by, bw, bh, lerp565(C_WORKING, BG, 0.55f));
    int mx = bx + (long)(min(max(cafeNm, cafeOm), cafeCm) - cafeOm) * bw / span;
    if (cafeNm > cafeOm) g.fillRect(bx, by, mx - bx, bh, lerp565(C_WORKING, BG, 0.82f));
    for (int i = 0; i < cafeSegN; i++) {
      int a = bx + (long)(cafeSegs[i].from - cafeOm) * bw / span;
      int b2 = bx + (long)(cafeSegs[i].to - cafeOm) * bw / span;
      uint16_t sc = cafeSegs[i].kind == 1 ? 0xFC80 : (cafeSegs[i].kind == 2 ? ACCENT : C_WAITING);
      g.fillRect(a, by, max(2, b2 - a), bh, lerp565(sc, BG, cafeNm >= cafeSegs[i].to ? 0.78f : 0.35f));
    }
    if (cafeNm >= cafeOm && cafeNm <= cafeCm) {
      g.fillRect(mx - 1, by - 4, 3, bh + 8, CREAMC);
      g.fillRect(mx - 3, by - 6, 7, 2, CREAMC);
    }
    g.setTextSize(1);
    g.setTextColor(lerp565(CREAMC, BG, 0.45f));
    cafeTime(cafeOm, buf); g.setCursor(bx, by + 18); g.print(buf);
    cafeTime(cafeCm, buf); g.setCursor(bx + bw - 30, by + 18); g.print(buf);
  } else {
    g.fillRect(bx, by, bw, bh, lerp565(C_IDLE, BG, 0.75f));
    g.setTextSize(1);
    g.setTextColor(lerp565(CREAMC, BG, 0.45f));
    drawTextRu(g, 140, by + 18, "ВЫХОДНОЙ", lerp565(CREAMC, BG, 0.45f), 1);
  }

  // таблица: часы, обед/уборка, чистое время
  cafeCounter();                     // стойка статична — бариста за ней
  g.drawFastHLine(12, 168, 296, lerp565(CREAMC, BG, 0.85f));
  g.setTextSize(1);
  int rowY = 176;
  g.setTextColor(lerp565(CREAMC, BG, 0.55f));
  drawTextRu(g, 12, rowY, "ЧАСЫ", lerp565(CREAMC, BG, 0.55f), 1);
  g.setTextColor(0xE71C);
  g.setCursor(220, rowY);
  if (cafeCm > cafeOm) {
    cafeTime(cafeOm, buf); g.print(buf); g.print('-');
    cafeTime(cafeCm, buf); g.print(buf);
  } else drawTextRu(g, 220, rowY, "ЗАКРЫТО", 0xE71C, 1);

  for (int i = 0; i < cafeSegN; i++) {
    if (cafeSegs[i].kind == 0) continue;
    rowY += 15;
    g.setTextColor(lerp565(CREAMC, BG, 0.55f));
    drawTextRu(g, 12, rowY, cafeSegs[i].kind == 1 ? "ОБЕД" : "УБОРКА",
               lerp565(CREAMC, BG, 0.55f), 1);
    g.setTextColor(cafeSegs[i].kind == 1 ? 0xFC80 : ACCENT);
    g.setCursor(220, rowY);
    cafeTime(cafeSegs[i].from, buf); g.print(buf); g.print('-');
    cafeTime(cafeSegs[i].to, buf); g.print(buf);
  }

  rowY += 15;
  g.setTextColor(lerp565(CREAMC, BG, 0.55f));
  drawTextRu(g, 12, rowY, "ИТОГО", lerp565(CREAMC, BG, 0.55f), 1);
  g.setTextColor(C_WORKING);
  g.setCursor(220, rowY);
  snprintf(line, sizeof(line), "%dЧ %dМ", cafeNet / 60, cafeNet % 60);
  drawTextRu(g, 220, rowY, line, C_WORKING, 1);
}

// =============================================================================
// Сон
// =============================================================================
void enterSleep() {
  tft.fillScreen(BG);
  tft.sendCommand(ILI9341_DISPOFF);
}

void leaveSleep() {
  tft.sendCommand(ILI9341_DISPON);
  cafeDirty = true;
  for (int i = 0; i < MAX_SESSIONS; i++) sessions[i].active = false;  // заставить полный diff
  redrawAll();
}

// =============================================================================
// Приём снэпшота
// =============================================================================
void readSerial() {
  while (Serial.available()) {
    char c = (char)Serial.read();
    if (c == '\n') {
      lineBuf[lineLen] = '\0';
      handleLine(lineBuf);
      lineLen = 0;
    } else if (lineLen < LINE_MAX - 1) {
      lineBuf[lineLen++] = c;
    } else {
      lineLen = 0;
    }
  }
}

// Документ статический, а не локальный: 2 КБ на стеке (у ESP8266 его ~4 КБ) плюс
// цепочка handleLine → applySnapshot → redrawCell → redrawRect — верный способ
// получить исключение по переполнению стека.
StaticJsonDocument<2048> doc;

// --- отладочный снимок экрана -------------------------------------------------
// Читать панель нельзя (MISO не разведён), но можно отдать то, что прошивка сама
// собрала. Экран собирается плитками 68×76 в канву со смещением и уходит в serial
// шестнадцатеричными строками; мост складывает из них PNG.
// Нужен затем, чтобы визуальные ошибки ловились до заливки, а не глазами человека.

// Композиция для снимка: те же функции, что рисуют на экран, только цель — канва.
void composeCurrentScreen(OffsetCanvas &g, int top, int bot, int left, int right, float tt) {
  if (curScreen == 1) { composeCafe(g); composeCafeScene(g, tt); return; }
  if (curScreen == 2) { composeRoulette(g, top, bot, left, right, tt); return; }
  if (curScreen == 3) { composeSlots(g, top, bot, left, right, tt); return; }
  composeGrid(g);
  CanvasSink sink(g);
  for (int i = 0; i < MAX_SESSIONS; i++) {
    if (!sessions[i].active) continue;
    int col = i % COLS, row = i / COLS;
    if (row * cellH + cellH - 1 < top || row * cellH > bot) continue;
    if (col * cellW + cellW - 1 < left || col * cellW > right) continue;
    SmogP p;
    smogParams(col, row, sessions[i].mb, p);
    unsigned long ts = micros();
    if (p.on)
      for (int y = max(p.y0, top); y <= min(p.y1, bot); y++) smogRow(sink, p, y);
    usSmog += micros() - ts;
    cardFrame(g, col, row, sessions[i]);
    int cx, cy, wx, wy;
    octoWindow(col, row, wx, wy, cx, cy);
    // осьминог занимает 72 строки и попадает в несколько полос; в те, где его
    // нет, не лезем вовсе — иначе полная перерисовка считала бы его 15 раз впустую
    if (wy + OCTO_H - 1 >= top && wy <= bot) {
      unsigned long to = micros();
      // рисуем РОВНО в то окно, которое обновляет анимация: то, что вышло бы за него
      // (клубы дыма, кончики щупалец), анимация уже никогда не сотрёт — и это
      // оставалось мусором у имени карточки
      g.clipTo(wx, wy, wx + BUF_W - 1, wy + OCTO_H - 1);
      drawOctopus(g, cx, cy, sessions[i], tt + i * 0.4f);
      g.clipOff();
      usOcto += micros() - to;
      nOcto++;
    }
    g.fillRect(col * cellW + 5, row * cellH + 5, 3, 3, stateColor(sessions[i].state));
  }
}

#if ESP_SHOT
void sendShot() {
  Serial.print(F("{\"esp\":\"shot\",\"w\":"));
  Serial.print(W);
  Serial.print(F(",\"h\":"));
  Serial.print(H);
  Serial.println(F("}"));

  // Один буфер на обе записи (RLE длиннее сырой, значит вмещает и её) и никакой
  // копии предыдущей строки: она лежит рядом в той же полосе. Раньше здесь было
  // три буфера на 3844 байта — это заметная доля запаса кучи ESP8266 ради
  // отладочной функции. static, а не на стеке: sendShot зовётся из разбора serial,
  // стека там 4 КБ.
  static char shotLine[W * 6 + 2];
  static const char HEXD[] = "0123456789abcdef";
  float tt = millis() / 1000.0f;

  // Полосы ТЕ ЖЕ, что рисуют экран, и тем же буфером: снимок физически не может
  // разойтись с картинкой — отдельной версии отрисовки для него не существует.
  for (int ty = 0; ty < H; ty += STRIP_H) {
    int h = min(STRIP_H, H - ty);
    stripBuf.moveTo(0, ty);
    stripBuf.fillScreen(BG);
    // Та же базовая обрезка, что у панели: без неё снимок рисовал то, чего на панели
    // нет, и один раз уже соврал (имя карточки).
    stripBuf.clipBase(0, ty, W - 1, ty + h - 1);
    composeCurrentScreen(stripBuf, ty, ty + h - 1, 0, W - 1, tt);
    stripBuf.clipBase(-32768, -32768, 32767, 32767);

    Serial.print(F("{\"esp\":\"tile\",\"x\":0,\"y\":")); Serial.print(ty);
    Serial.print(F(",\"w\":")); Serial.print(W);
    Serial.print(F(",\"h\":")); Serial.print(h);
    Serial.println(F("}"));

    // Сырой hex — это 307 КБ на экран, то есть ~27с при 115200: снимок не
    // успевал дойти и склеивался неполным. Поэтому на строку выбираем самую
    // короткую из трёх записей, а мост понимает все три.
    uint16_t *b = stripBuf.getBuffer();
    int dup = 0;
    for (int y = 0; y < h; y++) {
      uint16_t *cur = b + y * W;
      // предыдущая строка — соседняя в этой же полосе; за границу полосы повтор
      // не переходит намеренно, мост тоже сбрасывает его на каждой плитке
      if (y > 0 && memcmp(cur, cur - W, W * 2) == 0) { dup++; continue; }
      if (dup) { Serial.print('#'); Serial.println(dup); dup = 0; }

      int runs = 0;                        // сначала СЧИТАЕМ длину RLE, не собирая её
      for (int x = 0; x < W;) {
        int run = 1;
        while (x + run < W && cur[x + run] == cur[x] && run < 255) run++;
        runs++;
        x += run;
      }

      int n = 0;
      if (runs * 6 + 1 < W * 4) {          // rle: длина серии (2 hex) + цвет (4 hex)
        for (int x = 0; x < W;) {
          int run = 1;
          while (x + run < W && cur[x + run] == cur[x] && run < 255) run++;
          uint16_t v = cur[x];
          shotLine[n++] = HEXD[(run >> 4) & 15]; shotLine[n++] = HEXD[run & 15];
          shotLine[n++] = HEXD[(v >> 12) & 15];  shotLine[n++] = HEXD[(v >> 8) & 15];
          shotLine[n++] = HEXD[(v >> 4) & 15];   shotLine[n++] = HEXD[v & 15];
          x += run;
        }
        shotLine[n] = 0;
        Serial.print('L');
      } else {                             // raw: 4 hex на пиксель
        for (int x = 0; x < W; x++) {
          uint16_t v = cur[x];
          shotLine[n++] = HEXD[(v >> 12) & 15]; shotLine[n++] = HEXD[(v >> 8) & 15];
          shotLine[n++] = HEXD[(v >> 4) & 15];  shotLine[n++] = HEXD[v & 15];
        }
        shotLine[n] = 0;
      }
      Serial.println(shotLine);
      yield();
    }
    if (dup) { Serial.print('#'); Serial.println(dup); }
  }
  Serial.println(F("{\"esp\":\"shot_end\"}"));
}
#endif

void handleLine(const char *line) {
  doc.clear();
  if (deserializeJson(doc, line)) {
#if ESP_DIAG
    diagBadJson++;
#endif
    return;
  }
  // отладочная команда от моста — не снэпшот
  const char *cmd = doc["cmd"] | "";
  if (cmd[0]) {
#if ESP_SHOT
    if (strcmp(cmd, "shot") == 0) sendShot();
#endif
    // Щелчок ручки «руками моста»: единственный способ проверить физику барабана
    // без человека у энкодера.
    if (strcmp(cmd, "kick") == 0) {
      if (curScreen == 2) roulKick();
      else if (curScreen == 3) slotKick();
    }
    return;
  }
#if ESP_DIAG
  diagSnaps++;
#endif

  bool wantSleep = doc["slp"] | 0;
  if (wantSleep != sleeping) {
    sleeping = wantSleep;
    if (sleeping) enterSleep(); else leaveSleep();
  }
  if (sleeping) return;

  int night = doc["nl"] | 0;
  if (night / 10 != nightLevel / 10) {      // палитра меняется ступеньками, не в кадре
    nightLevel = night;
    buildSphere(night);
    for (int i = 0; i < MAX_SESSIONS; i++) lastTick[i] = 0;
  }

  int scr = doc["scr"] | 0;
  if (scr != curScreen) {
    curScreen = scr;
    // Смена экрана — ВСЕГДА полная перерисовка: в буфере лежит чужой экран.
    if (scr == 0) {
      for (int i = 0; i < MAX_SESSIONS; i++) sessions[i].active = false;
      redrawAll();
    }
    cafeDirty = true;
    roulDirty = true;
    slotDirty = true;
  }

  if (scr == 3) {
    JsonObject sl = doc["slot"];
    if (!sl.isNull()) {
      int pts = sl["pts"] | 0, bet = sl["bet"] | 5, rec = sl["rec"] | 0, prg = sl["prg"] | 0;
      int eta = sl["eta"] | -1;
      // eta в diff намеренно: надпись меняется редко, но если её не считать
      // изменением, она застрянет до ближайшей перерисовки по другой причине.
      if (pts != slotPts || bet != slotBet || rec != slotRec || eta != slotEta) slotDirty = true;
      slotPts = pts; slotBet = bet; slotRec = rec; slotPrg = prg; slotEta = eta;
      int sp = sl["sp"] | 0, win = sl["win"] | 0;
      if (sp != slotSp) {                  // новый ответ на нашу раскрутку
        slotSp = sp;
        slotWin = win;
        JsonArray arr = sl["r"].as<JsonArray>();
        int i = 0;
        for (JsonVariant v : arr) { if (i < 3) slotTarget[i++] = v | 0; }
        if (win == -1) { slotTarget[0] = slotTarget[1] = slotTarget[2] = -1; }
      }
    }
    return;
  }

  if (scr == 2) {
    JsonObject r = doc["roul"];
    if (!r.isNull()) {
      JsonArray arr = r["p"].as<JsonArray>();
      int n = 0;
      bool changed = false;
      for (JsonVariant v : arr) {
        if (n >= R_PLACES_MAX) break;
        const char *nm = v | "";
        if (strcmp(roulPlaces[n], nm) != 0) {
          strlcpy(roulPlaces[n], nm, R_NAME_MAX);
          changed = true;
        }
        n++;
      }
      if (n != roulN) { roulN = n; changed = true; }
      int win = r["win"] | -1, sp = r["sp"] | 0;
      cafeNm = r["nm"] | cafeNm;      // время для шапки: блока кофейни здесь нет
      if (sp != roulSp) { roulSp = sp; roulWin = win; }   // новый ответ на нашу раскрутку
      else roulWin = win;
      if (changed) {                                     // состав сменился — сбрасываем барабан
        roulState = R_IDLE; roulVel = 0; roulPos = 0; roulSeenSp = roulSp;
        roulDirty = true;
    slotDirty = true;
      }
    }
    return;
  }

  if (scr == 1) {
    JsonObject c = doc["cafe"];
    if (!c.isNull()) {
      int st = c["st"] | 3, nm = c["nm"] | 0, till = c["till"] | 0;
      if (st != cafeSt || nm != cafeNm || till != cafeTill) cafeDirty = true;
      cafeSt = st; cafeNm = nm; cafeTill = till;
      cafeDow = c["dow"] | 0;
      cafeOm = c["om"] | 0; cafeCm = c["cm"] | 0; cafeNet = c["net"] | 0;
      cafeSegN = 0;
      for (JsonArray seg : c["br"].as<JsonArray>()) {
        if (cafeSegN >= 8) break;
        cafeSegs[cafeSegN].from = seg[0] | 0;
        cafeSegs[cafeSegN].to   = seg[1] | 0;
        cafeSegs[cafeSegN].kind = seg[2] | 0;
        cafeSegN++;
      }
    }
    return;
  }

  int page = doc["p"] | 1, pages = doc["pn"] | 1;
  if (page != curPage || pages != curPages) {
    curPage = page; curPages = pages;
    popupPage = page; popupPages = pages;
    popupUntil = millis() + 1400;           // страница сменилась — показать «2/3»
    // Меняется ВЕСЬ состав, поэтому и перерисовка общая: по одной ячейке это
    // ~20мс каждая, и смена страницы читалась как волна слева направо.
    fullRedraw = true;
  }

  JsonArray arr = doc["sessions"].as<JsonArray>();
  int n = 0;
  for (JsonObject s : arr) {
    if (n >= MAX_SESSIONS) break;
    Session &c = incoming[n];
    c.active = true;
    strlcpy(c.id, s["id"] | "", sizeof(c.id));
    strlcpy(c.name, s["name"] | "", sizeof(c.name));
    c.state = (State)(int)(s["state"] | (int)IDLE);
    c.sub = s["sub"] | 0;
    c.mb  = s["mb"] | 0;
    c.seed = hash32(c.id);
    c.poke = 0;
    c.born = 0;
    n++;
  }
  for (int i = n; i < MAX_SESSIONS; i++) incoming[i].active = false;

  applySnapshot();
}

// Прошлый состав держим отдельно и статически: сессию надо искать ПО ID, а не по
// слоту. Иначе исчезнувшая из середины сессия (/new, /clear, конец работы) сдвигает
// все следующие на слот влево, и прошивка считает их новыми — всплывают все разом.
// Статически, а не на стеке: у ESP8266 его ~4 КБ, а цепочка вызовов тут глубокая.
Session prevCards[MAX_SESSIONS];

// Diff по ячейкам: перерисовываем только те, где сменился состав, имя, статус
// или вес (копоть — статика и живёт в той же перерисовке).
void applySnapshot() {
  memcpy(prevCards, sessions, sizeof(prevCards));

  for (int i = 0; i < MAX_SESSIONS; i++) {
    Session &b = incoming[i];

    // та же сессия в прошлом составе — могла переехать в другой слот
    int was = -1;
    if (b.active) {
      for (int k = 0; k < MAX_SESSIONS; k++) {
        if (prevCards[k].active && strcmp(prevCards[k].id, b.id) == 0) { was = k; break; }
      }
    }
    // события выводим из diff'а по СЕССИИ: протокол ради них не нужен
    unsigned long poke = 0, born = 0;
    if (was >= 0) {
      poke = prevCards[was].poke;
      born = prevCards[was].born;
      if (prevCards[was].state != WORKING && b.state == WORKING) poke = millis() + 400;
    } else if (b.active) {
      born = millis();                    // этой сессии на экране не было — всплывает
    }

    // перерисовка — уже по слоту: важно, изменилось ли то, что в нём нарисовано
    Session &a = prevCards[i];
    bool changed = a.active != b.active ||
                   (b.active && (strcmp(a.id, b.id) != 0 || a.state != b.state ||
                                 strcmp(a.name, b.name) != 0 || a.mb != b.mb));
    sessions[i] = b;
    sessions[i].poke = poke;
    sessions[i].born = born;
    if (changed && !fullRedraw) {
      redrawCell(i);
#if ESP_DIAG
      diagCells++;
#endif
    }
  }

  if (fullRedraw) {
    fullRedraw = false;
    redrawAll();          // один fillScreen, дальше все осьминоги одним тиком
  }
}

// =============================================================================
// setup / loop
// =============================================================================
void setup() {
  Serial.setRxBufferSize(1024);
  Serial.begin(115200);
  // Маркер загрузки печатаем всегда: мост его логирует, и по причине сброса сразу
  // видно, что случилось — watchdog, исключение или просто дёрнули DTR.
  Serial.println();
  Serial.print(F("{\"esp\":\"boot\",\"ver\":"));
  Serial.print(FW_VER);
  Serial.print(F(",\"reason\":\""));
  Serial.print(ESP.getResetReason());
  Serial.print(F("\",\"heap\":"));
  Serial.print(ESP.getFreeHeap());
  Serial.println(F("}"));

  pinMode(ENC_A, INPUT_PULLUP);
  pinMode(ENC_B, INPUT_PULLUP);
  pinMode(ENC_SW, INPUT);            // GPIO16: подтяжка внешняя, см. WIRING.md
  attachInterrupt(digitalPinToInterrupt(ENC_A), encISR, CHANGE);
  attachInterrupt(digitalPinToInterrupt(ENC_B), encISR, CHANGE);

  cellW = W / COLS;
  cellH = H / ROWS;

  tft.begin(40000000);
  tft.setSPISpeed(40000000);
  tft.setRotation(3);

  randomSeed(micros());
  evNext = millis() + 120000;        // первый вброс не раньше, чем через пару минут

  buildSin();
  buildSmogNoise();
  buildSphere(0);
  for (int i = 0; i < MAX_SESSIONS; i++) sessions[i].active = false;
  redrawAll();
}

void loop() {
  readSerial();
  pollEncoder();
  if (sleeping) return;              // спим: ни кадров, ни SPI

  unsigned long now = millis();

  if (curScreen == 3) {
    if (slotDirty) { slotDirty = false; slotStatusShown = -1; redrawAll(); }
    unsigned long frame = now / ROUL_FRAME_MS;
    if (frame != lastFrame) {
      lastFrame = frame;
      slotPhysics(now);
      // Барабаны — каждый кадр, они несут движение. Остальное живое (пузыри,
      // водоросли, огни корпуса) обновляется срезами по одному за кадр: цена кадра
      // от этого постоянная, а не прыгает.
      if (slotState == S_SPINNING || slotState == S_LANDING || (now - slotShownAt) < 2000)
        redrawRect(S_WX - 10, S_WIN_Y - 8, 3 * S_BW + 2 * S_GAP + 20, S_ROW + 12);
      // Крупье с рычагом — каждый кадр: заливки стали дешёвыми, и полоса влезает.
      // Полоса кончается ВЫШЕ строки состояния: раньше она задевала её верхние
      // две строки пикселей на левых 84px — и мигающий текст перерисовывался
      // огрызком, что читалось как «что-то накладывается».
      redrawRect(0, 78, S_CAB_X, S_STATUS_Y - 78);
      // Дно — половинками через кадр: целиком оно стоило 16мс и выбивало каждый
      // третий кадр за бюджет. Водоросли качаются медленно, деление незаметно.
      if ((frame & 1) == 0) redrawRect(0, S_BED_Y, W / 2, H - S_BED_Y);
      else                  redrawRect(W / 2, S_BED_Y, W / 2, H - S_BED_Y);
      // Строка состояния — ПОСЛЕДНЕЙ и целиком: она мигает, а мигающее нельзя
      // обновлять по кускам в разных кадрах — половины окажутся в разных фазах.
      // Прямоугольник ведёт по S_STATUS_Y, а не по низу экрана: строка автомата
      // сидит выше рулеточной, и старый (0,H-16) не накрывал её вовсе — потому
      // надпись и застревала, пока её случайно не задевали соседние полосы.
      int kind = slotWin == -1 ? 3
                 : slotState == S_SHOWN ? 2
                 : (slotState == S_SPINNING || slotState == S_LANDING) ? 1 : 0;
      bool flashing = (slotState == S_SHOWN   && now - slotShownAt   < 1200) ||
                      (slotState == S_REFUSED && now - slotRefusedAt < 1600);
      if (kind != slotStatusShown || flashing) {
        slotStatusShown = kind;
        redrawRect(0, S_STATUS_Y - 2, W, 13);
      }
    }
    return;
  }

  if (curScreen == 2) {
    if (roulDirty) { roulDirty = false; roulStatusShown = -1; redrawAll(); }
    // Свой период кадра: барабан стоит ~39мс, и на сетке 40мс кадры то влезали, то
    // нет — частота скакала. Стабильные 20 к/с лучше прыгающих 24: глаз замечает
    // не абсолютную частоту, а её рывки.
    unsigned long frame = now / ROUL_FRAME_MS;
    if (frame != lastFrame) {
      lastFrame = frame;
      roulPhysics(now);
      // Барабан — только когда движется: стоящий перерисовывать нечего, это 39мс
      // впустую. Живой ряд (барабан, крупье, пузыри) перерисовывается ОДНИМ
      // прямоугольником каждый кадр: раньше он делился на три среза, и всё в нём
      // обновлялось на треть частоты — пузыри и покачивание читались рвано.
      redrawRect(0, R_TOP - 2, W, R_BOT - R_TOP + 4);
      // Строка снизу лежит ВНЕ перерисовываемых полос, поэтому обновляем её
      // событием — когда состояние сменилось. Раньше она менялась только при
      // полной перерисовке и подолгу висела неверной («крути ручку» на летящем
      // барабане). На время вспышки победы обновляем каждый кадр: это 4мс.
      int kind = roulState == R_WON ? 2
                 : (roulState == R_SPIN || roulState == R_LAND) ? 1 : 0;
      bool flashing = (roulState == R_WON) && (now - roulWonAt) < 1200;
      if (kind != roulStatusShown || flashing) {
        roulStatusShown = kind;
        redrawRect(0, H - 16, W, 16);
      }

    }
    return;
  }

  if (curScreen == 1) {
    // Статика кофейни (шапка, статус, полоса дня, таблица) — тем же путём полосами,
    // что и аквариум: раньше она рисовалась примитив за примитивом прямо на панель,
    // и было видно, как текст выводится построчно.
    if (cafeDirty) { cafeDirty = false; redrawAll(); }
    unsigned long frame = now / FRAME_MS;
    if (frame != lastFrame) {              // динамика: бариста и реквизит, два окна
      lastFrame = frame;
      animateCafeScene(now / 1000.0f);
    }
    return;
  }

  // вбросы: раз в несколько минут, в случайную карточку
  if (now > evNext) {
    evNext = now + 120000 + random(180000);
    int live = 0;
    for (int i = 0; i < MAX_SESSIONS; i++) if (sessions[i].active) live++;
    if (live > 0) {
      int pick = random(live), cell = -1;
      for (int i = 0, k = 0; i < MAX_SESSIONS; i++)
        if (sessions[i].active && k++ == pick) { cell = i; break; }
      switch (random(3)) {
        case 0: evFishCell = cell; evFishUntil = now + 6500; break;
        case 1: evBubUntil = now + 4000; break;
        default: evCrabCell = cell; evCrabUntil = now + 9000; break;
      }
    }
  }

  // всплывашка страницы
  if (popupUntil > now && !popupDrawn) drawPopup();
  else if (popupUntil <= now && popupDrawn) hidePopup();

#if ESP_DIAG
  unsigned long frameT0 = micros();
#endif
  // Общая сетка кадров: карточка обновляется, когда номер кадра делится на её
  // делитель. Все обновления попадают на одни и те же границы, а не разъезжаются
  // по своим таймерам — после перелистывания это было видно как рваная анимация.
  unsigned long frame = now / FRAME_MS;
  if (frame != lastFrame) {
    lastFrame = frame;
    for (int i = 0; i < MAX_SESSIONS; i++) {
      if (!sessions[i].active) continue;
      if (frame % frameDiv(sessions[i].state)) continue;
      updateOctopusArea(i % COLS, i / COLS, sessions[i], now / 1000.0f + i * 0.4f);
    }
  }
#if ESP_DIAG
  unsigned long frameUs = micros() - frameT0;
  if (frameUs > maxFrameUs) maxFrameUs = frameUs;
  if (now - lastStat > 2000) {
    lastStat = now;
    Serial.print(F("{\"esp\":\"stat\",\"ver\":")); Serial.print(FW_VER);
    Serial.print(F(",\"heap\":"));                Serial.print(ESP.getFreeHeap());
    Serial.print(F(",\"frag\":"));                Serial.print(ESP.getHeapFragmentation());
    Serial.print(F(",\"maxframe_us\":"));         Serial.print(maxFrameUs);
    Serial.print(F(",\"draw_us\":"));             Serial.print(maxDrawUs);
    Serial.print(F(",\"blit_us\":"));             Serial.print(maxBlitUs);
    Serial.print(F(",\"snaps\":"));               Serial.print(diagSnaps);
    Serial.print(F(",\"cells\":"));               Serial.print(diagCells);
    Serial.print(F(",\"badjson\":"));             Serial.print(diagBadJson);
    Serial.println(F("}"));
    maxFrameUs = 0; maxDrawUs = 0; maxBlitUs = 0;
  }
#endif
}
