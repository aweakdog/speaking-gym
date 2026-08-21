#!/bin/bash
# 本地启动口语练习室：http://localhost:8787
# （本地没有配置 AI Key 时，评分功能自动关闭，其余功能正常）
cd "$(dirname "$0")"
echo "口语练习室已启动：http://localhost:8787  （按 Ctrl+C 停止）"
python3 server.py 8787
