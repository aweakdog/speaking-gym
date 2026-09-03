/* ============ 工具 ============ */
const $ = (sel) => document.querySelector(sel);
const view = $("#view");
const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
if (!SR) $("#srWarn").classList.remove("hidden");

const sample = (arr, n) => [...arr].sort(() => Math.random() - 0.5).slice(0, n);
/* 抽取时避开最近用过的条目：整个池子轮完一遍才会重复 */
function pickFresh(key, arr, n, idFn) {
  const recent = load(key, []);
  let pool = arr.filter((x) => !recent.includes(idFn(x)));
  if (pool.length < n) pool = arr;
  const picked = sample(pool, n);
  save(key, [...picked.map(idFn), ...recent].slice(0, Math.max(0, arr.length - n)));
  return picked;
}
const todayStr = () => {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
};
function load(k, def) { try { return JSON.parse(localStorage.getItem(k)) ?? def; } catch { return def; } }
const save = (k, v) => localStorage.setItem(k, JSON.stringify(v));
const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const fmt = (s) => `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;

/* ============ API 客户端与登录态 ============ */
const AUTH = { token: localStorage.getItem("sg_token") || null, user: null };

async function api(path, opts = {}) {
  const headers = {};
  if (AUTH.token) headers["Authorization"] = "Bearer " + AUTH.token;
  let body;
  if (opts.json !== undefined) { headers["Content-Type"] = "application/json"; body = JSON.stringify(opts.json); }
  else if (opts.blob) { headers["Content-Type"] = opts.mime || "application/octet-stream"; body = opts.blob; }
  const res = await fetch(path, { method: opts.method || (body ? "POST" : "GET"), headers, body });
  let data = {};
  try { data = await res.json(); } catch (_) {}
  if (res.status === 401 && AUTH.user) { doLogout(false); throw new Error("登录已过期"); }
  if (!res.ok) throw new Error(data.error || `请求失败（${res.status}）`);
  return data;
}

function doLogout(callServer = true) {
  if (callServer && AUTH.token) api("/api/logout", { method: "POST", json: {} }).catch(() => {});
  AUTH.token = null; AUTH.user = null;
  localStorage.removeItem("sg_token");
  updateHeader();
  renderAuth();
}

function updateHeader() {
  const el = $("#userArea");
  el.innerHTML = AUTH.user
    ? `<span class="user-name">${esc(AUTH.user.username)}</span><button class="link-btn" id="btnLogout">退出</button>`
    : "";
  const b = $("#btnLogout");
  if (b) b.onclick = () => doLogout();
}

async function refreshStreak() {
  if (!AUTH.user) { $("#streakBadge").textContent = "连续 0 天"; return; }
  try {
    const d = await api("/api/scores");
    $("#streakBadge").textContent = `连续 ${d.streak} 天`;
  } catch (_) {}
}

/* ============ 登录 / 注册界面 ============ */
function renderAuth(mode = "login") {
  stopAllRec(); discardRecording(); clearTimer();
  const isLogin = mode === "login";
  view.innerHTML = `
    <div class="card auth-card">
      <h2>${isLogin ? "登录" : "注册新账号"}</h2>
      <p class="desc">练习数据、AI 评分和录音都会存到你自己的服务器账号里，换设备也能同步。</p>
      <div class="auth-form">
        <input id="authUser" placeholder="用户名（2-20 位，可用中文）" maxlength="20" autocomplete="username">
        <input id="authPw" type="password" placeholder="密码（至少 6 位）" autocomplete="${isLogin ? "current-password" : "new-password"}">
        <div id="authErr" class="auth-err hidden"></div>
        <button class="btn" id="authGo">${isLogin ? "登录" : "注册并登录"}</button>
        <button class="link-btn" id="authSwitch">${isLogin ? "没有账号？注册一个" : "已有账号？去登录"}</button>
      </div>
    </div>`;
  $("#authSwitch").onclick = () => renderAuth(isLogin ? "register" : "login");
  const go = async () => {
    const username = $("#authUser").value.trim();
    const password = $("#authPw").value;
    const err = $("#authErr");
    err.classList.add("hidden");
    try {
      const d = await api(isLogin ? "/api/login" : "/api/register", { json: { username, password } });
      AUTH.token = d.token;
      localStorage.setItem("sg_token", d.token);
      AUTH.user = await api("/api/me");
      updateHeader(); refreshStreak();
      newSession(); renderDaily();
      document.querySelectorAll("#tabs button").forEach((x) => x.classList.toggle("active", x.dataset.tab === "daily"));
    } catch (e) {
      err.textContent = e.message;
      err.classList.remove("hidden");
    }
  };
  $("#authGo").onclick = go;
  $("#authPw").addEventListener("keydown", (e) => { if (e.key === "Enter") go(); });
}

/* ============ 朗读 TTS ============ */
let voices = [];
const loadVoices = () => { voices = speechSynthesis.getVoices().filter((v) => v.lang.startsWith("en")); };
loadVoices();
if (speechSynthesis.onvoiceschanged !== undefined) speechSynthesis.onvoiceschanged = loadVoices;

function pickVoice() {
  return voices.find((v) => /Natural|Samantha|Google US English|Aria|Ava|Zira/i.test(v.name))
    || voices.find((v) => v.lang === "en-US") || voices[0];
}
let ttsAudio = null;
/* 音色 / 语速选择记住上次的值（默认 Jenny 女声，0.9x） */
for (const [id, key] of [["voice", "sg_voice"], ["rate", "sg_rate"]]) {
  const sel = $("#" + id);
  if (!sel) continue;
  const saved = localStorage.getItem(key);
  if (saved && [...sel.options].some((o) => o.value === saved)) sel.value = saved;
  sel.addEventListener("change", () => localStorage.setItem(key, sel.value));
}

/* 返回 Promise：朗读播放完毕（或失败）时 resolve —— 免提模式靠它衔接下一轮 */
async function speak(text) {
  speechSynthesis.cancel();
  if (ttsAudio) { try { ttsAudio.pause(); } catch (_) {} ttsAudio = null; }
  const rate = $("#rate").value || "0.9";
  const voice = ($("#voice") && $("#voice").value) || "jenny";
  if (AUTH.token) {
    try {
      const res = await fetch(`/api/tts?text=${encodeURIComponent(text)}&rate=${rate}&voice=${voice}`, {
        headers: { Authorization: "Bearer " + AUTH.token },
      });
      if (res.ok) {
        const blob = await res.blob();
        return await new Promise((resolve) => {
          ttsAudio = new Audio(URL.createObjectURL(blob));
          ttsAudio.onended = () => resolve(true);
          ttsAudio.onerror = () => resolve(false);
          ttsAudio.play().catch(() => resolve(false));
        });
      }
    } catch (_) {}
  }
  return await new Promise((resolve) => {
    const u = new SpeechSynthesisUtterance(text);
    u.lang = "en-US";
    u.rate = parseFloat(rate);
    const v = pickVoice();
    if (v) u.voice = v;
    u.onend = () => resolve(true);
    u.onerror = () => resolve(false);
    speechSynthesis.speak(u);
  });
}

/* ============ 语音识别 ============ */
let onceRec = null;
function recognizeOnce(onDone, onState) {
  if (!SR) { alert("当前浏览器不支持语音识别，请换用 Edge 或 Safari。"); return; }
  stopAllRec();
  const r = new SR();
  r.lang = "en-US"; r.interimResults = false; r.maxAlternatives = 1; r.continuous = false;
  let result = "";
  r.onresult = (e) => { result = e.results[0][0].transcript; };
  r.onend = () => { onceRec = null; onDone(result); };
  r.onerror = (e) => {
    if (e.error === "not-allowed" || e.error === "service-not-allowed")
      alert("麦克风权限被拒绝，请在浏览器地址栏允许麦克风访问。");
  };
  onceRec = r;
  onState && onState("listening");
  r.start();
}

const dict = { rec: null, active: false, final: "", interim: "" };
function startDictation(onUpdate) {
  if (!SR) { alert("当前浏览器不支持语音识别，请换用 Edge 或 Safari。"); return false; }
  stopAllRec();
  dict.active = true; dict.final = ""; dict.interim = "";
  const boot = () => {
    const r = new SR();
    r.lang = "en-US"; r.continuous = true; r.interimResults = true;
    r.onresult = (e) => {
      dict.interim = "";
      for (let i = e.resultIndex; i < e.results.length; i++) {
        if (e.results[i].isFinal) dict.final += e.results[i][0].transcript + " ";
        else dict.interim += e.results[i][0].transcript;
      }
      onUpdate(dict.final, dict.interim);
    };
    r.onend = () => { if (dict.active) { try { boot(); } catch (_) {} } };
    r.onerror = (e) => {
      if (e.error === "not-allowed" || e.error === "service-not-allowed") {
        dict.active = false;
        alert("麦克风权限被拒绝，请在浏览器地址栏允许麦克风访问。");
      }
    };
    dict.rec = r;
    r.start();
  };
  boot();
  return true;
}
function stopDictation() {
  dict.active = false;
  if (dict.rec) try { dict.rec.stop(); } catch (_) {}
  return dict.final.trim();
}
function stopAllRec() {
  dict.active = false;
  if (dict.rec) try { dict.rec.stop(); } catch (_) {}
  if (onceRec) try { onceRec.stop(); } catch (_) {}
  onceRec = null;
}

/* ============ 录音（MediaRecorder，上传到服务器） ============ */
const recorder = { mr: null, stream: null, chunks: [] };
async function startRecording() {
  if (!navigator.mediaDevices || !window.MediaRecorder) return false;
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    const mime = ["audio/webm;codecs=opus", "audio/webm", "audio/mp4"].find((m) => MediaRecorder.isTypeSupported(m)) || "";
    const mr = new MediaRecorder(stream, mime ? { mimeType: mime, audioBitsPerSecond: 32000 } : { audioBitsPerSecond: 32000 });
    recorder.chunks = [];
    mr.ondataavailable = (e) => { if (e.data.size) recorder.chunks.push(e.data); };
    mr.start();
    recorder.mr = mr; recorder.stream = stream;
    return true;
  } catch (e) { console.warn("录音不可用：", e); return false; }
}
function stopRecording() {
  return new Promise((resolve) => {
    const { mr, stream } = recorder;
    if (!mr || mr.state === "inactive") return resolve(null);
    mr.onstop = () => {
      const blob = new Blob(recorder.chunks, { type: mr.mimeType || "audio/webm" });
      stream.getTracks().forEach((t) => t.stop());
      recorder.mr = null; recorder.stream = null; recorder.chunks = [];
      resolve(blob.size ? blob : null);
    };
    mr.stop();
  });
}
function discardRecording() {
  stopVad();
  const { mr, stream } = recorder;
  if (mr && mr.state !== "inactive") try { mr.stop(); } catch (_) {}
  if (stream) stream.getTracks().forEach((t) => t.stop());
  recorder.mr = null; recorder.stream = null; recorder.chunks = [];
}

/* ============ 音量检测（跟读自动断句：停顿 2 秒才结束，逗号换气不会截断） ============ */
const vad = { ctx: null, timer: null };
function startVad(stream, onStop, { silenceMs = 2000, maxMs = 20000, onSilence } = {}) {
  stopVad();
  const Ctx = window.AudioContext || window.webkitAudioContext;
  if (!Ctx) return;
  const ctx = new Ctx();
  const analyser = ctx.createAnalyser();
  analyser.fftSize = 2048;
  ctx.createMediaStreamSource(stream).connect(analyser);
  const buf = new Uint8Array(analyser.fftSize);
  const recent = [];
  let spoke = false, silent = 0, total = 0, last = performance.now();
  vad.ctx = ctx;
  vad.timer = setInterval(() => {
    analyser.getByteTimeDomainData(buf);
    let sum = 0;
    for (let i = 0; i < buf.length; i++) { const v = (buf[i] - 128) / 128; sum += v * v; }
    const rms = Math.sqrt(sum / buf.length);
    recent.push(rms);
    if (recent.length > 15) recent.shift();
    const floor = Math.min(...recent); // 最近 1.5 秒的环境噪声底
    const threshold = Math.max(0.008, floor * 2.5 + 0.004); // 自适应阈值：轻声说话也能被识别
    const now = performance.now();
    const dt = now - last;
    last = now; total += dt;
    if (rms > threshold) { spoke = true; silent = 0; if (onSilence) onSilence(null); }
    else if (spoke) { silent += dt; if (onSilence) onSilence(Math.max(0, silenceMs - silent)); }
    if ((spoke && silent >= silenceMs) || total >= maxMs) {
      stopVad();
      if (onSilence) onSilence(null);
      onStop();
    }
  }, 100);
}
function stopVad() {
  if (vad.timer) { clearInterval(vad.timer); vad.timer = null; }
  if (vad.ctx) { try { vad.ctx.close(); } catch (_) {} vad.ctx = null; }
}

/* ============ 文本分析 ============ */
const normWords = (s) => s.toLowerCase().replace(/[^a-z0-9' ]+/g, " ").split(/\s+/).filter(Boolean);
function lcs(a, b) {
  const dp = Array.from({ length: a.length + 1 }, () => new Array(b.length + 1).fill(0));
  for (let i = 1; i <= a.length; i++)
    for (let j = 1; j <= b.length; j++)
      dp[i][j] = a[i - 1] === b[j - 1] ? dp[i - 1][j - 1] + 1 : Math.max(dp[i - 1][j], dp[i][j - 1]);
  return dp[a.length][b.length];
}
function shadowScore(target, said) {
  const t = normWords(target), s = normWords(said);
  if (!t.length || !s.length) return 0;
  return Math.round((100 * lcs(t, s)) / t.length);
}
const FILLER_RE = new RegExp(`\\b(${FILLERS.join("|").replace(/ /g, "\\s+")})\\b`, "gi");
function countFillers(text) { return (text.match(FILLER_RE) || []).length; }
function highlightFillers(text) { return esc(text).replace(FILLER_RE, (m) => `<mark>${m}</mark>`); }

/* ============ 本地存储（表达清单） ============ */
const getNotes = () => load("sg_notes", []);

/* ============ 路由 ============ */
let timerId = null;
function clearTimer() { if (timerId) { clearInterval(timerId); timerId = null; } }

/* 原地变身按钮：锁定当前宽度后换文案/样式/动作，鼠标不用移动就能点下一步 */
function morphButton(btn, label, cls, onClick, disabled = false) {
  if (!btn) return;
  if (!btn.style.minWidth) btn.style.minWidth = btn.offsetWidth + "px";
  btn.textContent = label;
  btn.className = "btn " + (cls || "");
  btn.onclick = onClick || null;
  btn.disabled = disabled;
}

document.querySelectorAll("#tabs button").forEach((b) => {
  b.addEventListener("click", () => {
    document.querySelectorAll("#tabs button").forEach((x) => x.classList.remove("active"));
    b.classList.add("active");
    stopAllRec(); discardRecording(); clearTimer(); speechSynthesis.cancel();
    if (!AUTH.user) return renderAuth();
    ({ daily: renderDaily, chat: renderChat, memory: renderMemory, phrases: renderPhrases, progress: renderProgress })[b.dataset.tab]();
  });
});

/* ============ Tab 1：每日练习 ============ */
const ROUNDS = [
  { sec: 120, label: "第 1 轮 · 2:00" },
  { sec: 90, label: "第 2 轮 · 1:30" },
  { sec: 60, label: "第 3 轮 · 1:00" },
];
let session = null;

function newSession() {
  session = {
    step: 1, shadowSet: pickFresh("sg_recent_shadow", SHADOW_BANK, 5, (x) => x.en),
    shadowIdx: 0, scores: [],
    cat: null, topic: null, round: 0, rounds: [], startedAt: Date.now(),
  };
}

function renderDaily() {
  if (!AUTH.user) return renderAuth();
  if (!session) newSession();
  ({ 1: renderShadow, 2: renderTopic, 3: renderReview, 4: renderDone }[session.step])();
}

function stepHeader(step) {
  const names = { 1: "① 跟读热身 · 约 10 分钟", 2: "② 话题表达 4-3-2 · 约 15 分钟", 3: "③ 复盘收集 · 约 5 分钟", 4: "完成" };
  return `<span class="step-tag">${names[step]}</span>`;
}

/* ---- 步骤 1：跟读 ---- */
function renderShadow() {
  const i = session.shadowIdx;
  const item = session.shadowSet[i];
  const dots = session.shadowSet.map((_, k) => `<div class="dot ${k < i ? "done" : ""}"></div>`).join("");
  view.innerHTML = `
    <div class="card">
      ${stepHeader(1)}
      <div class="progress-dots">${dots}</div>
      <h2>句子 ${i + 1} / ${session.shadowSet.length}</h2>
      <p class="desc">先点「听读音」体会语调 → 再点「我来跟读」模仿着说 → 目标 85 分以上（录音由服务器 Whisper 精确识别，不用担心生僻表达被浏览器听错）。语调是情感的载体，重点模仿提示里的处理方式。</p>
      <div class="en-line">${esc(item.en)}</div>
      <div class="zh-line">${esc(item.zh)}</div>
      <div class="focus-line">语调提示：${esc(item.focus)}</div>
      <div>
        <button class="btn secondary" id="btnPlay">听读音</button>
        <button class="btn" id="btnRec">我来跟读</button>
        <button class="btn ghost" id="btnSkip">${i + 1 < session.shadowSet.length ? "跳过这句" : "跳过，进入话题表达"}</button>
      </div>
      <div id="shadowOut"></div>
    </div>`;
  $("#btnPlay").onclick = () => speak(item.en);
  const recHandler = async () => {
    const out = () => $("#shadowOut");
    const rec = () => $("#btnRec");
    const resetRecBtn = () => morphButton(rec(), "我来跟读", "", recHandler);
    const showResult = (text, sc, src, pitchVar) => {
      resetRecBtn();
      if (!out()) return;
      const cls = sc >= 85 ? "good" : sc >= 60 ? "mid" : "low";
      const tip = sc >= 85 ? "很好，可以进入下一句" : "再听一遍读音，重点模仿语调后重试";
      const srcLabel = src === "whisper" ? "Whisper 精确识别 · 你说的是" : "浏览器识别（可能不准）· 你说的是";
      let pitchHtml = "";
      if (pitchVar != null) {
        const [pLabel, pCls, pTip] = pitchVar < 1.8
          ? ["偏平直", "low", "语调像一条直线——试着夸张地把提示里的重音和升降调读出来"]
          : pitchVar < 3.2
            ? ["适中", "mid", "有起伏了，可以再大胆一点，突出关键词"]
            : ["丰富", "good", "语调起伏很自然，保持住"];
        pitchHtml = `<div style="margin-top:6px" class="muted-sm">语调起伏：<span class="score ${pCls}" style="font-size:15px">${pitchVar}</span> 半音 · ${pLabel}——${pTip}</div>`;
      }
      out().innerHTML = `
        <div class="result-box">
          <span class="label">${srcLabel}</span>${esc(text)}
          <div style="margin-top:10px"><span class="score ${cls}">${sc}</span> 分 · ${tip}</div>
          ${pitchHtml}
          <div style="margin-top:10px">
            <button class="btn secondary" id="btnAgain">再试一次</button>
            <button class="btn" id="btnNext">${i + 1 < session.shadowSet.length ? "下一句" : "进入话题表达"}</button>
          </div>
        </div>`;
      $("#btnAgain").onclick = () => renderShadow();
      $("#btnNext").onclick = () => { session.scores.push(sc); advanceShadow(); };
    };
    morphButton(rec(), "准备麦克风……", "", null, true);
    out().innerHTML = `<div class="rec-live"><div class="rec-dot"></div>正在准备麦克风……</div>`;
    const recOk = await startRecording();
    if (!recOk) {
      if (!SR) {
        resetRecBtn();
        out().innerHTML = `<div class="result-box">无法使用麦克风，请在浏览器地址栏允许麦克风权限后重试。</div>`;
        return;
      }
      morphButton(rec(), "正在听……", "", null, true);
      out().innerHTML = `<div class="rec-live"><div class="rec-dot"></div>正在听你说……（浏览器识别）</div>`;
      recognizeOnce((text) => {
        if (!text) { resetRecBtn(); if (out()) out().innerHTML = `<div class="result-box">没有听清，请再试一次。</div>`; return; }
        showResult(text, shadowScore(item.en, text), "browser");
      });
      return;
    }
    let done = false;
    const finish = async () => {
      if (done) return;
      done = true;
      stopVad();
      morphButton(rec(), "识别中……", "", null, true);
      const blob = await stopRecording();
      if (!out()) return;
      out().innerHTML = `<div class="result-box"><div class="muted-sm">Whisper 精确识别中……（约 2-5 秒）</div></div>`;
      if (blob) {
        try {
          const resp = await api(`/api/shadow-score?target=${encodeURIComponent(item.en)}`, { method: "POST", blob, mime: blob.type });
          if (resp.score != null) return showResult(resp.transcript, resp.score, "whisper", resp.pitch_var);
        } catch (_) {}
      }
      resetRecBtn();
      if (out()) out().innerHTML = `<div class="result-box">没有录到声音或识别失败，请再试一次（离麦克风近一点）。</div>`;
    };
    /* 同一个按钮原地变成「我说完了」：手不用挪，说完直接点；停顿 2 秒也会自动结束 */
    morphButton(rec(), "■ 我说完了", "stop", finish);
    out().innerHTML = `
      <div class="rec-live"><div class="rec-dot"></div>正在录音……读完整句后点「我说完了」，或<b>停顿 2 秒</b>自动结束（逗号处换气不会截断）</div>`;
    startVad(recorder.stream, finish);
  };
  $("#btnRec").onclick = recHandler;
  $("#btnSkip").onclick = () => advanceShadow();
}
function advanceShadow() {
  stopAllRec();
  discardRecording();
  if (session.shadowIdx + 1 < session.shadowSet.length) { session.shadowIdx++; renderShadow(); }
  else { session.step = 2; renderDaily(); }
}

/* ---- 步骤 2：话题 4-3-2 ---- */
function renderTopic() {
  if (!session.topic) return renderTopicPick();
  const t = session.topic;
  const r = session.round;
  if (r >= ROUNDS.length) { session.step = 3; return renderDaily(); }
  const doneRounds = session.rounds.map((rd, k) =>
    `<div class="stat"><div class="num">${rd.wpm}</div><div class="cap">${ROUNDS[k].label.split(" · ")[0]} WPM</div></div>`).join("");
  view.innerHTML = `
    <div class="card">
      ${stepHeader(2)}
      <h2>${esc(TOPICS[session.cat].label)}话题 · ${ROUNDS[r].label}</h2>
      <p class="desc">同一话题连说 3 轮、时间递减：第 1 轮找思路，第 2 轮说得更顺，第 3 轮逼自己提速。每轮结束后 AI 会对你的表达打分并给出改进建议。</p>
      <div class="en-line">${esc(t.q)}</div>
      <div class="zh-line">${esc(t.zh)}</div>
      <div class="hints">${t.hints.map((h) => `<span class="hint">${esc(h)}</span>`).join("")}</div>
      <div class="timer" id="timer">${fmt(ROUNDS[r].sec)}</div>
      <div>
        <button class="btn" id="btnStart">开始${ROUNDS[r].label.split(" · ")[0]}（开始说话）</button>
        <button class="btn ghost" id="btnChange">换个话题</button>
      </div>
      <div id="live"></div>
      ${doneRounds ? `<div class="stats-row">${doneRounds}</div>` : ""}
    </div>`;
  const roundSec = ROUNDS[r].sec;
  let startAt = null;
  let finished = false;
  $("#btnChange").onclick = () => { session.topic = null; session.round = 0; session.rounds = []; renderTopic(); };
  const startLabel = `开始${ROUNDS[r].label.split(" · ")[0]}（开始说话）`;
  const startHandler = async () => {
    morphButton($("#btnStart"), "准备麦克风……", "", null, true);
    const recOk = await startRecording();
    let srOk = false;
    if (SR) srOk = startDictation((fin, inter) => {
      $("#live").innerHTML = `
        <div class="rec-live"><div class="rec-dot"></div>正在${recOk ? "录音并" : ""}记录，请持续说英文……</div>
        <div class="result-box"><span class="label">实时转写</span>${esc(fin)}<i style="color:#999">${esc(inter)}</i></div>`;
    });
    if (!srOk && !recOk) {
      morphButton($("#btnStart"), startLabel, "", startHandler);
      alert("录音和语音识别都不可用。请在浏览器地址栏允许麦克风权限后重试。");
      return;
    }
    if (!srOk) {
      dict.final = "";
      $("#live").innerHTML = `
        <div class="rec-live"><div class="rec-dot"></div>正在录音……（本机无实时转写，结束后由服务器 Whisper 精确转写并评分）</div>`;
    }
    startAt = Date.now();
    /* 「开始」按钮原地变成「我说完了」，鼠标不用挪；时间到也会自动结束 */
    morphButton($("#btnStart"), "■ 我说完了", "stop", finishRound);
    $("#btnChange").classList.add("hidden");
    let left = roundSec;
    timerId = setInterval(() => {
      left--;
      const tEl = $("#timer");
      if (!tEl) return clearTimer();
      tEl.textContent = fmt(left);
      if (left <= 10) tEl.classList.add("warning");
      if (left <= 0) finishRound();
    }, 1000);
  };
  $("#btnStart").onclick = startHandler;
  async function finishRound() {
    if (finished) return;
    finished = true;
    clearTimer();
    morphButton($("#btnStart"), "整理中……", "", null, true);
    const text = stopDictation();
    const blob = await stopRecording();
    const elapsed = Math.max(5, Math.round((Date.now() - startAt) / 1000));
    const words = normWords(text).length;
    const wpm = Math.round((words / elapsed) * 60);
    const fillers = countFillers(text);
    const audioUrl = blob ? URL.createObjectURL(blob) : null;
    const roundObj = { text, words, wpm, fillers, sec: elapsed, audioUrl };
    session.rounds.push(roundObj);
    session.round++;
    const isLast = session.round >= ROUNDS.length;
    view.innerHTML = `
      <div class="card">
        ${stepHeader(2)}
        <h2>${ROUNDS[session.round - 1].label} 完成</h2>
        <div class="stats-row">
          <div class="stat"><div class="num" id="stWords">${words}</div><div class="cap">单词数</div></div>
          <div class="stat"><div class="num" id="stWpm">${wpm}</div><div class="cap">语速 WPM（目标 120+）</div></div>
          <div class="stat"><div class="num" id="stFillers">${fillers}</div><div class="cap">填充词 um/uh/like…</div></div>
        </div>
        <div id="aiBox" class="ai-box"><div class="ai-loading">Whisper 精确转写 + AI 评分中……（约 20-60 秒；可先回听录音，或直接开始下一轮，结果会自动存入历史）</div></div>
        ${audioUrl ? `<div class="result-box"><span class="label">回听录音（已自动上传到你的账号）</span><audio controls src="${audioUrl}"></audio></div>` : `<p class="muted-sm">本轮没有生成录音（浏览器可能不支持同时录音，转写不受影响）。</p>`}
        <div class="result-box" id="roundBox"><span class="label">你的转写（浏览器实时识别，稍后会被 Whisper 精确转写替换）</span>${highlightFillers(text) || "（没有识别到内容）"}</div>
        <div style="margin-top:12px">
          <button class="btn" id="btnGo">${isLast ? "进入复盘" : `开始${ROUNDS[session.round].label.split(" · ")[0]}（同一话题，再说一遍）`}</button>
          ${!isLast ? `<button class="btn ghost" id="btnJump">跳过剩余轮次，直接复盘</button>` : ""}
        </div>
      </div>`;
    $("#btnGo").onclick = () => { if (isLast) { session.step = 3; renderDaily(); } else renderTopic(); };
    const j = $("#btnJump");
    if (j) j.onclick = () => { session.step = 3; renderDaily(); };

    /* 上传录音 + AI 评分（异步，不阻塞界面） */
    let recordingId = null;
    if (blob) {
      try {
        const meta = { date: todayStr(), cat: TOPICS[session.cat].label, topic: t.q, round: r + 1, sec: elapsed, wpm };
        const resp = await api(`/api/recordings?meta=${encodeURIComponent(JSON.stringify(meta))}`, { method: "POST", blob, mime: blob.type });
        recordingId = resp.id;
      } catch (e) { console.warn("录音上传失败：", e); }
    }
    try {
      const resp = await api("/api/score", {
        json: {
          date: todayStr(), cat: TOPICS[session.cat].label, topic: t.q, round: r + 1,
          transcript: text, wpm, words, fillers, sec: elapsed, recording_id: recordingId,
        },
      });
      if (resp.asr === "whisper" && resp.transcript) {
        Object.assign(roundObj, { text: resp.transcript, wpm: resp.wpm, words: resp.words, fillers: resp.fillers });
        const tb = $("#roundBox");
        if (tb) tb.innerHTML = `<span class="label">你的转写（Whisper 精确转写 · 填充词已标黄）</span>${highlightFillers(resp.transcript)}`;
        for (const [id, v] of [["stWords", resp.words], ["stWpm", resp.wpm], ["stFillers", resp.fillers]]) {
          const el = $("#" + id);
          if (el) el.textContent = v;
        }
      }
      renderAiBox(resp);
    } catch (e) {
      const box = $("#aiBox");
      if (box) box.innerHTML = `<div class="ai-loading">AI 评分失败（${esc(e.message)}），本轮数据已保存。</div>`;
    }
  }
}

function aiDetailHtml(ai) {
  const dimName = { fluency: "流利度", vocabulary: "词汇", grammar: "语法", content: "内容" };
  return `
    <div class="ai-head">
      <div class="ai-score">${ai.score}<span>分</span></div>
      <div class="ai-dims">
        ${Object.entries(ai.dims || {}).map(([k, v]) => `
          <div class="ai-dim"><span class="ai-dim-name">${dimName[k] || k}</span>
            <div class="ai-dim-bar"><div style="width:${v}%"></div></div>
            <span class="ai-dim-val">${v}</span>
          </div>`).join("")}
      </div>
    </div>
    ${ai.comment_zh ? `<p class="ai-comment">${esc(ai.comment_zh)}</p>` : ""}
    ${(ai.fixes || []).length ? `
      <div class="ai-sec">改进原话</div>
      ${ai.fixes.map((f) => `
        <div class="ai-fix">
          <div class="ai-fix-orig">${esc(f.original || "")}</div>
          <div class="ai-fix-better">${esc(f.better || "")}</div>
          ${f.why_zh ? `<div class="ai-fix-why">${esc(f.why_zh)}</div>` : ""}
        </div>`).join("")}` : ""}
    ${(ai.upgrade || []).length ? `
      <div class="ai-sec">可以用上的更地道表达</div>
      ${ai.upgrade.map((u) => `
        <div class="ai-fix">
          <div class="ai-fix-better">${esc(u.expression || "")}</div>
          ${u.usage_zh ? `<div class="ai-fix-why">${esc(u.usage_zh)}</div>` : ""}
        </div>`).join("")}` : ""}
    ${ai.model ? `
      <div class="ai-sec">口语范本 · 用地道英语说出你想表达的内容</div>
      <div class="ai-model">
        <p>${esc(ai.model)}</p>
        <button class="btn secondary model-play" data-text="${encodeURIComponent(ai.model)}">朗读范本</button>
        <span class="muted-sm" style="margin-left:8px">建议：听 2 遍 → 跟读 1 遍 → 合上文字自己说 1 遍</span>
      </div>` : ""}`;
}

/* 朗读按钮：事件委托，覆盖评分页/历史/广场/对话所有位置 */
document.addEventListener("click", (e) => {
  const b = e.target.closest(".model-play, .msg-play");
  if (b) speak(decodeURIComponent(b.dataset.text));
});

/* ============ Tab：AI 对话（带长期记忆的陪聊） ============ */
let chatBusy = false;

async function downscaleImage(file, maxDim = 1600) {
  try {
    const bmp = await createImageBitmap(file);
    const scale = Math.min(1, maxDim / Math.max(bmp.width, bmp.height));
    if (scale >= 1 && file.type === "image/jpeg") return file;
    const canvas = document.createElement("canvas");
    canvas.width = Math.round(bmp.width * scale);
    canvas.height = Math.round(bmp.height * scale);
    canvas.getContext("2d").drawImage(bmp, 0, 0, canvas.width, canvas.height);
    const blob = await new Promise((res) => canvas.toBlob(res, "image/jpeg", 0.85));
    return blob || file;
  } catch (_) { return file; }
}

function chatBubble(role, content, fix, typedNote, photoSrc) {
  if (role === "user") return `
    <div class="msg user">
      <div class="bubble">${photoSrc ? `<img class="bubble-photo" src="${photoSrc}" alt="photo">` : ""}${esc(content)}</div>
      ${typedNote ? `<div class="typed-note">键入补充：${esc(typedNote)}</div>` : ""}
    </div>`;
  return `
    <div class="msg ai">
      <div class="bubble">${esc(content)}<button class="play-mini msg-play" data-text="${encodeURIComponent(content)}">朗读</button></div>
      ${fix ? `<div class="chat-fix"><span class="cf-orig">${esc(fix.original || "")}</span> → <span class="cf-better">${esc(fix.better || "")}</span>${fix.why_zh ? `<div class="cf-why">${esc(fix.why_zh)}</div>` : ""}</div>` : ""}
    </div>`;
}

async function renderChat() {
  if (!AUTH.user) return renderAuth();
  view.innerHTML = `
    <div class="card">
      <div class="chat-head">
        <div>
          <h2>AI 对话 · Buddy</h2>
          <p class="desc">不限时的自由聊天，不计入每日打卡。Buddy 有长期记忆——你们聊过的事、你的名字和近况它都记得，隔几天回来接着聊也没问题。</p>
        </div>
        <div class="chat-head-right">
          <label class="rate-label hf-label"><input type="checkbox" id="handsFree"> 免提连聊</label>
          <label class="rate-label">纠错力度
            <select id="fixLevel">
              <option value="light">轻度</option>
              <option value="standard">标准</option>
              <option value="strict">严格</option>
            </select>
          </label>
          <button class="link-btn" id="chatClear">清空对话</button>
        </div>
      </div>
      <div class="chat-box" id="chatBox"><p class="desc">加载中……</p></div>
      <div class="photo-stage hidden" id="photoStage"></div>
      <div class="chat-input">
        <button class="btn secondary" id="btnPhoto" title="发送图片">图</button>
        <input type="file" id="photoFile" accept="image/*" class="hidden">
        <button class="btn" id="btnTalk">按一下说话</button>
        <input id="chatText" placeholder="打字发送；录音时输入的内容作为关键词随语音发出" autocomplete="off">
        <button class="btn secondary" id="btnSend">发送</button>
      </div>
      <div class="muted-sm" id="chatHint">语音：点「按一下说话」，说完停顿 3.5 秒自动发送。开「免提连聊」后 Buddy 说完会自动听你说，戴耳机可全程不碰屏幕。点「图」发照片给 Buddy。</div>
    </div>`;
  const box = () => $("#chatBox");
  const scrollDown = () => { const b = box(); if (b) b.scrollTop = b.scrollHeight; };
  const append = (html) => { const b = box(); if (!b) return; b.insertAdjacentHTML("beforeend", html); scrollDown(); };

  $("#chatClear").onclick = async () => {
    const pw = prompt(
      "清空对话（短期记忆）：\nBuddy 会先自动把重要信息提炼进长期记忆（可在「长期记忆」页查看），然后删除全部对话记录。\n\n请输入登录密码确认："
    );
    if (!pw) return;
    try {
      const r = await api("/api/chat", { method: "DELETE", json: { password: pw } });
      alert(r.memory_kept ? "对话已清空，长期记忆已提炼保留。" : "对话已清空。");
      renderChat();
    } catch (e) { alert("清空失败：" + e.message); }
  };

  const fixSel = $("#fixLevel");
  fixSel.value = (AUTH.user && AUTH.user.chat_fix_level) || "standard";
  fixSel.onchange = async () => {
    const v = fixSel.value;
    try {
      await api("/api/settings", { json: { chat_fix_level: v } });
      if (AUTH.user) AUTH.user.chat_fix_level = v;
      const names = { light: "轻度：只纠影响理解的错误", standard: "标准：每轮最多一条值得学的纠错", strict: "严格：只要有错就必须纠" };
      const hint = $("#chatHint");
      if (hint) hint.textContent = "纠错力度已切换为「" + names[v] + "」，下一条消息开始生效。";
    } catch (e) { alert("设置失败：" + e.message); }
  };

  /* 免提连聊开关 */
  let handsFree = load("sg_handsfree", false);
  const hfBox = $("#handsFree");
  hfBox.checked = handsFree;
  hfBox.onchange = () => {
    handsFree = hfBox.checked;
    save("sg_handsfree", handsFree);
    const hint = $("#chatHint");
    if (hint) hint.textContent = handsFree
      ? "免提连聊已开启：Buddy 说完会自动开始听你说，戴上耳机聊起来吧。点一次「按一下说话」开始第一轮。"
      : "免提连聊已关闭。";
  };

  /* 图片暂存 */
  let pendingPhoto = null;
  const renderStage = () => {
    const s = $("#photoStage");
    if (!s) return;
    s.classList.toggle("hidden", !pendingPhoto);
    s.innerHTML = pendingPhoto
      ? `<img src="${pendingPhoto.url}" alt=""><span>图片已就绪：说话或打字描述它，随下一条消息发送</span><button class="note-del" id="stageRemove">移除</button>`
      : "";
    const r = $("#stageRemove");
    if (r) r.onclick = async () => {
      try { await api(`/api/photos/${pendingPhoto.id}`, { method: "DELETE" }); } catch (_) {}
      pendingPhoto = null;
      renderStage();
    };
  };
  $("#btnPhoto").onclick = () => $("#photoFile").click();
  $("#photoFile").onchange = async (e) => {
    const f = e.target.files[0];
    e.target.value = "";
    if (!f) return;
    const hint = $("#chatHint");
    if (hint) hint.textContent = "图片压缩上传中……";
    const blob = await downscaleImage(f);
    try {
      const resp = await api("/api/photos", { method: "POST", blob, mime: blob.type || "image/jpeg" });
      pendingPhoto = { id: resp.id, url: URL.createObjectURL(blob) };
      renderStage();
      if (hint) hint.textContent = "图片已就绪：现在说话或打字描述它，会随下一条消息一起发给 Buddy 并存入图库。";
    } catch (err) {
      if (hint) hint.textContent = "图片上传失败：" + err.message;
    }
  };

  try {
    const h = await api("/api/chat/history");
    if (!box()) return;
    box().innerHTML = h.items.length
      ? h.items.map((m) => {
          let fix = null;
          try { fix = m.fix_json ? JSON.parse(m.fix_json) : null; } catch (_) {}
          const src = m.photo_id ? `/api/photo/${m.photo_id}?t=${encodeURIComponent(AUTH.token)}` : null;
          return chatBubble(m.role, m.content, fix, m.typed_note, src);
        }).join("")
      : chatBubble("ai", "Hey! I'm Buddy, your English chat partner. Tell me about your day — or anything on your mind. What's up?", null);
    scrollDown();
  } catch (e) {
    if (box()) box().innerHTML = `<p class="desc">历史加载失败：${esc(e.message)}</p>`;
  }

  async function handleReply(promise, placeholderId, note) {
    let reply = null;
    try {
      const resp = await promise;
      const ph = document.getElementById(placeholderId);
      if (resp.user_text === "") {
        if (ph) ph.remove();
        const hint = $("#chatHint");
        if (hint) hint.textContent = "没有听清，请再试一次（离麦克风近一点）。";
        if (note) { const input = $("#chatText"); if (input && !input.value) input.value = note; }
        chatBusy = false;
        if (handsFree && $("#chatBox")) setTimeout(() => { if (!talking && !chatBusy) startTalk(); }, 600);
        return;
      }
      if (resp.user_text && ph) ph.outerHTML = chatBubble("user", resp.user_text, null, resp.typed_note || note, resp._photoUrl);
      else if (ph) ph.remove();
      append(chatBubble("ai", resp.reply, resp.fix));
      if (resp.memory_added) append(`<div class="msg ai"><div class="chat-note">已记入长期记忆：${esc(resp.memory_added)}</div></div>`);
      if (resp.word_added) append(`<div class="msg ai"><div class="chat-note">📒 ${resp.word_added.new ? "已加入单词本" : "单词本已有此词，释义已更新"}：<b>${esc(resp.word_added.word)}</b>${resp.word_added.meaning_zh ? " · " + esc(resp.word_added.meaning_zh) : ""}<button class="link-btn goto-words">去复习</button></div></div>`);
      const gw = box() && box().querySelector(".goto-words:last-of-type");
      if (gw) gw.onclick = () => { memSub = "words"; document.querySelector('#tabs button[data-tab="memory"]').click(); };
      if (resp.reminder_set) {
        const needPush = !("Notification" in window) || Notification.permission !== "granted";
        append(`<div class="msg ai"><div class="chat-note">已设置提醒：${esc(resp.reminder_set.label)} · ${esc(resp.reminder_set.text)}${needPush ? `<button class="link-btn note-push-btn">开启通知</button>` : ""}</div></div>`);
        const pb = box() && box().querySelector(".note-push-btn:last-of-type");
        if (pb) pb.onclick = async () => { if (await enablePush()) pb.replaceWith("（通知已开启）"); };
      }
      reply = resp.reply;
    } catch (e) {
      const ph = document.getElementById(placeholderId);
      if (ph) ph.outerHTML = `<div class="msg ai"><div class="bubble">（${esc(e.message)}）</div></div>`;
      if (note) { const input = $("#chatText"); if (input && !input.value) input.value = note; }
      chatBusy = false;
      return;
    }
    chatBusy = false;
    await speak(reply);
    if (handsFree && $("#chatBox") && !talking && !chatBusy) startTalk();
  }

  const sendText = () => {
    if (talking) {
      const hint = $("#chatHint");
      if (hint) hint.textContent = "正在录音：输入框里的内容会作为关键词随这段语音一起发送，无需单独发。";
      return;
    }
    const input = $("#chatText");
    const text = input.value.trim();
    if (!text || chatBusy) return;
    chatBusy = true;
    input.value = "";
    const photo = pendingPhoto;
    pendingPhoto = null;
    renderStage();
    append(chatBubble("user", text, null, null, photo ? photo.url : null));
    const pid = "ph" + Date.now();
    append(`<div class="msg ai" id="${pid}"><div class="bubble thinking">Buddy 正在输入…</div></div>`);
    handleReply(api("/api/chat/send", { json: { text, photo_id: photo ? photo.id : null } }), pid);
  };
  $("#btnSend").onclick = sendText;
  $("#chatText").addEventListener("keydown", (e) => { if (e.key === "Enter") sendText(); });

  let talking = false;
  let finishTalk = null;
  const startTalk = async () => {
    if (chatBusy || talking || !$("#chatBox")) return;
    const recOk = await startRecording();
    if (!recOk) { alert("无法使用麦克风，请在浏览器地址栏允许麦克风权限。"); return; }
    talking = true;
    const btn = $("#btnTalk");
    if (btn) { btn.textContent = "说完了（点击立即发送）"; btn.classList.add("recording"); }
    const defaultHint = "正在录音……说完停顿 3.5 秒自动发送；思考时的停顿不用慌，继续说就行。";
    const hintEl = () => $("#chatHint");
    if (hintEl()) hintEl().textContent = defaultHint;
    let done = false;
    finishTalk = async () => {
      if (done) return;
      done = true;
      talking = false;
      stopVad();
      const blob = await stopRecording();
      const b2 = $("#btnTalk");
      if (b2) { b2.textContent = "按一下说话"; b2.classList.remove("recording"); }
      if (hintEl()) hintEl().textContent = handsFree ? "识别中……（免提模式：回复播完后会自动继续听）" : "语音：点「按一下说话」开始，说完停顿 3.5 秒自动发送。";
      if (!blob) return;
      chatBusy = true;
      const noteInput = $("#chatText");
      const note = noteInput ? noteInput.value.trim() : "";
      if (noteInput) noteInput.value = "";
      const photo = pendingPhoto;
      pendingPhoto = null;
      renderStage();
      const pid = "ph" + Date.now();
      append(`<div class="msg user" id="${pid}"><div class="bubble thinking">（识别中…）</div></div>`);
      const params = new URLSearchParams();
      if (note) params.set("note", note);
      if (photo) params.set("photo_id", photo.id);
      const qs = params.toString();
      const p = api("/api/chat/voice" + (qs ? "?" + qs : ""), { method: "POST", blob, mime: blob.type });
      const wrapped = p.then((resp) => { if (photo) resp._photoUrl = photo.url; return resp; });
      handleReply(wrapped, pid, note);
    };
    startVad(recorder.stream, finishTalk, {
      silenceMs: 3500,
      maxMs: 120000,
      onSilence: (left) => {
        const h = hintEl();
        if (!h) return;
        h.textContent = left == null ? defaultHint
          : `检测到停顿，${(left / 1000).toFixed(1)} 秒后自动发送——继续说话即可取消。`;
      },
    });
  };
  $("#btnTalk").onclick = () => {
    if (talking) { if (finishTalk) finishTalk(); return; }
    startTalk();
  };
}

/* ============ Web Push 订阅 ============ */
function urlB64ToUint8(s) {
  const pad = "=".repeat((4 - (s.length % 4)) % 4);
  const b = atob((s + pad).replace(/-/g, "+").replace(/_/g, "/"));
  return Uint8Array.from([...b].map((c) => c.charCodeAt(0)));
}

async function enablePush() {
  if (!("serviceWorker" in navigator) || !("PushManager" in window) || !("Notification" in window)) {
    alert("此浏览器不支持推送通知。iPhone 需将本应用添加到主屏幕后在 App 内开启（iOS 16.4+）。");
    return false;
  }
  const perm = await Notification.requestPermission();
  if (perm !== "granted") { alert("通知权限未授予。"); return false; }
  try {
    const reg = await navigator.serviceWorker.ready;
    const { key } = await api("/api/push/key");
    const sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: urlB64ToUint8(key) });
    await api("/api/push/subscribe", { json: { subscription: sub.toJSON() } });
    return true;
  } catch (e) {
    alert("订阅失败：" + e.message);
    return false;
  }
}

/* ============ Tab：长期记忆（记忆笔记 + 提醒 + 图库） ============ */
let memSub = "notes";
async function renderMemory() {
  if (!AUTH.user) return renderAuth();
  view.innerHTML = `
    <div class="card">
      <h2>长期记忆</h2>
      <p class="desc">Buddy 关于你的持久记忆：对话攒多了自动归纳；清空对话时也会先提炼保留。单词本、图库同属长期记忆，不受对话清空影响。</p>
      <div class="pill-row">
        <button class="pill ${memSub === "notes" ? "active" : ""}" data-sub="notes">记忆笔记</button>
        <button class="pill ${memSub === "words" ? "active" : ""}" data-sub="words">单词本</button>
        <button class="pill ${memSub === "reminders" ? "active" : ""}" data-sub="reminders">提醒</button>
        <button class="pill ${memSub === "gallery" ? "active" : ""}" data-sub="gallery">图库</button>
      </div>
      <div id="memBody"><p class="desc">加载中……</p></div>
    </div>`;
  view.querySelectorAll(".pill[data-sub]").forEach((p) => {
    p.onclick = () => { memSub = p.dataset.sub; renderMemory(); };
  });
  if (memSub === "notes") renderMemoryNotes($("#memBody"));
  else if (memSub === "words") renderWords($("#memBody"));
  else if (memSub === "reminders") renderReminders($("#memBody"));
  else renderGalleryInto($("#memBody"));
}

/* ---- 单词本：对话中问到的生词自动收录 + 手动添加 + 间隔复习 ---- */
function wordDueLabel(w, now) {
  if (!w.reviews) return "新词 · 待首次复习";
  const days = Math.ceil((w.due - now) / 86400);
  return w.due <= now ? "到期，该复习了" : days <= 1 ? "明天复习" : `${days} 天后复习`;
}
async function renderWords(root) {
  let d;
  try { d = await api("/api/words"); } catch (e) {
    root.innerHTML = `<p class="desc">加载失败：${esc(e.message)}</p>`;
    return;
  }
  const items = d.items || [];
  const filterKey = (root.dataset.filter || "").toLowerCase();
  const shown = filterKey ? items.filter((w) => (w.word + " " + (w.meaning_zh || "") + " " + (w.meaning_en || "")).toLowerCase().includes(filterKey)) : items;
  root.innerHTML = `
    <p class="desc">在对话里问 Buddy "<i>xxx 是什么意思</i>" 或 "<i>how do you say … in English</i>"，它会自动把生词记到这里。复习按间隔重复安排：认识 → 间隔拉长（1/2/4/7/15/30/60 天），忘了 → 6 小时后再来。</p>
    <div class="stats-row">
      <div class="stat"><div class="num">${items.length}</div><div class="cap">收录单词</div></div>
      <div class="stat"><div class="num" style="color:${d.due ? "var(--danger)" : "inherit"}">${d.due}</div><div class="cap">今日待复习</div></div>
      <div class="stat"><div class="num">${items.filter((w) => w.streak >= 3).length}</div><div class="cap">已基本掌握（连对 3 次+）</div></div>
    </div>
    <div style="margin:12px 0">
      <button class="btn" id="btnQuizFlash" ${items.length ? "" : "disabled"}>抽词复习（看词想义）${d.due ? ` · ${d.due} 个到期` : ""}</button>
      <button class="btn secondary" id="btnQuizRecall" ${items.length ? "" : "disabled"}>拼写背诵（看义写词）</button>
    </div>
    <div class="word-add">
      <input id="wordNew" placeholder="手动添加：输入英文单词/短语（释义可留空，AI 自动补全）" maxlength="60">
      <input id="wordNewZh" placeholder="中文释义（可选）" maxlength="60">
      <button class="btn secondary" id="btnWordAdd">添加</button>
    </div>
    <div class="word-add" style="margin-top:6px">
      <input id="wordFilter" placeholder="搜索单词本…" value="${esc(root.dataset.filter || "")}">
    </div>
    ${shown.length ? shown.map((w) => `
      <div class="word-item" data-id="${w.id}">
        <div class="word-main">
          <div class="word-head"><b class="word-text">${esc(w.word)}</b><button class="link-btn word-say" data-w="${esc(w.word)}">🔊</button>
            <span class="word-streak">${"●".repeat(Math.min(w.streak, 5))}${"○".repeat(Math.max(0, 5 - Math.min(w.streak, 5)))}</span></div>
          <div class="word-zh">${esc(w.meaning_zh || "")}${w.meaning_en ? ` <span class="muted-sm">· ${esc(w.meaning_en)}</span>` : ""}</div>
          ${w.example ? `<div class="word-ex">${esc(w.example)}</div>` : ""}
          <div class="muted-sm">${w.date} 收录${w.source === "chat" ? "（对话中）" : ""} · 复习 ${w.reviews} 次 · ${wordDueLabel(w, d.now)}</div>
        </div>
        <button class="note-del word-del" data-id="${w.id}">删除</button>
      </div>`).join("") : `<p class="desc">${items.length ? "没有匹配的单词。" : "单词本还是空的。去和 Buddy 聊天时问问不认识的词，或在上面手动添加。"}</p>`}`;
  $("#btnQuizFlash").onclick = () => startWordQuiz("flash");
  $("#btnQuizRecall").onclick = () => startWordQuiz("recall");
  const addWord = async () => {
    const word = $("#wordNew").value.trim();
    if (!word) return;
    $("#btnWordAdd").disabled = true;
    $("#btnWordAdd").textContent = "添加中…";
    try {
      await api("/api/words", { json: { word, meaning_zh: $("#wordNewZh").value.trim() } });
      renderWords(root);
    } catch (e) { alert("添加失败：" + e.message); $("#btnWordAdd").disabled = false; $("#btnWordAdd").textContent = "添加"; }
  };
  $("#btnWordAdd").onclick = addWord;
  $("#wordNew").onkeydown = (e) => { if (e.key === "Enter") addWord(); };
  $("#wordFilter").oninput = (e) => { root.dataset.filter = e.target.value; renderWords(root); };
  if (filterKey) { const f = $("#wordFilter"); f.focus(); f.setSelectionRange(f.value.length, f.value.length); }
  root.querySelectorAll(".word-say").forEach((b) => { b.onclick = () => speak(b.dataset.w); });
  root.querySelectorAll(".word-del").forEach((b) => {
    b.onclick = async () => {
      if (!confirm("删除这个单词？")) return;
      try { await api(`/api/words/${b.dataset.id}`, { method: "DELETE" }); renderWords(root); }
      catch (e) { alert("删除失败：" + e.message); }
    };
  });
}

/* 复习：flash = 看词想义（自评）；recall = 看中文/例句填空写出单词（自动判分） */
async function startWordQuiz(mode) {
  let d;
  try { d = await api("/api/words/quiz?n=10"); } catch (e) { alert("加载失败：" + e.message); return; }
  const items = d.items || [];
  if (!items.length) { alert("单词本还是空的。"); return; }
  const quiz = { mode, items, idx: 0, tally: { know: 0, unsure: 0, forgot: 0 } };
  renderWordQuiz(quiz);
}
function blankOut(example, word) {
  if (!example) return "";
  const re = new RegExp(word.replace(/[.*+?^${}()|[\]\\]/g, "\\$&").replace(/\s+/g, "\\s+"), "ig");
  const blanked = example.replace(re, "_____");
  return blanked === example ? example.replace(new RegExp(word.split(/\s+/)[0].replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + "\\w*", "ig"), "_____") : blanked;
}
function renderWordQuiz(quiz) {
  const { mode, items, idx, tally } = quiz;
  if (idx >= items.length) {
    view.innerHTML = `
      <div class="card">
        <span class="step-tag">单词复习 · 完成</span>
        <h2>这一轮复习结束</h2>
        <div class="stats-row">
          <div class="stat"><div class="num" style="color:var(--accent)">${tally.know}</div><div class="cap">认识 / 拼对</div></div>
          <div class="stat"><div class="num">${tally.unsure}</div><div class="cap">模糊</div></div>
          <div class="stat"><div class="num" style="color:var(--danger)">${tally.forgot}</div><div class="cap">忘了 / 拼错</div></div>
        </div>
        <p class="desc">认识的词会被推到更远的日期再考；忘了的 6 小时后就会再出现。每天来一轮，单词本会自己"变薄"。</p>
        <div style="margin-top:12px">
          <button class="btn" id="btnQuizMore">再来一轮</button>
          <button class="btn secondary" id="btnQuizBack">返回单词本</button>
        </div>
      </div>`;
    $("#btnQuizMore").onclick = () => startWordQuiz(mode);
    $("#btnQuizBack").onclick = () => { memSub = "words"; renderMemory(); };
    return;
  }
  const w = items[idx];
  const progress = `${idx + 1} / ${items.length}`;
  const grade = async (result) => {
    tally[result]++;
    try { await api(`/api/words/${w.id}/review`, { json: { result } }); } catch (_) {}
    quiz.idx++;
    renderWordQuiz(quiz);
  };
  const detail = `
    <div class="word-reveal">
      <div class="word-zh" style="font-size:18px">${esc(w.meaning_zh || "（无中文释义）")}</div>
      ${w.meaning_en ? `<div class="muted-sm" style="margin-top:4px">${esc(w.meaning_en)}</div>` : ""}
      ${w.example ? `<div class="word-ex" style="margin-top:8px">${esc(w.example)}</div>` : ""}
    </div>`;
  if (mode === "flash") {
    view.innerHTML = `
      <div class="card">
        <span class="step-tag">单词复习 · 看词想义 · ${progress}</span>
        <div class="flashcard">
          <div class="flash-word">${esc(w.word)} <button class="link-btn" id="btnSay" style="font-size:22px">🔊</button></div>
          <div class="muted-sm">先在心里说出意思和一个例句，再翻开</div>
          <div id="flashBack" class="hidden">${detail}</div>
        </div>
        <div style="margin-top:14px" id="flashActions">
          <button class="btn" id="btnFlip">翻开看释义</button>
          <button class="btn ghost" id="btnQuizQuit">退出</button>
        </div>
      </div>`;
    $("#btnSay").onclick = () => speak(w.word);
    $("#btnQuizQuit").onclick = () => { memSub = "words"; renderMemory(); };
    $("#btnFlip").onclick = () => {
      $("#flashBack").classList.remove("hidden");
      $("#flashActions").innerHTML = `
        <button class="btn" id="gKnow">认识 ✓</button>
        <button class="btn secondary" id="gUnsure">模糊</button>
        <button class="btn secondary danger-btn" id="gForgot">忘了 ✗</button>`;
      $("#gKnow").onclick = () => grade("know");
      $("#gUnsure").onclick = () => grade("unsure");
      $("#gForgot").onclick = () => grade("forgot");
    };
    return;
  }
  view.innerHTML = `
    <div class="card">
      <span class="step-tag">单词复习 · 看义写词 · ${progress}</span>
      <div class="flashcard">
        <div class="word-zh" style="font-size:20px">${esc(w.meaning_zh || w.meaning_en || "（无释义）")}</div>
        ${w.meaning_zh && w.meaning_en ? `<div class="muted-sm" style="margin-top:4px">${esc(w.meaning_en)}</div>` : ""}
        ${w.example ? `<div class="word-ex" style="margin-top:8px">${esc(blankOut(w.example, w.word))}</div>` : ""}
        <div class="word-add" style="margin-top:12px">
          <input id="recallInput" placeholder="写出这个英文单词/短语，回车提交" autocomplete="off" autocapitalize="off">
          <button class="btn" id="btnRecallCheck">检查</button>
        </div>
        <div id="recallResult"></div>
      </div>
      <div style="margin-top:14px" id="recallActions">
        <button class="btn ghost" id="btnRecallReveal">想不起来，看答案</button>
        <button class="btn ghost" id="btnQuizQuit">退出</button>
      </div>
    </div>`;
  const input = $("#recallInput");
  input.focus();
  $("#btnQuizQuit").onclick = () => { memSub = "words"; renderMemory(); };
  const norm = (s) => s.toLowerCase().replace(/[^a-z0-9' ]/g, "").replace(/\s+/g, " ").trim();
  const showAnswer = (ok, typed) => {
    input.disabled = true;
    $("#btnRecallCheck").disabled = true;
    $("#recallResult").innerHTML = `
      <div class="result-box" style="margin-top:10px">
        ${ok ? `<span class="score good">✓ 正确</span>` : `<span class="score low">✗ ${typed ? "拼错了" : "看答案"}</span>`}
        <div style="margin-top:6px;font-size:20px;font-weight:700">${esc(w.word)} <button class="link-btn" id="btnSay2">🔊</button></div>
        ${typed && !ok ? `<div class="muted-sm">你写的是：${esc(typed)}</div>` : ""}
        ${w.example ? `<div class="word-ex" style="margin-top:6px">${esc(w.example)}</div>` : ""}
      </div>`;
    $("#btnSay2").onclick = () => speak(w.word);
    speak(w.word);
    $("#recallActions").innerHTML = ok
      ? `<button class="btn" id="gNext">下一个 →</button>`
      : `<button class="btn" id="gNext">记住了，下一个 →</button>`;
    $("#gNext").onclick = () => grade(ok ? "know" : "forgot");
    $("#gNext").focus();
  };
  const check = () => {
    const typed = input.value.trim();
    if (!typed) return;
    showAnswer(norm(typed) === norm(w.word), typed);
  };
  $("#btnRecallCheck").onclick = check;
  input.onkeydown = (e) => { if (e.key === "Enter") check(); };
  $("#btnRecallReveal").onclick = () => showAnswer(false, "");
}

async function renderReminders(root) {
  let d;
  try { d = await api("/api/reminders"); } catch (e) {
    root.innerHTML = `<p class="desc">加载失败：${esc(e.message)}</p>`;
    return;
  }
  const items = d.items || [];
  root.innerHTML = `
    <p class="desc">在对话里直接说"提醒我……"即可创建（如 "remind me every Monday at 8pm about the project meeting"）。到点时 Buddy 会在对话里留言，并推送到已开启通知的设备。</p>
    <div style="margin:10px 0">
      <button class="btn secondary" id="btnEnablePush">在此设备开启通知</button>
      <button class="btn ghost" id="btnTestPush">发测试通知</button>
      <span class="muted-sm">已订阅设备：${d.push_devices}</span>
    </div>
    ${items.length ? items.map((r) => `
      <div class="note-item">
        <span><b>${esc(r.label)}</b> · ${esc(r.text)}<br>
          <span class="muted-sm">下次提醒：${new Date(r.fire_at * 1000).toLocaleString("zh-CN")}</span></span>
        <button class="note-del rem-del" data-id="${r.id}">删除</button>
      </div>`).join("") : `<p class="desc">暂无提醒。</p>`}`;
  $("#btnEnablePush").onclick = async () => {
    if (await enablePush()) { alert("通知已开启！点「发测试通知」验证一下。"); renderMemory(); }
  };
  $("#btnTestPush").onclick = async () => {
    try {
      const r = await api("/api/push/test", { method: "POST", json: {} });
      if (!r.sent) alert("没有已订阅的设备。请先点「在此设备开启通知」。");
    } catch (e) { alert("发送失败：" + e.message); }
  };
  root.querySelectorAll(".rem-del").forEach((b) => {
    b.onclick = async () => {
      try { await api(`/api/reminders/${b.dataset.id}`, { method: "DELETE" }); renderMemory(); }
      catch (e) { alert("删除失败：" + e.message); }
    };
  });
}

async function renderMemoryNotes(root) {
  let d;
  try { d = await api("/api/memory"); } catch (e) {
    root.innerHTML = `<p class="desc">加载失败：${esc(e.message)}</p>`;
    return;
  }
  const t = d.updated ? new Date(d.updated * 1000).toLocaleString("zh-CN") : null;
  root.innerHTML = `
    ${d.summary
      ? `<div class="mem-notes">${esc(d.summary)}</div>
         <p class="muted-sm">最近更新：${t || "—"} · 这份笔记由 AI 自动维护，Buddy 每轮对话都会参考它。</p>`
      : `<p class="desc">还没有长期记忆。多和 Buddy 聊聊，它会自动记下关于你的重要信息（名字、生活、计划、常犯的错误……）。</p>`}
    <div style="margin-top:18px">
      <button class="btn secondary danger-btn" id="btnWipeMem">彻底抹除长期记忆与全部对话</button>
      <p class="muted-sm">需输入密码确认；图库照片不受影响。抹除后 Buddy 将完全不认识你。</p>
    </div>`;
  $("#btnWipeMem").onclick = async () => {
    const pw = prompt("危险操作：彻底抹除 Buddy 的长期记忆和全部对话（图库照片保留）。此操作不可恢复！\n\n请输入登录密码确认：");
    if (!pw) return;
    try {
      await api("/api/memory", { method: "DELETE", json: { password: pw } });
      alert("已彻底抹除。");
      renderMemory();
    } catch (e) { alert("操作失败：" + e.message); }
  };
}

async function renderGalleryInto(root) {
  let d;
  try { d = await api("/api/photos"); } catch (e) {
    root.innerHTML = `<p class="desc">加载失败：${esc(e.message)}</p>`;
    return;
  }
  const items = d.items || [];
  const groups = new Map();
  for (const p of items) {
    const key = p.album || "未整理";
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(p);
  }
  const ordered = [...groups.entries()].sort((a, b) => (a[0] === "未整理" ? -1 : b[0] === "未整理" ? 1 : 0));
  root.innerHTML = `
    <div class="chat-head">
      <p class="desc" style="margin:0">共 ${items.length} 张。在「AI 对话」里点「图」发照片，Buddy 会和你聊它并自动打标签存到这里。未整理照片攒够 8 张后每天自动归纳成相册，也可手动整理。</p>
      <button class="btn secondary" id="btnOrganize" ${items.length < 4 ? "disabled" : ""}>AI 整理图库</button>
    </div>
    <div id="orgMsg" class="muted-sm"></div>
    ${items.length ? ordered.map(([album, ps]) => `
      <h2 style="margin-top:16px">${esc(album)}（${ps.length}）</h2>
      <div class="photo-grid">
        ${ps.map((p) => {
          let tags = [];
          try { tags = p.tags_json ? JSON.parse(p.tags_json) : []; } catch (_) {}
          return `
          <div class="photo-card">
            <img src="/api/photo/${p.id}?t=${encodeURIComponent(AUTH.token)}" loading="lazy" data-open="${p.id}" alt="">
            <div class="photo-meta">
              <span class="photo-date">${p.date}</span>
              <button class="note-del photo-del" data-id="${p.id}">删除</button>
            </div>
            ${tags.length ? `<div class="photo-tags">${tags.map((t) => `<span class="tag-chip">${esc(t)}</span>`).join("")}</div>` : `<div class="photo-tags"><span class="tag-chip">标签生成中…</span></div>`}
          </div>`;
        }).join("")}
      </div>`).join("") : `<p class="desc" style="margin-top:14px">还没有照片。去「AI 对话」发第一张吧。</p>`}`;
  $("#btnOrganize").onclick = async () => {
    const btn = $("#btnOrganize");
    btn.disabled = true;
    btn.textContent = "整理中…";
    try {
      const r = await api("/api/photos/organize", { method: "POST", json: {} });
      $("#orgMsg").textContent = `已把 ${r.organized} 张照片整理进 ${r.albums.length} 个相册。`;
      setTimeout(renderMemory, 800);
    } catch (e) {
      btn.disabled = false;
      btn.textContent = "AI 整理图库";
      $("#orgMsg").textContent = "整理失败：" + e.message;
    }
  };
  root.querySelectorAll("[data-open]").forEach((img) => {
    img.onclick = () => window.open(img.src, "_blank");
  });
  root.querySelectorAll(".photo-del").forEach((b) => {
    b.onclick = async () => {
      if (!confirm("删除这张照片？")) return;
      try { await api(`/api/photos/${b.dataset.id}`, { method: "DELETE" }); renderMemory(); }
      catch (e) { alert("删除失败：" + e.message); }
    };
  });
}

/* ============ 词汇量测试 ============ */
/* 估算模型：伪词虚报校正 → 逻辑斯蒂曲线拟合（认识率随词频衰减的 S 曲线，
   全局拟合可压制单个罕见档的噪声）→ 数值积分得词汇量 → 参数化自助法给置信区间 */
function vocabEstimate(shown, known, fakeShown, fakeKnown) {
  const c = fakeShown ? fakeKnown / fakeShown : 0;
  const mids = VOCAB_BANDS.map((b) => (b.lo + b.hi) / 2);
  const pObs = VOCAB_BANDS.map((_, i) => {
    if (!shown[i]) return null;
    const raw = known[i] / shown[i];
    return Math.max(0, Math.min(1, c < 1 ? (raw - c) / (1 - c) : 0));
  });
  const logistic = (r, v, s) => 1 / (1 + Math.exp((r - v) / s));
  const fit = (ps) => {
    let best = { err: Infinity, v: 2000, s: 800 };
    for (let v = 250; v <= 42000; v += 250) {
      for (const s of [200, 400, 800, 1500, 3000, 6000]) {
        let err = 0;
        for (let i = 0; i < mids.length; i++) {
          if (ps[i] == null) continue;
          const d = ps[i] - logistic(mids[i], v, s);
          err += (shown[i] || 1) * d * d;
        }
        if (err < best.err) best = { err, v, s };
      }
    }
    return best;
  };
  const integrate = (v, s) => {
    let sum = 0;
    for (let r = 25; r < 42000; r += 50) sum += 50 * logistic(r, v, s);
    return sum;
  };
  const best = fit(pObs);
  const est = integrate(best.v, best.s);
  const boots = [];
  for (let t = 0; t < 60; t++) {
    const ps = mids.map((m, i) => {
      if (pObs[i] == null) return null;
      const pTrue = logistic(m, best.v, best.s);
      let k = 0;
      for (let j = 0; j < shown[i]; j++) if (Math.random() < pTrue) k++;
      return k / shown[i];
    });
    const b2 = fit(ps);
    boots.push(integrate(b2.v, b2.s));
  }
  boots.sort((a, b) => a - b);
  const round50 = (x) => Math.max(0, Math.round(x / 50) * 50);
  return {
    estimate: round50(est),
    low: round50(Math.min(boots[Math.floor(boots.length * 0.1)], est)),
    high: round50(Math.max(boots[Math.floor(boots.length * 0.9)], est)),
    overclaim: c,
    p: mids.map((m) => logistic(m, best.v, best.s)),
  };
}

function vocabLevel(n) {
  if (n < 1500) return "A1-A2 入门";
  if (n < 3500) return "A2-B1 初中级";
  if (n < 5500) return "B1-B2 中级";
  if (n < 9000) return "B2 中高级";
  if (n < 14000) return "C1 高级";
  if (n < 20000) return "C1-C2 精通";
  return "C2 · 接近母语者";
}

let vocab = null;
function startVocabTest() {
  vocab = {
    phase: 1,
    shown: VOCAB_BANDS.map(() => 0),
    known: VOCAB_BANDS.map(() => 0),
    fakeShown: 0, fakeKnown: 0,
    used: new Set(),
    items: [],
  };
  const items = [];
  // 跨测试轮换：pickFresh 避开最近几次测试用过的词，复测尽量不重词（降低练习效应）
  VOCAB_BANDS.forEach((b, bi) => {
    pickFresh("sg_vocab_b" + bi, b.words, 3, (w) => w).forEach((w) => { items.push({ w, band: bi }); vocab.used.add(w); });
  });
  pickFresh("sg_vocab_pseudo", VOCAB_PSEUDO, 5, (w) => w).forEach((w) => { items.push({ w, band: -1 }); vocab.used.add(w); });
  vocab.items = sample(items, items.length);
  renderVocabPhase();
}

function renderVocabPhase() {
  view.innerHTML = `
    <div class="card">
      <span class="step-tag">词汇量测试 · 第 ${vocab.phase} / 2 轮（共 ${vocab.items.length} 个词）</span>
      <h2>${vocab.phase === 1 ? "点亮你认识的单词" : "第二轮：精确定位你的词汇边界"}</h2>
      <p class="desc">只点你<b>知道至少一个意思</b>的词（不要求会拼写发音）。测试混有不存在的"伪词"，乱点会被数学扣正——诚实点击，结果才准。</p>
      <div class="vocab-grid">
        ${vocab.items.map((it, i) => `<button class="vword" data-i="${i}">${esc(it.w)}</button>`).join("")}
      </div>
      <div style="margin-top:14px">
        <button class="btn" id="vocabNext">${vocab.phase === 1 ? "下一轮" : "查看结果"}</button>
        <button class="btn ghost" id="vocabQuit">退出测试</button>
      </div>
    </div>`;
  view.querySelectorAll(".vword").forEach((b) => {
    b.onclick = () => b.classList.toggle("sel");
  });
  $("#vocabQuit").onclick = () => { vocab = null; renderProgress(); };
  $("#vocabNext").onclick = () => {
    view.querySelectorAll(".vword").forEach((b) => {
      const it = vocab.items[+b.dataset.i];
      const checked = b.classList.contains("sel");
      if (it.band === -1) {
        vocab.fakeShown++;
        if (checked) vocab.fakeKnown++;
      } else {
        vocab.shown[it.band]++;
        if (checked) vocab.known[it.band]++;
      }
    });
    if (vocab.phase === 1) {
      const c = vocab.fakeShown ? vocab.fakeKnown / vocab.fakeShown : 0;
      const frontier = new Set();
      VOCAB_BANDS.forEach((b, i) => {
        const raw = vocab.shown[i] ? vocab.known[i] / vocab.shown[i] : 0;
        const p = Math.max(0, Math.min(1, c < 1 ? (raw - c) / (1 - c) : 0));
        if (p > 0.05 && p < 0.95) { frontier.add(i - 1); frontier.add(i); frontier.add(i + 1); }
      });
      if (!frontier.size) {
        const allKnown = vocab.known.reduce((a, b) => a + b, 0) > vocab.shown.reduce((a, b) => a + b, 0) * 0.8;
        (allKnown ? [11, 12, 13, 14] : [0, 1, 2, 3]).forEach((i) => frontier.add(i));
      }
      const items = [];
      [...frontier].filter((i) => i >= 0 && i < VOCAB_BANDS.length).forEach((bi) => {
        const key = "sg_vocab_b" + bi;
        const recent = load(key, []);
        const pool = VOCAB_BANDS[bi].words.filter((w) => !vocab.used.has(w));
        const fresh = pool.filter((w) => !recent.includes(w));
        const stale = pool.filter((w) => recent.includes(w));
        const chosen = [...sample(fresh, Math.min(5, fresh.length)), ...sample(stale, Math.max(0, 5 - fresh.length))].slice(0, 5);
        chosen.forEach((w) => { items.push({ w, band: bi }); vocab.used.add(w); });
        save(key, [...chosen, ...recent].slice(0, VOCAB_BANDS[bi].words.length - 3));
      });
      sample(VOCAB_PSEUDO.filter((w) => !vocab.used.has(w)), 7).forEach((w) => {
        items.push({ w, band: -1 });
        vocab.used.add(w);
      });
      vocab.phase = 2;
      vocab.items = sample(items, items.length);
      renderVocabPhase();
    } else {
      finishVocabTest();
    }
  };
}

async function finishVocabTest() {
  const r = vocabEstimate(vocab.shown, vocab.known, vocab.fakeShown, vocab.fakeKnown);
  const details = {
    bands: VOCAB_BANDS.map((b, i) => ({ lo: b.lo, hi: b.hi, shown: vocab.shown[i], known: vocab.known[i] })),
    fakeShown: vocab.fakeShown, fakeKnown: vocab.fakeKnown,
  };
  try {
    await api("/api/vocab", { json: { estimate: r.estimate, low: r.low, high: r.high, overclaim: r.overclaim, details } });
  } catch (_) {}
  let hist = [];
  try { hist = (await api("/api/vocab/history")).items || []; } catch (_) {}
  const maxP = 1;
  view.innerHTML = `
    <div class="card">
      <span class="step-tag">词汇量测试 · 结果</span>
      <div class="vocab-result">
        <div class="vr-num">${r.estimate.toLocaleString()}</div>
        <div class="vr-cap">估计词汇量（词族） · 80% 置信区间 ${r.low.toLocaleString()} – ${r.high.toLocaleString()}</div>
        <div class="vr-level">${vocabLevel(r.estimate)}</div>
      </div>
      ${r.overclaim > 0.15 ? `<p class="desc" style="color:var(--danger)">注意：你勾选了 ${vocab.fakeKnown} 个不存在的伪词（虚报率 ${(r.overclaim * 100).toFixed(0)}%），结果已按猜测率扣正。下次更严格地只点真正认识的词，估计会更准。</p>` : ""}
      <h2 style="margin-top:16px">各频段认识率</h2>
      <div class="bars">
        ${r.p.map((p, i) => `<div class="bar" style="height:${Math.max(4, (p / maxP) * 100)}%"><span class="bar-tip">${Math.round(p * 100)}%</span></div>`).join("")}
      </div>
      <p class="muted-sm">横轴：词频从常见（左，前 1000 词）到罕见（右，第 33000-40000 词）</p>
      <h2 style="margin-top:18px">参照系</h2>
      <table>
        <tr><th>参照</th><th>大约词汇量（词族）</th></tr>
        <tr><td>CET-4 大学四级</td><td>4,000 – 4,500</td></tr>
        <tr><td>CET-6 大学六级 / 考研</td><td>5,500 – 6,500</td></tr>
        <tr><td>雅思 6.5 – 7</td><td>7,000 – 9,000</td></tr>
        <tr><td>英语母语成年人</td><td>20,000 – 35,000</td></tr>
      </table>
      ${hist.length > 1 ? `
        <h2 style="margin-top:18px">历史成绩</h2>
        ${hist.map((h) => `<div class="note-item"><span>${h.date} · <b>${h.estimate.toLocaleString()}</b>（${h.low.toLocaleString()}–${h.high.toLocaleString()}）</span></div>`).join("")}` : ""}
      <p class="muted-sm" style="margin-top:12px">方法说明：频率分层抽样 + 伪词虚报校正 + 保序回归平滑。测量的是<b>书面接受性词汇</b>（认识≠会用）；自测类测试通常有 ±10-15% 波动，建议每 1-2 个月复测看趋势。</p>
      <div style="margin-top:14px">
        <button class="btn" id="vocabAgain">再测一次</button>
        <button class="btn secondary" id="vocabBack">返回进度</button>
      </div>
    </div>`;
  $("#vocabAgain").onclick = startVocabTest;
  $("#vocabBack").onclick = () => renderProgress();
  vocab = null;
}

function renderAiBox(resp) {
  const box = $("#aiBox");
  if (!box) return;
  if (!resp.ai) {
    const msgs = { no_key: "服务器未配置 AI Key，已仅保存数据。", too_short: "本轮内容太短（不足 15 个词），未进行 AI 评分。" };
    box.innerHTML = `<div class="ai-loading">${msgs[resp.reason] || "AI 评分暂时不可用（转写已保存）。稍后可在「进度 → 我的历史记录」中对这条点「补评」。"}</div>`;
    return;
  }
  box.innerHTML = aiDetailHtml(resp.ai);
}

const fmtDate = (ts) => new Date(ts).toLocaleDateString("zh-CN", { month: "2-digit", day: "2-digit" });

function renderTopicPick() {
  view.innerHTML = `
    <div class="card">
      ${stepHeader(2)}
      <h2>选择今天的话题类别</h2>
      <p class="desc">建议按周轮换：周一日常 / 周二职场 / 周三深度 / 周四情景演练 / 周五科技与AI / 周六时事与观点 / 周日自由选。同类话题轮完一遍才会重复。情景演练是角色扮演；科技与时事类适合练"观点+论证"的表达结构。</p>
      <div class="pill-row">
        ${Object.entries(TOPICS).map(([k, v]) => `<button class="pill" data-cat="${k}">${v.label}</button>`).join("")}
        <button class="pill" data-cat="random">随机</button>
      </div>
    </div>`;
  document.querySelectorAll(".pill").forEach((p) => {
    p.onclick = () => {
      const cats = Object.keys(TOPICS);
      session.cat = p.dataset.cat === "random" ? cats[Math.floor(Math.random() * cats.length)] : p.dataset.cat;
      session.topic = pickFresh(`sg_recent_topic_${session.cat}`, TOPICS[session.cat].items, 1, (x) => x.q)[0];
      session.round = 0; session.rounds = [];
      renderTopic();
    };
  });
}

/* ---- 步骤 3：复盘 ---- */
function renderReview() {
  const last = session.rounds[session.rounds.length - 1];
  const trend = session.rounds.map((rd, k) =>
    `<div class="stat"><div class="num">${rd.wpm}</div><div class="cap">${ROUNDS[k] ? ROUNDS[k].label.split(" · ")[0] : "轮次"} WPM</div></div>`).join("");
  view.innerHTML = `
    <div class="card">
      ${stepHeader(3)}
      <h2>复盘：收集「说不出的表达」</h2>
      <p class="desc">读一遍你最后一轮的转写，回想刚才哪些意思你想表达却卡住了。用中文（或不确定的英文）记下来，每行一条。之后可以去问 AI（豆包/ChatGPT）地道说法，明天热身时用一遍。</p>
      ${trend ? `<div class="stats-row">${trend}</div>` : ""}
      ${session.rounds.map((rd, k) => rd.audioUrl
        ? `<div class="result-box"><span class="label">第 ${k + 1} 轮录音 · ${fmt(rd.sec)}</span><audio controls src="${rd.audioUrl}"></audio></div>` : "").join("")}
      ${last ? `<div class="result-box"><span class="label">最后一轮转写</span>${highlightFillers(last.text) || "（无内容）"}</div>` : ""}
      <div style="margin-top:14px">
        <textarea id="noteBox" placeholder="例如：\n想说“这事让我很纠结”没说出来\n“性价比高”英语怎么说？\nI want to express that I was moved but not sad..."></textarea>
      </div>
      <div style="margin-top:10px">
        <button class="btn" id="btnFinish">保存并完成今日练习</button>
      </div>
    </div>`;
  $("#btnFinish").onclick = () => {
    const lines = $("#noteBox").value.split("\n").map((s) => s.trim()).filter(Boolean);
    if (lines.length) {
      const notes = getNotes();
      lines.forEach((l) => notes.unshift({ text: l, date: todayStr() }));
      save("sg_notes", notes);
    }
    refreshStreak();
    session.step = 4;
    renderDaily();
  };
}

/* ---- 完成页 ---- */
function renderDone() {
  const bestWpm = Math.max(0, ...session.rounds.map((r) => r.wpm));
  view.innerHTML = `
    <div class="card done-banner">
      <div class="big">今日练习完成</div>
      <p class="desc">今日最快语速 ${bestWpm} WPM · 分数和录音已存入你的账号<br>
      别忘了：把复盘里记的表达拿去问 AI 地道说法，明天开口用一遍，它才真正属于你。</p>
      <button class="btn" id="btnMore">再来一组</button>
      <button class="btn secondary" id="btnProg">查看进度与曲线</button>
    </div>`;
  $("#btnMore").onclick = () => { newSession(); renderDaily(); };
  $("#btnProg").onclick = () => {
    document.querySelectorAll("#tabs button").forEach((x) => x.classList.toggle("active", x.dataset.tab === "progress"));
    renderProgress();
  };
}

/* ============ Tab 2：语块库 ============ */
let phraseCat = 0;
function renderPhrases() {
  view.innerHTML = `
    <div class="card">
      <h2>语块库：整块学，整块用</h2>
      <p class="desc">别背单词，背「语块」。每天挑 3-5 条：点「朗读」听 → 跟着说 → 用它现编一个自己的句子说出来。</p>
      <div class="pill-row">
        ${PHRASES.map((c, i) => `<button class="pill ${i === phraseCat ? "active" : ""}" data-i="${i}">${c.cat}</button>`).join("")}
      </div>
      <div id="phraseList">
        ${PHRASES[phraseCat].items.map((p, i) => `
          <div class="phrase-item">
            <div class="p-en">${esc(p.en)}<button class="play-mini" data-say="${i}">朗读</button></div>
            <div class="p-zh">${esc(p.zh)}</div>
            <div class="p-ex">${esc(p.ex)}</div>
          </div>`).join("")}
      </div>
    </div>`;
  document.querySelectorAll(".pill").forEach((p) => {
    p.onclick = () => { phraseCat = +p.dataset.i; renderPhrases(); };
  });
  document.querySelectorAll(".play-mini").forEach((b) => {
    b.onclick = () => {
      const item = PHRASES[phraseCat].items[+b.dataset.say];
      speak(/→/.test(item.en) ? item.ex : `${item.en} ... For example: ${item.ex}`);
    };
  });
}

/* ============ Tab 3：进度 ============ */
function svgLine(vals, labels) {
  if (!vals.length) return "";
  const W = 600, H = 170, P = 26;
  const xs = (i) => P + (W - 2 * P) * (vals.length === 1 ? 0.5 : i / (vals.length - 1));
  const ys = (v) => H - P - (H - 2 * P) * (v / 100);
  const pts = vals.map((v, i) => `${xs(i).toFixed(1)},${ys(v).toFixed(1)}`).join(" ");
  return `
    <svg viewBox="0 0 ${W} ${H}" class="chart" preserveAspectRatio="none">
      ${[0, 25, 50, 75, 100].map((g) => `
        <line x1="${P}" y1="${ys(g)}" x2="${W - P}" y2="${ys(g)}" stroke="#e3e8e4" stroke-width="1"/>
        <text x="4" y="${ys(g) + 4}" font-size="11" fill="#9aa7a1">${g}</text>`).join("")}
      <polyline fill="none" stroke="#0f766e" stroke-width="2.5" points="${pts}"/>
      ${vals.map((v, i) => `<circle cx="${xs(i).toFixed(1)}" cy="${ys(v).toFixed(1)}" r="3.5" fill="#0f766e"><title>${labels[i]}：${v} 分</title></circle>`).join("")}
    </svg>`;
}

async function renderProgress() {
  if (!AUTH.user) return renderAuth();
  view.innerHTML = `<div class="card"><p class="desc">加载中……</p></div>`;
  let scores = { items: [], streak: 0 }, mine = { items: [] }, pub = { items: [] }, me = AUTH.user, vhist = { items: [] };
  try {
    [scores, mine, pub, me, vhist] = await Promise.all([
      api("/api/scores"), api("/api/recordings"), api("/api/public-recordings"), api("/api/me"), api("/api/vocab/history"),
    ]);
    AUTH.user = me;
  } catch (e) {
    view.innerHTML = `<div class="card"><p class="desc">加载失败：${esc(e.message)}</p></div>`;
    return;
  }
  const notes = getNotes();
  const scored = scores.items.filter((s) => s.ai_score != null);
  const curveVals = scored.slice(-40).map((s) => s.ai_score);
  const curveLabels = scored.slice(-40).map((s) => `${s.date} 第${s.round}轮`);
  const wpmBars = scores.items.slice(-20);
  const maxWpm = Math.max(60, ...wpmBars.map((b) => b.wpm));
  view.innerHTML = `
    <div class="card">
      <div class="chat-head">
        <div>
          <h2>词汇量</h2>
          ${vhist.items.length
            ? `<p class="desc">最近一次（${vhist.items[0].date}）：<b style="font-size:18px">${vhist.items[0].estimate.toLocaleString()}</b> 词族（${vhist.items[0].low.toLocaleString()}–${vhist.items[0].high.toLocaleString()}）· ${vocabLevel(vhist.items[0].estimate)}${vhist.items.length > 1 ? ` · 上上次 ${vhist.items[1].estimate.toLocaleString()}` : ""}</p>`
            : `<p class="desc">还没测过。约 5 分钟：两轮自适应勾选 + 伪词防虚报校正，测你的书面接受性词汇量。</p>`}
        </div>
        <button class="btn secondary" id="btnVocabTest">${vhist.items.length ? "再测一次" : "开始测试"}</button>
      </div>
    </div>
    <div class="card">
      <h2>练习数据</h2>
      <div class="stats-row">
        <div class="stat"><div class="num">${scores.streak}</div><div class="cap">连续天数</div></div>
        <div class="stat"><div class="num">${scores.items.length}</div><div class="cap">累计表达轮数</div></div>
        <div class="stat"><div class="num">${scored.length ? scored[scored.length - 1].ai_score : "-"}</div><div class="cap">最近 AI 评分</div></div>
        <div class="stat"><div class="num">${notes.length}</div><div class="cap">表达清单条数</div></div>
      </div>
      ${curveVals.length ? `
        <h2 style="margin-top:18px">AI 评分曲线（最近 ${curveVals.length} 轮）</h2>
        ${svgLine(curveVals, curveLabels)}` : `<p class="desc" style="margin-top:14px">还没有 AI 评分记录，完成一轮话题表达后会出现在这里。</p>`}
      ${wpmBars.length ? `
        <h2 style="margin-top:18px">语速趋势（WPM，最近 ${wpmBars.length} 轮）</h2>
        <div class="bars">
          ${wpmBars.map((b) => `<div class="bar" style="height:${Math.max(6, (b.wpm / maxWpm) * 100)}%"><span class="bar-tip">${b.wpm}</span></div>`).join("")}
        </div>` : ""}
    </div>
    <div class="card">
      <h2>设置</h2>
      <label class="toggle-row">
        <input type="checkbox" id="pubToggle" ${me.public_audio ? "checked" : ""}>
        <span><b>公开我的语音</b> — 勾选后，其他用户可以在「大家的公开录音」里听到你的所有录音；随时可取消，取消后立即恢复私密。</span>
      </label>
      <div id="pubMsg" class="muted-sm"></div>
    </div>
    <div class="card">
      <h2>我的历史记录（${mine.items.length}）</h2>
      <p class="desc">每一轮的录音、转写和 AI 评分都按天存档。点「详情」查看当轮的完整反馈；每 2 周回听一次两周前的录音，是感知进步最直接的方式。</p>
      <div id="recList">${recListHtml(mine.items, false)}</div>
    </div>
    <div class="card">
      <h2>录音广场（${pub.items.length}）</h2>
      <p class="desc">这里显示勾选了「公开我的语音」的用户的练习记录——录音、转写和 AI 评分都可以看，听听别人怎么说同一个话题，也是一种学习。</p>
      <div id="pubList">${recListHtml(pub.items, true)}</div>
    </div>
    <div class="card">
      <h2>我的表达清单（${notes.length}）</h2>
      <p class="desc">这些是你练习中「想说但说不出」的表达。拿去问 AI 地道说法，学会后口头造 3 个句子再划掉。</p>
      ${notes.length ? notes.map((n, i) => `
        <div class="note-item">
          <span>${esc(n.text)}</span>
          <span class="note-date">${n.date}<button class="note-del" data-i="${i}">删除</button></span>
        </div>`).join("") : `<p class="desc">暂无记录。完成每日练习的复盘步骤后会自动收集到这里。</p>`}
    </div>`;
  $("#btnVocabTest").onclick = startVocabTest;
  $("#pubToggle").onchange = async (e) => {
    const val = e.target.checked;
    try {
      await api("/api/settings", { json: { public_audio: val } });
      $("#pubMsg").textContent = val ? "已公开：其他用户现在可以听到你的录音。" : "已恢复私密：其他用户无法再听到你的录音。";
    } catch (err) {
      e.target.checked = !val;
      $("#pubMsg").textContent = "设置失败：" + err.message;
    }
  };
  bindRecList(view);
  document.querySelectorAll(".note-del").forEach((b) => {
    b.onclick = () => {
      const ns = getNotes();
      ns.splice(+b.dataset.i, 1);
      save("sg_notes", ns);
      renderProgress();
    };
  });
}

function recListHtml(items, isPublic) {
  if (!items.length) return `<p class="desc">暂无录音。</p>`;
  let out = "", lastDate = null;
  for (const r of items) {
    if (r.date !== lastDate) {
      out += `<div class="date-sep">${r.date}</div>`;
      lastDate = r.date;
    }
    let ai = null;
    try { ai = r.ai_json ? JSON.parse(r.ai_json) : null; } catch (_) {}
    const hasDetail = !!(ai || r.transcript);
    out += `
    <div class="rec-item">
      <div class="rec-meta">
        ${isPublic ? `<span class="rec-user">${esc(r.username)}</span> · ` : ""}${esc(r.cat)} · 第 ${r.round} 轮 · ${fmt(r.sec)} · ${r.wpm} WPM${r.ai_score != null ? ` · <span class="score-badge">AI ${r.ai_score} 分</span>` : ""}
        <div class="rec-topic">${esc(r.topic.length > 60 ? r.topic.slice(0, 60) + "…" : r.topic)}</div>
      </div>
      <div class="rec-actions">
        <button class="btn secondary rec-play" data-id="${r.id}">播放</button>
        ${hasDetail ? `<button class="btn secondary rec-toggle">详情</button>` : ""}
        ${!isPublic ? `<button class="note-del rec-del" data-id="${r.id}">删除</button>` : ""}
      </div>
      <div class="rec-player"></div>
      ${hasDetail ? `
      <div class="rec-detail hidden">
        ${r.transcript ? `<div class="result-box"><span class="label">转写</span>${highlightFillers(r.transcript)}</div>` : ""}
        ${ai ? `<div class="ai-box" style="margin-top:10px">${aiDetailHtml(ai)}</div>` : `<p class="muted-sm">本条无 AI 评分（当时可能网络波动）。${!isPublic && r.transcript ? `<button class="btn secondary rec-rescore" data-id="${r.id}" style="margin-left:8px;padding:5px 12px;font-size:12.5px">补评</button>` : ""}</p>`}
      </div>` : ""}
    </div>`;
  }
  return out;
}

function bindRecList(root) {
  root.querySelectorAll(".rec-play").forEach((b) => {
    b.onclick = () => {
      const holder = b.closest(".rec-item").querySelector(".rec-player");
      holder.innerHTML = `<audio controls autoplay src="/api/audio/${b.dataset.id}?t=${encodeURIComponent(AUTH.token)}"></audio>`;
      b.disabled = true;
    };
  });
  root.querySelectorAll(".rec-toggle").forEach((b) => {
    b.onclick = () => {
      const d = b.closest(".rec-item").querySelector(".rec-detail");
      d.classList.toggle("hidden");
      b.textContent = d.classList.contains("hidden") ? "详情" : "收起";
    };
  });
  root.querySelectorAll(".rec-rescore").forEach((b) => {
    b.onclick = async () => {
      b.disabled = true;
      b.textContent = "评分中…";
      try { await api(`/api/rescore/${b.dataset.id}`, { method: "POST", json: {} }); renderProgress(); }
      catch (e) { b.disabled = false; b.textContent = "补评"; alert("补评失败：" + e.message); }
    };
  });
  root.querySelectorAll(".rec-del").forEach((b) => {
    b.onclick = async () => {
      try { await api(`/api/recordings/${b.dataset.id}`, { method: "DELETE" }); renderProgress(); }
      catch (e) { alert("删除失败：" + e.message); }
    };
  });
}

/* ============ 启动 ============ */
(async function boot() {
  if (AUTH.token) {
    try { AUTH.user = await api("/api/me"); } catch (_) { AUTH.token = null; localStorage.removeItem("sg_token"); }
  }
  updateHeader();
  if (AUTH.user) { refreshStreak(); renderDaily(); }
  else renderAuth();
})();
