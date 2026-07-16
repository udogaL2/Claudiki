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
#include <Adafruit_GFX.h>
#include <Adafruit_ILI9341.h>
#include <ArduinoJson.h>

#define TFT_CS   D8
#define TFT_DC   D4
#define TFT_RST  D3

Adafruit_ILI9341 tft = Adafruit_ILI9341(TFT_CS, TFT_DC, TFT_RST);

#define BG        0x0000
#define GRIDLINE  0x0861
#define BODY      0x9CD3
#define BODY_DARK 0x4A15
#define EYE_LIGHT 0xE73C
#define EYE_DARK  0x18E3
#define C_WORKING 0x37E7
#define C_WAITING 0xFEA0
#define C_IDLE    0x5AEB
#define C_ERROR   0xF8AC

// Off-screen буфер: зона одного осьминога. Локальный центр = (30, 44).
#define BUF_W 60
#define BUF_H 72
#define LCX   30
#define LCY   44
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
void drawEyes(GFXcanvas16 &g, int cx, int oy, State state) {
  if (state == ERR) {
    g.drawLine(cx - 6, oy - 4, cx - 2, oy, C_ERROR);
    g.drawLine(cx - 2, oy - 4, cx - 6, oy, C_ERROR);
    g.drawLine(cx + 2, oy - 4, cx + 6, oy, C_ERROR);
    g.drawLine(cx + 6, oy - 4, cx + 2, oy, C_ERROR);
  } else if (state == IDLE) {
    g.drawFastHLine(cx - 7, oy - 2, 4, EYE_LIGHT);
    g.drawFastHLine(cx + 3, oy - 2, 4, EYE_LIGHT);
  } else {
    g.fillRect(cx - 7, oy - 5, 4, 4, EYE_LIGHT);
    g.fillRect(cx + 3, oy - 5, 4, 4, EYE_LIGHT);
    g.fillRect(cx - 6, oy - 4, 2, 2, EYE_DARK);
    g.fillRect(cx + 4, oy - 4, 2, 2, EYE_DARK);
  }
}

void drawOctopus(GFXcanvas16 &g, int cx, int cy, State state, int phase) {
  if (state == ERR) {
    int oy = cy + 6;
    for (int i = 0; i < 4; i++) {
      int tx = cx - 9 + i * 6;
      g.fillRect(tx - 1, oy - 12, 3, 8, BODY_DARK);
    }
    g.fillRect(cx - 12, oy - 4, 24, 16, BODY);
    g.fillRect(cx - 9, oy + 9, 18, 4, BODY);
    drawEyes(g, cx, oy, state);
    return;
  }

  int bob = (state == IDLE) ? 0 : (sin(phase * 0.6) >= 0 ? -1 : 1);
  int oy = cy + bob;

  for (int i = 0; i < 4; i++) {
    int tx = cx - 9 + i * 6;
    int sway = 0;
    if (state == WORKING) sway = (sin(phase * 0.7 + i) >= 0) ? 2 : -2;
    else if (state != IDLE) sway = (sin(phase * 0.7 + i) >= 0) ? 1 : -1;
    g.fillRect(tx - 1 + sway, oy + 4, 3, 8, BODY_DARK);
  }

  g.fillRect(cx - 12, oy - 10, 24, 16, BODY);
  g.fillRect(cx - 9, oy - 13, 18, 4, BODY);

  drawEyes(g, cx, oy, state);

  if (state == WAITING) {
    for (int k = 0; k < 3; k++) {
      int p = (phase + k * 4) % 12;
      int yy = cy - 14 - p * 2;
      int size = 2 + p / 4;
      g.drawRect(cx + 10 - size / 2, yy, size, size, 0xAD75);
    }
  }
  if (state == WORKING) {
    int p = phase % 3;
    for (int k = 0; k < 3; k++) {
      if (k == p) g.fillRect(cx + 12, cy - 10 + k * 4, 2, 2, C_WORKING);
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
