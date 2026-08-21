# Speaking Gym 口语练习室

一套面向中级英语学习者的口语训练方案：12 周学习计划 + 自建的多用户口语练习 Web 应用。

## 内容

- `英语口语学习计划.md` — 12 周分阶段口语提升计划（每天 30 分钟）
- `speaking-gym/` — 口语练习应用
  - 每日引导流程：跟读热身（语调打分）→ 话题 4-3-2 表达训练 → 复盘收集表达
  - 语料库：100 跟读句 / 220 话题卡（日常、职场、深度、情景演练）/ 178 语块
  - 录音自动上传，服务器端 Whisper 精确转写
  - DeepSeek 按评分量表打分（流利度/词汇/语法/内容）+ 逐句纠错 + 口语范本
  - 多用户账号、评分曲线、历史存档、可选公开的录音广场

## 部署

```bash
# 服务器端（零第三方依赖，Python 3.8+；Whisper 转写可选）
pip3 install --user faster-whisper   # 可选：不装则回退浏览器转写
mkdir -p ~/speaking-gym-data
echo '{"deepseek_api_key": "sk-..."}' > ~/speaking-gym-data/config.json
chmod 600 ~/speaking-gym-data/config.json
# HTTPS 证书（浏览器麦克风要求 HTTPS）
openssl req -x509 -newkey rsa:2048 -keyout speaking-gym/key.pem \
  -out speaking-gym/cert.pem -days 3650 -nodes -subj "/CN=your.host"
SG_DATA_DIR=~/speaking-gym-data python3 speaking-gym/server.py 1511
```

本地试用：`speaking-gym/start.sh`（http://localhost:8787，无 Key 时评分自动关闭）。

## 注意

- API Key 只存于服务器数据目录，绝不进入仓库和前端代码
- `data/`（数据库与录音）、`*.pem` 已在 `.gitignore` 中排除
