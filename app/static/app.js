"use strict";
// Dialer 2.0 — frontend (a single file, no frameworks).
// Tabs; the campaign polls /status every 1.5 s while dialing.
//
// i18n: Ukrainian is the canonical language of the markup and of the string
// literals in this file; the EN map below translates the UI chrome when the
// user switches to English (button in the tab bar, persisted in localStorage).
// Spoken TTS content — announce mirrors, default connect/optout texts, digit
// words — is ALWAYS Ukrainian: that is what the callee hears, regardless of
// the UI language. Server responses (error messages, the campaign log) arrive
// in Ukrainian and are mapped to English client-side (trServer/trLog) so the
// backend stays untouched; an unknown message falls back to the original.

const $ = id => document.getElementById(id);
let LANG = localStorage.getItem("lang") === "en" ? "en" : "uk";

// ---------- i18n ----------
// Flat map: canonical Ukrainian string -> English. Keys for [data-i18n]
// elements are their initial textContent with whitespace collapsed.
const EN = {
  "Дзвонилка 2.0": "Dialer 2.0",
  "Кампанія": "Campaign",
  "Сценарії": "Scenarios",
  "Налаштування": "Settings",
  "Історія": "History",
  // campaign tab
  "Сценарій": "Scenario",
  "Збережений сценарій": "Saved scenario",
  "Прослухати повідомлення": "Listen to the message",
  "✎ редагувати": "✎ edit",
  "Набір": "Dialing",
  "Назва кампанії": "Campaign name",
  "(порожньо = назва сценарію)": "(empty = scenario name)",
  "SIP-профіль (транк)": "SIP profile (trunk)",
  "Одночасних дзвінків": "Concurrent calls",
  "Номери (по одному на рядок)": "Numbers (one per line)",
  "Запустити кампанію": "Start campaign",
  "Прогрес": "Progress",
  // scenarios tab
  "Збережені сценарії": "Saved scenarios",
  "Назва": "Name",
  "Тип": "Type",
  "Голос": "Voice",
  "Меню": "Menu",
  "Поки порожньо — заповніть редактор нижче і натисніть «Зберегти».":
    "Nothing here yet — fill in the editor below and press “Save”.",
  "Новий сценарій": "New scenario",
  "Назва сценарію": "Scenario name",
  "Акція червня": "June promo",
  "Тип кампанії": "Campaign type",
  "Інформаційна (автовідповідач → лишити повідомлення)":
    "Informational (answering machine → leave the message)",
  "З оператором (автовідповідач → покласти слухавку)":
    "With an operator (answering machine → hang up)",
  "Текст повідомлення": "Message text",
  "Мова": "Language",
  "інша (без мови)": "other (no language)",
  "Швидкість:": "Speed:",
  "Пауза між реченнями, с:": "Pause between sentences, s:",
  "Якість синтезу:": "Synthesis quality:",
  "▶ Прослухати": "▶ Listen",
  "IVR-меню": "IVR menu",
  "Що пропонувати абоненту після повідомлення. Без жодної опції — просто програється повідомлення і дзвінок завершується. Опція «підменю» відкриває ще один рівень (до 4 загалом) зі своїм текстом і своїми опціями; будь-який текст можна прослухати кнопкою ▶.":
    "What to offer the callee after the message. With no options the message just plays and the call ends. A “submenu” option opens one more level (up to 4 total) with its own text and options; any text can be auditioned with ▶.",
  "Таймаут очікування цифри, с": "Digit wait timeout, s",
  "Повторів анонсу": "Announcement repeats",
  "Якщо нічого не натиснуто": "If nothing is pressed",
  "завершити дзвінок": "end the call",
  "Зберегти": "Save",
  "Зберегти як новий": "Save as new",
  "Очистити форму": "Clear form",
  // settings tab
  "SIP-профілі (транки)": "SIP profiles (trunks)",
  "Сервер": "Server",
  "Логін": "Login",
  "Пароль": "Password",
  "За замовч.": "Default",
  "Додати / змінити профіль": "Add / edit a profile",
  "Порт": "Port",
  "(порожньо = не міняти)": "(empty = keep current)",
  "Оператори": "Operators",
  "Софтфон (MicroSIP/Zoiper) реєструється на цей сервер за extension+паролем. Колонка «онлайн» — жива реєстрація.":
    "A softphone (MicroSIP/Zoiper) registers to this server with the extension and password. The “online” column is the live registration state.",
  "Імʼя": "Name",
  "Онлайн": "Online",
  "Додати оператора": "Add an operator",
  "SIP-пароль": "SIP password",
  "Додати": "Add",
  // history tab
  "↻ Оновити": "↻ Refresh",
  "Статус": "Status",
  "Лічильники": "Counters",
  // statuses (pill/counter labels)
  "очікує": "pending",
  "дзвонить": "ringing",
  "прослухав": "answered",
  "оператор": "operator",
  "автовідп.": "voicemail",
  "автовідп.×": "machine ✗",
  "не відповів": "no answer",
  "зайнято": "busy",
  "помилка": "failed",
  "відписка": "opted out",
  "опер.зайнятий": "op. busy",
  // IVR editor (JS-generated)
  "повторити": "replay",
  "підменю": "submenu",
  "фраза": "phrase",
  "назад": "back",
  "головне меню": "main menu",
  "відписатися": "opt out",
  "завершити": "hang up",
  "потім — знову це меню": "then — this menu again",
  "потім — на рівень вище": "then — one level up",
  "потім — завершити дзвінок": "then — end the call",
  "інфо": "info",
  "Текст рівня": "Level text",
  "Анонс опцій (порожньо = автотекст; грає на кожному раунді очікування)":
    "Options announcement (empty = auto-text; plays on every waiting round)",
  "+ опція": "+ option",
  "Необов'язково: грає один раз при вході на цей рівень":
    "Optional: plays once when entering this level",
  "Підпис для анонсу (напр.: Графік роботи)":
    "Label for the announcement (e.g.: Working hours)",
  "Текст фрази": "Phrase text",
  "Клавіша": "Key",
  "Дія": "Action",
  "без меню: програти повідомлення і завершити":
    "no menu: play the message and finish",
  "Немає сценаріїв — створіть перший у вкладці «Сценарії».":
    "No scenarios — create the first one in the “Scenarios” tab.",
  "тип:": "type:",
  "меню:": "menu:",
  "Редагувати": "Edit",
  "Клонувати — копія для схожого сценарію": "Clone — a copy for a similar scenario",
  "Видалити": "Delete",
  "Видалити сценарій?": "Delete the scenario?",
  "Видалити профіль?": "Delete the profile?",
  "Видалити оператора?": "Delete the operator?",
  // banners / misc
  "Помилка": "Error",
  "Сценарій збережено": "Scenario saved",
  "Оберіть сценарій": "Select a scenario",
  "Порожній текст": "Empty text",
  "синтез…": "synthesizing…",
  "Немає з'єднання з сервером": "No connection to the server",
  "⚠ FreeSWITCH офлайн": "⚠ FreeSWITCH offline",
  "всього": "total",
  "Зараз:": "Now:",
  "Профіль збережено": "Profile saved",
  "у розмові": "on a call",
  "онлайн": "online",
  "офлайн": "offline",
  "Оператора додано": "Operator added",
  " (reloadxml пізніше)": " (reloadxml later)",
  "↻ невдалі": "↻ failed",
  "▶ продовжити": "▶ resume",
  "деталі": "details",
  "сценарій:": "scenario:",
  "Номер": "Number",
  "Причина": "Cause",
  "Спроб": "Attempts",
  "Кампанію відновлено": "Campaign resumed",
};
const t = s => (LANG === "en" ? (EN[s] ?? s) : s);
// parametrized strings: pick a language variant directly
const tr = (uk, en) => (LANG === "en" ? en : uk);

// Server messages (main.py / flow.py) arrive in Ukrainian; in EN mode they are
// mapped by pattern. Unknown messages pass through untranslated.
const SERVER_RE = [
  [/^Порожній текст повідомлення$/, () => "Empty message text"],
  [/^Порожній текст$/, () => "Empty text"],
  [/^Невідомий голос (.+)$/, m => `Unknown voice ${m[1]}`],
  [/^Невідома мова (.+)$/, m => `Unknown language ${m[1]}`],
  [/^Помилка синтезу$/, () => "Synthesis failed"],
  [/^Невідомий тип кампанії (.+)$/, m => `Unknown campaign type ${m[1]}`],
  [/^Некоректні параметри голосу$/, () => "Invalid voice parameters"],
  [/^Некоректна IVR-форма$/, () => "Invalid IVR form"],
  [/^Вкажіть назву сценарію$/, () => "Enter a scenario name"],
  [/^Сценарій «(.+)» уже існує$/, m => `Scenario “${m[1]}” already exists`],
  [/^Сценарій не знайдено$/, () => "Scenario not found"],
  [/^Некоректні номери: (.+)$/, m => `Invalid numbers: ${m[1]}`],
  [/^Вкажіть хоча б один номер$/, () => "Enter at least one number"],
  [/^max_concurrent має бути числом$/, () => "max_concurrent must be a number"],
  [/^max_concurrent поза межами 1\.\.5$/, () => "max_concurrent out of range 1..5"],
  [/^SIP-профіль не знайдено$/, () => "SIP profile not found"],
  [/^Вкажіть назву профілю$/, () => "Enter a profile name"],
  [/^Вкажіть сервер і логін$/, () => "Enter the server and login"],
  [/^Некоректний порт (.+)$/, m => `Invalid port ${m[1]}`],
  [/^Профіль «(.+)» уже існує$/, m => `Profile “${m[1]}” already exists`],
  [/^Профіль не знайдено$/, () => "Profile not found"],
  [/^Вкажіть ім'я оператора$/, () => "Enter the operator's name"],
  [/^Некоректний extension «(.+)» \(3–6 цифр\)$/, m => `Invalid extension “${m[1]}” (3–6 digits)`],
  [/^Пароль закороткий \(мінімум 6 символів\)$/, () => "Password too short (minimum 6 characters)"],
  [/^Extension (.+) уже існує$/, m => `Extension ${m[1]} already exists`],
  [/^Оператора не знайдено$/, () => "Operator not found"],
  [/^Немає невдалих номерів для повтору$/, () => "No failed numbers to retry"],
  [/^SIP-профіль цієї кампанії вже видалено$/, () => "This campaign's SIP profile has been deleted"],
  [/^FreeSWITCH недоступний \(ESL\)$/, () => "FreeSWITCH unavailable (ESL)"],
  [/^Дзвінок уже виконується$/, () => "A call is already in progress"],
  [/^Некоректний номер (.+)$/, m => `Invalid number ${m[1]}`],
  [/^Забагато фраз для синтезу: (.+)$/, m => `Too many phrases to synthesize: ${m[1]}`],
  [/^Невідома дія за таймаутом: (.+)$/, m => `Unknown timeout action: ${m[1]}`],
];
// flow.py validation errors come prefixed with the menu location
const WHERE_RE = /^(Головне меню|Підменю ([0-9→]+)): (.*)$/;
const FLOW_RE = [
  [/^меню глибше за (\d+) рівні\(в\)$/, m => `menu deeper than ${m[1]} levels`],
  [/^жодної опції$/, () => "no options"],
  [/^таймаут (\d+) поза межами (\d+)\.\.(\d+)$/, m => `timeout ${m[1]} out of range ${m[2]}..${m[3]}`],
  [/^повторів (\d+) поза межами (\d+)\.\.(\d+)$/, m => `repeats ${m[1]} out of range ${m[2]}..${m[3]}`],
  [/^некоректна клавіша «(.*)»$/, m => `invalid key “${m[1]}”`],
  [/^клавіша (\d) використана двічі$/, m => `key ${m[1]} used twice`],
  [/^опція (\d) потребує підпису для автоанонсу \(або заповніть анонс рівня\)$/,
   m => `option ${m[1]} needs a label for the auto-announcement (or fill in the level announcement)`],
  [/^«назад» неможливий на верхньому рівні$/, () => "“back” is impossible at the top level"],
  [/^«головне меню» неможливе на верхньому рівні$/, () => "“main menu” is impossible at the top level"],
  [/^опція (\d) \(фраза\) без тексту$/, m => `option ${m[1]} (phrase) has no text`],
  [/^опція (\d) — «потім назад» неможливе на верхньому рівні$/,
   m => `option ${m[1]} — “then back” is impossible at the top level`],
  [/^опція (\d) — невідоме «потім» «(.*)»$/, m => `option ${m[1]} — unknown “then” “${m[2]}”`],
  [/^невідома дія «(.*)»$/, m => `unknown action “${m[1]}”`],
  [/^таймаут має бути числом$/, () => "the timeout must be a number"],
  [/^кількість повторів має бути числом$/, () => "the repeat count must be a number"],
];
function trPatterns(msg, pats) {
  for (const [re, fn] of pats) { const m = re.exec(msg); if (m) return fn(m); }
  return null;
}
function trServer(msg) {
  if (LANG !== "en" || !msg) return msg;
  const direct = trPatterns(msg, SERVER_RE);
  if (direct) return direct;
  const w = WHERE_RE.exec(msg);
  if (w) {
    const where = w[1] === "Головне меню" ? "Main menu" : `Submenu ${w[2]}`;
    return `${where}: ${trPatterns(w[3], FLOW_RE) ?? w[3]}`;
  }
  return msg;
}
// campaign log lines ("HH:MM:SS <message>" — the templates live in jobs.py)
const LOG_RE = [
  [/^кампанія #(\d+) «(.*)»: синтез промптів$/, m => `campaign #${m[1]} “${m[2]}”: synthesizing prompts`],
  [/^синтез не вдався — кампанію зупинено$/, () => "synthesis failed — campaign stopped"],
  [/^FreeSWITCH недоступний — кампанію зупинено$/, () => "FreeSWITCH unavailable — campaign stopped"],
  [/^SIP-профіль не знайдено — кампанію зупинено$/, () => "SIP profile not found — campaign stopped"],
  [/^не вдалося підняти SIP-транк — кампанію зупинено$/, () => "failed to bring up the SIP trunk — campaign stopped"],
  [/^SIP-транк (.+)$/, m => `SIP trunk ${m[1]}`],
  [/^набір почато \(одночасно: (\d+)\)$/, m => `dialing started (concurrency: ${m[1]})`],
  [/^очікую вільного зареєстрованого оператора$/, () => "waiting for a free registered operator"],
  [/^кампанія завершена: (\w+)$/, m => `campaign finished: ${m[1]}`],
  [/^(\S+) failed \(застряг після відповіді\)$/, m => `${m[1]} failed (stuck after answer)`],
];
function trLog(line) {
  if (LANG !== "en") return line;
  const m = /^(\d\d:\d\d:\d\d )(.*)$/.exec(line);
  if (!m) return line;
  return m[1] + (trPatterns(m[2], LOG_RE) ?? m[2]);
}

const STATUSES = ["pending","ringing","answered","transferred","voicemail-left",
  "machine-hangup","no-answer","busy","failed","optout","missed-operator"];
const STATUS_UK = {pending:"очікує",ringing:"дзвонить",answered:"прослухав",
  transferred:"оператор",["voicemail-left"]:"автовідп.",["machine-hangup"]:"автовідп.×",
  ["no-answer"]:"не відповів",busy:"зайнято",failed:"помилка",optout:"відписка",
  ["missed-operator"]:"опер.зайнятий"};
const statusLabel = s => t(STATUS_UK[s] || s);

async function api(method, url, body, isForm) {
  const opts = { method };
  if (body && isForm) { opts.body = body; }
  else if (body) { opts.headers = {"Content-Type":"application/json"}; opts.body = JSON.stringify(body); }
  let r;
  try { r = await fetch(url, opts); }
  catch { return { ok: false, status: 0, data: { error: t("Немає з'єднання з сервером") } }; }
  let data = {};
  try { data = await r.json(); } catch {}
  if (!r.ok && !data.error && !data.detail) data.error = "HTTP " + r.status;
  if (typeof data.error === "string") data.error = trServer(data.error);
  if (typeof data.detail === "string") data.detail = trServer(data.detail);
  return { ok: r.ok, status: r.status, data };
}
function banner(el, msg, kind) {
  el.textContent = msg; el.className = "banner show " + (kind||"info");
  if (kind === "ok") setTimeout(() => el.classList.remove("show"), 4000);
}
function pill(s) { return `<span class="pill ${s}">${statusLabel(s)}</span>`; }

// ---------- tabs ----------
function openTab(name) {
  document.querySelectorAll("nav.tabs button").forEach(b =>
    b.classList.toggle("active", b.dataset.tab === name));
  document.querySelectorAll(".tab").forEach(el => el.classList.remove("active"));
  $("tab-" + name).classList.add("active");
  if (name === "campaign") loadScenarios();   // pick up edits from the editor
  if (name === "scenarios") loadScenarios();
  if (name === "settings") loadConfig();
  if (name === "history") loadHistory();
}
document.querySelectorAll("nav.tabs button").forEach(btn => {
  if (btn.dataset.tab) btn.onclick = () => openTab(btn.dataset.tab);
});

// ---------- preview ----------
// voice parameter sliders: live value next to the label
[["voiceSpeed", "voiceSpeedVal"], ["voicePause", "voicePauseVal"],
 ["voiceSteps", "voiceStepsVal"]].forEach(([slider, label]) => {
  $(slider).oninput = () => $(label).textContent = $(slider).value;
});
function voiceParams() {
  return {
    speed: parseFloat($("voiceSpeed").value) || 1.05,
    steps: parseInt($("voiceSteps").value, 10) || 8,
    silence: parseFloat($("voicePause").value) >= 0 ? parseFloat($("voicePause").value) : 0.3,
    lang: $("voiceLang").value || "uk",
  };
}
async function previewText(text, hintEl, btn, voice, vp) {
  // without explicit voice/vp they are taken from the scenario editor controls
  voice = voice || $("voice").value;
  vp = vp || voiceParams();
  const errBanner = $("tab-scenarios").classList.contains("active")
    ? $("scnBanner") : $("campBanner");
  if (!text.trim()) { banner(errBanner, t("Порожній текст"), "err"); return; }
  if (btn) btn.disabled = true; if (hintEl) hintEl.textContent = t("синтез…");
  const fd = new FormData(); fd.append("text", text); fd.append("voice", voice);
  fd.append("speed", vp.speed ?? 1.05); fd.append("steps", vp.steps ?? 8);
  fd.append("silence", vp.silence ?? 0.3); fd.append("lang", vp.lang ?? "uk");
  const { ok, data } = await api("POST", "/preview", fd, true);
  if (btn) btn.disabled = false;
  if (!ok) { if (hintEl) hintEl.textContent = data.error || tr("помилка", "error"); return; }
  if (hintEl) hintEl.textContent = data.secs + tr(" c", " s");
  const p = $("player"); p.src = data.url + "?t=" + Date.now(); p.style.display = "block"; p.play();
}
$("previewBtn").onclick = () => previewText($("text").value, $("previewHint"), $("previewBtn"));

// ---------- IVR editor (recursive tree of levels) ----------
// The state is a single object of the same shape that goes into POST /start (ivr.menu).
// Structural changes (add/remove an option, change action/key) fully
// re-render the editor from the state; text fields write into the state directly,
// without a re-render — otherwise focus is lost while typing.
const IVR_MAX_DEPTH = 4; // mirror of flow.MAX_DEPTH
// Spoken Ukrainian (always — this is what the callee hears, in any UI language):
const DIGIT_WORDS = {"0":"нуль","1":"один","2":"два","3":"три","4":"чотири",
  "5":"п'ять","6":"шість","7":"сім","8":"вісім","9":"дев'ять"};
const ACTION_UK = {operator:"оператор", replay:"повторити", menu:"підменю",
  play:"фраза", back:"назад", home:"головне меню", optout:"відписатися",
  hangup:"завершити"};
const THEN_UK = {stay:"потім — знову це меню", back:"потім — на рівень вище",
  hangup:"потім — завершити дзвінок"};
// Mirror of flow.ANNOUNCE_TEMPLATES (the server is the source of truth): the
// placeholder shows the auto-text that will be synthesized if the announce is left empty.
const ANNOUNCE_UK = {
  operator: w => `Щоб з'єднатися з оператором, натисніть ${w}.`,
  replay: w => `Щоб прослухати ще раз, натисніть ${w}.`,
  optout: w => `Щоб відписатися від дзвінків, натисніть ${w}.`,
  back: w => `Щоб повернутися назад, натисніть ${w}.`,
  home: w => `Щоб повернутися в головне меню, натисніть ${w}.`,
  hangup: w => `Щоб завершити дзвінок, натисніть ${w}.`,
};
const DEFAULT_CONNECT_TEXT = "Зачекайте, з'єднуємо з оператором";
const DEFAULT_OPTOUT_TEXT = "Вас видалено зі списку";

let ivrMenu = { announce_text: "", options: [] };

function announceMirror(options) {
  return options.map(o => {
    const w = DIGIT_WORDS[o.digit];
    if (!w) return "";
    if (ANNOUNCE_UK[o.action]) return ANNOUNCE_UK[o.action](w);
    const label = (o.label || "").trim();
    return label ? `${label}: натисніть ${w}.` : "";
  }).filter(Boolean).join(" ");
}
function el(tag, props = {}, ...kids) {
  const node = document.createElement(tag);
  Object.assign(node, props);
  kids.forEach(k => node.append(k));
  return node;
}
function fld(text) { return el("label", { className: "fld", textContent: text }); }
function frow(...kids) { return el("div", { className: "frow" }, ...kids); }
function prevBtn(getText) {
  const b = el("button", { type: "button", className: "small", textContent: "▶" });
  b.onclick = () => previewText(getText(), null, b);
  return b;
}
function freeDigit(options) {
  const used = new Set(options.map(o => o.digit));
  return "1234567890".split("").find(d => !used.has(d)) ?? null;
}

function renderIvr() {
  $("ivrRoot").replaceChildren(renderLevel(ivrMenu, 1));
  $("menuTimeoutWrap").style.display = ivrMenu.options.length ? "" : "none";
}

function renderLevel(menu, depth) {
  const wrap = el("div", { className: depth > 1 ? "ivr-level" : "" });
  if (depth > 1) {
    const ta = el("textarea", { rows: 2, value: menu.text || "",
      placeholder: t("Необов'язково: грає один раз при вході на цей рівень") });
    ta.oninput = () => menu.text = ta.value;
    wrap.append(fld(t("Текст рівня")), frow(ta, prevBtn(() => ta.value)));
  }
  let syncAnnounce = () => {};
  if (menu.options.length) {
    const ann = el("input", { value: menu.announce_text || "" });
    ann.oninput = () => menu.announce_text = ann.value;
    syncAnnounce = () => ann.placeholder = announceMirror(menu.options);
    syncAnnounce();
    wrap.append(fld(t("Анонс опцій (порожньо = автотекст; грає на кожному раунді очікування)")),
                frow(ann, prevBtn(() => ann.value || ann.placeholder)));
  }
  menu.options.forEach((opt, i) =>
    wrap.append(renderOption(menu, opt, i, depth, syncAnnounce)));
  const add = el("button", { type: "button", className: "small", textContent: t("+ опція") });
  add.onclick = () => {
    const digit = freeDigit(menu.options);
    if (digit === null) return;
    menu.options.push({ digit, action: "operator", connect_text: "" });
    renderIvr();
  };
  wrap.append(add);
  return wrap;
}

function renderOption(menu, opt, idx, depth, syncAnnounce) {
  const row = el("div", { className: "opt-row" });
  const dig = el("select", { title: t("Клавіша") });
  "1234567890".split("").forEach(d =>
    dig.append(el("option", { value: d, textContent: d, selected: opt.digit === d })));
  dig.onchange = () => { opt.digit = dig.value; renderIvr(); };
  const act = el("select", { title: t("Дія") });
  Object.keys(ACTION_UK).forEach(a => {
    if ((a === "back" || a === "home") && depth === 1) return; // nowhere to go back to
    if (a === "menu" && depth >= IVR_MAX_DEPTH) return; // depth limit
    act.append(el("option", { value: a, textContent: t(ACTION_UK[a]),
                              selected: opt.action === a }));
  });
  act.onchange = () => {
    // actions have different fields — on change reset everything except the key
    Object.keys(opt).forEach(k => { if (k !== "digit") delete opt[k]; });
    opt.action = act.value;
    if (act.value === "menu") opt.menu = { text: "", announce_text: "", options: [] };
    if (act.value === "play") opt.then = "stay";
    renderIvr();
  };
  const del = el("button", { type: "button", className: "small danger",
    textContent: "🗑", onclick: () => { menu.options.splice(idx, 1); renderIvr(); } });
  const fields = el("div", { className: "opt-fields" });
  row.append(dig, act, fields, del);

  const labelInput = () => {
    const inp = el("input", { value: opt.label || "",
      placeholder: t("Підпис для анонсу (напр.: Графік роботи)") });
    inp.oninput = () => { opt.label = inp.value; syncAnnounce(); };
    return inp;
  };
  if (opt.action === "operator") {
    const inp = el("input", { value: opt.connect_text || "",
      placeholder: DEFAULT_CONNECT_TEXT });
    inp.oninput = () => opt.connect_text = inp.value;
    fields.append(frow(inp, prevBtn(() => inp.value || inp.placeholder)));
  } else if (opt.action === "optout") {
    const inp = el("input", { value: opt.confirm_text || "",
      placeholder: DEFAULT_OPTOUT_TEXT });
    inp.oninput = () => opt.confirm_text = inp.value;
    fields.append(frow(inp, prevBtn(() => inp.value || inp.placeholder)));
  } else if (opt.action === "play") {
    const ta = el("textarea", { rows: 2, value: opt.text || "",
      placeholder: t("Текст фрази") });
    ta.oninput = () => opt.text = ta.value;
    const then = el("select", {});
    Object.keys(THEN_UK).forEach(k => {
      if (k === "back" && depth === 1) return;
      then.append(el("option", { value: k, textContent: t(THEN_UK[k]),
                                 selected: opt.then === k }));
    });
    then.onchange = () => opt.then = then.value;
    fields.append(frow(labelInput()), frow(ta, prevBtn(() => ta.value)), frow(then));
  } else if (opt.action === "menu") {
    fields.append(frow(labelInput()), renderLevel(opt.menu, depth + 1));
  }
  return row;
}

function collectIvr() {
  return {
    timeout_sec: parseInt($("timeoutSec").value || "5", 10),
    max_repeats: parseInt($("maxRepeats").value || "2", 10),
    menu: ivrMenu,
  };
}
renderIvr();

// ---------- scenarios: library + editor ----------
const TYPE_UK = { info: "інфо", operator: "оператор" };
let scenarios = [];

function selectedScenario() {
  return scenarios.find(s => String(s.id) === $("scnSel").value);
}

// "F3" for Ukrainian, "F3 · en" otherwise — the language matters only when
// it differs from the default
function voiceLabel(s) {
  const lang = (s.voice_params || {}).lang;
  return esc(s.voice) + (lang && lang !== "uk" ? ` · ${esc(lang)}` : "");
}

// one-line digest of the menu tree — both in the list and on the launch tab
function menuDigest(ivr) {
  const m = (ivr || {}).menu || {};
  if (!(m.options || []).length) return t("без меню: програти повідомлення і завершити");
  const walk = menu => (menu.options || []).map(o => {
    if (o.action === "menu") return `${o.digit} → «${o.label || t("підменю")}» (${walk(o.menu || {})})`;
    if (o.action === "play") return `${o.digit} → «${o.label || t("фраза")}»`;
    return `${o.digit} → ${t(ACTION_UK[o.action] || o.action)}`;
  }).join(", ");
  return walk(m);
}

async function loadScenarios() {
  const { data } = await api("GET", "/scenarios");
  scenarios = data.scenarios || [];
  renderScnTable();
  fillScnSelect();
}

function renderScnTable() {
  const tb = $("scnTbl").querySelector("tbody");
  $("scnEmptyHint").style.display = scenarios.length ? "none" : "";
  tb.innerHTML = scenarios.map(s => `<tr>
    <td>${esc(s.name)}</td><td>${t(TYPE_UK[s.campaign_type] || s.campaign_type)}</td>
    <td>${voiceLabel(s)}</td><td class="muted">${esc(menuDigest(s.ivr))}</td>
    <td style="white-space:nowrap">
      <button class="small" data-scnedit="${s.id}" title="${t("Редагувати")}">✎</button>
      <button class="small" data-scnclone="${s.id}" title="${t("Клонувати — копія для схожого сценарію")}">⧉</button>
      <button class="small danger" data-scndel="${s.id}" title="${t("Видалити")}">🗑</button></td></tr>`).join("");
  tb.querySelectorAll("[data-scnedit]").forEach(b => b.onclick = () =>
    editScenario(scenarios.find(s => s.id === +b.dataset.scnedit), false));
  tb.querySelectorAll("[data-scnclone]").forEach(b => b.onclick = () =>
    editScenario(scenarios.find(s => s.id === +b.dataset.scnclone), true));
  tb.querySelectorAll("[data-scndel]").forEach(b => b.onclick = async () => {
    if (!confirm(t("Видалити сценарій?"))) return;
    await api("DELETE", "/scenarios/" + b.dataset.scndel);
    loadScenarios();
  });
}

function fillScnSelect() {
  const sel = $("scnSel"); const prev = sel.value;
  sel.innerHTML = scenarios.map(s => `<option value="${s.id}">${esc(s.name)}</option>`).join("");
  if ([...sel.options].some(o => o.value === prev)) sel.value = prev;
  $("startBtn").disabled = !scenarios.length;
  syncDigest();
}

function syncDigest() {
  const s = selectedScenario();
  if (!s) {
    $("scnDigest").textContent = t("Немає сценаріїв — створіть перший у вкладці «Сценарії».");
    return;
  }
  const snip = s.message.length > 120 ? s.message.slice(0, 120) + "…" : s.message;
  $("scnDigest").innerHTML =
    `<div>«${esc(snip)}»</div>
     <div style="margin-top:4px">${t("Голос")} ${voiceLabel(s)} · ${t("тип:")} ${t(TYPE_UK[s.campaign_type] || s.campaign_type)}
       · ${t("меню:")} ${esc(menuDigest(s.ivr))}</div>`;
}
$("scnSel").onchange = syncDigest;
$("scnListenBtn").onclick = () => {
  const s = selectedScenario();
  if (s) previewText(s.message, null, $("scnListenBtn"), s.voice, s.voice_params);
};
$("scnEditBtn").onclick = () => {
  const s = selectedScenario();
  if (!s) return;
  openTab("scenarios");
  editScenario(s, false);
};

function setSlider(id, labelId, value) {
  $(id).value = value; $(labelId).textContent = $(id).value;
}

// the editor legend embeds a scenario name — track its state so a language
// switch can re-render it
let legendState = { mode: "new", name: "" };
function renderLegend() {
  const { mode, name } = legendState;
  $("scnFormLegend").textContent =
    mode === "edit" ? tr(`Сценарій «${name}»`, `Scenario “${name}”`)
    : mode === "clone" ? tr(`Новий сценарій (копія «${name}»)`, `New scenario (copy of “${name}”)`)
    : t("Новий сценарій");
}

function editScenario(s, asClone) {
  // clone = the same form without id: saving will create a new record
  $("scnId").value = asClone ? "" : s.id;
  $("scnName").value = asClone ? s.name + tr(" (копія)", " (copy)") : s.name;
  $("campaignType").value = s.campaign_type || "info";
  $("text").value = s.message;
  $("voice").value = s.voice || "F3";
  const vp = s.voice_params || {};
  setSlider("voiceSpeed", "voiceSpeedVal", vp.speed ?? 1.05);
  setSlider("voicePause", "voicePauseVal", vp.silence ?? 0.3);
  setSlider("voiceSteps", "voiceStepsVal", vp.steps ?? 8);
  $("voiceLang").value = vp.lang || "uk";
  const ivr = s.ivr || {};
  $("timeoutSec").value = ivr.timeout_sec ?? 5;
  $("maxRepeats").value = ivr.max_repeats ?? 2;
  ivrMenu = JSON.parse(JSON.stringify(ivr.menu || { announce_text: "", options: [] }));
  renderIvr();
  legendState = { mode: asClone ? "clone" : "edit", name: s.name };
  renderLegend();
  window.scrollTo(0, $("scnFormLegend").offsetTop - 60);
}

function resetScenarioForm() {
  $("scnId").value = ""; $("scnName").value = "";
  $("campaignType").value = "info";
  $("text").value = "";
  $("voice").value = "F3";
  setSlider("voiceSpeed", "voiceSpeedVal", 1.05);
  setSlider("voicePause", "voicePauseVal", 0.3);
  setSlider("voiceSteps", "voiceStepsVal", 8);
  $("voiceLang").value = "uk";
  $("timeoutSec").value = 5; $("maxRepeats").value = 2;
  ivrMenu = { announce_text: "", options: [] };
  renderIvr();
  legendState = { mode: "new", name: "" };
  renderLegend();
}
$("scnReset").onclick = resetScenarioForm;

function collectScenario() {
  return {
    name: $("scnName").value.trim(),
    campaign_type: $("campaignType").value,
    message: $("text").value,
    voice: $("voice").value,
    voice_params: voiceParams(),
    ivr: collectIvr(),
  };
}

async function saveScenario(asNew) {
  const id = asNew ? "" : $("scnId").value;
  const { ok, data } = await api("POST", id ? "/scenarios/" + id : "/scenarios",
                                 collectScenario());
  if (!ok) { banner($("scnBanner"), data.error || t("Помилка"), "err"); return; }
  if (!id && data.id) $("scnId").value = data.id;
  banner($("scnBanner"), t("Сценарій збережено"), "ok");
  legendState = { mode: "edit", name: $("scnName").value.trim() };
  renderLegend();
  loadScenarios();
}
$("scnSave").onclick = () => saveScenario(false);
$("scnSaveNew").onclick = () => saveScenario(true);

// ---------- start campaign ----------
$("startBtn").onclick = async () => {
  const s = selectedScenario();
  if (!s) { banner($("campBanner"), t("Оберіть сценарій"), "err"); return; }
  const numbers = $("numbers").value.split("\n").map(x => x.trim()).filter(Boolean);
  const payload = {
    scenario_id: s.id,
    name: $("name").value,
    numbers,
    profile_id: parseInt($("profileSel").value, 10) || null,
    max_concurrent: parseInt($("maxConc").value || "1", 10),
  };
  $("startBtn").disabled = true;
  const { ok, data } = await api("POST", "/start", payload);
  $("startBtn").disabled = false;
  if (!ok) { banner($("campBanner"), data.error || data.detail || t("Помилка"), "err"); return; }
  banner($("campBanner"),
         tr(`Кампанію запущено (#${data.campaign_id})`,
            `Campaign started (#${data.campaign_id})`), "ok");
  $("liveBox").style.display = "block";
  startPolling();
};

// ---------- live status polling ----------
let pollTimer = null;
function startPolling() { if (!pollTimer) pollTimer = setInterval(pollStatus, 1500); pollStatus(); }
async function pollStatus() {
  const { ok, data } = await api("GET", "/status");
  if (!ok) return;
  $("fsState").textContent = data.esl_connected ? "" : t("⚠ FreeSWITCH офлайн");
  if (!data.campaign_id) { $("liveBox").style.display = "none"; stopPoll(); return; }
  $("liveBox").style.display = "block";
  $("liveName").textContent = `#${data.campaign_id} ${data.name||""} — ${data.phase}`;
  const c = data.counts || {};
  $("liveCounts").innerHTML = `<span class="c">${t("всього")} <b>${c.total||0}</b></span>` +
    STATUSES.filter(s => c[s]).map(s => `<span class="c">${statusLabel(s)} <b>${c[s]}</b></span>`).join("");
  $("liveCurrent").textContent = data.current
    ? `${t("Зараз:")} ${data.current.number} (${data.current.state})` : "";
  $("liveLog").textContent = (data.log||[]).map(trLog).join("\n");
  $("liveLog").scrollTop = $("liveLog").scrollHeight;
  if (["done","stopped","interrupted","idle"].includes(data.phase)) stopPoll();
}
function stopPoll() { if (pollTimer) { clearInterval(pollTimer); pollTimer = null; } }

// ---------- settings: profiles ----------
async function loadConfig() {
  const { data } = await api("GET", "/config");
  const tb = $("profilesTbl").querySelector("tbody");
  tb.innerHTML = (data.profiles||[]).map(p => `<tr>
    <td>${esc(p.name)}</td><td>${esc(p.server)}:${p.port}</td><td>${esc(p.username)}</td>
    <td>${p.password_set ? "✓" : "—"}</td><td>${p.is_default ? "★" : ""}</td>
    <td><button class="small" data-edit='${jattr(p)}'>✎</button>
        <button class="small danger" data-delp="${p.id}">🗑</button></td></tr>`).join("");
  fillProfileSelect(data.profiles||[], data.default_id);
  tb.querySelectorAll("[data-edit]").forEach(b => b.onclick = () => editProfile(JSON.parse(b.dataset.edit)));
  tb.querySelectorAll("[data-delp]").forEach(b => b.onclick = () => delProfile(b.dataset.delp));
  loadOperators();
}
function fillProfileSelect(profiles, defId) {
  const sel = $("profileSel");
  sel.innerHTML = profiles.map(p =>
    `<option value="${p.id}" ${p.id===defId?"selected":""}>${esc(p.name)} (${esc(p.server)})</option>`).join("");
}
function editProfile(p) {
  $("pfId").value=p.id; $("pfName").value=p.name; $("pfServer").value=p.server;
  $("pfPort").value=p.port; $("pfUser").value=p.username; $("pfPass").value="";
  $("pfDefault").checked=p.is_default;
  window.scrollTo(0, document.body.scrollHeight);
}
$("pfReset").onclick = () => { ["pfId","pfName","pfServer","pfUser","pfPass"].forEach(i=>$(i).value="");
  $("pfPort").value="5060"; $("pfDefault").checked=false; };
$("pfSave").onclick = async () => {
  const fd = new FormData();
  fd.append("name",$("pfName").value); fd.append("server",$("pfServer").value);
  fd.append("port",$("pfPort").value); fd.append("username",$("pfUser").value);
  fd.append("password",$("pfPass").value); fd.append("is_default",$("pfDefault").checked);
  const id = $("pfId").value;
  const url = id ? "/config/profiles/"+id : "/config/profiles";
  const { ok, data } = await api("POST", url, fd, true);
  if (!ok) { banner($("setBanner"), data.error||t("Помилка"), "err"); return; }
  banner($("setBanner"), t("Профіль збережено"), "ok"); $("pfReset").onclick(); loadConfig();
};
async function delProfile(id) {
  if (!confirm(t("Видалити профіль?"))) return;
  await api("DELETE", "/config/profiles/"+id); loadConfig();
}

// ---------- settings: operators ----------
async function loadOperators() {
  const { data } = await api("GET", "/config/operators");
  const tb = $("opsTbl").querySelector("tbody");
  tb.innerHTML = (data.operators||[]).map(o => {
    const cls = o.registered===true ? (o.busy?"busy":"on") : (o.registered===false?"off":"off");
    const txt = o.registered===true ? (o.busy?t("у розмові"):t("онлайн"))
                                    : (o.registered===false?t("офлайн"):"?");
    return `<tr><td>${esc(o.name)}</td><td>${esc(o.extension)}</td>
      <td><span class="dot ${cls}"></span>${txt}</td>
      <td><button class="small danger" data-delop="${o.id}">🗑</button></td></tr>`;
  }).join("");
  tb.querySelectorAll("[data-delop]").forEach(b => b.onclick = async () => {
    if (!confirm(t("Видалити оператора?"))) return;
    await api("DELETE", "/config/operators/"+b.dataset.delop); loadOperators();
  });
}
$("opSave").onclick = async () => {
  const payload = { name:$("opName").value, extension:$("opExt").value, password:$("opPass").value };
  const { ok, data } = await api("POST", "/config/operators", payload);
  if (!ok) { banner($("setBanner"), data.error||t("Помилка"), "err"); return; }
  banner($("setBanner"), t("Оператора додано") + (data.reloadxml===false?t(" (reloadxml пізніше)"):""), "ok");
  $("opName").value=""; $("opExt").value=""; $("opPass").value=""; loadOperators();
};

// ---------- history ----------
$("histRefresh").onclick = loadHistory;
async function loadHistory() {
  const { data } = await api("GET", "/campaigns");
  const tb = $("histTbl").querySelector("tbody");
  tb.innerHTML = (data.campaigns||[]).map(c => {
    const cc = c.counts||{};
    const counters = STATUSES.filter(s=>cc[s]).map(s=>`${statusLabel(s)}:${cc[s]}`).join(" · ");
    const retry = `<button class="small" data-retry="${c.id}">${t("↻ невдалі")}</button>`;
    const resume = c.status==="interrupted" ? `<button class="small primary" data-resume="${c.id}">${t("▶ продовжити")}</button>` : "";
    const scn = c.scenario_name
      ? `<div class="hint">${t("сценарій:")} ${esc(c.scenario_name)}</div>` : "";
    return `<tr><td>${c.id}</td><td>${esc(c.name)}${scn}</td><td>${pill(c.status)}</td>
      <td class="muted">${cc.total||0}: ${counters||"—"}</td>
      <td>${resume} ${retry} <button class="small" data-det="${c.id}">${t("деталі")}</button></td></tr>
      <tr class="grow" id="det-${c.id}" style="display:none"><td colspan="5"></td></tr>`;
  }).join("");
  tb.querySelectorAll("[data-retry]").forEach(b => b.onclick = () => retryFailed(b.dataset.retry));
  tb.querySelectorAll("[data-resume]").forEach(b => b.onclick = () => resumeCampaign(b.dataset.resume));
  tb.querySelectorAll("[data-det]").forEach(b => b.onclick = () => toggleDetails(b.dataset.det));
}
async function toggleDetails(id) {
  const row = $("det-"+id); const cell = row.querySelector("td");
  if (row.style.display !== "none") { row.style.display = "none"; return; }
  const { data } = await api("GET", "/campaigns/"+id);
  cell.innerHTML = `<table><thead><tr><th>#</th><th>${t("Номер")}</th><th>${t("Статус")}</th>
    <th>AMD</th><th>DTMF</th><th>${t("Причина")}</th><th>${t("Спроб")}</th></tr></thead><tbody>` +
    (data.numbers||[]).map((n,i) => `<tr><td>${i+1}</td><td>${esc(n.number)}</td>
      <td>${pill(n.status)}</td><td>${n.amd_result||"—"}</td><td>${n.dtmf||"—"}</td>
      <td class="muted">${esc(n.hangup_cause||"—")}</td><td>${n.attempts}</td></tr>`).join("") +
    `</tbody></table>`;
  row.style.display = "";
}
async function retryFailed(id) {
  const { ok, data } = await api("POST", "/campaigns/"+id+"/retry-failed");
  if (!ok) { banner($("histBanner"), data.error||data.detail||t("Помилка"), "err"); return; }
  banner($("histBanner"),
         tr(`Повтор запущено (${data.count} номерів, #${data.campaign_id})`,
            `Retry started (${data.count} numbers, #${data.campaign_id})`), "ok");
  loadHistory();
}
async function resumeCampaign(id) {
  const { ok, data } = await api("POST", "/campaigns/"+id+"/resume");
  if (!ok) { banner($("histBanner"), data.error||t("Помилка"), "err"); return; }
  banner($("histBanner"), t("Кампанію відновлено"), "ok"); loadHistory();
}

// ---------- utils ----------
function esc(s) { return String(s==null?"":s).replace(/[&<>"]/g, c =>
  ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c])); }
function jattr(o) { return esc(JSON.stringify(o)); }

// ---------- language switch ----------
// [data-i18n] elements are translated by their initial (Ukrainian) text;
// captured once at boot so switching back restores the original.
const I18N_TEXT = [...document.querySelectorAll("[data-i18n]")].map(node =>
  [node, node.textContent.replace(/\s+/g, " ").trim()]);
const I18N_PH = [...document.querySelectorAll("[data-i18n-ph]")].map(node =>
  [node, node.placeholder]);
const I18N_TITLE = [...document.querySelectorAll("[data-i18n-t]")].map(node =>
  [node, node.title]);
function applyI18n() {
  document.documentElement.lang = LANG;
  document.title = t("Дзвонилка 2.0");
  I18N_TEXT.forEach(([node, uk]) => node.textContent = t(uk));
  I18N_PH.forEach(([node, uk]) => node.placeholder = t(uk));
  I18N_TITLE.forEach(([node, uk]) => node.title = t(uk));
  $("langBtn").textContent = LANG === "uk" ? "EN" : "УКР";
  renderLegend();
}
$("langBtn").onclick = () => {
  LANG = LANG === "uk" ? "en" : "uk";
  localStorage.setItem("lang", LANG);
  applyI18n();
  // re-render everything dynamic in the new language
  renderIvr(); loadScenarios(); loadConfig(); loadHistory(); pollStatus();
};

// ---------- boot ----------
applyI18n();
loadConfig();
loadScenarios();
pollStatus();
setInterval(() => { if ($("tab-campaign").classList.contains("active")) pollStatus(); }, 1500);
