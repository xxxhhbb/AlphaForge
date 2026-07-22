# AlphaForge 故障排查

## PowerShell 不是 Core

检查：

```powershell
$PSVersionTable.PSEdition
```

应为 `Core`。在 Windows 显式使用：

```powershell
pwsh.exe -NoLogo -NoProfile
```

## `python` 版本不对

```powershell
Get-Command python
python --version
py -0p
```

创建环境时可显式：

```powershell
py -3.11 -m venv .venv
```

## PowerShell 禁止激活虚拟环境

无需改变全局策略也可以直接调用：

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe agent\alphaforge_api_server.py
```

## `node` 或 `npm` not recognized

安装 Node.js 20 LTS，重开终端：

```powershell
node --version
npm --version
```

不要依赖仓库中已有 `node_modules`，应运行 `npm ci`。

## `npm ci` 失败

确认位于 `frontend/` 且锁文件存在：

```powershell
Set-Location frontend
Test-Path package-lock.json
npm cache verify
npm ci
```

公司网络下检查 npm registry/proxy；不要删除锁文件改用随意版本。

## 前端页面打开但 API 404

本地 Vite 开发应设置：

```powershell
$env:VITE_VERIQUANT_API_URL='http://127.0.0.1:8000'
npm run dev
```

确认 `frontend/vite.config.ts` 的 VeriQuant 代理包含 `/api/veriquant`。

直接运行构建版时先 `npm run build`，再由 `alphaforge_api_server.py` 托管。

## 专用服务器拒绝 Host/Origin

专用服务只允许 loopback。使用：

```text
http://127.0.0.1:8000
```

不要用局域网 IP、代理域名或 `0.0.0.0` 访问。403 `NON_LOOPBACK_HOST`/`UNTRUSTED_ORIGIN` 是安全行为。

## 端口占用

```powershell
Get-NetTCPConnection -LocalPort 8000 -ErrorAction SilentlyContinue
Get-NetTCPConnection -LocalPort 5899 -ErrorAction SilentlyContinue
Get-NetTCPConnection -LocalPort 8899 -ErrorAction SilentlyContinue
```

换端口启动专用服务：

```powershell
python agent\alphaforge_api_server.py --host 127.0.0.1 --port 8010
```

同步更新 `VITE_VERIQUANT_API_URL`。

## Docker daemon 不可用

```powershell
docker version
docker info
docker context show
```

启动 Docker Desktop，确认使用 Linux containers。WSL 2 可检查：

```powershell
wsl --version
wsl --status
```

## Compose 启动失败

```powershell
docker compose ps
docker compose logs --tail 300 vibe-trading
docker compose config --services
```

不要把完整 `docker compose config` 贴到公开 Issue，它可能展开环境值。

常见原因：

- `agent/.env` 不存在；
- 8899 已占用；
- Docker 内存不足；
- 锁文件或镜像 digest 变化；
- bind mount 的 `agent/.env` 权限异常。

## `OS_SANDBOX_UNAVAILABLE`

这表示 Docker CLI/daemon、固定本地镜像或 image ID 不满足，不是研究结果失败。

```powershell
docker image inspect veriquant-m2-test:local --format '{{.Id}}'
```

没有固定镜像时不要：

- 把 generated Python 放到主机直接运行；
- 临时改成 `python:latest`；
- 移除 `--network none`/`--read-only`；
- 把 `ProcessFencedResearchRunner` 的辅助输出改称官方。

应返回数据/能力缺口，并由维护者补可复现镜像资产。

## Provider 未配置或失败

检查脱敏配置名称，不打印值：

```powershell
Select-String -Path agent\.env -Pattern '^(ENABLE_LLM_RESEARCH|ALPHAFORGE_LLM_MODEL|ALPHAFORGE_LLM_ENDPOINT_URL|ALPHAFORGE_LLM_APPROVED_HOSTS)='
```

典型失败：

- Endpoint host 不在 allowlist；
- Key 仍是占位符；
- 兼容 Endpoint 路径不正确；
- timeout/token 预算不足；
- 模型反复返回不满足合同的代码。

正确状态是 `PROVIDER_FAILED` 或 `ADAPTER_UNAVAILABLE`，不是空白页面。

## 数据准备失败

`prepare-data` 必须显式 `allowNetwork=true`，并提供 `YYYYMMDD` 的 `endDate`。还需有效数据配置与资源预算。

存在字段但没有可用时间覆盖仍会被判不可用。新闻/研报晚于 Evidence 窗口时只能辅助。

## 运行一直显示“运行中”

1. 查询 `/status` 原始 `current_stage`；
2. 查看每组 `specialist_summaries`；
3. 检查模型调用是否已结束；
4. 检查 Docker Runner 是否仍在运行；
5. 查看 manifest/events 的更新时间；
6. 使用取消或恢复 API，不要删除运行目录；
7. 若 UI 未停止轮询而后端已终态，检查状态映射和缓存。

耗时的本地数据冻结/校验阶段可能没有模型调用，UI 应显示具体阶段，不能只显示旋转图标。

## 报告不存在或为空

只有合法终态才读取 `report.md`。检查：

```text
GET /api/veriquant/research-os/v3/runs/{run_id}/status
GET /api/veriquant/research-os/v3/runs/{run_id}/report
GET /api/veriquant/research-os/v3/runs/{run_id}/report.md
```

404 表示尚未发布或 ID 错误；409 表示产物损坏，应 fail closed。

## 报告表格显示错误

正式页面应使用 `react-markdown` + `remark-gfm`，不要用逐行手写 Markdown parser。禁用原始 HTML，确保表格在窄屏可横向滚动。

## UI 显示“官方”但收据是辅助

这是高优先级产品安全 bug：

1. 保存 run ID 和公开 JSON；
2. 不传播截图结论；
3. 检查 `evidence_authority`/`machine_evidence_status`；
4. 前端停止从文案或状态颜色推断；
5. 增加回归测试；
6. 修正文案和报告，不修改机器收据来迎合 UI。

## Git 说不是仓库

当前工作区 `.git` 为空。按照 [GitHub 协作指南](GITHUB_COLLABORATION.md) 做上传前检查，再运行：

```powershell
git init -b main
```

不要从不明来源复制 `.git` 目录。

## 新文档没有被 Git 跟踪

```powershell
git check-ignore -v docs/alphaforge/README.md
```

应无输出。旧 `docs/veriquant/**` 是内部历史文档，默认仍被忽略。

## 根状态与最新 Harness 冲突

`.autobuild/veriquant/state.json` 是恢复快照，当前可能落后于更新的 final harness。不要手工改成通过。以更晚、同一路径、带收据的 final verdict 为准，并安排状态源收敛。

## 报告 bug 时提供什么

- 操作系统、Python、Node、Docker 版本；
- 启动方式：专用本地/一体 Docker/Vite；
- run ID、Campaign ID；
- HTTP status 与脱敏错误；
- 相关测试命令；
- 最小复现；
- 是否涉及网络、Provider、Sandbox 或真实数据；
- 不含秘密和私有数据的截图/日志片段。
