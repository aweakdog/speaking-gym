#!/usr/bin/env python3
"""从 ECDICT 生成高频词表 freq_words.json（服务端「高频词积累」的数据源）。

ECDICT（https://github.com/skywind3000/ECDICT，MIT License）的 frq 字段是当代美国英语语料库 COCA 的
词元（lemma）词频排名。本脚本按排名取前 N 位的小写英文单词，清洗中文释义（去掉计算机/医学等专业义项
与网络释义，每个词性保留前几个义项），并把旧式音标记号统一成 IPA。

用法：
  curl -L -o /tmp/ecdict.csv https://raw.githubusercontent.com/skywind3000/ECDICT/master/ecdict.csv
  python3 tools/build_freq_words.py /tmp/ecdict.csv [--max-rank 20000]
"""
import argparse
import csv
import json
import os
import re

ECDICT_LICENSE = """MIT License

Copyright (c) 2025 Linwei

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""

WORD_OK = re.compile(r"[a-z]{2,}(?:-[a-z]+)*")
POS_RE = re.compile(r"^([a-z]+\.(?:\s*&?\s*[a-z]+\.)*)\s*", re.I)
TAG_RE = re.compile(r"\[[^\]]*\]")
MAX_LINES, MAX_SENSES = 3, 4

# 词典里只有专业义项、且日常义被译错或缺失的词：人工校正释义
OVERRIDES = {
    "online": "a. 在线的，联网的；adv. 在线，上网",
    "affordable": "a. 负担得起的，价格合理的",
    "download": "v. 下载；n. 下载的文件",
    "upload": "v. 上传；n. 上传的文件",
    "laptop": "n. 笔记本电脑",
    "limo": "n. 豪华轿车（limousine 的简称）",
    "macho": "a. 大男子气的，充满男子汉气概的",
    "hacker": "n. 黑客",
    "firewall": "n. 防火墙",
    "pager": "n. 寻呼机，BP 机",
    "donut": "n. 甜甜圈，炸面圈",
    "iconic": "a. 标志性的，偶像般的",
    "glitch": "n. 小故障，小毛病",
    "geopolitical": "a. 地缘政治的",
    "geographically": "adv. 在地理上",
    "improperly": "adv. 不恰当地，不正当地",
    "discretionary": "a. 可自由支配的，酌情决定的",
    "meltdown": "n. 崩溃，彻底失控；（核反应堆的）熔毁",
    "payback": "n. 报复；回报，投资回收",
    "attractiveness": "n. 吸引力，魅力",
    "extremist": "n. 极端分子；a. 极端主义的",
    "competitiveness": "n. 竞争力，好胜心",
    "closeness": "n. 亲密，接近",
    "recycling": "n. 回收利用，再循环",
    "deployment": "n. 部署，调度",
    "guiding": "a. 指导性的，引导的",
    "restructuring": "n. 重组，改组",
    "staffing": "n. 人员配备",
    "morph": "v. （使）变形，（使）逐渐转变",
    "micro": "a. 微小的，微观的",
    "slider": "n. 滑块，滑动器",
    "gal": "n. 姑娘，女孩（口语）",
    "biz": "n. 生意，行业（business 的口语说法）",
    "unjustified": "a. 不合理的，无正当理由的",
    "unsigned": "a. 未签名的，未签约的",
    "subtype": "n. 亚型，子类",
    "unbeknownst": "adv. 不为……所知地（unbeknownst to sb）",
    "empathize": "v. 感同身受，产生共鸣",
    "sync": "n./v. 同步",
    "cyberspace": "n. 网络空间，网络世界",
    "horizontally": "adv. 水平地，横向地",
    "unilaterally": "adv. 单方面地",
    "conceptualization": "n. 概念化",
    "intrinsically": "adv. 本质上，内在地",
    "evaluator": "n. 评估者，评价者",
    "unanticipated": "a. 意料之外的",
    "transformational": "a. 变革性的，转型的",
    "unsupervised": "a. 无人监管的，无监督的",
    "functionality": "n. 功能，功能性",
    "worksheet": "n. 练习题纸，工作表",
    "misrepresentation": "n. 歪曲，误传",
    "reschedule": "v. 改期，重新安排时间",
    "mega": "a. 巨大的，超级的（口语）",
    "rollover": "n. （车辆）翻车；（资金）展期",
    "navigational": "a. 导航的，航行的",
    "hauler": "n. 运输公司，运货车",
    "airbag": "n. （汽车的）安全气囊",
    "percentile": "n. 百分位（数）",
    "cloning": "n. 克隆，无性繁殖",
    "colonization": "n. 殖民，殖民地化",
    "globally": "adv. 在全球范围内",
    "revitalization": "n. 复兴，振兴",
    "autistic": "a. 自闭症的",
    "cohesiveness": "n. 凝聚力",
    "concurrently": "adv. 同时地，并存地",
    "internally": "adv. 在内部，内在地",
    "labeling": "n. 贴标签，标识",
    "bipartisanship": "n. 两党合作，两党一致",
    "mentoring": "n. 指导，辅导（尤指资深者对新人的帮带）",
}
# 只剩专业义项且无日常用途的缩写、拟声词、专名
SKIP = {"stat", "para", "mmm", "umm", "comp", "terra", "magnum", "modus", "coldwater", "rusher",
        "hmm", "ahh", "ye", "ya", "cfc", "napa", "pol"}
INFLECTION_NOTE = re.compile(r"\s*[（(]\s*[a-z]+\s*的[^（()）]*[)）]|\s*[（(][A-Za-z][A-Za-z ,.'&-]*[)）]")
CJK = re.compile(r"[\u4e00-\u9fff]")


def split_senses(body):
    """按不在括号里的逗号/分号切分义项（避免切开"使(马,鹰等)戴头罩"）。"""
    out, buf, depth = [], [], 0
    for ch in body:
        if ch in "(（":
            depth += 1
        elif ch in ")）":
            depth = max(0, depth - 1)
        if ch in ",，;；" and depth == 0:
            out.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    out.append("".join(buf))
    seen = []
    for s in (x.strip() for x in out):
        if s and s not in seen:
            seen.append(s)
    return seen


def clean_line(line, general=True):
    if general:  # 通用义项行里的计算机义项（如 a 的 "art. [计] 累加器…"）整段丢弃，其它领域标签只去掉标签
        line = re.sub(r"\[计\].*$", "", line)
    line = INFLECTION_NOTE.sub("", TAG_RE.sub("", line)).strip()
    m = POS_RE.match(line)
    pos, body = (re.sub(r"\s+", "", m.group(1)), line[m.end():]) if m else ("", line)
    senses = [s for s in split_senses(body) if CJK.search(s) and len(s) <= 40 and "。" not in s]  # 去掉纯英文解释和百科式长文
    senses = senses[:1 if pos.lower() == "abbr." else MAX_SENSES]
    return ((pos + " ") if pos else "") + "，".join(senses) if senses else ""


def clean_translation(raw):
    lines = [ln.strip() for ln in raw.replace("\\r", "").replace("\r", "").split("\\n")]
    out = [clean_line(ln) for ln in lines if ln and not ln.startswith("[")]
    if not any(out):  # 没有通用义项时退回专业义项（去掉 [计] 等标签），网络释义和人名地名不要
        out = [clean_line(ln, general=False) for ln in lines
               if ln.startswith("[") and not re.match(r"\[(?:网络|人名|地名)\]", ln)]
    return "；".join(list(dict.fromkeys(x for x in out if x))[:MAX_LINES])


def clean_phonetic(p):
    p = re.split(r";|,\s", p or "")[0].strip()  # 多个读音只留第一个
    p = re.sub(r"\\+", "ɜ", p).replace("әj", "əʊ").replace("^", "g")
    for a, b in (("ә", "ə"), ("є", "e"), ("ε", "e"), (":", "ː"), ("ɒː", "ɔː"), ("'", "ˈ"), (".", "ˌ"), (",", "ˌ")):
        p = p.replace(a, b)
    return p


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv", help="ecdict.csv 路径")
    ap.add_argument("--max-rank", type=int, default=20000, help="只收 COCA 词频排名前 N 位（默认 20000）")
    ap.add_argument("--out", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "freq_words.json"))
    args = ap.parse_args()

    with open(args.csv, encoding="utf-8") as f:
        rows = [r for r in csv.DictReader(f)
                if r["frq"].isdigit() and 0 < int(r["frq"]) <= args.max_rank and WORD_OK.fullmatch(r["word"])]
    listed = {r["word"] for r in rows}
    words, seen = [], set()
    for r in sorted(rows, key=lambda r: int(r["frq"])):
        w = r["word"]
        if w in SKIP or w in seen:
            continue
        has_general = any(ln.strip() and not ln.strip().startswith("[") for ln in r["translation"].split("\\n"))
        lemma = re.search(r"(?:^|/)0:([a-z]+)", r["exchange"])
        if not has_general and lemma and lemma.group(1) != w and lemma.group(1) in listed:
            continue  # 只有专业义项的屈折形式（assisted、answering…）：原形已在表里
        zh = OVERRIDES.get(w) or clean_translation(r["translation"])
        if not zh:
            continue
        seen.add(w)
        words.append([int(r["frq"]), w, clean_phonetic(r["phonetic"]), zh])

    head = {
        "source": "ECDICT frq field (COCA lemma frequency rank), https://github.com/skywind3000/ECDICT",
        "license": ECDICT_LICENSE,
        "fields": ["rank", "word", "phonetic", "zh"],
    }
    body = ",\n".join(json.dumps(x, ensure_ascii=False, separators=(",", ":")) for x in words)
    with open(args.out, "w", encoding="utf-8") as f:  # 每词一行，git diff 友好
        f.write(json.dumps(head, ensure_ascii=False)[:-1] + ', "words": [\n' + body + "\n]}\n")
    print("freq_words.json 导出完成：%d 词（COCA 排名 ≤ %d）" % (len(words), args.max_rank))


if __name__ == "__main__":
    main()
