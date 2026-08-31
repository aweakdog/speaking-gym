#!/usr/bin/env bash
# 一键部署：导出话题库 -> 同步代码 -> （可选）重启服务
# 用法：./deploy.sh          仅同步静态资源（前端改动）
#       ./deploy.sh restart  同步并重启服务（server.py 改动时用）
set -euo pipefail
cd "$(dirname "$0")"

REMOTE="yliog@eez041.ece.ust.hk"

# 1. 从 data.js 导出话题库给服务端（Buddy 的话题菜单数据源）
node -e "
const fs = require('fs');
eval(fs.readFileSync('data.js','utf8') + \`
fs.writeFileSync('topics.json', JSON.stringify({
  categories: Object.values(TOPICS).map(t => ({ label: t.label, topics: t.items.map(i => i.q) }))
}));
console.log('topics.json 导出完成：' + Object.values(TOPICS).reduce((s,t)=>s+t.items.length,0) + ' 题');
\`)"

# 2. 语法检查
node --check app.js && node --check data.js && node --check sw.js
python3 -c "import ast; ast.parse(open('server.py').read())"

# 3. 同步（排除本地私密与数据文件）
rsync -a --exclude='.DS_Store' --exclude='*.pem' --exclude='data/' --exclude='server.log' ./ "$REMOTE":~/speaking-gym/
COMMIT="$(git -C .. rev-parse HEAD)"
ssh "$REMOTE" "printf '%s\\n' '$COMMIT' > ~/speaking-gym-data/deployed-commit"

# 4. 需要时重启
if [[ "${1:-}" == "restart" ]]; then
  ssh "$REMOTE" '~/speaking-gym/run.sh'
  sleep 6
fi

# 5. 健康检查（客户端固定校验 TLS 证书指纹）
python3 tools/emergency_admin.py status >/dev/null
echo "服务状态: 200"
