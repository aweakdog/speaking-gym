#!/usr/bin/env python3
"""口语练习室后端：静态文件 + 账号 + DeepSeek 评分 + 录音存储/公开广场。
零第三方依赖（仅 Python 标准库）。API Key 存放在数据目录的 config.json 中，绝不下发给前端。
用法：SG_DATA_DIR=~/speaking-gym-data python3 server.py 1511
"""
import functools
import hashlib
import hmac
import http.server
import importlib.util
import json
import os
import re
import secrets
import shutil
import sqlite3
import ssl
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.abspath(os.environ.get("SG_DATA_DIR", os.path.join(BASE, "data")))
REC_DIR = os.path.join(DATA, "recordings")
TTS_DIR = os.path.join(DATA, "tts")
PHOTO_DIR = os.path.join(DATA, "photos")
os.makedirs(REC_DIR, exist_ok=True)
os.makedirs(TTS_DIR, exist_ok=True)
os.makedirs(PHOTO_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA, "gym.db")

CONFIG = {}
_cfg = os.path.join(DATA, "config.json")
if os.path.exists(_cfg):
    with open(_cfg) as f:
        CONFIG = json.load(f)
DEEPSEEK_KEY = CONFIG.get("deepseek_api_key") or os.environ.get("DEEPSEEK_API_KEY", "")
QWEN_KEY = CONFIG.get("qwen_api_key") or CONFIG.get("dashscope_api_key") or os.environ.get("DASHSCOPE_API_KEY", "")
QWEN_BASE = CONFIG.get("qwen_base_url") or "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
QWEN_VISION_MODEL = CONFIG.get("qwen_vision_model") or "qwen3.8-flash"
DEEPSEEK_VISION_MODEL = CONFIG.get("deepseek_vision_model") or "deepseek-v4-flash-vision-exp"
# 视觉提供商：默认 deepseek（与文本共用一个账号）；配置 vision_provider="qwen" 可切回千问
VISION_PROVIDER = (CONFIG.get("vision_provider") or ("deepseek" if DEEPSEEK_KEY else ("qwen" if QWEN_KEY else ""))).lower()
ADMIN_TOKEN = CONFIG.get("admin_token") or os.environ.get("SG_ADMIN_TOKEN", "")
STARTED_AT = time.time()
ADMIN_AUDIT_PATH = os.path.join(DATA, "admin-audit.log")
ADMIN_DEPLOYED_PATH = os.path.join(DATA, "deployed-commit")
ADMIN_FAILURE_LOCK = threading.Lock()
ADMIN_ACTION_LOCK = threading.Lock()
ADMIN_FAILURES = {}
ADMIN_NONCES = {}


def vision_available():
    return (VISION_PROVIDER == "deepseek" and bool(DEEPSEEK_KEY)) or (VISION_PROVIDER == "qwen" and bool(QWEN_KEY))

USERNAME_RE = re.compile(r"^[A-Za-z0-9_\u4e00-\u9fa5]{2,20}$")
STATIC_ALLOWED = re.compile(
    r"^/(?:$|index\.html|style\.css|(?:app|data|sw)\.js|manifest\.webmanifest|"
    r"icons/[^/]+\.(?:png|jpg|jpeg|svg|webp|ico))$"
)


# ---------- 数据库 ----------
def db():
    c = sqlite3.connect(DB_PATH, timeout=15)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c


def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            pw_hash TEXT NOT NULL,
            salt TEXT NOT NULL,
            public_audio INTEGER DEFAULT 0,
            created_at INTEGER
        );
        CREATE TABLE IF NOT EXISTS tokens (
            token TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            created_at INTEGER
        );
        CREATE TABLE IF NOT EXISTS scores (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            date TEXT, ts INTEGER,
            cat TEXT, topic TEXT, round INTEGER,
            wpm INTEGER, words INTEGER, fillers INTEGER, sec INTEGER,
            ai_score INTEGER, ai_json TEXT
        );
        CREATE TABLE IF NOT EXISTS recordings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            date TEXT, ts INTEGER,
            cat TEXT, topic TEXT, round INTEGER,
            sec INTEGER, wpm INTEGER, ai_score INTEGER,
            mime TEXT, path TEXT
        );
        """)
        cols = [r[1] for r in c.execute("PRAGMA table_info(recordings)")]
        if "transcript" not in cols:
            c.execute("ALTER TABLE recordings ADD COLUMN transcript TEXT")
        if "ai_json" not in cols:
            c.execute("ALTER TABLE recordings ADD COLUMN ai_json TEXT")
        ucols = [r[1] for r in c.execute("PRAGMA table_info(users)")]
        if "chat_fix_level" not in ucols:
            c.execute("ALTER TABLE users ADD COLUMN chat_fix_level TEXT DEFAULT 'standard'")
        c.executescript("""
        CREATE TABLE IF NOT EXISTS chat_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            ts INTEGER, role TEXT, content TEXT, fix_json TEXT
        );
        CREATE TABLE IF NOT EXISTS chat_memory (
            user_id INTEGER PRIMARY KEY,
            summary TEXT, last_msg_id INTEGER DEFAULT 0, updated INTEGER
        );
        """)
        ccols = [r[1] for r in c.execute("PRAGMA table_info(chat_messages)")]
        if "channel" not in ccols:
            c.execute("ALTER TABLE chat_messages ADD COLUMN channel TEXT")
        if "typed_note" not in ccols:
            c.execute("ALTER TABLE chat_messages ADD COLUMN typed_note TEXT")
        if "photo_id" not in ccols:
            c.execute("ALTER TABLE chat_messages ADD COLUMN photo_id INTEGER")
        c.executescript("""
        CREATE TABLE IF NOT EXISTS photos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            ts INTEGER, date TEXT,
            caption TEXT, tags_json TEXT, album TEXT,
            mime TEXT, path TEXT
        );
        CREATE TABLE IF NOT EXISTS reminders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            text TEXT, rtype TEXT, weekday INTEGER, time TEXT,
            fire_at INTEGER, active INTEGER DEFAULT 1, created INTEGER
        );
        CREATE TABLE IF NOT EXISTS push_subs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            endpoint TEXT UNIQUE,
            sub_json TEXT, created INTEGER
        );
        CREATE TABLE IF NOT EXISTS vocab_tests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            ts INTEGER, date TEXT,
            estimate INTEGER, low INTEGER, high INTEGER,
            overclaim REAL, details TEXT
        );
        """)


def hash_pw(pw, salt_hex):
    return hashlib_pbkdf2(pw, salt_hex)


def hashlib_pbkdf2(pw, salt_hex):
    import hashlib
    return hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt_hex), 200000).hex()


def calc_streak(dates):
    if not dates:
        return 0
    ds = sorted(set(dates), reverse=True)
    today = date.today()
    latest = date.fromisoformat(ds[0])
    if (today - latest).days > 1:
        return 0
    n, cur = 1, latest
    for s in ds[1:]:
        d = date.fromisoformat(s)
        if (cur - d).days == 1:
            n += 1
            cur = d
        elif (cur - d).days > 1:
            break
    return n


# ---------- Whisper 服务器端转写（可选：安装了 faster-whisper 才启用） ----------
WHISPER_LOCK = threading.Lock()
_whisper = {"model": None, "ok": None}


def whisper_available():
    if _whisper["ok"] is None:
        try:
            import faster_whisper  # noqa: F401
            _whisper["ok"] = True
        except Exception:
            _whisper["ok"] = False
    return _whisper["ok"]


def get_whisper():
    if _whisper["model"] is None:
        from faster_whisper import WhisperModel
        _whisper["model"] = WhisperModel(
            os.environ.get("SG_WHISPER_MODEL", "small"),
            device="cpu",
            compute_type="int8",
            cpu_threads=int(os.environ.get("SG_WHISPER_THREADS", "8")),
        )
    return _whisper["model"]


def warm_whisper():
    with WHISPER_LOCK:
        get_whisper()


def transcribe_file(path):
    with WHISPER_LOCK:
        segments, _info = get_whisper().transcribe(path, language="en", vad_filter=True, beam_size=5)
        return " ".join(s.text.strip() for s in segments).strip()


WORD_RE = re.compile(r"[A-Za-z0-9']+")
FILLER_RE_PY = re.compile(r"\b(um|uh|er|erm|hmm|you\s+know|i\s+mean|like)\b", re.I)

# ---------- 跟读相似度打分 ----------
CONTRACTIONS = {
    "i'm": "i am", "i'd": "i would", "i've": "i have", "i'll": "i will",
    "you're": "you are", "you've": "you have", "you'd": "you would", "you'll": "you will",
    "he's": "he is", "she's": "she is", "it's": "it is", "that's": "that is",
    "we're": "we are", "we've": "we have", "we'd": "we would", "we'll": "we will",
    "they're": "they are", "they've": "they have", "they'd": "they would", "they'll": "they will",
    "don't": "do not", "doesn't": "does not", "didn't": "did not",
    "can't": "cannot", "couldn't": "could not", "won't": "will not", "wouldn't": "would not",
    "shouldn't": "should not", "isn't": "is not", "aren't": "are not",
    "wasn't": "was not", "weren't": "were not",
    "haven't": "have not", "hasn't": "has not", "hadn't": "had not",
    "let's": "let us", "there's": "there is", "what's": "what is", "who's": "who is",
    "how's": "how is", "where's": "where is",
    "gonna": "going to", "wanna": "want to", "gotta": "got to", "kinda": "kind of",
}


def norm_words(s):
    toks = re.sub(r"[^a-z0-9' ]+", " ", s.lower()).split()
    out = []
    for t in toks:
        t = t.strip("'")
        if t in CONTRACTIONS:
            out.extend(CONTRACTIONS[t].split())
        elif t:
            out.append(t)
    return out


def lcs_len(a, b):
    dp = [[0] * (len(b) + 1) for _ in range(len(a) + 1)]
    for i in range(1, len(a) + 1):
        for j in range(1, len(b) + 1):
            dp[i][j] = dp[i - 1][j - 1] + 1 if a[i - 1] == b[j - 1] else max(dp[i - 1][j], dp[i][j - 1])
    return dp[len(a)][len(b)]


def shadow_similarity(target, said):
    t, s = norm_words(target), norm_words(said)
    if not t or not s:
        return 0
    return int(round(100.0 * lcs_len(t, s) / len(t)))


# ---------- 语调起伏度（音高半音标准差） ----------
def load_audio_mono(path, sr=16000):
    import av
    import numpy as np
    chunks = []
    with av.open(path) as container:
        resampler = av.AudioResampler(format="s16", layout="mono", rate=sr)
        for frame in container.decode(container.streams.audio[0]):
            for f in resampler.resample(frame):
                chunks.append(f.to_ndarray())
    if not chunks:
        return None, sr
    audio = np.concatenate(chunks, axis=1).flatten().astype("float32") / 32768.0
    return audio, sr


def pitch_variability(path):
    """返回音高起伏度（半音标准差），无法分析时返回 None。"""
    try:
        import numpy as np
        import parselmouth
        audio, sr = load_audio_mono(path)
        if audio is None or len(audio) < sr:
            return None
        snd = parselmouth.Sound(audio, sampling_frequency=sr)
        pitch = snd.to_pitch(time_step=0.01, pitch_floor=75, pitch_ceiling=500)
        f0 = pitch.selected_array["frequency"]
        f0 = f0[f0 > 0]
        if len(f0) < 20:
            return None
        semitones = 12.0 * np.log2(f0 / np.median(f0))
        semitones = semitones[np.abs(semitones) <= 12]  # 去除倍频跟踪错误的离群点
        if len(semitones) < 20:
            return None
        return round(float(np.std(semitones)), 1)
    except Exception as e:
        sys.stderr.write("pitch analysis failed: %s\n" % e)
        return None


# ---------- 神经网络 TTS（edge-tts，带磁盘缓存） ----------
TTS_VOICES = {"aria": "en-US-AriaNeural", "guy": "en-US-GuyNeural", "jenny": "en-US-JennyNeural"}
TTS_RATES = {"0.75": "-25%", "0.9": "-10%", "1.0": "+0%"}


def tts_generate(text, voice, rate, out_path):
    import asyncio
    import edge_tts

    async def run():
        await asyncio.wait_for(edge_tts.Communicate(text, voice, rate=rate).save(out_path), 20)

    asyncio.run(run())


# ---------- DeepSeek 评分 ----------
SCORE_SYSTEM = (
    "You are a rigorous but supportive English speaking examiner for Chinese intermediate learners (CEFR B1-B2). "
    "You receive the ASR transcript of a spoken answer (no punctuation; ASR may garble homophones — never penalize "
    "likely ASR artifacts), plus stats: duration in seconds, words-per-minute (wpm), filler count, and the round "
    "number (learners speak on the SAME topic for 3 rounds with decreasing time, so round 2-3 should sound more fluent "
    "than round 1; judge accordingly).\n\n"
    "SCORING RUBRIC — be consistent, use the full range. Calibration: a typical B1-B2 answer lands at 60-78; "
    "85+ means genuinely impressive for a learner; 95+ near-native. Never inflate.\n"
    "- fluency (0-100): flow and pace. Guide: wpm below 70 or more than 6 fillers/min -> below 60; "
    "wpm 90-110 with few fillers -> 65-75; wpm 120+ with natural linking -> 80+. "
    "Penalize abandoned sentences and long self-repairs; reward connected speech and varied sentence rhythm.\n"
    "- vocabulary (0-100): range, precision, idiomaticity. Penalize heavy repetition of basic words (very, good, "
    "thing, make), word-for-word Chinese translations, and vagueness. Reward precise verbs, natural collocations, "
    "and idiomatic chunks used correctly.\n"
    "- grammar (0-100): weigh recurring systematic errors (tense consistency, third-person -s, articles, plurals, "
    "prepositions) much more than one-off slips. Ignore punctuation and capitalization entirely.\n"
    "- content (0-100): direct relevance to the topic, concrete details and examples versus vague generalities, "
    "recognizable structure (a point, development, a close), and depth of personal reflection.\n"
    "- score (0-100): weighted overall = fluency 30% + vocabulary 25% + grammar 20% + content 25%, rounded.\n\n"
    "THEN PRODUCE:\n"
    "- fixes: up to 3 of the learner's most instructive REAL mistakes. Quote their actual words, give the natural "
    "spoken version, add a brief Chinese explanation. Prioritize recurring/systematic problems over slips.\n"
    "- upgrade: up to 2 vivid, idiomatic expressions that would elevate exactly what THEY were trying to say "
    "(not generic vocabulary lists).\n"
    "- model: a natural SPOKEN model answer of 80-140 words that expresses THE LEARNER'S OWN ideas, stories and "
    "personal details from their transcript — NOT a generic essay. Keep their meaning and specifics; fix all errors; "
    "use contractions and natural spoken discourse markers; weave in 1-2 idiomatic chunks they could realistically "
    "reuse. If their content was thin, keep their core idea and demonstrate how to develop it with one concrete "
    "detail. It must sound like something a real person says out loud, not written prose.\n"
    "- comment_zh: 两三句中文总评：先具体指出本轮最大的亮点（引用原话），再点出最值得优先改进的一个问题和练习方法。\n\n"
    "Respond with JSON ONLY:\n"
    '{"score": <int>, "dims": {"fluency": <int>, "vocabulary": <int>, "grammar": <int>, "content": <int>}, '
    '"fixes": [{"original": "...", "better": "...", "why_zh": "..."}], '
    '"upgrade": [{"expression": "...", "usage_zh": "..."}], '
    '"model": "...", "comment_zh": "..."}'
)


def deepseek_call(messages, temperature=0.3, max_tokens=900, json_mode=True):
    body = {"model": "deepseek-chat", "messages": messages, "temperature": temperature, "max_tokens": max_tokens}
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    req = urllib.request.Request(
        "https://api.deepseek.com/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + DEEPSEEK_KEY},
    )
    for attempt in range(2):  # 瞬时故障自动重试一次
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                resp = json.load(r)
            choice = resp["choices"][0]
            if choice.get("finish_reason") == "length":
                sys.stderr.write("deepseek reply truncated by max_tokens=%s\n" % max_tokens)
            return choice["message"]["content"]
        except Exception as e:
            sys.stderr.write("deepseek attempt %d failed: %s\n" % (attempt + 1, e))
            if attempt == 1:
                raise
            time.sleep(2)


def deepseek_score(payload):
    content = deepseek_call(
        [{"role": "system", "content": SCORE_SYSTEM},
         {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
        temperature=0.3, max_tokens=1500,
    )
    data = json.loads(content)
    data["score"] = max(0, min(100, int(data.get("score", 0))))
    dims = data.get("dims") or {}
    data["dims"] = {k: max(0, min(100, int(dims.get(k, 0)))) for k in ("fluency", "vocabulary", "grammar", "content")}
    data["fixes"] = (data.get("fixes") or [])[:3]
    data["upgrade"] = (data.get("upgrade") or [])[:2]
    data["model"] = str(data.get("model", "")).strip()
    data["comment_zh"] = str(data.get("comment_zh", ""))
    return data


# ---------- AI 对话（带长期记忆） ----------
FIX_RULES = {
    "light": (
        "4. Corrections: only add a fix when an error genuinely hurts understanding or sounds jarring; "
        "otherwise use null. At most one fix per turn."
    ),
    "standard": (
        "4. Corrections: if their message contains one notable error or unnatural phrasing, add a brief fix "
        "(their original words, the natural version, short Chinese explanation). Skip trivial slips and "
        "speech-recognition artifacts; at most one fix per turn, or null."
    ),
    "strict": (
        "4. Corrections: check EVERY message carefully. If there is ANY grammatical error or unnatural phrasing, "
        "you MUST include a fix for the most instructive one (their original words, the natural version, short "
        "Chinese explanation). Only return null when the message is genuinely flawless. Ignore pure "
        "speech-recognition artifacts (impossible homophones), but never let real learner errors slide."
    ),
}

STRICT_CHECK_SYSTEM = (
    "You are a precise English grammar checker for a Chinese learner's spoken sentence (an ASR transcript; ignore "
    "punctuation, casing and obvious mishearings). If the sentence contains a real grammatical error or clearly "
    "unnatural phrasing, return the single most instructive fix. If it is acceptable spoken English, return null. "
    'JSON ONLY: {"fix": {"original": "...", "better": "...", "why_zh": "..."} or null}'
)

CHAT_SYSTEM_TMPL = (
    "You are Buddy, a warm, witty English conversation partner and speaking coach for a Chinese intermediate learner "
    "(CEFR B1-B2). Rules:\n"
    "1. Reply in natural spoken English, 2-4 short sentences, so the learner does most of the talking. Exception: "
    "when they explicitly ask for a story, an explanation or an example, you may go up to ~180 words — but always "
    "finish your last sentence. Ask at most ONE follow-up question, digging into what they actually said. "
    "Be genuinely curious.\n"
    "2. Use the MEMORY notes to stay consistent and personal: reference their name, job, plans and past "
    "conversations naturally when relevant, like a friend who remembers.\n"
    "3. Keep vocabulary mostly B1-B2, but occasionally drop ONE vivid idiomatic expression worth learning.\n"
    "{fix_rule}\n"
    "5. Never lecture, never write essays. Keep it flowing like a real chat.\n"
    "6. Input channel tags: learner messages may start with [spoken] (voice via speech recognition — names and rare "
    "words may be garbled) and may include a [typed keywords] line (exact spellings the learner typed: treat these "
    "as the authoritative names/terms, and use them to interpret the spoken part — e.g. if the spoken text garbled a "
    "name that appears in [typed keywords], assume they mean that name). [typed] alone means the whole message was "
    "typed. NEVER echo these tags in your reply, and never correct the [typed keywords] content.\n"
    "7. Photos: [photo attached] means the learner sent a photo with that caption. If a [photo content] line is "
    "present, it is a factual description from a vision model — treat it as what the photo actually shows and react "
    "to specific details in it (including mismatches with their caption). If there is NO [photo content] line, you "
    "can NOT see the image at all: say so naturally (e.g. 'the photo isn't loading for me'), NEVER invent or imply "
    "any visual detail, and instead ask them to describe it. Either way, ask ONE question that pushes them to "
    "describe the photo in richer English (colors, people, place, the story behind it).\n"
    "8. Time awareness: the system message tells you the CURRENT TIME, and learner messages carry a [time] tag. "
    "Use them naturally (\"yesterday you mentioned...\", \"how did Monday's meeting go?\") and to resolve relative "
    "dates. Never echo the tags.\n"
    "9. Memory requests: when the learner explicitly asks you to remember something (记住 / remember / note this "
    "down), or shares a durable personal fact clearly worth keeping, put a concise English note in memory_add "
    "(otherwise null). Briefly confirm in your reply what you saved.\n"
    "10. Reminders: when the learner asks to be reminded of something, you MUST fill the reminder field — never say "
    "a reminder is set unless the field is filled (the system only creates it from the field, not from your words). "
    "Shape: text (short, in their words), type (\"once\"|\"daily\"|\"weekly\"), weekday (0-6, Monday=0, weekly "
    "only), time (\"HH:MM\" 24h, default \"09:00\"), datetime (\"YYYY-MM-DD HH:MM\", REQUIRED for once). Resolve "
    "relative dates using CURRENT TIME. Example: {{\"text\": \"take out the laundry\", \"type\": \"once\", "
    "\"datetime\": \"2026-08-28 21:06\"}}. Confirm the exact schedule in your reply. Otherwise null.\n"
    "11. Topic picking: the system message may include a TOPIC MENU (a fresh random sample from the learner's own "
    "practice-topic library, resampled every message) and a PRACTICE SNAPSHOT from their training database. When "
    "the learner asks you to pick a topic, suggest one, or asks what to talk about, choose exactly ONE menu item — "
    "announce it in one short sentence and ask ONE easy opening question to get them talking. If they name a "
    "category or theme, prefer a matching menu item; if nothing matches, improvise a topic in that spirit and say "
    "you did. During the chat, stay on the chosen topic with follow-ups unless they steer away. Use the PRACTICE "
    "SNAPSHOT like a caring coach — mention streaks, recent scores, practiced topics or vocabulary size naturally "
    "when relevant (praise progress, nudge gaps), but never dump raw data.\n"
    'Respond with JSON ONLY: {{"reply": "...", "fix": {{"original": "...", "better": "...", "why_zh": "..."}} or '
    'null, "memory_add": "..." or null, "reminder": {{"text": "...", "type": "...", "weekday": 0, "time": "HH:MM", '
    '"datetime": "..."}} or null}}'
)


# ---------- 话题库（部署时由 data.js 导出 topics.json，Buddy 每回合抽样） ----------
_TOPICS_CACHE = {"mtime": 0, "data": None}


def load_topics():
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "topics.json")
    try:
        mt = os.path.getmtime(p)
        if _TOPICS_CACHE["data"] is None or mt != _TOPICS_CACHE["mtime"]:
            with open(p, encoding="utf-8") as f:
                _TOPICS_CACHE["data"] = json.load(f).get("categories") or []
            _TOPICS_CACHE["mtime"] = mt
    except OSError:
        return []
    return _TOPICS_CACHE["data"]


def topic_menu_block():
    import random
    cats = load_topics()
    if not cats:
        return ""
    stats = "; ".join("%s %d" % (c["label"], len(c["topics"])) for c in cats)
    pool = [(c["label"], q) for c in cats for q in c["topics"]]
    sample = random.sample(pool, min(12, len(pool)))
    lines = "\n".join("- [%s] %s" % (lbl, q) for lbl, q in sample)
    return ("TOPIC MENU (learner's library: %s — %d total; random sample this turn):\n%s"
            % (stats, len(pool), lines))


def practice_snapshot_block(uid):
    with db() as c:
        rows = c.execute(
            "SELECT date, topic, ai_score FROM scores WHERE user_id=? ORDER BY ts DESC LIMIT 5", (uid,)).fetchall()
        dates = [r["date"] for r in c.execute("SELECT date FROM scores WHERE user_id=?", (uid,)).fetchall()]
        total = len(dates)
        vocab = c.execute(
            "SELECT date, estimate FROM vocab_tests WHERE user_id=? ORDER BY ts DESC LIMIT 1", (uid,)).fetchone()
    if not total and not vocab:
        return ""
    parts = []
    if total:
        parts.append("streak %d days, %d rounds total" % (calc_streak(dates), total))
        recent = "; ".join(
            "%s (%s%s)" % ((r["topic"] or "?")[:60], r["date"],
                           ", scored %s" % r["ai_score"] if r["ai_score"] is not None else "")
            for r in rows)
        parts.append("recent practice: " + recent)
    if vocab:
        parts.append("vocabulary size test: ~%s word families (%s)" % (vocab["estimate"], vocab["date"]))
    return "PRACTICE SNAPSHOT: " + " | ".join(parts)


def tag_user_content(content, channel, typed_note, photo_desc=None):
    if channel == "voice":
        base = "[spoken] " + content
    elif channel == "mixed":
        base = "[spoken] %s\n[typed keywords] %s" % (content, typed_note or "")
    else:
        base = "[typed] " + content
    if photo_desc is not None:  # None=无图；""=有图但无视觉模型；非空=视觉模型的画面描述
        base = "[photo attached]\n" + base + (("\n[photo content] " + photo_desc) if photo_desc else "")
    return base


# ---------- Web Push 通知（macOS 浏览器 + iPhone 主屏幕 PWA） ----------
def push_available():
    try:
        import pywebpush  # noqa: F401
        return True
    except Exception:
        return False


def ensure_vapid():
    p = os.path.join(DATA, "vapid.pem")
    if not os.path.exists(p):
        from py_vapid import Vapid02
        v = Vapid02()
        v.generate_keys()
        v.save_key(p)
        os.chmod(p, 0o600)
    return p


def vapid_public_b64():
    import base64
    from cryptography.hazmat.primitives import serialization
    from py_vapid import Vapid02
    v = Vapid02.from_file(ensure_vapid())
    raw = v.public_key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def push_to_user(uid, title, body):
    if not push_available():
        return 0
    from pywebpush import webpush, WebPushException
    with db() as c:
        subs = [dict(r) for r in c.execute("SELECT * FROM push_subs WHERE user_id=?", (uid,)).fetchall()]
    sent = 0
    for s in subs:
        try:
            webpush(
                json.loads(s["sub_json"]),
                json.dumps({"title": title, "body": body, "url": "/"}, ensure_ascii=False),
                vapid_private_key=ensure_vapid(),
                vapid_claims={"sub": "mailto:speaking-gym@example.com"},
                timeout=15,
            )
            sent += 1
        except WebPushException as e:
            code = getattr(getattr(e, "response", None), "status_code", None)
            if code in (404, 410):  # 订阅已失效
                with db() as c:
                    c.execute("DELETE FROM push_subs WHERE id=?", (s["id"],))
            else:
                sys.stderr.write("push failed (sub %s): %s\n" % (s["id"], e))
        except Exception as e:
            sys.stderr.write("push failed (sub %s): %s\n" % (s["id"], e))
    return sent


# ---------- 定时提醒 ----------
def next_fire(rtype, weekday, time_str, base=None):
    now = base or datetime.now()
    hh, mm = map(int, (time_str or "09:00").split(":"))
    cand = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if rtype == "daily":
        if cand <= now:
            cand += timedelta(days=1)
    else:  # weekly
        cand += timedelta(days=(int(weekday) - cand.weekday()) % 7)
        if cand <= now:
            cand += timedelta(days=7)
    return int(cand.timestamp())


def schedule_label(r):
    wd = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    if r["rtype"] == "once":
        return datetime.fromtimestamp(r["fire_at"]).strftime("%Y-%m-%d %H:%M")
    if r["rtype"] == "daily":
        return "每天 %s" % r["time"]
    return "每%s %s" % (wd[int(r["weekday"] or 0)], r["time"])


def reminders_loop():
    """每 30 秒检查一次到期提醒：写入对话 + 推送到所有订阅设备。"""
    while True:
        time.sleep(30)
        try:
            now = int(time.time())
            with db() as c:
                due = [dict(r) for r in c.execute(
                    "SELECT * FROM reminders WHERE active=1 AND fire_at<=?", (now,)).fetchall()]
            for r in due:
                body = "Reminder: %s" % r["text"]
                ts = int(time.time() * 1000)
                with db() as c:
                    c.execute(
                        "INSERT INTO chat_messages (user_id, ts, role, content) VALUES (?,?,?,?)",
                        (r["user_id"], ts, "assistant", body),
                    )
                    if r["rtype"] == "once":
                        c.execute("UPDATE reminders SET active=0 WHERE id=?", (r["id"],))
                    else:
                        c.execute("UPDATE reminders SET fire_at=? WHERE id=?",
                                  (next_fire(r["rtype"], r["weekday"], r["time"]), r["id"]))
                n = push_to_user(r["user_id"], "Buddy 提醒", r["text"])
                sys.stderr.write("reminder %s fired (user %s, pushed to %d devices)\n" % (r["id"], r["user_id"], n))
        except Exception as e:
            sys.stderr.write("reminders loop error: %s\n" % e)


# ---------- 视觉模型（deepseek-v4-flash-vision-exp / qwen3.8-flash，可配置切换） ----------
def vision_describe(path, mime, caption):
    import base64
    with open(path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    content = [
        {"type": "image_url", "image_url": {"url": "data:%s;base64,%s" % (mime or "image/jpeg", b64)}},
        {"type": "text", "text": (
            "Describe this photo factually in 2-4 English sentences: scene, people (count only), objects, "
            "any visible text (transcribe it), food, mood. Caption from the photo owner for context: %s"
            % (caption or "(none)")
        )},
    ]
    if VISION_PROVIDER == "deepseek":
        url = "https://api.deepseek.com/chat/completions"
        key = DEEPSEEK_KEY
        body = {"model": DEEPSEEK_VISION_MODEL, "messages": [{"role": "user", "content": content}],
                "max_tokens": 400, "thinking": {"type": "disabled"}}  # 实验版默认思考，必须显式关闭
    else:
        url = QWEN_BASE + "/chat/completions"
        key = QWEN_KEY
        body = {"model": QWEN_VISION_MODEL, "messages": [{"role": "user", "content": content}],
                "max_tokens": 400, "enable_thinking": False}
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + key},
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        resp = json.load(r)
    return resp["choices"][0]["message"]["content"].strip()


TAG_SYSTEM = (
    "You tag photos in a personal library. Given the owner's caption, an optional vision-model description and the "
    "chat reply, produce 3-8 short keyword tags (English words or Chinese, whichever fits: places, people/pets by "
    "name if mentioned, activities, objects, food, mood). The photo_description is the ACTUAL image content — when "
    "it conflicts with the caption, trust the photo_description; use the caption only for names and context the "
    "image cannot show. "
    'JSON ONLY: {"tags": ["...", "..."]}'
)


def tag_photo(pid, caption, vision_desc, reply):
    try:
        data = parse_chat_json(deepseek_call(
            [{"role": "system", "content": TAG_SYSTEM},
             {"role": "user", "content": json.dumps(
                 {"caption": caption, "photo_description": vision_desc or None, "chat_reply": reply},
                 ensure_ascii=False)}],
            temperature=0.2, max_tokens=200,
        ))
        tags = [str(t).strip()[:30] for t in (data.get("tags") or []) if str(t).strip()][:8]
        if tags:
            with db() as c:
                c.execute("UPDATE photos SET tags_json=? WHERE id=?", (json.dumps(tags, ensure_ascii=False), pid))
    except Exception as e:
        sys.stderr.write("tag photo %s failed: %s\n" % (pid, e))


ORGANIZE_SYSTEM = (
    "You organize a personal photo library. Given photos with id, date, caption and tags, group ALL of them into "
    "3-10 themed albums with concise Chinese names (例：港岛徒步、美食记录、和朋友的周末). Prefer themes over "
    "dates; use dates only when a clear event emerges. Every photo id must appear in exactly one album. "
    'JSON ONLY: {"albums": [{"name": "...", "photo_ids": [1, 2]}]}'
)


def organize_photos(uid):
    with db() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT id, date, caption, tags_json FROM photos WHERE user_id=? ORDER BY ts ASC", (uid,)
        ).fetchall()]
    if len(rows) < 4:
        return {"organized": 0, "albums": []}
    payload = [{"id": r["id"], "date": r["date"], "caption": (r["caption"] or "")[:100],
                "tags": json.loads(r["tags_json"]) if r["tags_json"] else []} for r in rows]
    data = parse_chat_json(deepseek_call(
        [{"role": "system", "content": ORGANIZE_SYSTEM},
         {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
        temperature=0.2, max_tokens=1200,
    ))
    valid_ids = {r["id"] for r in rows}
    albums, n = [], 0
    with db() as c:
        for alb in (data.get("albums") or []):
            name = str(alb.get("name", "")).strip()[:40]
            ids = [i for i in (alb.get("photo_ids") or []) if i in valid_ids]
            if not name or not ids:
                continue
            albums.append({"name": name, "count": len(ids)})
            n += len(ids)
            c.execute(
                "UPDATE photos SET album=? WHERE user_id=? AND id IN (%s)" % ",".join(map(str, ids)),
                (name, uid),
            )
    return {"organized": n, "albums": albums}


def auto_organize_loop():
    """每 24 小时检查一次：未整理照片攒到 8 张以上的用户，自动整理图库。"""
    while True:
        time.sleep(24 * 3600)
        try:
            with db() as c:
                uids = [r[0] for r in c.execute(
                    "SELECT user_id FROM photos WHERE album IS NULL GROUP BY user_id HAVING COUNT(*)>=8"
                )]
            for uid in uids:
                organize_photos(uid)
                sys.stderr.write("auto organized photos for user %s\n" % uid)
        except Exception as e:
            sys.stderr.write("auto organize failed: %s\n" % e)

SUMMARY_SYSTEM = (
    "You maintain long-term memory notes about an English learner, based on their chats with an AI partner. "
    "Merge the existing notes and the new conversation excerpt into updated notes, max 250 words, in English. "
    "Keep: personal facts (name, job, family, hobbies), preferences, ongoing plans and events, recurring English "
    "mistakes, and topics already discussed (so the partner never repeats itself). Drop small talk. "
    "Respond with the plain-text notes only."
)


def parse_chat_json(content):
    """模型输出的容错解析：裸 JSON → 提取 {} 块 → 整段当作 reply。"""
    content = (content or "").strip()
    if content.startswith("```"):
        content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content, flags=re.S).strip()
    try:
        return json.loads(content)
    except Exception:
        m = re.search(r"\{.*\}", content, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                pass
        # JSON 被截断时，抢救出 reply 字符串已生成的部分
        m = re.search(r'"reply"\s*:\s*"((?:[^"\\]|\\.)*)', content)
        if m:
            try:
                return {"reply": json.loads('"' + m.group(1) + '"'), "fix": None}
            except Exception:
                return {"reply": m.group(1), "fix": None}
    sys.stderr.write("chat json fallback, raw head: %r\n" % content[:200])
    return {"reply": content, "fix": None}


def chat_turn(uid, text, channel="text", typed_note=None, photo_id=None):
    photo_desc = None
    photo = None
    if photo_id:
        with db() as c:
            photo = c.execute("SELECT * FROM photos WHERE id=? AND user_id=?", (int(photo_id), uid)).fetchone()
        if photo:
            photo_desc = ""
            if vision_available():
                try:
                    photo_desc = vision_describe(
                        os.path.join(PHOTO_DIR, photo["path"]), photo["mime"], text)
                except Exception as e:
                    sys.stderr.write("vision failed: %s\n" % e)
        else:
            photo_id = None
    with db() as c:
        mem = c.execute("SELECT summary FROM chat_memory WHERE user_id=?", (uid,)).fetchone()
        urow = c.execute("SELECT chat_fix_level FROM users WHERE id=?", (uid,)).fetchone()
        recent = [dict(r) for r in c.execute(
            "SELECT ts, role, content, channel, typed_note, photo_id FROM chat_messages "
            "WHERE user_id=? ORDER BY id DESC LIMIT 16",
            (uid,)
        ).fetchall()][::-1]
    level = (urow["chat_fix_level"] if urow else None) or "standard"
    system = CHAT_SYSTEM_TMPL.format(fix_rule=FIX_RULES.get(level, FIX_RULES["standard"]))
    now = datetime.now()
    wd_en = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"][now.weekday()]
    time_line = "CURRENT TIME: %s (%s), timezone Asia/Hong_Kong." % (now.strftime("%Y-%m-%d %H:%M"), wd_en)
    summary = (mem["summary"] if mem else "") or "(nothing yet — the conversation just started)"
    extra = ""
    menu = topic_menu_block()
    if menu:
        extra += "\n\n" + menu
    snap = practice_snapshot_block(uid)
    if snap:
        extra += "\n\n" + snap
    msgs = [{"role": "system", "content": system + "\n\n" + time_line + extra
             + "\n\nMEMORY ABOUT THE LEARNER:\n" + summary}]
    for m in recent:
        if m["role"] == "assistant":
            # 历史 assistant 消息统一还原成完整 JSON 形态，让模型持续模仿全字段输出格式
            msgs.append({"role": "assistant", "content": json.dumps(
                {"reply": m["content"], "fix": None, "memory_add": None, "reminder": None}, ensure_ascii=False)})
        else:
            ttag = "[time %s] " % datetime.fromtimestamp((m["ts"] or 0) / 1000).strftime("%m-%d %H:%M")
            msgs.append({"role": "user", "content": ttag + tag_user_content(
                m["content"], m["channel"] or "text", m["typed_note"],
                "" if m["photo_id"] else None)})
    msgs.append({"role": "user", "content": ("[time %s] " % now.strftime("%m-%d %H:%M"))
                 + tag_user_content(text, channel, typed_note, photo_desc)})
    data = parse_chat_json(deepseek_call(msgs, temperature=0.7, max_tokens=1000))
    reply = str(data.get("reply", "")).strip() or "Sorry, could you say that again?"
    fix = data.get("fix") or None

    # 备忘：明确要求记住的内容直接追加进长期记忆笔记
    memory_added = None
    mem_add = data.get("memory_add")
    if isinstance(mem_add, str) and mem_add.strip():
        memory_added = mem_add.strip()[:300]
        with db() as c:
            row_m = c.execute("SELECT summary FROM chat_memory WHERE user_id=?", (uid,)).fetchone()
            base_s = ((row_m["summary"] if row_m else "") or "").rstrip()
            new_s = (base_s + ("\n" if base_s else "") + "* [%s] %s" % (date.today().isoformat(), memory_added))[:8000]
            if row_m:
                c.execute("UPDATE chat_memory SET summary=?, updated=? WHERE user_id=?",
                          (new_s, int(time.time()), uid))
            else:
                c.execute("INSERT INTO chat_memory (user_id, summary, last_msg_id, updated) VALUES (?,?,0,?)",
                          (uid, new_s, int(time.time())))

    # 定时提醒：解析并入库
    reminder_set = None
    rem = data.get("reminder") if isinstance(data.get("reminder"), dict) else None
    if rem:
        try:
            rtype = rem.get("type")
            rtext = str(rem.get("text") or "").strip()[:200]
            if rtype in ("once", "daily", "weekly") and rtext:
                if rtype == "once":
                    dt = datetime.strptime(str(rem.get("datetime")), "%Y-%m-%d %H:%M")
                    fire, weekday_v, time_v = int(dt.timestamp()), None, dt.strftime("%H:%M")
                else:
                    time_v = str(rem.get("time") or "09:00")
                    weekday_v = int(rem.get("weekday") or 0) if rtype == "weekly" else None
                    fire = next_fire(rtype, weekday_v, time_v)
                if fire > time.time():
                    with db() as c:
                        cur = c.execute(
                            "INSERT INTO reminders (user_id, text, rtype, weekday, time, fire_at, active, created) "
                            "VALUES (?,?,?,?,?,?,1,?)",
                            (uid, rtext, rtype, weekday_v, time_v, fire, int(time.time())),
                        )
                    reminder_set = {"id": cur.lastrowid, "text": rtext, "rtype": rtype,
                                    "weekday": weekday_v, "time": time_v, "fire_at": fire}
                    reminder_set["label"] = schedule_label(reminder_set)
        except Exception as e:
            sys.stderr.write("reminder parse failed: %s (%r)\n" % (e, rem))

    if level == "strict" and not fix:
        # 严格模式下主回复没给纠错时，用低温度的专职语法检查兜底
        try:
            data2 = parse_chat_json(deepseek_call(
                [{"role": "system", "content": STRICT_CHECK_SYSTEM}, {"role": "user", "content": text}],
                temperature=0.0, max_tokens=300,
            ))
            fix = data2.get("fix") or None
        except Exception as e:
            sys.stderr.write("strict check failed: %s\n" % e)
    now = int(time.time() * 1000)
    with db() as c:
        c.execute(
            "INSERT INTO chat_messages (user_id, ts, role, content, channel, typed_note, photo_id) "
            "VALUES (?,?,?,?,?,?,?)",
            (uid, now, "user", text, channel, typed_note, photo_id),
        )
        c.execute(
            "INSERT INTO chat_messages (user_id, ts, role, content, fix_json) VALUES (?,?,?,?,?)",
            (uid, now + 1, "assistant", reply, json.dumps(fix, ensure_ascii=False) if fix else None),
        )
        if photo_id:
            c.execute("UPDATE photos SET caption=? WHERE id=? AND user_id=?", (text[:300], int(photo_id), uid))
    if photo_id:
        threading.Thread(target=tag_photo, args=(int(photo_id), text, photo_desc, reply), daemon=True).start()
    threading.Thread(target=maybe_summarize, args=(uid,), daemon=True).start()
    return {"reply": reply, "fix": fix, "photo_desc": photo_desc or None,
            "memory_added": memory_added, "reminder_set": reminder_set}


def check_password(user, pw):
    return bool(pw) and hash_pw(pw, user["salt"]) == user["pw_hash"]


def distill_memory(uid):
    """清空对话前调用：把现有笔记 + 未归纳的消息提炼成新的长期记忆。"""
    with db() as c:
        mem = c.execute("SELECT * FROM chat_memory WHERE user_id=?", (uid,)).fetchone()
        last_id = mem["last_msg_id"] if mem else 0
        rows = [dict(r) for r in c.execute(
            "SELECT role, content, typed_note FROM chat_messages WHERE user_id=? AND id>? ORDER BY id ASC LIMIT 150",
            (uid, last_id)
        ).fetchall()]
    old = (mem["summary"] if mem else "") or ""
    if not rows:
        return old or None
    convo = "\n".join(
        "%s: %s%s" % (
            "Learner" if r["role"] == "user" else "Buddy",
            r["content"],
            (" (typed: %s)" % r["typed_note"]) if r.get("typed_note") else "",
        ) for r in rows
    )
    return deepseek_call(
        [{"role": "system", "content": SUMMARY_SYSTEM},
         {"role": "user", "content": "EXISTING NOTES:\n%s\n\nNEW CONVERSATION:\n%s" % (old or "(none)", convo)}],
        temperature=0.2, max_tokens=500, json_mode=False,
    ).strip()


def maybe_summarize(uid):
    """未摘要消息超过 40 条时，把较早的部分并入长期记忆笔记。"""
    try:
        with db() as c:
            mem = c.execute("SELECT * FROM chat_memory WHERE user_id=?", (uid,)).fetchone()
            last_id = mem["last_msg_id"] if mem else 0
            rows = [dict(r) for r in c.execute(
                "SELECT id, role, content, typed_note FROM chat_messages WHERE user_id=? AND id>? ORDER BY id ASC",
                (uid, last_id)
            ).fetchall()]
        if len(rows) < 40:
            return
        to_sum = rows[:-12]
        convo = "\n".join(
            "%s: %s%s" % (
                "Learner" if r["role"] == "user" else "Buddy",
                r["content"],
                (" (typed: %s)" % r["typed_note"]) if r.get("typed_note") else "",
            ) for r in to_sum
        )
        old = (mem["summary"] if mem else "") or "(none)"
        new_summary = deepseek_call(
            [{"role": "system", "content": SUMMARY_SYSTEM},
             {"role": "user", "content": "EXISTING NOTES:\n%s\n\nNEW CONVERSATION:\n%s" % (old, convo)}],
            temperature=0.2, max_tokens=500, json_mode=False,
        ).strip()
        with db() as c:
            c.execute(
                "INSERT INTO chat_memory (user_id, summary, last_msg_id, updated) VALUES (?,?,?,?) "
                "ON CONFLICT(user_id) DO UPDATE SET summary=excluded.summary, "
                "last_msg_id=excluded.last_msg_id, updated=excluded.updated",
                (uid, new_summary, to_sum[-1]["id"], int(time.time())),
            )
        sys.stderr.write("chat memory updated for user %s (%d msgs folded)\n" % (uid, len(to_sum)))
    except Exception as e:
        sys.stderr.write("summarize failed: %s\n" % e)


def admin_audit(ip, action, result, detail=""):
    row = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "ip": ip,
        "action": action,
        "result": result,
        "detail": str(detail)[:300],
    }
    try:
        with open(ADMIN_AUDIT_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        os.chmod(ADMIN_AUDIT_PATH, 0o600)
    except OSError:
        pass


def deployed_commit():
    try:
        value = open(ADMIN_DEPLOYED_PATH, encoding="ascii").read().strip()
        return value if re.fullmatch(r"[0-9a-f]{40}", value) else None
    except OSError:
        return None


def build_version():
    try:
        text = open(os.path.join(BASE, "index.html"), encoding="utf-8").read(10000)
        match = re.search(r'class="ver">([^<]+)', text)
        return match.group(1) if match else "unknown"
    except OSError:
        return "unknown"


def admin_status_data():
    with db() as c:
        counts = {
            "users": c.execute("SELECT COUNT(*) FROM users").fetchone()[0],
            "practice_rounds": c.execute("SELECT COUNT(*) FROM scores").fetchone()[0],
            "recordings": c.execute("SELECT COUNT(*) FROM recordings").fetchone()[0],
            "active_reminders": c.execute("SELECT COUNT(*) FROM reminders WHERE active=1").fetchone()[0],
        }
    disk = shutil.disk_usage(DATA)
    return {
        "ok": True,
        "service": "speaking-gym",
        "version": build_version(),
        "deployed_commit": deployed_commit(),
        "pid": os.getpid(),
        "uptime_seconds": int(time.time() - STARTED_AT),
        "database_bytes": os.path.getsize(DB_PATH) if os.path.exists(DB_PATH) else 0,
        "disk_free_bytes": disk.free,
        "counts": counts,
    }


def admin_health_data():
    checks = {}
    try:
        with db() as c:
            checks["database"] = c.execute("PRAGMA quick_check").fetchone()[0]
    except Exception:
        checks["database"] = "error"
    checks.update({
        "deepseek_configured": bool(DEEPSEEK_KEY),
        "vision_available": vision_available(),
        "whisper_available": whisper_available(),
        "edge_tts_available": importlib.util.find_spec("edge_tts") is not None,
        "push_available": push_available(),
        "update_script": os.path.isfile(os.path.join(BASE, "admin_update.sh")),
        "restart_script": os.path.isfile(os.path.join(BASE, "run.sh")),
        "admin_token_configured": len(ADMIN_TOKEN) >= 32,
    })
    return {"ok": checks["database"] == "ok", "checks": checks}


def admin_log_tail():
    path = os.path.join(BASE, "server.log")
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            end = f.tell()
            f.seek(max(0, end - 65536))
            text = f.read().decode("utf-8", "replace")
    except OSError:
        return []
    for value in (DEEPSEEK_KEY, QWEN_KEY, ADMIN_TOKEN):
        if value:
            text = text.replace(value, "[REDACTED]")
    text = re.sub(r"sk-[A-Za-z0-9_-]{8,}", "sk-[REDACTED]", text)
    return text.splitlines()[-100:]


def admin_backup_db():
    folder = os.path.join(DATA, "backups")
    os.makedirs(folder, mode=0o700, exist_ok=True)
    name = "gym-%s.db" % datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    target = os.path.join(folder, name)
    source = sqlite3.connect(DB_PATH, timeout=15)
    destination = sqlite3.connect(target)
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()
    check = sqlite3.connect(target)
    try:
        integrity = check.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        check.close()
    if integrity != "ok":
        os.remove(target)
        raise RuntimeError("backup integrity check failed")
    os.chmod(target, 0o600)
    return {"file": name, "bytes": os.path.getsize(target), "integrity": integrity}


def admin_run_update():
    script = os.path.join(BASE, "admin_update.sh")
    if not os.path.isfile(script):
        raise RuntimeError("update script is not installed")
    result = subprocess.run(
        ["/bin/bash", script], cwd=BASE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, timeout=180, check=False,
    )
    output = (result.stdout or "").splitlines()[-30:]
    if result.returncode:
        raise RuntimeError("update failed (%d): %s" % (result.returncode, " | ".join(output[-5:])))
    changed = not any(line.startswith("already current:") for line in output)
    return {"updated": changed, "output": output, "restart_required": changed, "deployed_commit": deployed_commit()}


# ---------- HTTP ----------
class Handler(http.server.SimpleHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = 60  # 防止慢速/恶意连接长期占用工作线程

    def end_headers(self):
        # 代码类静态资源禁止启发式缓存：每次都向服务器校验，保证部署即生效
        try:
            p = urllib.parse.urlparse(self.path).path
            if p.startswith("/api/admin/"):
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
            elif not p.startswith("/api/") and (p == "/" or re.search(r"\.(html|js|css|webmanifest)$", p)):
                self.send_header("Cache-Control", "no-cache")
        except Exception:
            pass
        super().end_headers()

    # --- 工具 ---
    def send_json(self, obj, code=200):
        raw = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def fail(self, msg, code=400):
        self.send_json({"error": msg}, code)

    def body_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0 or n > 2_000_000:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode())
        except Exception:
            return {}

    def body_raw(self, limit=30_000_000):
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0 or n > limit:
            return b""
        return self.rfile.read(n)

    def token(self):
        h = self.headers.get("Authorization") or ""
        if h.startswith("Bearer "):
            return h[7:].strip()
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        return (q.get("t") or [""])[0]

    def auth(self):
        tok = self.token()
        if not tok:
            return None
        with db() as c:
            row = c.execute(
                "SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?", (tok,)
            ).fetchone()
        return row

    def qpath(self):
        return urllib.parse.urlparse(self.path).path

    def admin_auth(self, action, raw_body=b""):
        ip = self.client_address[0]
        if self.headers.get("Origin"):
            admin_audit(ip, action, "rejected", "browser origin not allowed")
            self.fail("unauthorized", 401)
            return False
        if len(ADMIN_TOKEN) < 32:
            self.fail("admin interface disabled", 503)
            return False
        now = time.time()
        with ADMIN_FAILURE_LOCK:
            if len(ADMIN_FAILURES) > 1000:
                ADMIN_FAILURES.clear()
            recent = [ts for ts in ADMIN_FAILURES.get(ip, []) if now - ts < 600]
            ADMIN_FAILURES[ip] = recent
            if len(recent) >= 5:
                admin_audit(ip, action, "rate_limited")
                self.fail("too many failed attempts", 429)
                return False
        timestamp = self.headers.get("X-Speaking-Gym-Timestamp") or ""
        nonce = self.headers.get("X-Speaking-Gym-Nonce") or ""
        supplied = self.headers.get("X-Speaking-Gym-Signature") or ""
        try:
            fresh = abs(now - int(timestamp)) <= 60
        except ValueError:
            fresh = False
        valid_shape = bool(re.fullmatch(r"[0-9a-f]{32}", nonce) and re.fullmatch(r"[0-9a-f]{64}", supplied))
        message = "\n".join((timestamp, nonce, self.command, self.qpath(), hashlib.sha256(raw_body).hexdigest()))
        expected = hmac.new(ADMIN_TOKEN.encode(), message.encode(), hashlib.sha256).hexdigest()
        if not fresh or not valid_shape or not hmac.compare_digest(supplied, expected):
            with ADMIN_FAILURE_LOCK:
                ADMIN_FAILURES.setdefault(ip, []).append(now)
            admin_audit(ip, action, "unauthorized", "invalid request signature")
            self.fail("unauthorized", 401)
            return False
        with ADMIN_FAILURE_LOCK:
            for old_nonce, used_at in list(ADMIN_NONCES.items()):
                if now - used_at > 120:
                    ADMIN_NONCES.pop(old_nonce, None)
            if nonce in ADMIN_NONCES:
                admin_audit(ip, action, "rejected", "replayed nonce")
                self.fail("unauthorized", 401)
                return False
            ADMIN_NONCES[nonce] = now
            ADMIN_FAILURES.pop(ip, None)
        return True

    def api_admin_get(self, p):
        action = "status" if p == "/api/admin/status" else "logs" if p == "/api/admin/logs" else "unknown"
        if action == "unknown":
            return self.fail("not found", 404)
        if not self.admin_auth(action):
            return
        try:
            data = admin_status_data() if action == "status" else {"ok": True, "lines": admin_log_tail()}
            admin_audit(self.client_address[0], action, "ok")
            return self.send_json(data)
        except Exception:
            admin_audit(self.client_address[0], action, "error")
            return self.fail("admin action failed", 500)

    def api_admin_action(self):
        raw = self.body_raw(limit=4096)
        if not self.admin_auth("action", raw):
            return
        try:
            body = json.loads(raw.decode())
        except Exception:
            body = None
        if not isinstance(body, dict) or set(body) != {"action"}:
            return self.fail("exactly one action is required")
        action = body.get("action")
        if action not in {"health", "update", "restart", "backup"}:
            admin_audit(self.client_address[0], str(action), "rejected", "not allowlisted")
            return self.fail("action not allowed", 403)
        if action == "health":
            result = admin_health_data()
            admin_audit(self.client_address[0], action, "ok" if result["ok"] else "degraded")
            return self.send_json(result, 200 if result["ok"] else 503)
        if not ADMIN_ACTION_LOCK.acquire(blocking=False):
            return self.fail("another admin action is running", 409)
        try:
            if action == "backup":
                result = {"ok": True, "backup": admin_backup_db()}
            elif action == "update":
                result = {"ok": True, **admin_run_update()}
            else:
                script = os.path.join(BASE, "run.sh")
                if not os.path.isfile(script):
                    raise RuntimeError("restart script is not installed")
                def restart_later():
                    time.sleep(1.0)
                    with open(os.devnull, "wb") as devnull:
                        subprocess.Popen(["/bin/bash", script], cwd=BASE, stdin=devnull,
                                         stdout=devnull, stderr=devnull, start_new_session=True)
                threading.Thread(target=restart_later, daemon=True).start()
                result = {"ok": True, "restart_scheduled": True}
            admin_audit(self.client_address[0], action, "ok", result.get("deployed_commit", ""))
            return self.send_json(result)
        except subprocess.TimeoutExpired:
            admin_audit(self.client_address[0], action, "error", "timeout")
            return self.fail("admin action timed out", 504)
        except Exception as e:
            admin_audit(self.client_address[0], action, "error", type(e).__name__)
            sys.stderr.write("admin %s failed: %s\n" % (action, e))
            return self.fail("admin action failed", 500)
        finally:
            ADMIN_ACTION_LOCK.release()

    # --- 路由 ---
    def do_GET(self):
        p = self.qpath()
        if p.startswith("/api/admin/"):
            return self.api_admin_get(p)
        if p.startswith("/api/"):
            return self.api_get(p)
        if p == "/cert":  # 下载自签名证书（公钥，供 iPhone/Mac 安装信任）
            fp = os.path.join(BASE, "cert.pem")
            if not os.path.exists(fp):
                return self.fail("not found", 404)
            with open(fp, "rb") as f:
                raw = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "application/x-x509-ca-cert")
            self.send_header("Content-Disposition", 'attachment; filename="speaking-gym.crt"')
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        if not STATIC_ALLOWED.fullmatch(p):
            return self.fail("not found", 404)
        return super().do_GET()

    def do_POST(self):
        p = self.qpath()
        try:
            if p == "/api/admin/action":
                return self.api_admin_action()
            if p == "/api/register":
                return self.api_register()
            if p == "/api/login":
                return self.api_login()
            if p == "/api/logout":
                return self.api_logout()
            if p == "/api/settings":
                return self.api_settings()
            if p == "/api/score":
                return self.api_score()
            if p == "/api/shadow-score":
                return self.api_shadow_score()
            if p == "/api/recordings":
                return self.api_upload()
            if p == "/api/chat/send":
                return self.api_chat_send()
            if p == "/api/chat/voice":
                return self.api_chat_voice()
            if p == "/api/photos":
                return self.api_photo_upload()
            if p == "/api/photos/organize":
                return self.api_photo_organize()
            if p == "/api/vocab":
                user = self.auth()
                if not user:
                    return self.fail("unauthorized", 401)
                d = self.body_json()
                try:
                    est = max(0, min(45000, int(d.get("estimate") or 0)))
                    low = max(0, min(45000, int(d.get("low") or 0)))
                    high = max(0, min(45000, int(d.get("high") or 0)))
                    oc = max(0.0, min(1.0, float(d.get("overclaim") or 0)))
                except (TypeError, ValueError):
                    return self.fail("bad payload")
                with db() as c:
                    c.execute(
                        "INSERT INTO vocab_tests (user_id, ts, date, estimate, low, high, overclaim, details) "
                        "VALUES (?,?,?,?,?,?,?,?)",
                        (user["id"], int(time.time() * 1000), date.today().isoformat(),
                         est, low, high, oc, json.dumps(d.get("details") or {}, ensure_ascii=False)[:20000]),
                    )
                return self.send_json({"ok": True})
            if p == "/api/push/subscribe":
                user = self.auth()
                if not user:
                    return self.fail("unauthorized", 401)
                sub = self.body_json().get("subscription") or {}
                if not sub.get("endpoint"):
                    return self.fail("bad subscription")
                with db() as c:
                    c.execute(
                        "INSERT INTO push_subs (user_id, endpoint, sub_json, created) VALUES (?,?,?,?) "
                        "ON CONFLICT(endpoint) DO UPDATE SET user_id=excluded.user_id, sub_json=excluded.sub_json",
                        (user["id"], sub["endpoint"], json.dumps(sub), int(time.time())),
                    )
                return self.send_json({"ok": True})
            if p == "/api/push/test":
                user = self.auth()
                if not user:
                    return self.fail("unauthorized", 401)
                n = push_to_user(user["id"], "Buddy 提醒", "通知已打通！到点提醒会像这样出现。")
                return self.send_json({"sent": n})
            m = re.match(r"^/api/rescore/(\d+)$", p)
            if m:
                return self.api_rescore(int(m.group(1)))
            return self.fail("not found", 404)
        except Exception as e:
            return self.fail("server error: %s" % e, 500)

    def do_DELETE(self):
        if self.qpath() == "/api/chat":
            # 清空对话（短期记忆）：先提炼长期记忆，需密码确认
            user = self.auth()
            if not user:
                return self.fail("unauthorized", 401)
            if not check_password(user, self.body_json().get("password") or ""):
                return self.fail("密码错误", 403)
            summary = None
            if DEEPSEEK_KEY:
                try:
                    summary = distill_memory(user["id"])
                except Exception as e:
                    return self.fail("长期记忆提炼失败，已取消清空（数据未动）：%s" % e, 502)
            with db() as c:
                c.execute("DELETE FROM chat_messages WHERE user_id=?", (user["id"],))
                if summary:
                    c.execute(
                        "INSERT INTO chat_memory (user_id, summary, last_msg_id, updated) VALUES (?,?,0,?) "
                        "ON CONFLICT(user_id) DO UPDATE SET summary=excluded.summary, last_msg_id=0, "
                        "updated=excluded.updated",
                        (user["id"], summary, int(time.time())),
                    )
                else:
                    c.execute("UPDATE chat_memory SET last_msg_id=0 WHERE user_id=?", (user["id"],))
            return self.send_json({"ok": True, "memory_kept": bool(summary)})
        if self.qpath() == "/api/memory":
            # 彻底抹除长期记忆 + 全部对话（图库不动），需密码确认
            user = self.auth()
            if not user:
                return self.fail("unauthorized", 401)
            if not check_password(user, self.body_json().get("password") or ""):
                return self.fail("密码错误", 403)
            with db() as c:
                c.execute("DELETE FROM chat_messages WHERE user_id=?", (user["id"],))
                c.execute("DELETE FROM chat_memory WHERE user_id=?", (user["id"],))
            return self.send_json({"ok": True})
        mr = re.match(r"^/api/reminders/(\d+)$", self.qpath())
        if mr:
            user = self.auth()
            if not user:
                return self.fail("unauthorized", 401)
            with db() as c:
                c.execute("UPDATE reminders SET active=0 WHERE id=? AND user_id=?", (int(mr.group(1)), user["id"]))
            return self.send_json({"ok": True})
        mp = re.match(r"^/api/photos/(\d+)$", self.qpath())
        if mp:
            user = self.auth()
            if not user:
                return self.fail("unauthorized", 401)
            pid = int(mp.group(1))
            with db() as c:
                row = c.execute("SELECT * FROM photos WHERE id=? AND user_id=?", (pid, user["id"])).fetchone()
                if not row:
                    return self.fail("not found", 404)
                c.execute("DELETE FROM photos WHERE id=?", (pid,))
                c.execute("UPDATE chat_messages SET photo_id=NULL WHERE photo_id=? AND user_id=?", (pid, user["id"]))
            try:
                os.remove(os.path.join(PHOTO_DIR, row["path"]))
            except OSError:
                pass
            return self.send_json({"ok": True})
        m = re.match(r"^/api/recordings/(\d+)$", self.qpath())
        if not m:
            return self.fail("not found", 404)
        user = self.auth()
        if not user:
            return self.fail("unauthorized", 401)
        rid = int(m.group(1))
        with db() as c:
            row = c.execute("SELECT * FROM recordings WHERE id=?", (rid,)).fetchone()
            if not row or row["user_id"] != user["id"]:
                return self.fail("not found", 404)
            c.execute("DELETE FROM recordings WHERE id=?", (rid,))
        try:
            os.remove(os.path.join(REC_DIR, row["path"]))
        except OSError:
            pass
        return self.send_json({"ok": True})

    # --- GET 类 API ---
    def api_get(self, p):
        try:
            if p == "/api/me":
                user = self.auth()
                if not user:
                    return self.fail("unauthorized", 401)
                return self.send_json({
                    "username": user["username"],
                    "public_audio": bool(user["public_audio"]),
                    "chat_fix_level": (user["chat_fix_level"] if "chat_fix_level" in user.keys() else None) or "standard",
                    "ai_enabled": bool(DEEPSEEK_KEY),
                })
            if p == "/api/scores":
                user = self.auth()
                if not user:
                    return self.fail("unauthorized", 401)
                with db() as c:
                    rows = c.execute(
                        "SELECT date, ts, cat, round, wpm, words, fillers, sec, ai_score "
                        "FROM scores WHERE user_id=? ORDER BY ts ASC LIMIT 500", (user["id"],)
                    ).fetchall()
                items = [dict(r) for r in rows]
                return self.send_json({"items": items, "streak": calc_streak([r["date"] for r in items])})
            if p == "/api/recordings":
                user = self.auth()
                if not user:
                    return self.fail("unauthorized", 401)
                with db() as c:
                    rows = c.execute(
                        "SELECT id, date, ts, cat, topic, round, sec, wpm, ai_score, mime, transcript, ai_json "
                        "FROM recordings WHERE user_id=? ORDER BY ts DESC LIMIT 100", (user["id"],)
                    ).fetchall()
                return self.send_json({"items": [dict(r) for r in rows]})
            if p == "/api/public-recordings":
                user = self.auth()
                if not user:
                    return self.fail("unauthorized", 401)
                with db() as c:
                    rows = c.execute(
                        "SELECT r.id, r.date, r.ts, r.cat, r.topic, r.round, r.sec, r.wpm, r.ai_score, "
                        "r.transcript, r.ai_json, u.username "
                        "FROM recordings r JOIN users u ON u.id=r.user_id "
                        "WHERE u.public_audio=1 ORDER BY r.ts DESC LIMIT 50"
                    ).fetchall()
                return self.send_json({"items": [dict(r) for r in rows]})
            if p == "/api/tts":
                return self.api_tts()
            if p == "/api/memory":
                user = self.auth()
                if not user:
                    return self.fail("unauthorized", 401)
                with db() as c:
                    mem = c.execute("SELECT summary, updated FROM chat_memory WHERE user_id=?", (user["id"],)).fetchone()
                return self.send_json({
                    "summary": (mem["summary"] if mem else "") or "",
                    "updated": mem["updated"] if mem else None,
                })
            if p == "/api/vocab/history":
                user = self.auth()
                if not user:
                    return self.fail("unauthorized", 401)
                with db() as c:
                    rows = [dict(r) for r in c.execute(
                        "SELECT id, ts, date, estimate, low, high, overclaim FROM vocab_tests "
                        "WHERE user_id=? ORDER BY ts DESC LIMIT 20", (user["id"],)).fetchall()]
                return self.send_json({"items": rows})
            if p == "/api/push/key":
                user = self.auth()
                if not user:
                    return self.fail("unauthorized", 401)
                if not push_available():
                    return self.fail("服务器未安装 pywebpush")
                return self.send_json({"key": vapid_public_b64()})
            if p == "/api/reminders":
                user = self.auth()
                if not user:
                    return self.fail("unauthorized", 401)
                with db() as c:
                    rows = [dict(r) for r in c.execute(
                        "SELECT id, text, rtype, weekday, time, fire_at, active FROM reminders "
                        "WHERE user_id=? AND active=1 ORDER BY fire_at ASC", (user["id"],)).fetchall()]
                for r in rows:
                    r["label"] = schedule_label(r)
                with db() as c:
                    nsub = c.execute("SELECT COUNT(*) FROM push_subs WHERE user_id=?", (user["id"],)).fetchone()[0]
                return self.send_json({"items": rows, "push_devices": nsub})
            if p == "/api/chat/history":
                user = self.auth()
                if not user:
                    return self.fail("unauthorized", 401)
                with db() as c:
                    rows = c.execute(
                        "SELECT id, ts, role, content, fix_json, channel, typed_note, photo_id FROM chat_messages "
                        "WHERE user_id=? ORDER BY id DESC LIMIT 100", (user["id"],)
                    ).fetchall()
                return self.send_json({"items": [dict(r) for r in reversed(rows)]})
            if p == "/api/photos":
                user = self.auth()
                if not user:
                    return self.fail("unauthorized", 401)
                with db() as c:
                    rows = c.execute(
                        "SELECT id, ts, date, caption, tags_json, album FROM photos "
                        "WHERE user_id=? ORDER BY ts DESC LIMIT 500", (user["id"],)
                    ).fetchall()
                return self.send_json({"items": [dict(r) for r in rows]})
            m = re.match(r"^/api/photo/(\d+)$", p)
            if m:
                return self.api_photo_file(int(m.group(1)))
            m = re.match(r"^/api/audio/(\d+)$", p)
            if m:
                return self.api_audio(int(m.group(1)))
            return self.fail("not found", 404)
        except Exception as e:
            return self.fail("server error: %s" % e, 500)

    # --- 神经网络朗读 ---
    def api_tts(self):
        user = self.auth()
        if not user:
            return self.fail("unauthorized", 401)
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        text = (q.get("text") or [""])[0].strip()
        if len(text) > 2000:  # 超长时在句子边界截断，避免读到一半戛然而止
            cut = text[:2000]
            m2 = re.search(r"^.*[.!?…]", cut, re.S)
            text = m2.group(0) if m2 else cut
        if not text:
            return self.fail("missing text")
        voice = TTS_VOICES.get((q.get("voice") or ["jenny"])[0], TTS_VOICES["jenny"])
        rate = TTS_RATES.get((q.get("rate") or ["0.9"])[0], "-10%")
        import hashlib
        key = hashlib.sha1(("%s|%s|%s" % (voice, rate, text)).encode()).hexdigest()
        fp = os.path.join(TTS_DIR, key + ".mp3")
        if not os.path.exists(fp) or os.path.getsize(fp) == 0:
            try:
                tts_generate(text, voice, rate, fp)
            except Exception as e:
                try:
                    os.remove(fp)
                except OSError:
                    pass
                return self.fail("tts failed: %s" % e, 502)
        with open(fp, "rb") as f:
            raw = f.read()
        self.send_response(200)
        self.send_header("Content-Type", "audio/mpeg")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "private, max-age=604800")
        self.end_headers()
        self.wfile.write(raw)

    # --- 账号 ---
    def api_register(self):
        d = self.body_json()
        username = (d.get("username") or "").strip()
        password = d.get("password") or ""
        if not USERNAME_RE.match(username):
            return self.fail("用户名需为 2-20 位字母/数字/下划线/中文")
        if len(password) < 6:
            return self.fail("密码至少 6 位")
        salt = secrets.token_hex(16)
        try:
            with db() as c:
                cur = c.execute(
                    "INSERT INTO users (username, pw_hash, salt, created_at) VALUES (?,?,?,?)",
                    (username, hash_pw(password, salt), salt, int(time.time())),
                )
                uid = cur.lastrowid
        except sqlite3.IntegrityError:
            return self.fail("用户名已存在")
        return self.send_json({"token": self._new_token(uid), "username": username})

    def api_login(self):
        d = self.body_json()
        username = (d.get("username") or "").strip()
        password = d.get("password") or ""
        with db() as c:
            u = c.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        if not u or hash_pw(password, u["salt"]) != u["pw_hash"]:
            return self.fail("用户名或密码错误", 401)
        return self.send_json({"token": self._new_token(u["id"]), "username": username})

    def _new_token(self, uid):
        tok = secrets.token_hex(32)
        with db() as c:
            c.execute("INSERT INTO tokens (token, user_id, created_at) VALUES (?,?,?)", (tok, uid, int(time.time())))
        return tok

    def api_logout(self):
        tok = self.token()
        if tok:
            with db() as c:
                c.execute("DELETE FROM tokens WHERE token=?", (tok,))
        return self.send_json({"ok": True})

    def api_settings(self):
        user = self.auth()
        if not user:
            return self.fail("unauthorized", 401)
        d = self.body_json()
        with db() as c:
            if "public_audio" in d:
                c.execute("UPDATE users SET public_audio=? WHERE id=?", (1 if d["public_audio"] else 0, user["id"]))
            if d.get("chat_fix_level") in ("light", "standard", "strict"):
                c.execute("UPDATE users SET chat_fix_level=? WHERE id=?", (d["chat_fix_level"], user["id"]))
        return self.send_json({"ok": True})

    # --- 评分 ---
    def api_score(self):
        user = self.auth()
        if not user:
            return self.fail("unauthorized", 401)
        d = self.body_json()
        transcript = (d.get("transcript") or "").strip()
        sec = int(d.get("sec") or 0)
        wpm = int(d.get("wpm") or 0)
        words = int(d.get("words") or 0)
        fillers = int(d.get("fillers") or 0)
        rid = d.get("recording_id")
        asr = "browser"
        # 优先用 Whisper 对上传的录音做精确转写（浏览器转写仅作实时显示/兜底）
        if rid and whisper_available():
            with db() as c:
                rec = c.execute(
                    "SELECT * FROM recordings WHERE id=? AND user_id=?", (int(rid), user["id"])
                ).fetchone()
            if rec:
                fp = os.path.join(REC_DIR, rec["path"])
                if os.path.exists(fp):
                    try:
                        wt = transcribe_file(fp)
                        if wt and len(WORD_RE.findall(wt)) >= 3:
                            transcript = wt
                            asr = "whisper"
                            words = len(WORD_RE.findall(wt))
                            wpm = int(round(words / max(5, sec) * 60.0))
                            fillers = len(FILLER_RE_PY.findall(wt))
                    except Exception as e:
                        sys.stderr.write("whisper transcribe failed: %s\n" % e)
        row = (
            user["id"], d.get("date") or date.today().isoformat(), int(time.time() * 1000),
            str(d.get("cat") or ""), str(d.get("topic") or "")[:300], int(d.get("round") or 0),
            wpm, words, fillers, sec,
        )
        ai, reason = None, None
        if not DEEPSEEK_KEY:
            reason = "no_key"
        elif len(transcript.split()) < 15:
            reason = "too_short"
        else:
            try:
                ai = deepseek_score({
                    "topic": d.get("topic"), "transcript": transcript,
                    "duration_sec": sec, "wpm": wpm, "filler_count": fillers, "round": row[5],
                })
            except Exception as e:
                reason = "ai_failed: %s" % e
                sys.stderr.write("deepseek score failed (user %s): %s\n" % (user["id"], e))
        with db() as c:
            c.execute(
                "INSERT INTO scores (user_id, date, ts, cat, topic, round, wpm, words, fillers, sec, ai_score, ai_json) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                row + (ai["score"] if ai else None, json.dumps(ai, ensure_ascii=False) if ai else None),
            )
            if rid:
                c.execute(
                    "UPDATE recordings SET transcript=?, wpm=? WHERE id=? AND user_id=?",
                    (transcript, wpm, int(rid), user["id"]),
                )
                if ai:
                    c.execute(
                        "UPDATE recordings SET ai_score=?, ai_json=? WHERE id=? AND user_id=?",
                        (ai["score"], json.dumps(ai, ensure_ascii=False), int(rid), user["id"]),
                    )
        return self.send_json({
            "ai": ai, "reason": reason, "asr": asr,
            "transcript": transcript, "wpm": wpm, "words": words, "fillers": fillers,
        })

    # --- 图片 ---
    PHOTO_EXT = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif", "image/heic": ".heic"}

    def api_photo_upload(self):
        user = self.auth()
        if not user:
            return self.fail("unauthorized", 401)
        blob = self.body_raw(limit=15_000_000)
        if not blob:
            return self.fail("empty image")
        mime = (self.headers.get("Content-Type") or "image/jpeg").split(";")[0].strip()
        ext = self.PHOTO_EXT.get(mime, ".jpg")
        fname = secrets.token_hex(16) + ext
        with open(os.path.join(PHOTO_DIR, fname), "wb") as f:
            f.write(blob)
        with db() as c:
            cur = c.execute(
                "INSERT INTO photos (user_id, ts, date, mime, path) VALUES (?,?,?,?,?)",
                (user["id"], int(time.time() * 1000), date.today().isoformat(), mime, fname),
            )
        return self.send_json({"id": cur.lastrowid})

    def api_photo_file(self, pid):
        user = self.auth()
        if not user:
            return self.fail("unauthorized", 401)
        with db() as c:
            row = c.execute("SELECT * FROM photos WHERE id=? AND user_id=?", (pid, user["id"])).fetchone()
        if not row:
            return self.fail("not found", 404)
        fp = os.path.join(PHOTO_DIR, row["path"])
        if not os.path.exists(fp):
            return self.fail("file missing", 404)
        with open(fp, "rb") as f:
            raw = f.read()
        self.send_response(200)
        self.send_header("Content-Type", row["mime"] or "image/jpeg")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "private, max-age=604800")
        self.end_headers()
        self.wfile.write(raw)

    def api_photo_organize(self):
        user = self.auth()
        if not user:
            return self.fail("unauthorized", 401)
        if not DEEPSEEK_KEY:
            return self.fail("服务器未配置 AI Key")
        try:
            return self.send_json(organize_photos(user["id"]))
        except Exception as e:
            return self.fail("整理失败：%s" % e, 502)

    # --- AI 对话 ---
    def api_chat_send(self):
        user = self.auth()
        if not user:
            return self.fail("unauthorized", 401)
        if not DEEPSEEK_KEY:
            return self.fail("服务器未配置 AI Key")
        d = self.body_json()
        text = (d.get("text") or "").strip()[:2000]
        if not text:
            return self.fail("empty text")
        try:
            return self.send_json(chat_turn(user["id"], text, channel="text", photo_id=d.get("photo_id")))
        except Exception as e:
            sys.stderr.write("chat failed (user %s): %s\n" % (user["id"], e))
            return self.fail("对话服务暂时不可用，请重试", 502)

    def api_chat_voice(self):
        user = self.auth()
        if not user:
            return self.fail("unauthorized", 401)
        if not DEEPSEEK_KEY:
            return self.fail("服务器未配置 AI Key")
        if not whisper_available():
            return self.fail("服务器未启用 Whisper")
        blob = self.body_raw(limit=20_000_000)
        if not blob:
            return self.fail("empty audio")
        mime = self.headers.get("Content-Type") or "audio/webm"
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".m4a" if "mp4" in mime else ".webm", delete=False) as f:
            f.write(blob)
            tmp = f.name
        try:
            text = transcribe_file(tmp)
        except Exception as e:
            return self.fail("transcribe failed: %s" % e, 500)
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass
        if len(WORD_RE.findall(text)) < 1:
            return self.send_json({"user_text": "", "reply": None, "fix": None})
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        note = (q.get("note") or [""])[0].strip()[:500]
        photo_id = (q.get("photo_id") or [""])[0]
        try:
            result = chat_turn(
                user["id"], text, channel="mixed" if note else "voice",
                typed_note=note or None, photo_id=int(photo_id) if photo_id.isdigit() else None,
            )
        except Exception as e:
            sys.stderr.write("chat failed (user %s): %s\n" % (user["id"], e))
            return self.fail("对话服务暂时不可用，请重试", 502)
        result["user_text"] = text
        result["typed_note"] = note or None
        return self.send_json(result)

    # --- 补评：对已有转写但缺 AI 结果的录音重新评分 ---
    def api_rescore(self, rid):
        user = self.auth()
        if not user:
            return self.fail("unauthorized", 401)
        if not DEEPSEEK_KEY:
            return self.fail("服务器未配置 AI Key")
        with db() as c:
            rec = c.execute("SELECT * FROM recordings WHERE id=? AND user_id=?", (rid, user["id"])).fetchone()
        if not rec:
            return self.fail("not found", 404)
        transcript = (rec["transcript"] or "").strip()
        if len(transcript.split()) < 15:
            return self.fail("转写太短，无法评分")
        try:
            ai = deepseek_score({
                "topic": rec["topic"], "transcript": transcript,
                "duration_sec": rec["sec"], "wpm": rec["wpm"],
                "filler_count": len(FILLER_RE_PY.findall(transcript)), "round": rec["round"],
            })
        except Exception as e:
            return self.fail("AI 评分失败：%s" % e, 502)
        aij = json.dumps(ai, ensure_ascii=False)
        with db() as c:
            c.execute(
                "UPDATE recordings SET ai_score=?, ai_json=? WHERE id=? AND user_id=?",
                (ai["score"], aij, rid, user["id"]),
            )
            srow = c.execute(
                "SELECT id FROM scores WHERE user_id=? AND ai_score IS NULL AND round=? AND sec=? AND wpm=? "
                "ORDER BY ts DESC LIMIT 1",
                (user["id"], rec["round"], rec["sec"], rec["wpm"]),
            ).fetchone()
            if srow:
                c.execute("UPDATE scores SET ai_score=?, ai_json=? WHERE id=?", (ai["score"], aij, srow["id"]))
        return self.send_json({"ai": ai})

    # --- 跟读打分（Whisper 转写 + 相似度，不入库） ---
    def api_shadow_score(self):
        user = self.auth()
        if not user:
            return self.fail("unauthorized", 401)
        if not whisper_available():
            return self.send_json({"score": None, "reason": "no_whisper"})
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        target = (q.get("target") or [""])[0].strip()
        if not target:
            return self.fail("missing target")
        blob = self.body_raw(limit=10_000_000)
        if not blob:
            return self.fail("empty audio")
        mime = self.headers.get("Content-Type") or "audio/webm"
        suffix = ".m4a" if "mp4" in mime else ".webm"
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
            f.write(blob)
            tmp_path = f.name
        try:
            text = transcribe_file(tmp_path)
            pv = pitch_variability(tmp_path)
        except Exception as e:
            return self.fail("transcribe failed: %s" % e, 500)
        finally:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        return self.send_json({
            "score": shadow_similarity(target, text), "transcript": text, "pitch_var": pv,
        })

    # --- 录音 ---
    def api_upload(self):
        user = self.auth()
        if not user:
            return self.fail("unauthorized", 401)
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        try:
            meta = json.loads((q.get("meta") or ["{}"])[0])
        except Exception:
            meta = {}
        blob = self.body_raw()
        if not blob:
            return self.fail("empty audio")
        mime = self.headers.get("Content-Type") or "audio/webm"
        ext = ".m4a" if "mp4" in mime else ".webm"
        fname = secrets.token_hex(16) + ext
        with open(os.path.join(REC_DIR, fname), "wb") as f:
            f.write(blob)
        with db() as c:
            cur = c.execute(
                "INSERT INTO recordings (user_id, date, ts, cat, topic, round, sec, wpm, mime, path) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (user["id"], meta.get("date") or date.today().isoformat(), int(time.time() * 1000),
                 str(meta.get("cat") or ""), str(meta.get("topic") or "")[:300], int(meta.get("round") or 0),
                 int(meta.get("sec") or 0), int(meta.get("wpm") or 0), mime, fname),
            )
        return self.send_json({"id": cur.lastrowid})

    def api_audio(self, rid):
        user = self.auth()
        if not user:
            return self.fail("unauthorized", 401)
        with db() as c:
            row = c.execute(
                "SELECT r.*, u.public_audio AS pub FROM recordings r JOIN users u ON u.id=r.user_id WHERE r.id=?",
                (rid,),
            ).fetchone()
        if not row or (row["user_id"] != user["id"] and not row["pub"]):
            return self.fail("not found", 404)
        fp = os.path.join(REC_DIR, row["path"])
        if not os.path.exists(fp):
            return self.fail("file missing", 404)
        size = os.path.getsize(fp)
        rng = self.headers.get("Range")
        start, end = 0, size - 1
        status = 200
        if rng:
            m = re.match(r"bytes=(\d*)-(\d*)$", rng.strip())
            if m and (m.group(1) or m.group(2)):
                if m.group(1):
                    start = int(m.group(1))
                    if m.group(2):
                        end = min(int(m.group(2)), size - 1)
                else:
                    start = max(0, size - int(m.group(2)))
                if start <= end < size:
                    status = 206
                else:
                    start, end = 0, size - 1
        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", row["mime"] or "audio/webm")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if status == 206:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        self.end_headers()
        with open(fp, "rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(65536, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 1511
    init_db()
    http.server.SimpleHTTPRequestHandler.extensions_map[".webmanifest"] = "application/manifest+json"
    if whisper_available():
        threading.Thread(target=warm_whisper, daemon=True).start()
        print("Whisper: enabled, warming up model '%s'" % os.environ.get("SG_WHISPER_MODEL", "small"))
    else:
        print("Whisper: not installed, falling back to browser transcripts")
    threading.Thread(target=auto_organize_loop, daemon=True).start()
    threading.Thread(target=reminders_loop, daemon=True).start()
    print("Vision: %s" % ((VISION_PROVIDER + " / " + (DEEPSEEK_VISION_MODEL if VISION_PROVIDER == "deepseek" else QWEN_VISION_MODEL)) if vision_available() else "off"))
    print("Push: %s" % ("on" if push_available() else "off — pip3 install --user pywebpush"))
    handler = functools.partial(Handler, directory=BASE)
    cert, key = os.path.join(BASE, "cert.pem"), os.path.join(BASE, "key.pem")
    if os.path.exists(cert) and os.path.exists(key):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert, key)

        class TLSServer(http.server.ThreadingHTTPServer):
            daemon_threads = True

            # 关键：accept 只收 TCP 连接，TLS 握手推迟到各连接的工作线程中进行，
            # 否则一个不完成握手的客户端（如端口扫描）就能卡死主 accept 循环。
            def get_request(self):
                sock, addr = self.socket.accept()
                return ctx.wrap_socket(sock, server_side=True, do_handshake_on_connect=False), addr

            def handle_error(self, request, client_address):
                sys.stderr.write("conn error from %s: %s\n" % (client_address, sys.exc_info()[1]))

        httpd = TLSServer(("0.0.0.0", port), handler)
        print("Serving HTTPS on 0.0.0.0:%d  (data: %s, AI: %s)" % (port, DATA, "on" if DEEPSEEK_KEY else "off"))
    else:
        httpd = http.server.ThreadingHTTPServer(("0.0.0.0", port), handler)
        httpd.daemon_threads = True
        print("Serving HTTP on 0.0.0.0:%d  (data: %s, AI: %s)" % (port, DATA, "on" if DEEPSEEK_KEY else "off"))
    httpd.serve_forever()


if __name__ == "__main__":
    main()
