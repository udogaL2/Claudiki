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

#define TFT_CS   D8
#define TFT_DC   D4
#define TFT_RST  D3

Adafruit_ILI9341 tft = Adafruit_ILI9341(TFT_CS, TFT_DC, TFT_RST);

#define BG        0x0000
#define GRIDLINE  0x0861
#define BODY      0xBC7F   // фиолетовое тельце
#define BODY_DARK 0x7A9A   // тёмно-фиолетовый (щупальца/тень)
#define BELLY     0xDE1F   // светлый блик на тельце
#define EYE_LIGHT 0xF7BF
#define EYE_DARK  0x10C4
#define GLINT     0x8FBD   // бирюзовый блик в глазу
#define C_WORKING 0x37E7   // статусные цвета = палитра симулятора (RGB565)
#define C_WAITING 0xFEA0
#define C_IDLE    0x5AEB
#define C_ERROR   0xF8AC

// Off-screen буфер: зона одного осьминога (~15 КБ RAM, WiFi не используется).
#define BUF_W 80
#define BUF_H 96
#define LCX   40
#define LCY   48
GFXcanvas16 octoBuf(BUF_W, BUF_H);

enum State { WORKING, WAITING, IDLE, ERR };

// active=false → пустой слот (сессий меньше 6). id — для diff.
struct Session {
  bool  active;
  char  id[24];
  char  name[20];
  State state;
};

const int COLS = 3, ROWS = 2;
const int MAX_SESSIONS = COLS * ROWS;   // 6
const int W = 320, H = 240;
int cellW, cellH;

Session sessions[MAX_SESSIONS];   // то, что сейчас на экране
Session incoming[MAX_SESSIONS];   // распарсенный снэпшот

unsigned long lastTick = 0;
const unsigned long TICK_MS = 140;
int t = 0;

// --- Serial line reader ------------------------------------------------------
static const size_t LINE_MAX = 512;
char lineBuf[LINE_MAX];
size_t lineLen = 0;

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

// Осьминог: купол-голова + прямоугольное туловище, 6 синус-щупалец, характер
// под состояние. Алгоритм 1:1 с веб-симулятором (GFXcanvas клипует лишнее).
void drawOctopus(GFXcanvas16 &g, int cx, int cy, State state, int phase) {
  float tt = phase * 0.14f;                 // ~секунды (TICK_MS=140)
  bool flipped = (state == ERR);
  int dir = flipped ? -1 : 1;

  float speed = state == WORKING ? 7.5f : state == WAITING ? 2.2f : state == IDLE ? 1.4f : 5.5f;
  float amp   = state == WORKING ? 2.6f : state == WAITING ? 1.0f : state == IDLE ? 0.7f : 2.0f;
  float breath = sinf(tt * (state == IDLE ? 1.6f : 3.0f));
  int bob = (state == IDLE) ? 0 : (int)roundf(sinf(tt * (state == WORKING ? 4.4f : 2.4f)) * 1.2f);
  int hy = cy + bob + (flipped ? 5 : 0);
  int R = 17 + (int)roundf(breath * (state == IDLE ? 0.6f : 1.0f));

  // прямоугольное туловище ниже купола; щупальца растут от его низа
  const int torsoDrop = 9;
  const int seg = 7;
  int baseY = hy + dir * (R + torsoDrop);

  // щупальца (за телом): тейперятся и колышутся синусом с фазовым сдвигом
  const int legs = 6;
  float step = (2 * R - 8) / (float)(legs - 1);
  for (int i = 0; i < legs; i++) {
    float bx = cx - R + 4 + i * step;
    float ph = tt * speed + i * 0.75f;
    for (int s = 0; s < seg; s++) {
      float sway = sinf(ph + s * 0.55f) * amp * (0.25f + (float)s / seg);
      int yy = baseY + dir * (s * 2);
      int w = (s < 2) ? 3 : (s < 4 ? 2 : 1);
      g.fillRect((int)(bx + sway) - (w >> 1), yy, w, 2, BODY_DARK);
    }
    int kx = (int)(bx + sinf(ph + seg * 0.55f) * amp);
    g.drawPixel(kx, baseY + dir * (seg * 2), BODY);
  }

  // тело: купол-голова + прямоугольное туловище + блик
  int halfW = R - 3;
  int ty = (baseY < hy) ? baseY : hy, th = abs(baseY - hy);
  g.fillRect(cx - halfW, ty, halfW * 2, th, BODY);       // прямые бока туловища
  g.fillCircle(cx, hy, R, BODY);                          // купол-голова
  if (dir > 0) g.fillRect(cx - halfW + 1, baseY - 3, halfW * 2 - 2, 3, BODY_DARK); // тень
  g.fillCircle(cx - 5, hy - 6, 4, BELLY);                 // блик

  int eyY = hy - (flipped ? -5 : 5);
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
}

void updateOctopusArea(int col, int row, Session &s, int phase) {
  int bw = cellW - 4, bh = cellH - 4;
  int x0 = col * cellW + 2, y0 = row * cellH + 2;
  uint16_t c = stateColor(s.state);
  int cx = x0 + bw / 2, cy = y0 + bh / 2 - 6;

  // 1. собираем кадр в буфере (в RAM)
  octoBuf.fillScreen(BG);
  drawOctopus(octoBuf, LCX, LCY, s.state, phase);

  // 2. выкидываем весь буфер на экран одной операцией — без чёрной вспышки
  tft.drawRGBBitmap(cx - LCX, cy - LCY, octoBuf.getBuffer(), BUF_W, BUF_H);

  // 3. точка-статус (вне буфера, крошечная) — рисуем напрямую
  bool blink = (s.state != WAITING) || ((phase % 6) < 3);
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
  if (deserializeJson(doc, line)) return;   // не JSON — игнор

  JsonArray arr = doc["sessions"].as<JsonArray>();
  int n = 0;
  for (JsonObject s : arr) {
    if (n >= MAX_SESSIONS) break;
    Session &c = incoming[n];
    c.active = true;
    strlcpy(c.id, s["id"] | "", sizeof(c.id));
    strlcpy(c.name, s["name"] | "", sizeof(c.name));
    c.state = (State)(int)(s["state"] | (int)IDLE);
    n++;
  }
  for (int i = n; i < MAX_SESSIONS; i++) incoming[i].active = false;

  applySnapshot();
}

// Diff: состав / имя / статус изменились → полная перерисовка (цвет рамки завязан
// на статус). Иначе — ничего, loop() продолжает анимировать существующие карточки.
void applySnapshot() {
  bool changed = false;
  for (int i = 0; i < MAX_SESSIONS; i++) {
    Session &a = sessions[i], &b = incoming[i];
    if (a.active != b.active ||
        (b.active && (a.state != b.state ||
                      strcmp(a.id, b.id) != 0 ||
                      strcmp(a.name, b.name) != 0))) {
      changed = true;
    }
    sessions[i] = b;
  }
  if (changed) redrawAll();
}

void setup() {
  Serial.begin(115200);
  cellW = W / COLS;
  cellH = H / ROWS;

  tft.begin();
  tft.setRotation(3);

  for (int i = 0; i < MAX_SESSIONS; i++) sessions[i].active = false;
  redrawAll();   // пустая сетка до первого снэпшота
}

void loop() {
  readSerial();

  unsigned long now = millis();
  if (now - lastTick > TICK_MS) {
    lastTick = now;
    t++;
    for (int i = 0; i < MAX_SESSIONS; i++) {
      if (sessions[i].active) {
        updateOctopusArea(i % COLS, i / COLS, sessions[i], t + i * 2);
      }
    }
  }
}
