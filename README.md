# AlphaForge（千因研究院）

> 面向 Alpha 研究的多专业智能体研究操作系统：让研究代理负责提出问题、调查、实验和解释，让确定性服务负责数据时点、证据、权限与复现。

[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-3776AB)](https://www.python.org/)
[![React 19](https://img.shields.io/badge/React-19-61DAFB)](https://react.dev/)
[![FastAPI](https://img.shields.io/badge/FastAPI-Research_OS-009688)](https://fastapi.tiangolo.com/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

AlphaForge 将一个自然语言 Alpha 问题编译成受预算约束的 Research Campaign，选择一到六个专业研究组，在隔离环境里形成候选与标准研究表，再由独立的确定性服务给出可追溯的 Evidence 和中文报告。

产品名统一使用 **AlphaForge（千因研究院）**。代码中的 `VeriQuant` 是内部可信研究内核；`vibe-trading`、`vibe-trading-ai` 等名称是上游运行时与包兼容标识，不是本产品对外品牌。

## 当前状态

最近的产品收敛门禁结论为：

- `QUALIFIED_FOR_RESEARCH_PRODUCT_WITH_AUXILIARY_LIMITATION`；
- 500 个冻结问题已完成静态解析与路由，未声称逐题真实执行；
- 25 个代表研究家族通过；
- 六个专业研究组均有资格收据，其中 5 组形成官方 Evidence 路径，1 组受辅助证据上限约束；
- 最终产品脊柱与收敛门禁记录为 194 项测试通过；
- 最新三组专业报告已经跑通规划、只读调查、代码审查、隔离执行和中文报告，但报告中的回归结果仍标记为 `AUXILIARY_ONLY`，尚未完成成本后收益、直接增量性、完整状态稳定性和独立晋级。

这意味着 AlphaForge 已可用于可复现研究与报告生成，但任何单次结果都不等于可交易 Alpha，也不构成投资建议。

详见 [当前状态与已知限制](docs/alphaforge/CURRENT_STATUS.md)。

## 一条完整产品路径

```text
研究问题
  → 研究计划与数据范围
  → 用户显式确认
  → 选择 1–6 个专业研究组
  → 有界 AgentLoop + 只读工具
  → 冻结数据与来源身份
  → 研究代码机械审查
  → 无网络隔离运行
  → 标准研究表
  → 独立确定性评价
  → Evidence / 明确的数据缺口
  → 面向用户的中文研究报告
```

六个专业研究组为：

1. 基本面研究组；
2. 预期与事件研究组；
3. 市场行为与交易可行性研究组；
4. 另类数据研究组；
5. 宏观状态研究组；
6. 组合增量研究组。

系统按问题选择必要研究组，不会为了“多智能体”而让六组全部运行。

## 五分钟开始

### 方式 A：本地 AlphaForge 专用服务（前端开发首选）

要求 Python 3.11、Node.js 20 和 PowerShell 7。

```powershell
Set-Location "C:\path\to\AlphaForge"
Copy-Item agent\.env.example agent\.env

python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -e ".[dev]"

Set-Location frontend
npm ci
npm run build
Set-Location ..

python agent\alphaforge_api_server.py --host 127.0.0.1 --port 8000
```

打开 <http://127.0.0.1:8000>。该入口只注册 AlphaForge Research OS、只读 Workbench 与健康检查，不注册 Vibe 的实时交易、Broker、OAuth、订单或 Mandate 路由。

如需真实模型或数据源，在本地 `agent/.env` 中配置；不要提交该文件。没有凭据时应使用离线/冻结数据路径，缺失能力必须显示为明确的部分结果或数据缺口。

### 方式 B：Docker 一体化兼容服务

要求 Docker Desktop 或 Docker Engine + Compose。

```powershell
Set-Location "C:\path\to\AlphaForge"
Copy-Item agent\.env.example agent\.env
docker compose up --build -d
docker compose ps
```

打开 <http://127.0.0.1:8899>。默认 Compose 使用上游兼容服务器并内嵌 AlphaForge 页面和 API；它还包含 Vibe 的兼容路由，因此不要把该模式描述为“只暴露研究接口”。默认端口只绑定本机回环地址。

停止服务：

```powershell
docker compose down
```

命名卷保存运行、会话和用户状态；只有明确要清空本地状态时才使用 `docker compose down -v`。

完整步骤见 [快速开始](docs/alphaforge/QUICKSTART.md) 与 [Docker 指南](docs/alphaforge/DOCKER.md)。

## 文档导航

| 你要解决的问题 | 文档 |
| --- | --- |
| 先理解产品和研究理念 | [产品理念](docs/alphaforge/PRODUCT_VISION.md) |
| 看真实架构、数据流和权限边界 | [系统架构](docs/alphaforge/ARCHITECTURE.md) |
| 从零安装并运行 | [快速开始](docs/alphaforge/QUICKSTART.md) |
| 安装和维护 Docker | [Docker 指南](docs/alphaforge/DOCKER.md) |
| 配置模型、数据与环境变量 | [配置指南](docs/alphaforge/CONFIGURATION.md) |
| 发起研究并阅读结果 | [用户指南](docs/alphaforge/USER_GUIDE.md) |
| 对接 Research OS V3 API | [API 参考](docs/alphaforge/API_REFERENCE.md) |
| 理解安全、Evidence 与金融权限 | [安全与权限](docs/alphaforge/SECURITY_AND_AUTHORITY.md) |
| 做后端或研究内核开发 | [开发与测试](docs/alphaforge/DEVELOPMENT.md) |
| 上传 GitHub、分支和 PR 协作 | [GitHub 协作](docs/alphaforge/GITHUB_COLLABORATION.md) |
| 让 AI 继续完成前端 | [前端产品需求](docs/alphaforge/FRONTEND_PRODUCT_REQUIREMENTS.md) |
| 直接复制给前端 AI 的任务 | [前端 AI Prompt](docs/alphaforge/FRONTEND_AI_PROMPT.md) |
| 看完成度、缺口和后续里程碑 | [当前状态](docs/alphaforge/CURRENT_STATUS.md) · [路线图](docs/alphaforge/ROADMAP.md) |
| 排查常见问题 | [故障排查](docs/alphaforge/TROUBLESHOOTING.md) |

完整索引见 [AlphaForge 文档中心](docs/alphaforge/README.md)。

## 仓库结构

```text
agent/
  alphaforge_api_server.py      # AlphaForge 本地专用安全入口
  api_server.py                 # Vibe 兼容的一体化入口
  src/agent/                    # ReAct AgentLoop、上下文与工具注册
  src/swarm/                    # DAG/Swarm 调度与隔离 worker
  src/veriquant/                # 冻结数据、研究控制、Evidence、报告等可信内核
  tests/                        # Python 测试
frontend/
  src/pages/                    # AlphaForge 页面
  src/features/research-os-v3/  # V3 类型、客户端、投影与组件
  src/features/research-os-v3-launch/ # 启动与运行流程
docs/alphaforge/                # 面向用户、协作者和 AI 的公开文档
.autobuild/veriquant/           # 本地运行产物与收据；不要整体提交
```

## 开发检查

后端窄集：

```powershell
python -m pytest -q -p no:cacheprovider --confcutdir=agent/tests/veriquant `
  agent/tests/veriquant/research_os_v3 `
  agent/tests/veriquant/agent_research_product
```

前端：

```powershell
Set-Location frontend
npm ci
npm run test:run
npm run build
```

任何涉及 Evidence、PIT、研究沙箱、Promotion、风险、订单或账本的修改，都必须补针对性负向测试，不能仅用页面可显示或报告已生成作为通过依据。

## GitHub 协作建议

推荐仓库名：`alphaforge-research-os`。

当前工作区的 `.git` 元数据为空，`git status` 会报告“not a git repository”。首次上传前应先做密钥和产物检查，再初始化新仓库；不要把真实 `agent/.env`、模型密钥、Token、运行产物、账户信息或本地 Agent 记忆提交到 GitHub。

完整、可复制的初始化与推送命令见 [GitHub 协作指南](docs/alphaforge/GITHUB_COLLABORATION.md)。

## 安全声明

- 默认离线、回环地址、最小权限、失败关闭；
- LLM 只能提出 `DRAFT` 或辅助研究结果，不能生成官方指标或自行晋级；
- 研究代码只能在经过机械审查的无网络隔离环境中运行；
- 前端只能展示后端允许的公开字段，不能创建或修改 Evidence、Promotion、组合、风险、订单或账本；
- 默认禁止真实 Broker、真实订单、真实资金与密钥进入 AlphaForge 研究产品路径。

详见 [SECURITY.md](SECURITY.md) 和 [安全与权限边界](docs/alphaforge/SECURITY_AND_AUTHORITY.md)。

## 上游与许可

AlphaForge 在 HKUDS 的开源项目 [Vibe-Trading](https://github.com/HKUDS/Vibe-Trading) 基础上扩展了 VeriQuant 可信研究内核、Research OS、产品门禁、报告与 AlphaForge 前端。为兼容上游生态，部分包名、CLI、目录和 Docker 服务名仍保留 `vibe-trading`。

项目遵循 [MIT License](LICENSE)。上游与因子公式归属见 [NOTICE](NOTICE) 及各因子库目录中的 `LICENSE.md`/`NOTICE`。重命名产品不改变上游版权与许可证义务。

## 免责声明

本项目用于研究与软件工程验证，不构成投资建议、收益承诺或交易授权。研究结果可能为负向、部分完成、数据缺口或系统失败；只有绑定冻结输入并由独立确定性服务生成的指标才可标记为官方 Evidence，且官方 Evidence 也不自动意味着可上线或可交易。
