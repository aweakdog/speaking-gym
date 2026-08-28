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
async function speak(text) {
  speechSynthesis.cancel();
  if (ttsAudio) { try { ttsAudio.pause(); } catch (_) {} ttsAudio = null; }
  const rate = $("#rate").value || "0.9";
  const voice = ($("#voice") && $("#voice").value) || "aria";
  if (AUTH.token) {
    try {
      const res = await fetch(`/api/tts?text=${encodeURIComponent(text)}&rate=${rate}&voice=${voice}`, {
        headers: { Authorization: "Bearer " + AUTH.token },
      });
      if (res.ok) {
        const blob = await res.blob();
        ttsAudio = new Audio(URL.createObjectURL(blob));
        ttsAudio.play();
        return;
      }
    } catch (_) {}
  }
  const u = new SpeechSynthesisUtterance(text);
  u.lang = "en-US";
  u.rate = parseFloat(rate);
  const v = pickVoice();
  if (v) u.voice = v;
  speechSynthesis.speak(u);
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
function startVad(stream, onStop, { threshold = 0.02, silenceMs = 2000, maxMs = 20000 } = {}) {
  stopVad();
  const Ctx = window.AudioContext || window.webkitAudioContext;
  if (!Ctx) return;
  const ctx = new Ctx();
  const analyser = ctx.createAnalyser();
  analyser.fftSize = 2048;
  ctx.createMediaStreamSource(stream).connect(analyser);
  const buf = new Uint8Array(analyser.fftSize);
  let spoke = false, silent = 0, total = 0, last = performance.now();
  vad.ctx = ctx;
  vad.timer = setInterval(() => {
    analyser.getByteTimeDomainData(buf);
    let sum = 0;
    for (let i = 0; i < buf.length; i++) { const v = (buf[i] - 128) / 128; sum += v * v; }
    const rms = Math.sqrt(sum / buf.length);
    const now = performance.now();
    const dt = now - last;
    last = now; total += dt;
    if (rms > threshold) { spoke = true; silent = 0; }
    else if (spoke) silent += dt;
    if ((spoke && silent >= silenceMs) || total >= maxMs) { stopVad(); onStop(); }
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

document.querySelectorAll("#tabs button").forEach((b) => {
  b.addEventListener("click", () => {
    document.querySelectorAll("#tabs button").forEach((x) => x.classList.remove("active"));
    b.classList.add("active");
    stopAllRec(); discardRecording(); clearTimer(); speechSynthesis.cancel();
    if (!AUTH.user) return renderAuth();
    ({ daily: renderDaily, chat: renderChat, phrases: renderPhrases, progress: renderProgress })[b.dataset.tab]();
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
  $("#btnRec").onclick = async () => {
    const out = () => $("#shadowOut");
    const showResult = (text, sc, src, pitchVar) => {
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
    out().innerHTML = `<div class="rec-live"><div class="rec-dot"></div>正在准备麦克风……</div>`;
    const recOk = await startRecording();
    if (!recOk) {
      if (!SR) { out().innerHTML = `<div class="result-box">无法使用麦克风，请在浏览器地址栏允许麦克风权限后重试。</div>`; return; }
      out().innerHTML = `<div class="rec-live"><div class="rec-dot"></div>正在听你说……（浏览器识别）</div>`;
      recognizeOnce((text) => {
        if (!text) { if (out()) out().innerHTML = `<div class="result-box">没有听清，请再试一次。</div>`; return; }
        showResult(text, shadowScore(item.en, text), "browser");
      });
      return;
    }
    let done = false;
    const finish = async () => {
      if (done) return;
      done = true;
      stopVad();
      const blob = await stopRecording();
      if (!out()) return;
      out().innerHTML = `<div class="result-box"><div class="muted-sm">Whisper 精确识别中……（约 2-5 秒）</div></div>`;
      if (blob) {
        try {
          const resp = await api(`/api/shadow-score?target=${encodeURIComponent(item.en)}`, { method: "POST", blob, mime: blob.type });
          if (resp.score != null) return showResult(resp.transcript, resp.score, "whisper", resp.pitch_var);
        } catch (_) {}
      }
      if (out()) out().innerHTML = `<div class="result-box">没有录到声音或识别失败，请再试一次（离麦克风近一点）。</div>`;
    };
    out().innerHTML = `
      <div class="rec-live"><div class="rec-dot"></div>正在录音……读完整句后<b>停顿 2 秒</b>自动结束（逗号处换气不会截断）</div>
      <div style="margin-top:8px"><button class="btn secondary" id="btnStopShadow">说完了</button></div>`;
    $("#btnStopShadow").onclick = finish;
    startVad(recorder.stream, finish);
  };
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
        <button class="btn secondary hidden" id="btnStop">提前结束本轮</button>
        <button class="btn ghost" id="btnChange">换个话题</button>
      </div>
      <div id="live"></div>
      ${doneRounds ? `<div class="stats-row">${doneRounds}</div>` : ""}
    </div>`;
  const roundSec = ROUNDS[r].sec;
  let startAt = null;
  let finished = false;
  $("#btnChange").onclick = () => { session.topic = null; session.round = 0; session.rounds = []; renderTopic(); };
  $("#btnStart").onclick = async () => {
    const recOk = await startRecording();
    let srOk = false;
    if (SR) srOk = startDictation((fin, inter) => {
      $("#live").innerHTML = `
        <div class="rec-live"><div class="rec-dot"></div>正在${recOk ? "录音并" : ""}记录，请持续说英文……</div>
        <div class="result-box"><span class="label">实时转写</span>${esc(fin)}<i style="color:#999">${esc(inter)}</i></div>`;
    });
    if (!srOk && !recOk) {
      alert("录音和语音识别都不可用。请在浏览器地址栏允许麦克风权限后重试。");
      return;
    }
    if (!srOk) {
      dict.final = "";
      $("#live").innerHTML = `
        <div class="rec-live"><div class="rec-dot"></div>正在录音……（本机无实时转写，结束后由服务器 Whisper 精确转写并评分）</div>`;
    }
    startAt = Date.now();
    $("#btnStart").classList.add("hidden");
    $("#btnStop").classList.remove("hidden");
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
    $("#btnStop").onclick = finishRound;
  };
  async function finishRound() {
    if (finished) return;
    finished = true;
    clearTimer();
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
function chatBubble(role, content, fix) {
  if (role === "user") return `<div class="msg user"><div class="bubble">${esc(content)}</div></div>`;
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
        <button class="link-btn" id="chatClear">清空对话与记忆</button>
      </div>
      <div class="chat-box" id="chatBox"><p class="desc">加载中……</p></div>
      <div class="chat-input">
        <button class="btn" id="btnTalk">按一下说话</button>
        <input id="chatText" placeholder="或者打字…（Enter 发送）" autocomplete="off">
        <button class="btn secondary" id="btnSend">发送</button>
      </div>
      <div class="muted-sm" id="chatHint">语音：点「按一下说话」开始，说完停顿 2 秒自动发送；再点一下可手动结束。</div>
    </div>`;
  const box = () => $("#chatBox");
  const scrollDown = () => { const b = box(); if (b) b.scrollTop = b.scrollHeight; };
  const append = (html) => { const b = box(); if (!b) return; b.insertAdjacentHTML("beforeend", html); scrollDown(); };

  $("#chatClear").onclick = async () => {
    if (!confirm("确定清空所有对话记录和 Buddy 的记忆吗？此操作不可恢复。")) return;
    try { await api("/api/chat", { method: "DELETE" }); renderChat(); } catch (e) { alert("清空失败：" + e.message); }
  };

  try {
    const h = await api("/api/chat/history");
    if (!box()) return;
    box().innerHTML = h.items.length
      ? h.items.map((m) => {
          let fix = null;
          try { fix = m.fix_json ? JSON.parse(m.fix_json) : null; } catch (_) {}
          return chatBubble(m.role, m.content, fix);
        }).join("")
      : chatBubble("ai", "Hey! I'm Buddy, your English chat partner. Tell me about your day — or anything on your mind. What's up?", null);
    scrollDown();
  } catch (e) {
    if (box()) box().innerHTML = `<p class="desc">历史加载失败：${esc(e.message)}</p>`;
  }

  async function handleReply(promise, placeholderId) {
    try {
      const resp = await promise;
      const ph = document.getElementById(placeholderId);
      if (resp.user_text === "") {
        if (ph) ph.remove();
        const hint = $("#chatHint");
        if (hint) hint.textContent = "没有听清，请再试一次（离麦克风近一点）。";
        return;
      }
      if (resp.user_text && ph) ph.outerHTML = chatBubble("user", resp.user_text, null);
      else if (ph) ph.remove();
      append(chatBubble("ai", resp.reply, resp.fix));
      speak(resp.reply);
    } catch (e) {
      const ph = document.getElementById(placeholderId);
      if (ph) ph.outerHTML = `<div class="msg ai"><div class="bubble">（${esc(e.message)}）</div></div>`;
    } finally {
      chatBusy = false;
    }
  }

  const sendText = () => {
    const input = $("#chatText");
    const text = input.value.trim();
    if (!text || chatBusy) return;
    chatBusy = true;
    input.value = "";
    append(chatBubble("user", text, null));
    const pid = "ph" + Date.now();
    append(`<div class="msg ai" id="${pid}"><div class="bubble thinking">Buddy 正在输入…</div></div>`);
    handleReply(api("/api/chat/send", { json: { text } }), pid);
  };
  $("#btnSend").onclick = sendText;
  $("#chatText").addEventListener("keydown", (e) => { if (e.key === "Enter") sendText(); });

  let talking = false;
  let finishTalk = null;
  $("#btnTalk").onclick = async () => {
    if (chatBusy) return;
    if (talking) { if (finishTalk) finishTalk(); return; }
    const recOk = await startRecording();
    if (!recOk) { alert("无法使用麦克风，请在浏览器地址栏允许麦克风权限。"); return; }
    talking = true;
    const btn = $("#btnTalk");
    btn.textContent = "说完了（停顿 2 秒自动发送）";
    btn.classList.add("recording");
    let done = false;
    finishTalk = async () => {
      if (done) return;
      done = true;
      talking = false;
      stopVad();
      const blob = await stopRecording();
      const b2 = $("#btnTalk");
      if (b2) { b2.textContent = "按一下说话"; b2.classList.remove("recording"); }
      if (!blob) return;
      chatBusy = true;
      const pid = "ph" + Date.now();
      append(`<div class="msg user" id="${pid}"><div class="bubble thinking">（识别中…）</div></div>`);
      handleReply(api("/api/chat/voice", { method: "POST", blob, mime: blob.type }), pid);
    };
    startVad(recorder.stream, finishTalk, { maxMs: 60000 });
  };
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
      <p class="desc">建议按周轮换：周一日常 / 周二职场 / 周三深度 / 周四情景演练……不确定就点「随机」。同类话题轮完一遍才会重复。情景演练是角色扮演：把 AI 想象成对方，直接开口演。</p>
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
  let scores = { items: [], streak: 0 }, mine = { items: [] }, pub = { items: [] }, me = AUTH.user;
  try {
    [scores, mine, pub, me] = await Promise.all([
      api("/api/scores"), api("/api/recordings"), api("/api/public-recordings"), api("/api/me"),
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
