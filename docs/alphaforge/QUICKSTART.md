# AlphaForge 快速开始

## 选择运行方式

| 目标 | 推荐方式 |
| --- | --- |
| 开发 AlphaForge 前端和 Research OS | 本地 Python 专用服务器 + Vite |
| 只想打开完整构建后的产品 | 本地 Python 专用服务器托管 `frontend/dist` |
| 验证上游 Vibe 兼容功能和一体镜像 | Docker Compose |
| 运行 generated research Python 的完整隔离路径 | 本地 Python 服务 + 宿主机 Docker + 已固定研究镜像 |

AlphaForge 专用服务器与默认 Docker 服务的权限面不同，见 [Docker 指南](DOCKER.md#两种-docker-用途不要混淆)。

## 前置要求

- Python 3.11（项目声明支持 3.11+，当前 Docker 固定 3.11）；
- Node.js 20 和 npm（前端开发/构建）；
- PowerShell 7（Windows 命令）；
- Docker Desktop/Engine（Docker 部署或 Research Sandbox）；
- Git（GitHub 协作）。

官方安装入口：

- Python：<https://www.python.org/downloads/>
- Node.js：<https://nodejs.org/en/download>
- PowerShell：<https://learn.microsoft.com/powershell/scripting/install/installing-powershell>
- Docker Desktop for Windows：<https://docs.docker.com/desktop/setup/install/windows-install/>
- Git：<https://git-scm.com/downloads>

## 1. 获取代码

仓库上传 GitHub 后：

```powershell
git clone https://github.com/<YOUR_ORG>/alphaforge-research-os.git
Set-Location alphaforge-research-os
```

当前本地工作区尚不是有效 Git 仓库；首次上传步骤见 [GitHub 协作](GITHUB_COLLABORATION.md)。

## 2. 检查版本

```powershell
$PSVersionTable.PSEdition
python --version
node --version
npm --version
docker --version
docker compose version
```

`PSEdition` 应为 `Core`，Python 应为 3.11.x，Node 建议为 20.x。

## 3. 创建环境文件

```powershell
Copy-Item agent\.env.example agent\.env
```

`agent/.env` 被 Git 忽略。不要提交真实密钥。

第一次只看离线界面和运行冻结测试时，无需配置真实 Provider；要发起真实模型研究时再按 [配置指南](CONFIGURATION.md) 设置 AlphaForge LLM 变量。

## 4. 安装 Python 依赖

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -e ".[dev]"
```

如果 PowerShell 阻止激活脚本，可在当前用户范围启用签名脚本，或直接使用虚拟环境 Python：

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

## 5. 安装并构建前端

```powershell
Set-Location frontend
npm ci
npm run build
Set-Location ..
```

必须使用 `npm ci`，让安装与 `frontend/package-lock.json` 一致。

## 6. 启动 AlphaForge 专用服务

```powershell
python agent\alphaforge_api_server.py --host 127.0.0.1 --port 8000
```

打开：

- 产品：<http://127.0.0.1:8000>
- 健康检查：<http://127.0.0.1:8000/healthz>
- 系统状态：<http://127.0.0.1:8000/veriquant/status>

预期健康结果包括：

```json
{
  "status": "ok",
  "product": "AlphaForge（千因研究院）",
  "loopback_only": true,
  "live_routes_registered": false
}
```

专用服务不会暴露 Swagger/OpenAPI，也不会注册实时交易、Broker、OAuth、Mandate 或订单接口。

## 7. 前端热更新开发

终端 A：

```powershell
python agent\alphaforge_api_server.py --host 127.0.0.1 --port 8000
```

终端 B：

```powershell
Set-Location frontend
$env:VITE_VERIQUANT_API_URL='http://127.0.0.1:8000'
npm run dev
```

打开 <http://127.0.0.1:5899>。Vite 应把 `/api/veriquant`、`/veriquant/status` 和 Workbench 读取代理到专用服务器。

## 8. 第一次研究

推荐先使用一个边界清晰的问题：

> 在大 A 市场中，检验盈利质量和换手率与未来 20 个交易日收益的关系，并说明样本、时点、成本和增量性缺口。

正确流程是：

1. 输入问题；
2. 查看计划、数据范围、研究组、预算与停止条件；
3. 显式确认；
4. 启动运行；
5. 查看各组状态和 typed failures；
6. 在合法终态读取报告；
7. 核对 Evidence 等级，辅助研究不能当成官方结果。

如果没有真实模型、固定研究沙箱镜像或合格数据，系统应返回受控的数据/能力缺口，而不是伪造成功。

## 9. 最小验证

```powershell
python agent\alphaforge_api_server.py --help

python -m pytest -q -p no:cacheprovider --confcutdir=agent/tests/veriquant `
  agent/tests/veriquant/research_os_v3/test_u6_api.py `
  agent/tests/veriquant/research_os_v3/test_u6_control.py

Set-Location frontend
npm run test:run
npm run build
```

如果只修改文档，不需要跑真实模型或真实数据；至少执行文档链接和忽略规则检查，见 [开发与测试](DEVELOPMENT.md)。

## macOS/Linux 差异

虚拟环境激活：

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

启动：

```bash
python agent/alphaforge_api_server.py --host 127.0.0.1 --port 8000
```

Compose 命令相同。路径示例需要改为 POSIX 形式。

## 下一步

- 配置模型和数据：[CONFIGURATION.md](CONFIGURATION.md)
- 用 Docker：[DOCKER.md](DOCKER.md)
- 理解页面与报告：[USER_GUIDE.md](USER_GUIDE.md)
- 做前端：[FRONTEND_PRODUCT_REQUIREMENTS.md](FRONTEND_PRODUCT_REQUIREMENTS.md)
- 遇到问题：[TROUBLESHOOTING.md](TROUBLESHOOTING.md)
