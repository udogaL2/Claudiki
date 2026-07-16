// OctoDash firmware — ESP8266 (WeMos D1 mini) + ILI9341 320x240.
//
// Рендерер «аквариума» осьминогов. База анимации — рабочий скетч с захардкоженными
// сессиями; сверху добавлен приём снэпшота по USB serial (одна строка JSON + '\n'),
// парсинг и diff-перерисовка. Вся логика (какие сессии живы, статусы) — на стороне
// моста; прошивка только рисует. Контракт — CLAUDE.md §«Контракт прошивки».
//
// Формат снэпшота (полный, не дельты):
//   {"v":1,"sessions":[{"id":"abc","name":"proj","state":0}, ...]}\n
// Коды состояний: WORKING=0, WAITING=1, IDLE=2, ERR=3.
//
// Зависимости (Arduino Library Manager): Adafruit GFX, Adafruit ILI9341, ArduinoJson (v6).

#include <SPI.h>
#include <math.h>
#include <Adafruit_GFX.h>
#include <Adafruit_ILI9341.h>
#include <ArduinoJson.h>

// --- Диагностика (для отладки «моргания»; выключи ESP_DIAG 0 в проде) ---------
// Печатает в serial маркеры, которые мост читает и логирует (см. OCTO_DIAG):
//   boot  — при старте: причина сброса + free heap  → видно РЕБУТЫ и их причину
//   stat  — раз в 2с: активных сессий, heap, фрагментация, макс. фрейм-тайм,
//           число снэпшотов и битого JSON → ловит «порог 3» в цифрах
//   badjson — снэпшот не распарсился (переполнение UART при тяжёлом рендере?)
#define ESP_DIAG 1

#define TFT_CS   D8
#define TFT_DC   D4
#define TFT_RST  D3

Adafruit_ILI9341 tft = Adafruit_ILI9341(TFT_CS, TFT_DC, TFT_RST);

#define BG        0x0000
#define GRIDLINE  0x0861
// Палитра тела (глянцевая сфера, «вариант 3» из style-lab), RGB565
#define BODY_LIGHT 0xEF1F  // свет на сфере
#define BODY_MID   0x8B77  // средний тон
#define BODY_BOT   0x410F  // тень сферы
#define BODY_DK    0x28CC  // самый тёмный (корни щупалец / стык)
#define TENT       0x7A57  // щупальца (средний)
#define TENT_TIP   0xC4FF  // кончики щупалец
#define EYE_LIGHT 0xF7BF
#define EYE_DARK  0x10C4
#define GLINT     0x8FBD   // бирюзовый блик в глазу
#define C_WORKING 0x37E7   // статусные цвета = палитра симулятора (RGB565)
#define C_WAITING 0xFEA0
#define C_IDLE    0x5AEB
#define C_ERROR   0xF8AC

// Off-screen буфер: зона одного осьминога. Ужат под реальный силуэт (меньше пустого
// чёрного → меньше пикселей по SPI за кадр). ~9 КБ RAM, WiFi не используется.
#define BUF_W 68
#define BUF_H 72
#define LCX   32
#define LCY   30
GFXcanvas16 octoBuf(BUF_W, BUF_H);
#define SUBA    0x475B   // суб-агент: бирюзовая искра
#define SUBA_D  0x1BD2   // тёмный хвост искры

// Предрасчёт глянцевой сферы: считается ОДИН раз в setup() (дорогой per-pixel
// sqrt/float), в кадре только копируется — иначе 6 сфер = слайдшоу.
#define SPH_R 17
#define SPH_D (2 * SPH_R + 1)
uint16_t sphereTile[SPH_D * SPH_D];
bool     sphereMask[SPH_D * SPH_D];

enum State { WORKING, WAITING, IDLE, ERR };

// active=false → пустой слот (сессий меньше 6). id — для diff.
struct Session {
  bool  active;
  char  id[24];
  char  name[20];
  State state;
  int   sub;      // число активных суб-агентов
};

const int COLS = 3, ROWS = 2;
const int MAX_SESSIONS = COLS * ROWS;   // 6
const int W = 320, H = 240;
int cellW, cellH;

Session sessions[MAX_SESSIONS];   // то, что сейчас на экране
Session incoming[MAX_SESSIONS];   // распарсенный снэпшот

unsigned long lastTick = 0;
const unsigned long TICK_MS = 40;   // цель ~25 fps; время анимации от millis() (см. loop)

// --- Serial line reader ------------------------------------------------------
static const size_t LINE_MAX = 512;
char lineBuf[LINE_MAX];
size_t lineLen = 0;

#if ESP_DIAG
unsigned long lastStat = 0;         // когда последний раз печатали stat
unsigned long maxFrameUs = 0;       // макс. время рендера кадра за интервал (мкс)
uint16_t diagSnaps = 0;             // принято снэпшотов
uint16_t diagBadJson = 0;           // снэпшотов не распарсилось
uint16_t diagCells = 0;             // перерисовано ячеек (diff)

void diagPrintBoot() {
  Serial.println();
  Serial.print(F("{\"esp\":\"boot\",\"reason\":\""));
  Serial.print(ESP.getResetReason());   // "External System"=DTR, "Software Watchdog", "Exception"...
  Serial.print(F("\",\"heap\":"));
  Serial.print(ESP.getFreeHeap());
  Serial.println(F("}"));
}

void diagPrintStat() {
  int nActive = 0;
  for (int i = 0; i < MAX_SESSIONS; i++) if (sessions[i].active) nActive++;
  Serial.print(F("{\"esp\":\"stat\",\"n\":"));       Serial.print(nActive);
  Serial.print(F(",\"heap\":"));                     Serial.print(ESP.getFreeHeap());
  Serial.print(F(",\"frag\":"));                     Serial.print(ESP.getHeapFragmentation());
  Serial.print(F(",\"maxframe_us\":"));              Serial.print(maxFrameUs);
  Serial.print(F(",\"snaps\":"));                    Serial.print(diagSnaps);
  Serial.print(F(",\"cells\":"));                    Serial.print(diagCells);
  Serial.print(F(",\"badjson\":"));                  Serial.print(diagBadJson);
  Serial.println(F("}"));
  maxFrameUs = 0;   // окно замера обнуляем; счётчики snaps/cells/badjson — накопительные
}
#endif

uint16_t stateColor(State s) {
  switch (s) {
    case WORKING: return C_WORKING;
    case WAITING: return C_WAITING;
    case IDLE:    return C_IDLE;
    case ERR:     return C_ERROR;
  }
  return C_IDLE;
}

void drawGrid() {
  tft.fillScreen(BG);
  for (int i = 1; i < COLS; i++) tft.drawFastVLine(i * cellW, 0, H, GRIDLINE);
  for (int j = 1; j < ROWS; j++) tft.drawFastHLine(0, j * cellH, W, GRIDLINE);
}

void drawCardFrame(int col, int row, Session &s) {
  int bw = cellW - 4, bh = cellH - 4;
  int x0 = col * cellW + 2, y0 = row * cellH + 2;
  uint16_t c = stateColor(s.state);

  tft.drawRect(x0, y0, bw, bh, c);
  tft.setTextColor(0xCE79);
  tft.setTextSize(1);
  tft.setCursor(x0 + bw / 2 - (int)(strlen(s.name) * 3), y0 + bh - 10);
  tft.print(s.name);
}

// Рисуем в буфер g (а не на экран). Координаты — локальные для буфера.
// Глаза в координатах буфера. tt — время (сек) для «взгляда» в WORKING.
void drawEyes(GFXcanvas16 &g, int cx, int eyY, State state, float tt) {
  int exL = cx - 6, exR = cx + 6;
  if (state == ERR) {                       // крестики
    g.drawLine(exL - 3, eyY - 3, exL + 3, eyY + 3, C_ERROR);
    g.drawLine(exL + 3, eyY - 3, exL - 3, eyY + 3, C_ERROR);
    g.drawLine(exR - 3, eyY - 3, exR + 3, eyY + 3, C_ERROR);
    g.drawLine(exR + 3, eyY - 3, exR - 3, eyY + 3, C_ERROR);
    return;
  }
  if (state == IDLE) {                      // закрытые глаза
    g.drawFastHLine(exL - 3, eyY, 6, EYE_LIGHT);
    g.drawFastHLine(exR - 3, eyY, 6, EYE_LIGHT);
    return;
  }
  int look = (state == WORKING) ? (int)roundf(sinf(tt * 2.2f) * 1.4f) : 0;
  int r = (state == WAITING) ? 4 : 3;       // WAITING — глаза шире
  g.fillCircle(exL, eyY, r, EYE_LIGHT);
  g.fillCircle(exR, eyY, r, EYE_LIGHT);
  g.fillCircle(exL + look, eyY + 1, 1, EYE_DARK);
  g.fillCircle(exR + look, eyY + 1, 1, EYE_DARK);
  g.drawPixel(exL + look - 1, eyY - 1, GLINT);
  g.drawPixel(exR + look - 1, eyY - 1, GLINT);
}

// Линейная интерполяция двух RGB565 (для градиентов тела/щупалец).
uint16_t lerp565(uint16_t a, uint16_t b, float t) {
  if (t < 0) t = 0; if (t > 1) t = 1;
  int r = (a >> 11) & 31, g = (a >> 5) & 63, bl = a & 31;
  int r2 = (b >> 11) & 31, g2 = (b >> 5) & 63, b2 = b & 31;
  r += (int)((r2 - r) * t); g += (int)((g2 - g) * t); bl += (int)((b2 - bl) * t);
  return (uint16_t)((r << 11) | (g << 5) | bl);
}

// Один раз считаем радиальный градиент сферы в тайл (свет сверху-слева).
void buildSphere() {
  const int R = SPH_R;
  const float lx = -R * 0.34f, ly = -R * 0.42f, spread = 1.5f;
  for (int y = -R; y <= R; y++) {
    for (int x = -R; x <= R; x++) {
      int idx = (y + R) * SPH_D + (x + R);
      if (x * x + y * y > R * R) { sphereMask[idx] = false; continue; }
      float nx = (x - lx) / (float)R, ny = (y - ly) / (float)R;
      float d = sqrtf(nx * nx + ny * ny) / spread;
      sphereTile[idx] = lerp565(BODY_LIGHT, BODY_BOT, d);
      sphereMask[idx] = true;
    }
  }
}

// Тело: копируем предрасчитанный тайл (целочисленно, быстро) + блик поверх.
void sphereBody(GFXcanvas16 &g, int cx, int hy) {
  for (int yy = 0; yy < SPH_D; yy++) {
    for (int xx = 0; xx < SPH_D; xx++) {
      int idx = yy * SPH_D + xx;
      if (sphereMask[idx]) g.drawPixel(cx - SPH_R + xx, hy - SPH_R + yy, sphereTile[idx]);
    }
  }
  const int R = SPH_R;
  g.fillCircle(cx - (int)(R * 0.32f), hy - (int)(R * 0.38f), (int)(R * 0.26f), lerp565(BODY_LIGHT, 0xFFFF, 0.6f));
  g.fillCircle(cx - (int)(R * 0.30f), hy - (int)(R * 0.36f), (int)(R * 0.12f), 0xFFFF);
  g.fillCircle(cx + (int)(R * 0.34f), hy + (int)(R * 0.20f), 2, lerp565(BODY_MID, BODY_LIGHT, 0.5f));
}

// Осьминог: глянцевая сфера-тело + 6 синус-щупалец, характер под состояние.
// Алгоритм 1:1 с веб-эмулятором (GFXcanvas клипует лишнее).
void drawOctopus(GFXcanvas16 &g, int cx, int cy, State state, float tt, int sub) {
  bool flipped = (state == ERR);
  int dir = flipped ? -1 : 1;

  float speed = state == WORKING ? 7.5f : state == WAITING ? 2.2f : state == IDLE ? 1.4f : 5.5f;
  float amp   = state == WORKING ? 2.6f : state == WAITING ? 1.0f : state == IDLE ? 0.7f : 2.0f;
  int bob = (state == IDLE) ? 0 : (int)roundf(sinf(tt * (state == WORKING ? 4.4f : 2.4f)) * 1.2f);
  int hy = cy + bob + (flipped ? 5 : 0);
  const int R = SPH_R;                       // фикс. радиус — сфера предрасчитана

  // щупальца: корни ВНУТРИ тела (прикрыты сферой), тёмные у основания → светлые
  // к кончику. Тело рисуется поверх → бесшовное крепление без светлого канта.
  const int seg = 9, legs = 6;
  const int baseHW = (int)roundf(R * 0.66f);
  const int rootY = hy + dir * (R - 4);
  const float step = (2.0f * baseHW - 2) / (legs - 1);
  for (int i = 0; i < legs; i++) {
    float bx = cx - baseHW + 1 + i * step;
    float ph = tt * speed + i * 0.7f;
    for (int s = 0; s < seg; s++) {
      float sway = sinf(ph + s * 0.5f) * amp * (0.2f + (float)s / seg);
      int w = (s < 3) ? 3 : (s < 6 ? 2 : 1);
      uint16_t col = lerp565(BODY_DK, TENT_TIP, (s < 3) ? 0.0f : (float)(s - 3) / (seg - 3));
      g.fillRect((int)roundf(bx + sway) - (w >> 1), rootY + dir * (s * 2), w, 2, col);
    }
    int kx = (int)roundf(bx + sinf(ph + seg * 0.5f) * amp);
    g.drawPixel(kx, rootY + dir * (seg * 2), TENT_TIP);
  }

  // тело: глянцевая сфера (одинакова для всех состояний; сверху — лицо/акценты)
  sphereBody(g, cx, hy);

  int eyY = hy - dir * 4;
  drawEyes(g, cx, eyY, state, tt);

  // акценты по состоянию
  if (state == WORKING) {                   // 🚬 сигарета у рта + дым
    int mx = cx + 1, my = hy + 5;
    g.fillRect(mx, my, 3, 3, 0xCD0B);                 // фильтр у рта (охра)
    g.fillRect(mx + 3, my, 15, 3, EYE_LIGHT);         // толстый длинный корпус
    g.drawFastHLine(mx + 4, my, 11, 0xFFFF);          // блик
    bool hot = (((int)(tt * 3)) % 2) == 0;            // уголёк подрагивает
    g.fillRect(mx + 18, my, 3, 3, hot ? 0xFF0C : 0xFC00); // большой тлеющий уголёк
    int tipx = mx + 22, tipy = my - 1;
    for (int s = 0; s < 6; s++) {                     // густой дым волнами вверх
      float st = fmodf(tt * 1.2f + s * 0.4f, 1.0f);
      int yy = tipy - 1 - (int)(st * 26);
      int xx = tipx + (int)roundf(sinf(tt * 2.1f + s * 1.1f + st * 3.2f) * 5);
      g.fillCircle(xx, yy, st < 0.4f ? 1 : (st < 0.75f ? 2 : 3), 0x9CD3); // серый дым
    }
  } else if (state == WAITING) {            // «?» — ждёт тебя
    int yy = hy - R - 8;
    g.fillRect(cx + 8, yy, 4, 1, C_WAITING);
    g.drawPixel(cx + 11, yy + 1, C_WAITING);
    g.drawPixel(cx + 10, yy + 2, C_WAITING);
    g.drawPixel(cx + 10, yy + 4, C_WAITING);
  } else if (state == IDLE) {               // «z z» — спит
    int zt = ((int)(tt * 1.2f)) % 3;
    for (int z = 0; z <= zt; z++) {
      int zx = cx + 7 + z * 4, zy = hy - R - 2 - z * 5;
      g.fillRect(zx, zy, 3, 1, C_IDLE);
      g.fillRect(zx, zy + 2, 3, 1, C_IDLE);
      g.drawLine(zx + 2, zy, zx, zy + 2, C_IDLE);
    }
  }

  // суб-агенты: пузырьки-искры на медленной орбите вокруг головы (до 5)
  int nsub = sub > 5 ? 5 : sub;
  for (int k = 0; k < nsub; k++) {
    float a = tt * 1.0f + k * (6.2832f / nsub);
    int ox = cx + (int)roundf(cosf(a) * (R + 9));
    int oy = hy - 2 + (int)roundf(sinf(a) * (R * 0.62f));
    int tx = cx + (int)roundf(cosf(a - 0.4f) * (R + 9));   // хвост позади
    int ty = hy - 2 + (int)roundf(sinf(a - 0.4f) * (R * 0.62f));
    g.drawPixel(tx, ty, SUBA_D);
    g.fillCircle(ox, oy, 2, SUBA);
    g.drawPixel(ox - 1, oy - 1, 0xFFFF);
  }
}

void updateOctopusArea(int col, int row, Session &s, float tt) {
  int bw = cellW - 4, bh = cellH - 4;
  int x0 = col * cellW + 2, y0 = row * cellH + 2;
  uint16_t c = stateColor(s.state);
  int cx = x0 + bw / 2, cy = y0 + bh / 2 - 6;

  // 1. собираем кадр в буфере (в RAM)
  octoBuf.fillScreen(BG);
  drawOctopus(octoBuf, LCX, LCY, s.state, tt, s.sub);

  // 2. выкидываем буфер ОДНИМ блоком: одно окно + потоковый writePixels.
  //    (drawRGBBitmap у Adafruit_ILI9341 идёт пиксель-за-пикселем → ~40мс/осьминог!)
  tft.startWrite();
  tft.setAddrWindow(cx - LCX, cy - LCY, BUF_W, BUF_H);
  tft.writePixels(octoBuf.getBuffer(), (uint32_t)BUF_W * BUF_H);
  tft.endWrite();

  // 3. точка-статус (вне буфера, крошечная) — рисуем напрямую
  bool blink = (s.state != WAITING) || (((int)(tt * 3)) & 1);
  tft.fillRect(x0 + 3, y0 + 3, 3, 3, blink ? c : BG);
}

// Полная перерисовка сетки и рамок под текущий sessions[] (пустые слоты — просто фон).
void redrawAll() {
  drawGrid();
  for (int i = 0; i < MAX_SESSIONS; i++) {
    if (sessions[i].active) {
      drawCardFrame(i % COLS, i / COLS, sessions[i]);
    }
  }
}

// Очистить одну ячейку под фон, восстановив пару гридлайнов на её левой/верхней
// границе (их затирает fillRect). Правый/нижний край принадлежат соседям — их
// fillRect не трогает, восстанавливать не нужно.
void clearCell(int col, int row) {
  int x = col * cellW, y = row * cellH;
  tft.fillRect(x, y, cellW, cellH, BG);
  if (col > 0) tft.drawFastVLine(x, y, cellH, GRIDLINE);
  if (row > 0) tft.drawFastHLine(x, y, cellW, GRIDLINE);
}

// Перерисовать ОДНУ ячейку (без касания соседей и без fillScreen): фон + гридлайны,
// затем рамка и осьминог в новом состоянии сразу (не ждём следующего тика анимации).
void redrawCell(int i) {
  int col = i % COLS, row = i / COLS;
  clearCell(col, row);
  if (sessions[i].active) {
    drawCardFrame(col, row, sessions[i]);
    updateOctopusArea(col, row, sessions[i], millis() / 1000.0f + i * 0.4f);
  }
}

// --- Приём снэпшота по serial ------------------------------------------------
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
      lineLen = 0;  // переполнение — сбрасываем строку
    }
  }
}

void handleLine(const char* line) {
  StaticJsonDocument<1024> doc;
  if (deserializeJson(doc, line)) {         // не JSON — игнор (переполнение UART/буфера?)
#if ESP_DIAG
    diagBadJson++;
#endif
    return;
  }
#if ESP_DIAG
  diagSnaps++;
#endif

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
    n++;
  }
  for (int i = n; i < MAX_SESSIONS; i++) incoming[i].active = false;

  applySnapshot();
}

// Diff по-ячеечно: перерисовываем ТОЛЬКО те клетки, где сменился состав / имя /
// статус (цвет рамки завязан на статус) — без fillScreen и без касания соседей.
// Неизменившиеся карточки loop() продолжает анимировать как ни в чём не бывало.
void applySnapshot() {
  for (int i = 0; i < MAX_SESSIONS; i++) {
    Session &a = sessions[i], &b = incoming[i];
    bool cellChanged = a.active != b.active ||
        (b.active && (a.state != b.state ||
                      strcmp(a.id, b.id) != 0 ||
                      strcmp(a.name, b.name) != 0));
    sessions[i] = b;
    if (cellChanged) {
      redrawCell(i);
#if ESP_DIAG
      diagCells++;
#endif
    }
  }
}

void setup() {
  Serial.begin(115200);
#if ESP_DIAG
  diagPrintBoot();   // причина сброса + heap — первым делом после старта serial
#endif
  cellW = W / COLS;
  cellH = H / ROWS;

  tft.begin(40000000);   // 40 МГц SPI — быстрый блочный вывод буфера
  tft.setRotation(3);

  buildSphere();  // предрасчёт тела один раз
  for (int i = 0; i < MAX_SESSIONS; i++) sessions[i].active = false;
  redrawAll();   // пустая сетка до первого снэпшота
}

void loop() {
  readSerial();

  unsigned long now = millis();
  if (now - lastTick > TICK_MS) {
    lastTick = now;
    // Время анимации — от millis() (wall-clock), НЕ от числа тиков: под нагрузкой
    // кадры пропускаются, но скорость остаётся правильной (без слоу-мо).
    float tt = now / 1000.0f;
#if ESP_DIAG
    unsigned long frameT0 = micros();
#endif
    for (int i = 0; i < MAX_SESSIONS; i++) {
      if (sessions[i].active) {
        updateOctopusArea(i % COLS, i / COLS, sessions[i], tt + i * 0.4f);
      }
    }
#if ESP_DIAG
    unsigned long frameUs = micros() - frameT0;   // сколько заняла отрисовка всех активных
    if (frameUs > maxFrameUs) maxFrameUs = frameUs;
#endif
  }

#if ESP_DIAG
  if (now - lastStat > 2000) {   // раз в 2с — телеметрия в serial (мост залогирует)
    lastStat = now;
    diagPrintStat();
  }
#endif
}
