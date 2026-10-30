#!/usr/bin/env python3
"""给高频词表配英文义项 + 口语例句，生成 freq_defs.json（高频词卡片先看英文、再看中文；入本后复习也用它）。

ECDICT 自带的英文释义来自 WordNet：义项按词性排、偏学术（get 第一条是网球救球、bank 第一条是河岸），也没有
例句，所以按 freq_words.json 里的中文义项让 DeepSeek 逐批生成每个词日常最常用的 1-3 个义项，每个义项：
  词性 + 一句简单的英文释义（<= 15 词，只用常见词）
  例句：第一个义项 2 句、其余每个 1 句，像美剧台词、朋友聊天那样的口语；有常见搭配 / 短语动词 / 固定说法时
  围绕它来造（原创，不照搬歌词台词）
结果按词存（不按位置），可断点续跑：已有的词跳过、只补缺的。重新生成 freq_words.json 后再跑一遍即可补齐新词。

用法（不设环境变量时读 $SG_DATA_DIR/config.json 或 speaking-gym/data/config.json 里的 deepseek_api_key）：
  DEEPSEEK_API_KEY=sk-... python3 tools/build_freq_defs.py --limit 40 --out /tmp/try.json   # 先试 40 个词看效果
  DEEPSEEK_API_KEY=sk-... python3 tools/build_freq_defs.py                                   # 全量，约 1.7 万词
"""
import argparse
import json
import os
import queue
import random
import re
import sys
import threading
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # speaking-gym/
CJK = re.compile(r"[\u3000-\u303f\u4e00-\u9fff\uff00-\uffef]")
SYSTEM = (
    "You write entries for an English-English learner's dictionary for Chinese learners (around B1) who are "
    "training themselves to think in English instead of translating. Input: one word per line, as "
    "\"word | its Chinese senses from a bilingual dictionary\". For each word give \"senses\": its 1-3 most common "
    "meanings in everyday modern English, most common first. Add a second or third sense only when it is also really "
    "common and clearly different (run: move fast on foot / manage a business); many words need just one. Senses "
    "don't have to follow the Chinese order (mean → to have or express a meaning, not \"unkind\"; well → in a good "
    "way, not a hole for water), and may include a common meaning the Chinese list misses. For each sense:\n"
    "- \"pos\": its part of speech: n., v., adj., adv., prep., conj., pron., det., interj. or num.\n"
    "- \"en\": a plain-English definition in dictionary style (lowercase, no final period), at most 15 words, only "
    "simple common words, not using the word itself. For grammar words (the, of, to...), say briefly how the word "
    "is used.\n"
    "- \"ex\": example sentences that all show this same meaning (a different meaning gets its own sense): 2 for the "
    "first sense, 1 for each other sense. They should sound like real speech — lines from a TV show or a chat "
    "between friends, not a textbook: everyday situations, I/you, contractions, questions, reactions — and use a "
    "common collocation, phrasal verb or fixed expression with the word when there is one (make sense, give up, take "
    "a break, no big deal...). At most 15 words each. Each must contain the word itself, as given or with a regular "
    "ending (-s, -es, -ed, -ing, -er, -est). Write original sentences: don't quote real song lyrics or scripts. Keep "
    "them friendly; for swear words show a mild everyday use. For sexual, crude or offensive words, keep the "
    "definition neutral and the examples non-explicit (clinical, or about using the word) and never describe sexual "
    "acts.\n"
    "Use American English. Return JSON ONLY: {\"items\": [{\"word\": \"...\", \"senses\": [{\"pos\": \"...\", "
    "\"en\": \"...\", \"ex\": [\"...\", \"...\"]}]}]} with one item per input word, in input order, \"word\" copied "
    "exactly."
)
POS = {"n.", "v.", "adj.", "adv.", "prep.", "conj.", "pron.", "det.", "interj.", "num."}
LOWER_STARTS = set("a an the to used in if very someone something somebody not of relating related having being more "
                   "most one when with without able made full showing causing feeling done connected belonging "
                   "happening any all how what who where at on for from by so too extremely completely really "
                   "people person things nothing no as like about said say describing past present plural "
                   "informal".split())
USAGE = {"in": 0, "out": 0}
LOCK = threading.Lock()


class Fatal(Exception):
    """Key 无效、余额不足：重试也没用，直接停。"""


def api_key():
    key = os.environ.get("DEEPSEEK_API_KEY", "")
    for d in (os.environ.get("SG_DATA_DIR"), os.path.join(ROOT, "data")):
        if key or not d:
            continue
        try:
            with open(os.path.join(os.path.expanduser(d), "config.json"), encoding="utf-8") as f:
                key = json.load(f).get("deepseek_api_key") or ""
        except (OSError, ValueError):
            pass
    return key


def uses(word, sentence):
    """例句里有没有这个词（允许规则变化：make → making、try → tried）。"""
    stem = word[:-1] if len(word) > 3 and word[-1] in "ey" else word
    return re.search(r"\b" + re.escape(stem), sentence, re.I) is not None


def clean(v):
    return re.sub(r"\s+", " ", str(v or "")).strip()


def tidy(en):
    """释义统一成词典体：常见词开头的大写改小写，去掉句号（etc. 除外）。"""
    m = re.match(r"([A-Z][a-z]*)\b", en)
    if m and m.group(1).lower() in LOWER_STARTS:
        en = en[0].lower() + en[1:]
    return en[:-1] if en.endswith(".") and not en.endswith("etc.") else en


def parse_senses(word, raw):
    """模型给的 senses → ([[词性, 释义, 例句…], …], 是否合格)。最多 3 个义项、每个最多 2 句例句；缺字段、太长、
    混进中文的义项丢掉。合格 = 至少 2 句例句、且每句都用了原词。"""
    out = []
    for s in (raw if isinstance(raw, list) else [])[:3]:
        if not isinstance(s, dict):
            continue
        en, exs = tidy(clean(s.get("en"))), s.get("ex") if isinstance(s.get("ex"), list) else [s.get("ex")]
        exs = [x for x in map(clean, exs[:2]) if x and len(x) <= 200 and not CJK.search(x)]
        if en and exs and len(en) <= 160 and not CJK.search(en):
            pos = clean(s.get("pos")).lower()
            out.append([pos if pos in POS else pos + "." if pos + "." in POS else "", en] + exs)
    examples = [x for s in out for x in s[2:]]
    return out, len(examples) >= 2 and all(uses(word, x) for x in examples)


def ask(key, batch):
    """batch: [(word, zh)] → {word: (义项, 是否合格)}。"""
    req = urllib.request.Request("https://api.deepseek.com/chat/completions", data=json.dumps({
        "model": "deepseek-flash", "thinking": {"type": "disabled"}, "temperature": 0.7,
        "max_tokens": 400 + 160 * len(batch), "response_format": {"type": "json_object"},
        "messages": [{"role": "system", "content": SYSTEM},
                     {"role": "user", "content": "\n".join("%s | %s" % wz for wz in batch)}],
    }).encode(), headers={"Content-Type": "application/json", "Authorization": "Bearer " + key})
    for attempt in range(3):  # 限流、超时、断线：等一会儿再试
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                resp = json.load(r)
            break
        except urllib.error.HTTPError as e:
            if e.code in (401, 402):
                raise Fatal("DeepSeek 拒绝请求（HTTP %d）：Key 无效或余额不足" % e.code)
            if e.code == 400 or attempt == 2:  # 400 多是内容审核拦下了这一批：原样重发没用，交给后面的小批重试
                raise
        except Exception:
            if attempt == 2:
                raise
        time.sleep(3 + 7 * attempt)
    usage = resp.get("usage") or {}
    with LOCK:
        USAGE["in"] += usage.get("prompt_tokens") or 0
        USAGE["out"] += usage.get("completion_tokens") or 0
    want, out = {w for w, _ in batch}, {}
    for it in json.loads(resp["choices"][0]["message"]["content"]).get("items") or []:
        if not isinstance(it, dict):
            continue
        w = str(it.get("word") or "").strip().lower()
        senses, ok = parse_senses(w, it.get("senses"))
        if w in want and senses:
            out[w] = (senses, ok)
    return out


def run(key, batches, workers):
    """并发请求，按完成先后逐批产出 (结果, 异常)；守护线程，Ctrl+C 立刻能停。"""
    todo, done = queue.Queue(), queue.Queue()
    for b in batches:
        todo.put(b)

    def worker():
        while True:
            try:
                b = todo.get_nowait()
            except queue.Empty:
                return
            try:
                done.put((ask(key, b), None))
            except Exception as e:
                done.put((None, e))

    for _ in range(min(workers, len(batches))):
        threading.Thread(target=worker, daemon=True).start()
    for _ in batches:
        yield done.get()


def save(path, defs, order):
    keys = [w for w in order if w in defs]
    keys += sorted(set(defs) - set(keys))  # 词表里已经没有的词也留着，以后换回来不用重新生成
    head = json.dumps({"source": "DeepSeek (deepseek-flash) via tools/build_freq_defs.py, from the Chinese senses in "
                                 "freq_words.json", "fields": ["senses: [pos, definition, example, ...]"]})
    body = ",\n".join("%s:%s" % (json.dumps(w), json.dumps(defs[w], ensure_ascii=False, separators=(",", ":")))
                      for w in keys)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:  # 每词一行，git diff 友好；先写临时文件再替换，中途打断也不会写坏
        f.write(head[:-1] + ', "defs": {\n' + body + "\n}}\n")
    os.replace(tmp, path)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--words", default=os.path.join(ROOT, "freq_words.json"), help="词表（默认 freq_words.json）")
    ap.add_argument("--out", default=os.path.join(ROOT, "freq_defs.json"), help="输出文件，已存在则续跑（默认 freq_defs.json）")
    ap.add_argument("--limit", type=int, default=0, help="只补词表里前 N 个还没有释义的词（先小批量试效果）")
    ap.add_argument("--batch", type=int, default=25, help="每次请求多少个词（默认 25）")
    ap.add_argument("--workers", type=int, default=8, help="并发请求数（默认 8）")
    args = ap.parse_args()
    key = api_key()
    if not key:
        sys.exit("找不到 DeepSeek API Key：设置环境变量 DEEPSEEK_API_KEY，或写进 $SG_DATA_DIR/config.json 的 deepseek_api_key")
    with open(args.words, encoding="utf-8") as f:
        words = [(w[1], w[3]) for w in json.load(f)["words"]]
    try:
        with open(args.out, encoding="utf-8") as f:
            defs = json.load(f).get("defs") or {}
    except FileNotFoundError:
        defs = {}
    defs = {w: v for w, v in defs.items() if v and isinstance(v[0], list)}  # 旧格式 [释义, 例句] 的条目重新生成
    order = [w for w, _ in words]
    todo = [wz for wz in words if wz[0] not in defs][:args.limit or None]
    print("词表 %d 个词，已有释义 %d 个，这次要补 %d 个" % (len(words), len(defs), len(todo)))
    fresh, loose, last = [], {}, time.time()
    try:
        for size in (args.batch, max(1, args.batch // 5), 1):  # 没成的词换小批重试，最后逐个来，把出问题的词隔离出来
            if not todo:
                break
            batches = [todo[i:i + size] for i in range(0, len(todo), size)]
            print("每批 %d 个词，共 %d 批" % (size, len(batches)))
            for n, (res, err) in enumerate(run(key, batches, args.workers), 1):
                if isinstance(err, Fatal):
                    raise err
                if err:
                    print("  一批请求失败：%s" % err, file=sys.stderr)
                for w, (senses, ok) in (res or {}).items():
                    if ok:
                        defs[w] = senses
                        fresh.append(w)
                    else:
                        loose[w] = senses
                if time.time() - last > 10 or n == len(batches):
                    save(args.out, defs, order)
                    last = time.time()
                    print("  %d/%d 批 · 共有释义 %d 个 · 用量：输入 %d / 输出 %d tokens"
                          % (n, len(batches), len(defs), USAGE["in"], USAGE["out"]))
            todo = [wz for wz in todo if wz[0] not in defs]
    except (KeyboardInterrupt, Fatal) as e:
        save(args.out, defs, order)
        sys.exit("\n%s。进度已保存（共有释义 %d 个），再运行一次会从断点继续" % (e if isinstance(e, Fatal) else "已中断", len(defs)))
    for w, _ in todo:  # 几轮下来仍不合格的（多是例句用了不规则变化，如 bring → brought），退而求其次收下
        if w in loose:
            defs[w] = loose[w]
            fresh.append(w)
    save(args.out, defs, order)
    missing = [w for w, _ in todo if w not in defs]
    print("完成：新增 %d 个，共有释义 %d / %d 个；用量：输入 %d / 输出 %d tokens"
          % (len(fresh), len(defs), len(words), USAGE["in"], USAGE["out"]))
    if missing:
        print("仍缺 %d 个（再运行一次会补）：%s%s" % (len(missing), " ".join(missing[:30]), " …" if len(missing) > 30 else ""))
    for w in random.sample(fresh, min(6, len(fresh))):
        print("  " + w + "".join("\n    %s %s\n      %s" % (s[0], s[1], "\n      ".join(s[2:])) for s in defs[w]))


if __name__ == "__main__":
    main()
