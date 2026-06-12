"use strict";
// Дзвонилка 2.0 — фронтенд (один файл, без фреймворків).
// Три вкладки; кампанія полить /status кожні 1.5 c під час набору.

const $ = id => document.getElementById(id);
const STATUSES = ["pending","ringing","answered","transferred","voicemail-left",
  "machine-hangup","no-answer","busy","failed","optout","missed-operator"];
const STATUS_UK = {pending:"очікує",ringing:"дзвонить",answered:"прослухав",
  transferred:"оператор",["voicemail-left"]:"автовідп.",["machine-hangup"]:"автовідп.×",
  ["no-answer"]:"не відповів",busy:"зайнято",failed:"помилка",optout:"відписка",
  ["missed-operator"]:"опер.зайнятий"};

async function api(method, url, body, isForm) {
  const opts = { method };
  if (body && isForm) { opts.body = body; }
  else if (body) { opts.headers = {"Content-Type":"application/json"}; opts.body = JSON.stringify(body); }
  let r;
  try { r = await fetch(url, opts); }
  catch { return { ok: false, status: 0, data: { error: "Немає з'єднання з сервером" } }; }
  let data = {};
  try { data = await r.json(); } catch {}
  if (!r.ok && !data.error && !data.detail) data.error = "HTTP " + r.status;
  return { ok: r.ok, status: r.status, data };
}
function banner(el, msg, kind) {
  el.textContent = msg; el.className = "banner show " + (kind||"info");
  if (kind === "ok") setTimeout(() => el.classList.remove("show"), 4000);
}
function pill(s) { return `<span class="pill ${s}">${STATUS_UK[s]||s}</span>`; }

// ---------- tabs ----------
document.querySelectorAll("nav.tabs button").forEach(btn => {
  btn.onclick = () => {
    document.querySelectorAll("nav.tabs button").forEach(b => b.classList.remove("active"));
    document.querySelectorAll(".tab").forEach(t => t.classList.remove("active"));
    btn.classList.add("active");
    $("tab-" + btn.dataset.tab).classList.add("active");
    if (btn.dataset.tab === "settings") loadConfig();
    if (btn.dataset.tab === "history") loadHistory();
  };
});

// ---------- preview ----------
// повзунки параметрів голосу: живе значення поруч із підписом
[["voiceSpeed", "voiceSpeedVal"], ["voicePause", "voicePauseVal"],
 ["voiceSteps", "voiceStepsVal"]].forEach(([slider, label]) => {
  $(slider).oninput = () => $(label).textContent = $(slider).value;
});
function voiceParams() {
  return {
    speed: parseFloat($("voiceSpeed").value) || 1.05,
    steps: parseInt($("voiceSteps").value, 10) || 8,
    silence: parseFloat($("voicePause").value) >= 0 ? parseFloat($("voicePause").value) : 0.3,
  };
}
async function previewText(text, hintEl, btn) {
  if (!text.trim()) { banner($("campBanner"), "Порожній текст", "err"); return; }
  if (btn) btn.disabled = true; if (hintEl) hintEl.textContent = "синтез…";
  const fd = new FormData(); fd.append("text", text); fd.append("voice", $("voice").value);
  const vp = voiceParams();
  fd.append("speed", vp.speed); fd.append("steps", vp.steps); fd.append("silence", vp.silence);
  const { ok, data } = await api("POST", "/preview", fd, true);
  if (btn) btn.disabled = false;
  if (!ok) { if (hintEl) hintEl.textContent = data.error || "помилка"; return; }
  if (hintEl) hintEl.textContent = data.secs + " c";
  const p = $("player"); p.src = data.url + "?t=" + Date.now(); p.style.display = "block"; p.play();
}
$("previewBtn").onclick = () => previewText($("text").value, $("previewHint"), $("previewBtn"));

// ---------- IVR editor (рекурсивне дерево рівнів) ----------
// Стан — один об'єкт тієї ж форми, що йде у POST /start (ivr.menu).
// Структурні зміни (додати/видалити опцію, змінити дію/клавішу) повністю
// перемальовують редактор зі стану; текстові поля пишуть у стан напряму,
// без ре-рендеру — інакше губиться фокус під час набору.
const IVR_MAX_DEPTH = 4; // дзеркало flow.MAX_DEPTH
const DIGIT_WORDS = {"0":"нуль","1":"один","2":"два","3":"три","4":"чотири",
  "5":"п'ять","6":"шість","7":"сім","8":"вісім","9":"дев'ять"};
const ACTION_UK = {operator:"оператор", replay:"повторити", menu:"підменю",
  play:"фраза", back:"назад", optout:"відписатися", hangup:"завершити"};
const THEN_UK = {stay:"потім — знову це меню", back:"потім — на рівень вище",
  hangup:"потім — завершити дзвінок"};
// Дзеркало flow.ANNOUNCE_TEMPLATES (сервер — джерело правди): плейсхолдер
// показує автотекст, який буде синтезовано, якщо анонс лишити порожнім.
const ANNOUNCE_UK = {
  operator: w => `Щоб з'єднатися з оператором, натисніть ${w}.`,
  replay: w => `Щоб прослухати ще раз, натисніть ${w}.`,
  optout: w => `Щоб відписатися від дзвінків, натисніть ${w}.`,
  back: w => `Щоб повернутися назад, натисніть ${w}.`,
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
      placeholder: "Необов'язково: грає один раз при вході на цей рівень" });
    ta.oninput = () => menu.text = ta.value;
    wrap.append(fld("Текст рівня"), frow(ta, prevBtn(() => ta.value)));
  }
  let syncAnnounce = () => {};
  if (menu.options.length) {
    const ann = el("input", { value: menu.announce_text || "" });
    ann.oninput = () => menu.announce_text = ann.value;
    syncAnnounce = () => ann.placeholder = announceMirror(menu.options);
    syncAnnounce();
    wrap.append(fld("Анонс опцій (порожньо = автотекст; грає на кожному раунді очікування)"),
                frow(ann, prevBtn(() => ann.value || ann.placeholder)));
  }
  menu.options.forEach((opt, i) =>
    wrap.append(renderOption(menu, opt, i, depth, syncAnnounce)));
  const add = el("button", { type: "button", className: "small", textContent: "+ опція" });
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
  const dig = el("select", { title: "Клавіша" });
  "1234567890".split("").forEach(d =>
    dig.append(el("option", { value: d, textContent: d, selected: opt.digit === d })));
  dig.onchange = () => { opt.digit = dig.value; renderIvr(); };
  const act = el("select", { title: "Дія" });
  Object.keys(ACTION_UK).forEach(a => {
    if (a === "back" && depth === 1) return;          // нікуди повертатись
    if (a === "menu" && depth >= IVR_MAX_DEPTH) return; // ліміт глибини
    act.append(el("option", { value: a, textContent: ACTION_UK[a],
                              selected: opt.action === a }));
  });
  act.onchange = () => {
    // дії мають різні поля — при зміні скидаємо все, крім клавіші
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
      placeholder: "Підпис для анонсу (напр.: Графік роботи)" });
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
      placeholder: "Текст фрази" });
    ta.oninput = () => opt.text = ta.value;
    const then = el("select", {});
    Object.keys(THEN_UK).forEach(t => {
      if (t === "back" && depth === 1) return;
      then.append(el("option", { value: t, textContent: THEN_UK[t],
                                 selected: opt.then === t }));
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

// ---------- start campaign ----------
$("startBtn").onclick = async () => {
  const numbers = $("numbers").value.split("\n").map(s => s.trim()).filter(Boolean);
  const payload = {
    name: $("name").value, message: $("text").value, voice: $("voice").value,
    voice_params: voiceParams(),
    numbers, profile_id: parseInt($("profileSel").value, 10) || null,
    campaign_type: $("campaignType").value,
    max_concurrent: parseInt($("maxConc").value || "1", 10),
    ivr: collectIvr(),
  };
  $("startBtn").disabled = true;
  const { ok, data } = await api("POST", "/start", payload);
  $("startBtn").disabled = false;
  if (!ok) { banner($("campBanner"), data.error || data.detail || "Помилка", "err"); return; }
  banner($("campBanner"), "Кампанію запущено (#" + data.campaign_id + ")", "ok");
  $("liveBox").style.display = "block";
  startPolling();
};

// ---------- live status polling ----------
let pollTimer = null;
function startPolling() { if (!pollTimer) pollTimer = setInterval(pollStatus, 1500); pollStatus(); }
async function pollStatus() {
  const { ok, data } = await api("GET", "/status");
  if (!ok) return;
  $("fsState").textContent = data.esl_connected ? "" : "⚠ FreeSWITCH офлайн";
  if (!data.campaign_id) { $("liveBox").style.display = "none"; stopPoll(); return; }
  $("liveBox").style.display = "block";
  $("liveName").textContent = `#${data.campaign_id} ${data.name||""} — ${data.phase}`;
  const c = data.counts || {};
  $("liveCounts").innerHTML = `<span class="c">всього <b>${c.total||0}</b></span>` +
    STATUSES.filter(s => c[s]).map(s => `<span class="c">${STATUS_UK[s]||s} <b>${c[s]}</b></span>`).join("");
  $("liveCurrent").textContent = data.current
    ? `Зараз: ${data.current.number} (${data.current.state})` : "";
  $("liveLog").textContent = (data.log||[]).join("\n");
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
  if (!ok) { banner($("setBanner"), data.error||"Помилка", "err"); return; }
  banner($("setBanner"), "Профіль збережено", "ok"); $("pfReset").onclick(); loadConfig();
};
async function delProfile(id) {
  if (!confirm("Видалити профіль?")) return;
  await api("DELETE", "/config/profiles/"+id); loadConfig();
}

// ---------- settings: operators ----------
async function loadOperators() {
  const { data } = await api("GET", "/config/operators");
  const tb = $("opsTbl").querySelector("tbody");
  tb.innerHTML = (data.operators||[]).map(o => {
    const cls = o.registered===true ? (o.busy?"busy":"on") : (o.registered===false?"off":"off");
    const txt = o.registered===true ? (o.busy?"у розмові":"онлайн") : (o.registered===false?"офлайн":"?");
    return `<tr><td>${esc(o.name)}</td><td>${esc(o.extension)}</td>
      <td><span class="dot ${cls}"></span>${txt}</td>
      <td><button class="small danger" data-delop="${o.id}">🗑</button></td></tr>`;
  }).join("");
  tb.querySelectorAll("[data-delop]").forEach(b => b.onclick = async () => {
    if (!confirm("Видалити оператора?")) return;
    await api("DELETE", "/config/operators/"+b.dataset.delop); loadOperators();
  });
}
$("opSave").onclick = async () => {
  const payload = { name:$("opName").value, extension:$("opExt").value, password:$("opPass").value };
  const { ok, data } = await api("POST", "/config/operators", payload);
  if (!ok) { banner($("setBanner"), data.error||"Помилка", "err"); return; }
  banner($("setBanner"), "Оператора додано" + (data.reloadxml===false?" (reloadxml пізніше)":""), "ok");
  $("opName").value=""; $("opExt").value=""; $("opPass").value=""; loadOperators();
};

// ---------- history ----------
$("histRefresh").onclick = loadHistory;
async function loadHistory() {
  const { data } = await api("GET", "/campaigns");
  const tb = $("histTbl").querySelector("tbody");
  tb.innerHTML = (data.campaigns||[]).map(c => {
    const cc = c.counts||{};
    const counters = STATUSES.filter(s=>cc[s]).map(s=>`${STATUS_UK[s]||s}:${cc[s]}`).join(" · ");
    const retry = `<button class="small" data-retry="${c.id}">↻ невдалі</button>`;
    const resume = c.status==="interrupted" ? `<button class="small primary" data-resume="${c.id}">▶ продовжити</button>` : "";
    return `<tr><td>${c.id}</td><td>${esc(c.name)}</td><td>${pill(c.status)}</td>
      <td class="muted">${cc.total||0}: ${counters||"—"}</td>
      <td>${resume} ${retry} <button class="small" data-det="${c.id}">деталі</button></td></tr>
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
  cell.innerHTML = `<table><thead><tr><th>#</th><th>Номер</th><th>Статус</th>
    <th>AMD</th><th>DTMF</th><th>Причина</th><th>Спроб</th></tr></thead><tbody>` +
    (data.numbers||[]).map((n,i) => `<tr><td>${i+1}</td><td>${esc(n.number)}</td>
      <td>${pill(n.status)}</td><td>${n.amd_result||"—"}</td><td>${n.dtmf||"—"}</td>
      <td class="muted">${esc(n.hangup_cause||"—")}</td><td>${n.attempts}</td></tr>`).join("") +
    `</tbody></table>`;
  row.style.display = "";
}
async function retryFailed(id) {
  const { ok, data } = await api("POST", "/campaigns/"+id+"/retry-failed");
  if (!ok) { banner($("histBanner"), data.error||data.detail||"Помилка", "err"); return; }
  banner($("histBanner"), `Повтор запущено (${data.count} номерів, #${data.campaign_id})`, "ok");
  loadHistory();
}
async function resumeCampaign(id) {
  const { ok, data } = await api("POST", "/campaigns/"+id+"/resume");
  if (!ok) { banner($("histBanner"), data.error||"Помилка", "err"); return; }
  banner($("histBanner"), "Кампанію відновлено", "ok"); loadHistory();
}

// ---------- utils ----------
function esc(s) { return String(s==null?"":s).replace(/[&<>"]/g, c =>
  ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c])); }
function jattr(o) { return esc(JSON.stringify(o)); }

// ---------- boot ----------
loadConfig();
pollStatus();
setInterval(() => { if ($("tab-campaign").classList.contains("active")) pollStatus(); }, 1500);
