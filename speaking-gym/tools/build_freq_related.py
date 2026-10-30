#!/usr/bin/env python3
"""给高频词表配相关词，生成 freq_related.json（卡片翻开后「相关词 · 词汇图」用）。

每个词：
  forms  词形变化（三单 / 过去式 / 过去分词 / -ing / 复数 / 比较级 / 最高级），直接取 ECDICT 的 exchange 字段
  syn    近义词，各标语体（口语 / 中性 / 书面）和一句区别
  ant    反义词
  fam    词族：加前后缀派生出来的词（happy → unhappy, happiness, happily）
  cmp    含这个词的常见复合词 / 固定组合（book → bookshelf, notebook）
后四项按 freq_words.json 的中文义项让 DeepSeek 逐批生成。按词存、可断点续跑：已有的词跳过、只补缺的。

用法（Key 的读取方式同 build_freq_defs.py）：
  python3 tools/build_freq_related.py --ecdict /tmp/ecdict.csv                      # 词形 + 相关词
  python3 tools/build_freq_related.py --limit 40 --out /tmp/try.json               # 先试 40 个词看效果
"""
import argparse
import csv
import json
import os
import random
import re
import sys
import time

from build_freq_defs import CJK, POS, ROOT, USAGE, Fatal, api_key, clean, request_items, run, save

SYSTEM = (
    "You build word maps for Chinese learners of English (around B1). Input: one word per line, as \"word | its "
    "Chinese senses\". For each word, about its most common everyday meaning, give related words that are real, "
    "common English (use an empty list when nothing good fits; never invent words):\n"
    "- \"syn\": up to 4 synonyms or near-synonyms, each [word or short phrase, \"informal\" | \"neutral\" | "
    "\"formal\", how it differs from the word in at most 6 words]. Cover the range when it exists: casual spoken "
    "options (informal) and formal or written ones.\n"
    "- \"ant\": up to 2 common antonyms, as plain strings.\n"
    "- \"fam\": up to 4 words from its word family made with prefixes or suffixes (happy → unhappy, happiness, "
    "happily), each [word, n. | v. | adj. | adv.]; not simple inflections like goes, went, bigger.\n"
    "- \"cmp\": up to 3 common compound words or fixed combinations containing it, as plain strings (book → "
    "bookshelf, notebook, book club).\n"
    "Use American English. Return JSON ONLY, e.g. {\"items\": [{\"word\": \"big\", \"syn\": [[\"huge\", "
    "\"informal\", \"very big, more emphatic\"], [\"large\", \"neutral\", \"plainer, for size or amount\"]], "
    "\"ant\": [\"small\"], \"fam\": [[\"bigness\", \"n.\"]], \"cmp\": [\"big deal\", \"big-time\"]}]} with one "
    "item per input word, in input order, \"word\" copied exactly."
)
HEAD = {"source": "forms: ECDICT exchange field; syn/ant/fam/cmp: DeepSeek (deepseek-flash) via "
                  "tools/build_freq_related.py", "fields": {
            "forms": "[[3|p|d|i|s|r|t, form], ...]", "syn": "[[word, i|n|f (informal/neutral/formal), note], ...]",
            "ant": "[word, ...]", "fam": "[[word, pos], ...]", "cmp": "[word, ...]"}}
REGISTER = {"informal": "i", "neutral": "n", "formal": "f"}
WORDISH = re.compile(r"[A-Za-z][A-Za-z' .-]{0,39}")
FORM_POS = {"3": "v", "p": "v", "d": "v", "i": "v", "s": "n", "r": "a", "t": "a"}  # 词形只配给对应词性的词
POS_GROUP = {"n": "n", "v": "v", "vt": "v", "vi": "v", "aux": "v", "a": "a", "adj": "a", "adv": "a"}


def ecdict_forms(path, pos):
    """pos: {word: {"n", "v", "a"}} → {word: [[类型, 词形], …]}，类型按 三单 3 / 过去式 p / 过去分词 d / -ing i /
    复数 s / 比较级 r / 最高级 t 排。ECDICT 会给形容词配出动词变化（taut → tauting），按词性滤掉。"""
    out = {}
    with open(path, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            w = r["word"]
            if w in pos and r["exchange"]:
                ex = dict(x.split(":", 1) for x in r["exchange"].split("/") if ":" in x)
                forms = [[k, ex[k]] for k in "3pdisrt" if ex.get(k) and FORM_POS[k] in pos[w]
                         and not (ex[k] == w and w.endswith(("ing", "ed")))]
                if forms:
                    out[w] = forms
    return out


def word_pos(words, defs):
    """词性：中文释义的 n. / v. / a. 等，加上英文义项的词性（text 的中文只有名词，英文义项里有动词）。"""
    out = {}
    for w, zh in words:
        tags = {POS_GROUP[t] for t in re.findall(r"\b(n|v|vt|vi|a|adj|adv|aux)\.", zh)}
        tags |= {POS_GROUP[s[0].rstrip(".")] for s in defs.get(w, []) if s[0].rstrip(".") in POS_GROUP}
        out[w] = tags
    return out


def parse_related(word, it):
    def rows(field, limit, keys):  # 每项可能是字符串、[词, …] 或 {"w": 词, …}
        for x in (it.get(field) if isinstance(it.get(field), list) else [])[:limit]:
            x = dict(zip(keys, x)) if isinstance(x, list) else {keys[0]: x} if isinstance(x, str) else x
            w = clean(x.get(keys[0])) if isinstance(x, dict) else ""
            if WORDISH.fullmatch(w) and w.lower() != word:
                yield w, x

    def dedupe(items):
        seen, out = set(), []
        for x in items:
            if x[0].lower() not in seen:
                seen.add(x[0].lower())
                out.append(x)
        return out

    syn = dedupe([w, REGISTER[r], n] for w, x in rows("syn", 5, ("w", "r", "n"))
                 for r, n in [(clean(x.get("r")).lower(), clean(x.get("n")))]
                 if r in REGISTER and len(n) <= 80 and not CJK.search(n))
    fam = dedupe([w, p if p in POS else p + "." if p + "." in POS else ""] for w, x in rows("fam", 5, ("w", "p"))
                 for p in [clean(x.get("p")).lower()])
    return {"syn": syn, "ant": [x[0] for x in dedupe([w] for w, _ in rows("ant", 3, ("w",)))],
            "fam": fam, "cmp": [x[0] for x in dedupe([w] for w, _ in rows("cmp", 4, ("w",)))]}


def ask(key, batch):
    want = {w for w, _ in batch}
    return {w: parse_related(w, it) for it in request_items(key, SYSTEM, batch, 200)
            for w in [str(it.get("word") or "").strip().lower()] if w in want}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--words", default=os.path.join(ROOT, "freq_words.json"), help="词表（默认 freq_words.json）")
    ap.add_argument("--out", default=os.path.join(ROOT, "freq_related.json"), help="输出文件，已存在则续跑")
    ap.add_argument("--ecdict", help="ecdict.csv 路径：给了就（重新）填词形变化")
    ap.add_argument("--limit", type=int, default=0, help="只补词表里前 N 个还没有相关词的词")
    ap.add_argument("--batch", type=int, default=25, help="每次请求多少个词（默认 25）")
    ap.add_argument("--workers", type=int, default=8, help="并发请求数（默认 8）")
    args = ap.parse_args()
    with open(args.words, encoding="utf-8") as f:
        words = [(w[1], w[3]) for w in json.load(f)["words"]]
    order = [w for w, _ in words]
    try:
        with open(args.out, encoding="utf-8") as f:
            rel = json.load(f).get("words") or {}
    except FileNotFoundError:
        rel = {}
    if args.ecdict:
        try:
            with open(os.path.join(ROOT, "freq_defs.json"), encoding="utf-8") as f:
                defs = json.load(f).get("defs") or {}
        except FileNotFoundError:
            defs = {}
        forms = ecdict_forms(args.ecdict, word_pos(words, defs))
        for w in order:
            if w in forms:
                rel.setdefault(w, {})["forms"] = forms[w]
            elif w in rel:
                rel[w].pop("forms", None)
        save(args.out, rel, order, HEAD, "words")
        print("词形变化：%d 个词有记录" % len(forms))
    key = api_key()
    if not key:
        sys.exit(None if args.ecdict else "找不到 DeepSeek API Key（读取方式同 build_freq_defs.py）")
    todo = [wz for wz in words if "syn" not in rel.get(wz[0], {})][:args.limit or None]
    print("词表 %d 个词，已有相关词 %d 个，这次要补 %d 个" % (len(words), sum("syn" in v for v in rel.values()), len(todo)))
    fresh, last = [], time.time()
    try:
        for size in (args.batch, max(1, args.batch // 5), 1):  # 没成的词换小批重试，最后逐个来
            if not todo:
                break
            batches = [todo[i:i + size] for i in range(0, len(todo), size)]
            print("每批 %d 个词，共 %d 批" % (size, len(batches)))
            for n, (res, err) in enumerate(run(lambda b: ask(key, b), batches, args.workers), 1):
                if isinstance(err, Fatal):
                    raise err
                if err:
                    print("  一批请求失败：%s" % err, file=sys.stderr)
                for w, r in (res or {}).items():
                    rel.setdefault(w, {}).update(r)
                    fresh.append(w)
                if time.time() - last > 10 or n == len(batches):
                    save(args.out, rel, order, HEAD, "words")
                    last = time.time()
                    print("  %d/%d 批 · 用量：输入 %d / 输出 %d tokens" % (n, len(batches), USAGE["in"], USAGE["out"]))
            todo = [wz for wz in todo if "syn" not in rel.get(wz[0], {})]
    except (KeyboardInterrupt, Fatal) as e:
        save(args.out, rel, order, HEAD, "words")
        sys.exit("\n%s。进度已保存，再运行一次会从断点继续" % (e if isinstance(e, Fatal) else "已中断"))
    save(args.out, rel, order, HEAD, "words")
    print("完成：新增 %d 个，共有相关词 %d / %d 个；用量：输入 %d / 输出 %d tokens"
          % (len(fresh), sum("syn" in v for v in rel.values()), len(words), USAGE["in"], USAGE["out"]))
    if todo:
        print("仍缺 %d 个（再运行一次会补）：%s" % (len(todo), " ".join(w for w, _ in todo[:30])))
    for w in random.sample(fresh, min(5, len(fresh))):
        print("  %s %s" % (w, json.dumps(rel[w], ensure_ascii=False)))


if __name__ == "__main__":
    main()
