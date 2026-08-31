# Speaking Gym 口语练习室

一套面向中级英语学习者的口语训练方案：12 周学习计划 + 自建的多用户口语练习 Web 应用。

## 内容

- `英语口语学习计划.md` — 12 周分阶段口语提升计划（每天 30 分钟）
- `speaking-gym/` — 口语练习应用
  - 每日引导流程：跟读热身（语调打分）→ 话题 4-3-2 表达训练 → 复盘收集表达
  - 语料库：260 跟读句 / 818 话题卡（10 类）/ 370 语块（24 类）
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

## VPN 故障时的应急维护

1511 提供一个独立令牌和证书指纹双重保护的受限管理接口，仅允许状态、健康检查、日志尾部、数据库备份、从固定 GitHub `main` 快进更新、重启六个动作；它不接受任意命令或参数。**仅在 Ivanti VPN 无法工作时作为应急后备，正常维护仍优先使用校园网/VPN SSH。**完整配置、安全边界与操作步骤见 [`speaking-gym/EMERGENCY_ADMIN.md`](speaking-gym/EMERGENCY_ADMIN.md)。

## 注意

- API Key 只存于服务器数据目录，绝不进入仓库和前端代码
- `data/`（数据库与录音）、`*.pem` 已在 `.gitignore` 中排除
