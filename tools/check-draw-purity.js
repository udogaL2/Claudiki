// Отрисовка обязана быть ЧИСТОЙ: composeCurrentScreen вызывается на каждую полосу
// экрана (их до пятнадцати), поэтому любое изменение состояния анимации внутри
// рисующей функции повторяется столько же раз за кадр. Наступали дважды: пузыри
// рулетки и рычаг автомата — рычаг успевал сдвинуться восемь раз за кадр, и каждая
// полоса рисовала его в своём положении, то есть экран показывал куски разных кадров.
//
// Правило: переменная состояния анимации, помеченная в объявлении `// @phase`,
// не должна получать присваивание внутри функции, рисующей в канву (параметр `&g`).
// Двигать её положено в физике — там, где кадр обрабатывается ровно один раз.
//
//   node tools/check-draw-purity.js firmware/octodash/octodash.ino
const fs = require("fs");
const CR = String.fromCharCode(13), LF = String.fromCharCode(10);
const src = fs.readFileSync(process.argv[2], "utf8").split(CR + LF).join(LF);

// комментарии нужны, чтобы найти пометку @phase, поэтому режем только строки
const code = src.replace(/"(?:\\.|[^"\\])*"/g, '""');

const phase = new Set();
for (const m of code.matchAll(/^\s*(?:static\s+)?(?:float|int|uint\d+_t|bool|double)\s+(\w+)[^;\n]*;\s*\/\/[^\n]*@phase/gm))
  phase.add(m[1]);

const problems = [];
if (!phase.size) {
  problems.push("ни одна переменная не помечена `// @phase` — проверка ничего не сторожит");
}

// тела функций, которые рисуют: в сигнатуре есть ссылка на канву
for (const fn of code.matchAll(/^[A-Za-z_][\w:<>*&\s]*?\**(\w+)\s*\(([^;{]*)\)\s*\{/gm)) {
  if (!/(OffsetCanvas|Adafruit_GFX|PixelSink)\s*&\s*\w+/.test(fn[2])) continue;
  const start = fn.index + fn[0].length;
  const end = code.indexOf("\n}\n", start);
  const body = code.slice(start, end < 0 ? code.length : end);
  for (const name of phase) {
    const write = new RegExp(`(^|[^\\w.])${name}\\s*(\\+=|-=|\\*=|/=|=[^=])`, "m");
    if (write.test(body))
      problems.push(`${fn[1]}(): меняет ${name} — а рисование зовётся на каждую полосу; ` +
                    `двигать состояние надо в физике`);
  }
}

console.log(problems.length
  ? "НАЙДЕНО:\n  " + problems.join("\n  ")
  : `отрисовка чистая (сторожим: ${[...phase].join(", ")})`);
process.exit(problems.length ? 1 : 0);
