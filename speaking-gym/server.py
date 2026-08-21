#!/usr/bin/env python3
"""口语练习室后端：静态文件 + 账号 + DeepSeek 评分 + 录音存储/公开广场。
零第三方依赖（仅 Python 标准库）。API Key 存放在数据目录的 config.json 中，绝不下发给前端。
用法：SG_DATA_DIR=~/speaking-gym-data python3 server.py 1511
"""
import functools
import http.server
import json
import os
import re
import secrets
import sqlite3
import ssl
import sys
import threading
import time
import urllib.parse
import urllib.request
from datetime import date

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.abspath(os.environ.get("SG_DATA_DIR", os.path.join(BASE, "data")))
REC_DIR = os.path.join(DATA, "recordings")
os.makedirs(REC_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA, "gym.db")

CONFIG = {}
_cfg = os.path.join(DATA, "config.json")
if os.path.exists(_cfg):
    with open(_cfg) as f:
        CONFIG = json.load(f)
DEEPSEEK_KEY = CONFIG.get("deepseek_api_key") or os.environ.get("DEEPSEEK_API_KEY", "")

USERNAME_RE = re.compile(r"^[A-Za-z0-9_\u4e00-\u9fa5]{2,20}$")
FORBIDDEN_STATIC = re.compile(r"(^|/)(data(/|$)|[^/]*\.(pem|db|log)$|run\.sh$|start\.sh$)")


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


def deepseek_score(payload):
    body = {
        "model": "deepseek-chat",
        "messages": [
            {"role": "system", "content": SCORE_SYSTEM},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.3,
        "max_tokens": 1500,
    }
    req = urllib.request.Request(
        "https://api.deepseek.com/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + DEEPSEEK_KEY},
    )
    with urllib.request.urlopen(req, timeout=90) as r:
        resp = json.load(r)
    data = json.loads(resp["choices"][0]["message"]["content"])
    data["score"] = max(0, min(100, int(data.get("score", 0))))
    dims = data.get("dims") or {}
    data["dims"] = {k: max(0, min(100, int(dims.get(k, 0)))) for k in ("fluency", "vocabulary", "grammar", "content")}
    data["fixes"] = (data.get("fixes") or [])[:3]
    data["upgrade"] = (data.get("upgrade") or [])[:2]
    data["model"] = str(data.get("model", "")).strip()
    data["comment_zh"] = str(data.get("comment_zh", ""))
    return data


# ---------- HTTP ----------
class Handler(http.server.SimpleHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = 60  # 防止慢速/恶意连接长期占用工作线程

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

    # --- 路由 ---
    def do_GET(self):
        p = self.qpath()
        if p.startswith("/api/"):
            return self.api_get(p)
        if FORBIDDEN_STATIC.search(p):
            return self.fail("not found", 404)
        return super().do_GET()

    def do_POST(self):
        p = self.qpath()
        try:
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
            return self.fail("not found", 404)
        except Exception as e:
            return self.fail("server error: %s" % e, 500)

    def do_DELETE(self):
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
            m = re.match(r"^/api/audio/(\d+)$", p)
            if m:
                return self.api_audio(int(m.group(1)))
            return self.fail("not found", 404)
        except Exception as e:
            return self.fail("server error: %s" % e, 500)

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
            c.execute("UPDATE users SET public_audio=? WHERE id=?", (1 if d.get("public_audio") else 0, user["id"]))
        return self.send_json({"ok": True, "public_audio": bool(d.get("public_audio"))})

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
        except Exception as e:
            return self.fail("transcribe failed: %s" % e, 500)
        finally:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        return self.send_json({"score": shadow_similarity(target, text), "transcript": text})

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
    if whisper_available():
        threading.Thread(target=warm_whisper, daemon=True).start()
        print("Whisper: enabled, warming up model '%s'" % os.environ.get("SG_WHISPER_MODEL", "small"))
    else:
        print("Whisper: not installed, falling back to browser transcripts")
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
