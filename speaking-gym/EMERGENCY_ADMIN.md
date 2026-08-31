# Speaking Gym 应急管理接口

## 使用边界

本接口仅用于 **Ivanti VPN 无法工作、而 Speaking Gym 的 1511 HTTPS 端口仍可访问时**进行应急维护。正常维护仍优先使用校园网或 Ivanti VPN 后的 SSH。

它不是 Web Shell，也不接受任何 shell 命令、路径、仓库地址、分支或自定义参数。服务器只允许六个编译在代码中的固定动作；若这些动作不足以修复故障，应恢复 VPN/校园网并使用 SSH。

## 固定动作

| 命令 | HTTP 动作 | 用途 | 是否改变状态 |
|---|---|---|---|
| `status` | `GET /api/admin/status` | 查看版本、进程、运行时长、磁盘和数据量 | 否 |
| `health` | `POST /api/admin/action` | 检查数据库和 AI/Whisper/TTS/Push 组件 | 否 |
| `logs` | `GET /api/admin/logs` | 查看 `server.log` 最后 100 行（自动遮盖 API Key） | 否 |
| `backup` | `POST /api/admin/action` | 用 SQLite 在线备份 API 创建数据库快照 | 是 |
| `update` | `POST /api/admin/action` | 仅从固定私有仓库 `aweakdog/speaking-gym` 的 `main` 快进更新 | 是 |
| `restart` | `POST /api/admin/action` | 延迟一秒后，仅重启 Speaking Gym | 是 |

API 不支持其他动作。`update` 与 `restart` 分开，是为了在重启前能够看到更新校验结果。

## 日常应急更新流程

先在本地完成测试、提交并推送：

```bash
cd "/Users/yuanhangli/Documents/Documents - yuanhang’s MacBook Pro/code/English"
git push origin main
```

VPN 不可用但 1511 可访问时：

```bash
cd speaking-gym
python3 tools/emergency_admin.py status
python3 tools/emergency_admin.py backup
python3 tools/emergency_admin.py update
python3 tools/emergency_admin.py restart   # 客户端自动等待并验证新 PID 与健康状态
python3 tools/emergency_admin.py health
python3 tools/emergency_admin.py status
```

如果只改了静态文件，仍建议执行 `restart`，使服务端的版本与话题缓存状态一致。

## 更新动作的安全限制

`admin_update.sh` 固定执行以下过程：

1. 仅访问 `git@github.com:aweakdog/speaking-gym.git`；
2. 仅获取 `main`；
3. 拒绝 origin 被替换；
4. 拒绝非快进更新，防止公网接口回滚到任意旧提交；
5. 用 `git archive` 导出 `speaking-gym/` 到临时目录；
6. 拒绝仓库中出现 `.pem`、`.db` 或 `config.json`；
7. 检查 JavaScript 和 Python 语法，并重新生成 `topics.json`；
8. 拒绝符号链接，并以 `rsync --no-links` 再做一次防护；
9. 只同步应用代码，不接触证书、数据库、日志、`run.sh`、`admin_update.sh` 和数据目录；应急更新脚本自身只能通过正常 SSH 部署更新；
10. 写入已部署提交编号，等待单独的 `restart` 动作。

同一时刻只允许一个改变状态的管理动作运行。服务器使用用户目录下的 `~/.local/node/bin/node`（Node.js 18+）执行现代 JavaScript 语法检查；当前固定安装的版本为 Node.js 20.20.2 LTS，安装包经过 nodejs.org 官方 SHA-256 校验，不替换系统 Node。

## 认证与 TLS

管理接口不使用普通 Speaking Gym 账号密码，而使用独立随机生成的 256 位管理令牌：

```text
~/Library/Application Support/Speaking Gym/admin-token
```

令牌只放在本机受限文件和服务器的 `~/speaking-gym-data/config.json` 中，不进入 Git、URL、浏览器存储、HTTP Header 明文或命令行参数。本机客户端会拒绝所有者不是当前用户、或 group/other 具有任何权限的凭据文件。每个 HTTP 请求使用该令牌对“时间戳 + 随机 nonce + 方法 + 路径 + 请求体哈希”生成 HMAC-SHA256 签名；服务器只接受 60 秒内的请求，同一 nonce 不能重放。

客户端还固定校验服务器 TLS 证书的 SHA-256 指纹：

```text
~/Library/Application Support/Speaking Gym/server-cert.sha256
```

即使当前使用自签证书，客户端也不会静默接受另一张证书。证书更新后必须通过校园网/VPN SSH 重新核对并更新本地指纹。

其他保护：

- 带浏览器 `Origin` 的请求一律拒绝，接口只能由非浏览器管理客户端调用；
- 请求签名超过 60 秒、格式错误或 nonce 已使用时一律拒绝；
- 每个来源 IP 在 10 分钟内最多允许 5 次错误签名；
- 返回内容禁止缓存；
- 所有尝试写入 `~/speaking-gym-data/admin-audit.log`；
- 日志接口固定为最后 100 行，不能指定任意文件或路径；
- 管理进程以普通用户 `yliog` 运行，没有 `sudo` 权限。

## 故障处理

- `401 unauthorized`：本地令牌、请求签名或本机时钟异常；不要反复重试，5 次后当前 IP 会锁定 10 分钟。
- `TLS certificate fingerprint mismatch`：立即停止使用；可能是证书更新、连接到错误主机或中间人攻击。通过校园网/VPN SSH 核对证书。
- `refusing non-fast-forward update`：GitHub `main` 比当前部署旧或历史被重写。接口刻意不提供强制回滚；通过 SSH 人工检查。
- `admin interface disabled`：服务器没有配置管理令牌，必须通过校园网/VPN SSH 初始化。
- 1511 本身不可访问、Python 无法启动或管理代码语法损坏：本接口无法自救，必须使用校园网/VPN SSH。

## 令牌撤销

应急接口不再需要或怀疑令牌泄漏时，通过校园网/VPN SSH 删除服务器配置中的 `admin_token` 并重启服务；接口随即返回 `503 admin interface disabled`。需要继续使用时，应生成新令牌并同步更新本地受限文件。
