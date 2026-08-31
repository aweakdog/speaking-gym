# 无 VPN 时的受限 HTTPS 应急维护方案

这份手册总结 Speaking Gym 的实际落地经验，供其他自建服务或私有仓库复用。目标不是用 HTTP 模拟 SSH，而是在 VPN 故障、SSH 被来源白名单限制、但服务的 HTTPS 端口仍可访问时，保留一条**能力有限、可审计、可撤销的应急维护通道**。

> 正常维护仍应优先使用组织批准的 VPN、SSH 跳板机或控制台。本方案是业务级应急后备，不是绕过网络安全策略的通用远程终端。

---

## 1. 先判断问题发生在哪一层

“SSH 连不上”至少可能是四种完全不同的问题，不能看到超时就开始改端口。

### 1.1 DNS、端口和认证要分开测试

```bash
# 公共 DNS 是否有记录
dig @1.1.1.1 example.internal A +short

# TCP 端口是否可达（macOS nc）
nc -G 5 -zv 203.0.113.10 22
nc -G 5 -zv 203.0.113.10 443

# Linux 常见 nc；不同发行版参数可能略有差异
nc -z -w 5 203.0.113.10 22

# SSH 到底卡在握手还是认证
ssh -vv -o BatchMode=yes -o ConnectTimeout=10 user@203.0.113.10 true

# HTTPS 是否能直接通过 IP 到达；域名用于 TLS/SNI 时可用 --resolve
curl --resolve app.example.com:443:203.0.113.10 https://app.example.com/health
```

判断方法：

| 现象 | 常见原因 |
|---|---|
| 域名不解析，但 IP 端口可达 | DNS 配置或仅内部 DNS |
| 22 端口超时，HTTPS 正常 | 防火墙/NAT 没开放 SSH |
| SSH 完成密钥交换，随后 `Permission denied` | 用户、公钥、密码或来源地址策略 |
| 校园网成功、手机热点失败，且 22 都能连接 | `AllowUsers` / `Match Address` 等来源白名单 |
| VPN 连接后校内资源正常、Internet 失败 | VPN 分流、默认路由、DNS 或客户端残留问题 |

### 1.2 Speaking Gym 的实际根因

服务器的 22 端口在公网可达，但 sshd 配置包含来源白名单：

```text
AllowUsers *@143.89.*.*
AllowUsers *@175.159.*.*
AllowUsers *@172.159.240.*
AllowUsers *@172.16.*.*
```

因此：

- 校园网或 Ivanti VPN 下，来源地址符合白名单，SSH 公钥认证成功；
- 手机热点不连 VPN 时，TCP 22 仍可达，但 sshd 在认证阶段拒绝登录；
- `ssh-copy-id` 不能解决，因为问题不是公钥缺失，而是来源不被允许。

经验：**端口可达不等于有权登录；密码提示出现也不等于服务器会接受该来源的密码。**

---

## 2. 为什么不能做通用 HTTP Shell

下面这种接口即使有账号密码，也不应部署：

```http
POST /admin/exec
Authorization: Bearer ...

{"command":"任意 shell 命令"}
```

它本质上是公网远程代码执行入口，而不是“SSH 模拟器”。主要风险包括：

- 命令注入是功能本身，而不只是潜在漏洞；
- 一次认证缺陷就等于整个 Unix 账号失守；
- 很难正确实现 SSH 已有的公钥、主机指纹、会话、PTY、转发与审计模型；
- 用户输入可能控制命令、参数、路径、仓库或环境变量；
- Web 应用漏洞会直接升级成系统命令执行；
- 它可能绕过管理员明确设置的 SSH 来源控制。

如果需要完整交互式终端，应选择组织批准的 VPN、堡垒机、云控制台或经批准的零信任 SSH。HTTPS 应急接口只适合有限的应用维护动作。

---

## 3. 推荐架构

```text
维护者电脑
  ├─ 私有 Git 仓库：提交并 push
  ├─ 独立应急客户端
  │    ├─ 本地 0600 管理密钥
  │    ├─ TLS 证书指纹固定
  │    └─ 时间戳 + nonce + HMAC 请求签名
  │
  └──────── HTTPS 公网端口 ────────┐
                                     │
服务端应用（普通 Unix 用户）         │
  ├─ 请求签名验证                    │
  ├─ 固定动作白名单                  │
  ├─ 失败限速 + 防重放               │
  ├─ 审计日志                        │
  ├─ 固定 GitHub 仓库/分支更新器     │
  ├─ SQLite 在线备份                 │
  └─ 应用专用重启脚本                │
```

核心原则：**客户端只能选择预先编译在服务端代码里的动作，不能提交 shell 命令或可扩展参数。**

---

## 4. 动作白名单如何设计

一个小型 Web 服务通常只需要以下动作：

| 动作 | 建议方法 | 是否改变状态 | 典型返回 |
|---|---|---:|---|
| `status` | GET | 否 | 版本、提交、PID、运行时间、磁盘空间 |
| `health` | POST | 否 | 数据库、依赖和外部服务是否可用 |
| `logs` | GET | 否 | 固定日志最后 N 行 |
| `backup` | POST | 是 | 备份文件名、大小、完整性 |
| `update` | POST | 是 | 固定源目标提交、校验结果、是否需重启 |
| `restart` | POST | 是 | 已计划重启，客户端随后验证新 PID |

服务端应严格验证 JSON 键集合：

```python
ALLOWED_ACTIONS = {"health", "backup", "update", "restart"}

if not isinstance(body, dict) or set(body) != {"action"}:
    reject_request()
if body["action"] not in ALLOWED_ACTIONS:
    reject_request()
```

不要提供以下“方便功能”：

- `command`、`args`、`cwd`、`env`；
- 任意日志路径；
- 任意仓库 URL、分支或 tag；
- 任意备份目标路径；
- `sudo` 或系统服务管理；
- 通用文件上传并执行；
- 强制 reset、强制回滚或删除操作。

如果新需求不能安全映射成一个固定动作，就回到 SSH/VPN 处理。

---

## 5. 认证：不要重复发送账号密码

### 5.1 独立管理密钥

应急接口应使用与普通产品账号完全独立的随机密钥：

```python
import secrets
admin_token = secrets.token_urlsafe(32)  # 256 bit 随机性
```

存储位置：

- 服务端：应用数据目录中的受限配置，权限 `0600`；
- 客户端：操作系统用户私有目录，权限 `0600`；
- 不进入 Git、URL、浏览器存储、命令行参数、截图或聊天记录。

在 macOS 中可使用：

```text
~/Library/Application Support/<Service Name>/admin-token
```

客户端读取前应检查：

- 必须是普通文件；
- 所有者必须是当前用户；
- group 和 other 不能有任何权限。

### 5.2 HMAC 请求签名

不要在每个请求中直接传管理密钥。客户端为每个请求生成：

```text
message = timestamp + "\n"
        + nonce + "\n"
        + HTTP_METHOD + "\n"
        + request_path + "\n"
        + sha256(raw_body)

signature = HMAC-SHA256(admin_token, message)
```

推荐 Header（其中 `Service` 替换为项目名）：

```text
X-Service-Timestamp: 1788190000
X-Service-Nonce: 32位随机十六进制
X-Service-Signature: 64位HMAC十六进制
```

Speaking Gym 的参考实现具体使用 `X-Speaking-Gym-Timestamp`、`X-Speaking-Gym-Nonce` 和 `X-Speaking-Gym-Signature`；客户端与服务端名称必须完全一致。

服务端验证：

1. 时间戳与服务器时间差不超过 60 秒；
2. nonce 和签名格式正确；
3. 使用常量时间比较验证 HMAC；
4. nonce 在最近 120 秒内未使用；
5. 验证成功后才解析并执行动作。

这样即使某个 HTTPS 请求被捕获，也不能无限重放 `update` 或 `restart`。

参考实现是单进程服务器，nonce 放在进程内存中。如果目标服务有多个 worker、多个副本或会被负载均衡，应将 nonce 存到所有实例共享且支持原子“仅首次写入”的存储（例如 Redis `SET NX EX` 或数据库唯一键）；否则同一请求可能被发往另一个进程重放。

### 5.3 限速与浏览器隔离

- 每个来源 IP 在十分钟内最多允许五次错误签名；
- 携带浏览器 `Origin` 的请求直接拒绝；
- 不实现 CORS；
- 管理响应使用 `Cache-Control: no-store`；
- 认证失败只返回通用 `401 unauthorized`，不区分密钥、时间戳或签名哪里错误。

---

## 6. TLS：自签证书也必须验证身份

`curl -k` 只加密，不验证服务器是谁，不能作为管理客户端的最终方案。

如果暂时使用自签证书，客户端可以固定证书 SHA-256。客户端指纹文件通常需要纯 64 位十六进制，不能直接保存带文件名或 `-` 后缀的校验输出：

```bash
# macOS
openssl x509 -in cert.pem -outform DER | shasum -a 256 | awk '{print $1}' > server-cert.sha256

# Linux
openssl x509 -in cert.pem -outform DER | sha256sum | awk '{print $1}' > server-cert.sha256
```

客户端连接后读取同一 TLS 会话的 DER 证书并比较指纹；不匹配时必须立即中止。不要先用不可信连接下载“新指纹”再继续，否则固定没有意义。

证书续期会改变完整证书指纹。更新证书后，应通过校园网/VPN SSH 或可信控制台重新核对并更新本地指纹。若希望减少正常续期带来的变更，可进一步固定 SPKI 公钥哈希。

---

## 7. 固定 Git 更新器

### 7.1 固定所有可变量

```bash
REPO="git@github.com:owner/private-repo.git"
BRANCH="main"
SOURCE="$HOME/service-source"
LIVE="$HOME/service-live"
DATA="$HOME/service-data"
```

这些值应写死在服务端脚本中，而不是来自 HTTP 请求。

更新器还必须有跨请求互斥锁。Speaking Gym 部署在 Ubuntu，因此使用 Linux 的 `flock`；macOS 默认没有 `flock`。若目标服务器是 macOS，应改用 Python `fcntl.flock`、`shlock`，或具备崩溃恢复逻辑的原子 `mkdir` 锁，不能直接照抄 Linux 命令。

### 7.2 推荐更新顺序

1. 用专用只读 deploy key 访问私有仓库；
2. 确认现有 origin 与固定 URL 完全一致；
3. `git fetch` 固定分支；
4. 检查目标是当前部署提交的后代，拒绝非快进更新；
5. 使用 `git archive` 导出目标提交到临时目录；
6. 拒绝 symlink、证书、数据库和运行配置；
7. 在临时目录执行语法检查、单元测试或构建；
8. 校验通过后同步到运行目录；
9. 最后写入 deployed-commit 标记；
10. 由单独的 restart 动作激活新后端。

如果仓库含由源码生成的派生文件，应在可信的本地流程或 CI 中生成并提交。应急更新器只验证派生文件格式和一致性，不应在预检阶段用 `eval`、`source` 或 import 提前执行刚下载的仓库代码。Speaking Gym 的 `topics.json` 由正常本地部署流程生成并纳入 Git；应急更新只用 Python 解析并验证其类别和题目结构。

关键检查示意：

```bash
origin="$(git -C "$SOURCE" remote get-url origin)"
[[ "$origin" == "$REPO" ]] || exit 1

git -C "$SOURCE" fetch origin "refs/heads/$BRANCH"
target="$(git -C "$SOURCE" rev-parse FETCH_HEAD)"
git -C "$SOURCE" merge-base --is-ancestor "$current" "$target" || exit 1
```

### 7.3 防止 Git 配置劫持

```bash
export GIT_CONFIG_GLOBAL=/dev/null
export GIT_CONFIG_NOSYSTEM=1
export GIT_SSH_COMMAND="ssh -o BatchMode=yes -o StrictHostKeyChecking=yes"
```

### 7.4 防止归档路径和符号链接问题

导出后遍历整个树：

```python
for base, dirs, files in os.walk(stage):
    for name in dirs + files:
        path = os.path.join(base, name)
        if os.path.islink(path):
            raise SystemExit("symlink rejected")
```

同步时再加一层：

```bash
rsync -a --no-links \
  --exclude='*.pem' \
  --exclude='*.db' \
  --exclude='*.log' \
  --exclude='*.md' \
  --exclude='tools/' \
  --exclude='run.sh' \
  --exclude='admin_update.sh' \
  "$STAGE/" "$LIVE/"
```

应急更新器应禁止覆盖自身。更新管理平面本身需要正常 SSH 或可信控制台，避免一次仓库更新永久改变应急边界。

### 7.5 供应链边界

固定仓库和 HMAC 能防止未认证触发，但不能防止 GitHub 账号或仓库本身被攻破。更高安全等级应增加：

- 只读 deploy key；
- 分支保护和强制审查；
- CI 必须通过；
- signed commit/tag 与服务器固定签名者；
- 两人审批或发布环境审批。

---

## 8. 备份设计

对 SQLite，不要直接在数据库活跃写入时简单 `cp`。使用 SQLite Online Backup API：

```python
source = sqlite3.connect(live_db)
destination = sqlite3.connect(backup_path)
source.backup(destination)
destination.close()
source.close()
```

然后：

- 文件权限设为 `0600`；
- 运行 `PRAGMA integrity_check`；
- 返回文件名和大小，不返回任意服务器路径；
- 按容量设计保留策略；
- 代码更新不会覆盖数据目录。

Speaking Gym 的数据库很小，当前选择保留全部应急备份而不自动删除；因此需要监控磁盘。迁移到较大数据库时，应明确设置容量/数量上限和归档位置，但不要在没有恢复策略的情况下静默删除唯一备份。

备份应发生在可能执行数据库迁移的更新之前。

---

## 9. 重启不能只相信“命令已发送”

服务端必须先返回响应，再延迟启动应用专用重启脚本；否则当前 HTTP 连接可能在响应发出前被自身杀死。

客户端应验证：

1. 重启前读取旧 PID；
2. 发送 restart；
3. 最多等待固定时间；
4. 轮询 status；
5. 确认 PID 已变化；
6. 再运行 health；
7. 超时或健康失败时明确报错。

```text
Restart scheduled
PID 312486 -> 312956
health.database = ok
```

“已调度”不等于“重启成功”，必须由客户端做闭环验证。

---

## 10. 日志与审计

### 10.1 日志接口

只允许返回固定应用日志的最后 N 行，例如 100 行。不要接收文件路径参数。

返回前替换所有当前配置中的敏感值：

```python
for value in (api_key_1, api_key_2, admin_token):
    if value:
        text = text.replace(value, "[REDACTED]")
```

再用模式匹配遮盖常见 Key 格式作为第二道防线。

### 10.2 审计日志

每次管理请求记录：

```json
{
  "ts": "2026-08-31T23:39:14",
  "ip": "203.0.113.7",
  "action": "update",
  "result": "ok",
  "detail": "目标提交的短摘要"
}
```

不要记录管理密钥、完整 Header、请求签名或用户聊天内容。审计文件权限应为 `0600`，且不能通过普通静态路径访问。

---

## 11. 静态服务器必须使用允许列表

这次落地中发现，`SimpleHTTPRequestHandler` 默认会把运行目录里的以下文件全部公开：

- `server.py`；
- 更新和部署脚本；
- 管理客户端；
- 运维文档；
- 内部导出数据。

仅靠禁止 `.db` 和 `.pem` 不够；只按 `.js` 等扩展名放行也不够，因为 `tools/` 中的本地脚本可能同样是 JavaScript。更稳妥的是允许明确的运行文件名和资产目录：

```python
STATIC_ALLOWED = re.compile(
    r"^/(?:$|index\.html|style\.css|(?:app|data|sw)\.js|manifest\.webmanifest|"
    r"icons/[^/]+\.(?:png|jpg|jpeg|svg|webp|ico))$"
)
```

API 路由和证书下载应在静态处理之前显式匹配；其他路径统一返回 404。还应禁止目录列表，并在部署阶段不复制本地工具和运维文档。允许列表必须按项目的实际前端资产调整：如果确实使用 `.woff2`、`.ttf` 等字体，应明确加入对应文件或专用目录；不要为了修复一个 404 又退回“允许所有未知文件”。

---

## 12. 运行时与验证环境必须一致

Speaking Gym 第一次真实更新失败，并不是代码错误，而是：

- 浏览器代码使用合法的现代 JavaScript 可选链 `?.`；
- 服务器系统 Node.js 只有 12；
- `node --check` 将合法代码误判为语法错误。

最终做法是在普通用户目录安装 Node.js 20 LTS：

```text
~/.local/node/bin/node
```

安装包通过 nodejs.org 官方 SHA-256 校验，且不替换系统 Node。通用经验：

- 校验工具必须理解目标运行环境的语法版本；
- 新运行时优先装在应用用户目录，不擅自替换系统包；
- 下载二进制必须固定版本并校验官方哈希；
- 更新脚本启动时应显式检查最低版本。

---

## 13. 必须测试真实更新，不只是“已是最新版”

一次可靠验收至少包括：

### 13.1 认证测试

- 无签名：401；
- 错误密钥：401；
- 过期时间戳：401；
- 重放 nonce：401；
- 浏览器 Origin：401；
- 连续错误触发限速；
- 正确签名可访问。

### 13.2 白名单测试

- 允许 `status`、`health`、`logs`、`backup`、`update`、`restart`；
- `shell` 或其他动作：403；
- `{"action":"backup","path":"..."}` 等额外参数：400。

### 13.3 更新测试

不要只测试：

```text
already current
```

必须实际准备一个后继提交，验证：

```text
旧提交 → fetch → archive → preflight → sync → 新提交标记
```

然后通过 HTTPS 管理接口重启并验证新 PID。

### 13.4 数据与静态暴露测试

- 在线备份 `PRAGMA integrity_check = ok`；
- 数据库大小和主要记录数未异常变化；
- `server.py`、运维脚本、内部 JSON、日志、数据库均返回 404；
- 首页、JS、CSS、manifest 和图片仍返回 200。

---

## 14. 从零迁移到另一个仓库

### 阶段 A：资格判断

- [ ] 服务已有公网可达的 HTTPS 端口；
- [ ] 组织策略允许应用级管理动作；
- [ ] 不需要通用 shell；
- [ ] 服务以普通用户运行；
- [ ] 服务器能出站访问固定私有 Git 仓库；
- [ ] 数据目录与代码目录分离；
- [ ] 有可靠的应用专用重启方式。

任一核心条件不满足，应优先申请 VPN/堡垒机，不要强行套用。

### 阶段 B：服务端

- [ ] 定义最小动作白名单；
- [ ] 实现 HMAC、时间戳和 nonce 防重放；
- [ ] 增加错误签名限速；
- [ ] 拒绝浏览器 Origin；
- [ ] 固定仓库、分支、运行目录和数据目录；
- [ ] 拒绝非快进更新；
- [ ] 在临时目录预检；
- [ ] 拒绝 symlink 和敏感文件；
- [ ] 备份数据库；
- [ ] 写审计日志；
- [ ] 静态资源改为允许列表。

### 阶段 C：客户端

- [ ] 生成独立 256 位密钥；
- [ ] 凭据文件权限 `0600`；
- [ ] 固定 TLS 证书或 SPKI 指纹；
- [ ] 密钥不作为命令行参数；
- [ ] update 与 restart 分开；
- [ ] restart 后验证 PID 和 health；
- [ ] 所有失败默认停止，而不是自动强制继续。

### 阶段 D：发布与验收

- [ ] 先通过 SSH 引导部署管理接口；
- [ ] 推送固定仓库；
- [ ] 执行一次真实快进更新；
- [ ] 从校外网络、不连 VPN 运行 status；
- [ ] 验证备份完整性；
- [ ] 验证静态敏感文件全部 404；
- [ ] 记录撤销方式；
- [ ] 明确“VPN 不工作时才用”。

---

## 15. 撤销和故障恢复

应急通道必须易于撤销：

1. 通过校园网/VPN SSH 删除服务端配置中的管理密钥；如果服务也支持环境变量（参考实现为 `SG_ADMIN_TOKEN`），必须同时从启动脚本或服务管理器中取消该变量；
2. 重启服务并确认旧进程已退出；
3. 管理接口应返回 `503 admin interface disabled`；
4. 删除客户端本地密钥；
5. 保留审计日志用于复核。

下列故障无法依靠同一个 1511 接口自救：

- 1511 端口不可达；
- TLS 证书丢失或服务无法启动；
- Python 主程序有语法错误；
- 管理认证代码损坏；
- Unix 账号、磁盘或主机本身故障；
- GitHub 不可访问且本地没有可部署版本。

此时必须回到 VPN、校园网 SSH、管理员控制台或物理访问。

---

## 16. Speaking Gym 参考实现

本仓库中可以直接参考：

| 文件 | 用途 |
|---|---|
| `speaking-gym/server.py` | HMAC 验证、动作白名单、状态、健康、日志、备份和重启 |
| `speaking-gym/admin_update.sh` | 固定 GitHub 源、快进检查、临时预检和安全同步 |
| `speaking-gym/tools/emergency_admin.py` | TLS 指纹固定、请求签名和重启闭环验证 |
| `speaking-gym/tools/export_topics.js` | 在可信本地环境生成并提交派生话题 JSON |
| `speaking-gym/EMERGENCY_ADMIN.md` | 本项目的具体操作手册 |
| `speaking-gym/deploy.sh` | 正常 SSH 部署和提交标记同步 |

线上实测过的完整链路：

```text
GitHub 快进更新成功
SQLite 在线备份及完整性检查成功
应用重启 PID 变化并恢复健康
无认证/过期/重放/非白名单动作被拒绝
后端源码和运维文件返回 404
手机热点、无 VPN 状态下 status 成功
```

---

## 17. 最重要的经验

1. **先定位网络层次，再设计方案。** DNS、端口和认证不是一回事。
2. **已有 HTTPS 可达，不代表应该在里面塞一个 shell。**
3. **真正的安全来自能力缩减。** 固定动作比“更强密码的任意命令”安全得多。
4. **管理密钥不能裸传，管理请求不能重放。**
5. **更新源不仅要私有，还要固定、快进、预检和审计。**
6. **重启必须闭环验证。** “已发送”不是“已恢复”。
7. **验证工具的版本也是系统的一部分。** 旧 Node 会误判新 JavaScript。
8. **静态文件服务默认可能泄露整个后端源码。** 优先使用允许列表。
9. **一定要测试真正的版本跃迁。** `already current` 不证明更新能工作。
10. **应急通道必须能被迅速撤销，并承认自己无法自救的故障范围。**
