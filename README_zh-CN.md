<p align="center">
  <a href="README.md">English</a> · <strong>简体中文</strong>
</p>

<p align="center">
  <img src="frontend/public/brand/alphaforge-hero-v1.png" alt="AlphaForge 千因研究院 — 自主多智能体 Alpha 研究操作系统" width="100%" />
</p>

<h1 align="center">AlphaForge · 千因研究院</h1>

<p align="center">
  <strong>自主多智能体 Alpha 研究操作系统</strong><br />
  从一个开放问题，到一个受证据约束的答案。
</p>

<p align="center">
  <a href="docs/alphaforge/QUICKSTART.md"><img alt="五分钟上手" src="https://img.shields.io/badge/五分钟-快速开始-20B2AA?style=flat-square" /></a>
  <a href="pyproject.toml"><img alt="Python 3.11+" src="https://img.shields.io/badge/Python-3.11+-3776AB?style=flat-square&logo=python&logoColor=white" /></a>
  <a href="frontend/package.json"><img alt="React 19" src="https://img.shields.io/badge/React-19-20232A?style=flat-square&logo=react&logoColor=61DAFB" /></a>
  <a href="docs/alphaforge/DOCKER.md"><img alt="Docker" src="https://img.shields.io/badge/Docker-ready-2496ED?style=flat-square&logo=docker&logoColor=white" /></a>
  <a href="LICENSE"><img alt="MIT License" src="https://img.shields.io/badge/License-MIT-8A2BE2?style=flat-square" /></a>
  <img alt="仅限研究" src="https://img.shields.io/badge/边界-仅限研究-6B7280?style=flat-square" />
</p>

<p align="center">
  <a href="#为什么是-alphaforge">为什么选择</a> ·
  <a href="#系统如何工作">研究流程</a> ·
  <a href="#六个专业研究组">六大研究组</a> ·
  <a href="#快速开始">快速开始</a> ·
  <a href="#文档地图">文档地图</a> ·
  <a href="CONTRIBUTING.md">参与贡献</a>
</p>

---

AlphaForge（千因研究院）把自然语言提出的 Alpha 问题，转化为一场有边界、可回放的自主研究活动。多个专业研究组协作探索假设、构造公式、申请实验、质疑薄弱证据，最后形成“答案优先”的研究报告。

整个系统遵循一个核心原则：

> **智能体负责探索，确定性证据负责裁决。**

Vibe-Trading 提供 AgentLoop、工具注册、Swarm 与编排等运行外壳；AlphaForge 在此之上补齐真正的研究契约：公式语义审查、隔离实验、冻结数据证据、完整溯源，以及明确的权限边界。

> [!IMPORTANT]
> AlphaForge 是研究系统，不下单、不接管券商账户、不管理资金，也不提供投资建议。动态或未固定的数据在通过确定性证据链之前，只能作为辅助信息。

## 一个问题，一场完整研究

~~~powershell
alphaforge research "盈利预测上修广度能否预测未来 20 日超额收益？" --label-horizon 20 --print-report
~~~

<div align="center">

**问题** → **规划** → **多研究组并行探索** → **审查** → **沙箱实验** → **证据** → **答案**

</div>

最终产物不只是一段对话。AlphaForge 会同时生成直接答案、证据卡片、实验产物、样本覆盖与失败披露、因子溯源信息和可回放元数据。

## 为什么是 AlphaForge

<table>
  <tr>
    <td width="33%"><strong>自主研究</strong><br />系统会拆解问题、分配研究组、沿证据继续追查，并主动修正薄弱假设。</td>
    <td width="33%"><strong>六个专业研究组</strong><br />基本面、预期事件、市场结构、另类数据、宏观状态与 Meta-Alpha 并行工作。</td>
    <td width="33%"><strong>理解公式含义</strong><br />AST、单位、定义域、时序和预测周期共同审查，而不只是检查语法能否运行。</td>
  </tr>
  <tr>
    <td><strong>隔离实验</strong><br />通过审查的代码在固定、无网络的沙箱中，对受控数据执行。</td>
    <td><strong>证据先于置信度</strong><br />确定性服务统一计算指标、溯源、覆盖率和证据状态。</td>
    <td><strong>答案优先的报告</strong><br />每个结论都能看到支持证据、适用边界、失败分支与复现路径。</td>
  </tr>
</table>

### 一个可信答案应当包含什么

| 层级 | AlphaForge 记录的内容 |
| --- | --- |
| 结论 | 直接答案、置信度、适用周期和决策边界 |
| 证据 | 指标、基线、稳健性检验和证据状态 |
| 覆盖 | 标的、日期、股票池、缺失率和剔除样本 |
| 溯源 | 数据指纹、公式身份、运行环境、随机种子和产物路径 |
| 失败披露 | 被拒公式、不可用数据、薄弱分支和未解决风险 |
| 回放 | 复现已接受结果所需的稳定输入与命令 |

## 系统如何工作

<p align="center">
  <img src="frontend/public/brand/alphaforge-research-flow-v1.png" alt="AlphaForge 从研究问题到可信答案的完整流程" width="100%" />
</p>

### 权限由架构决定

| 组件 | 可以做 | 不可以做 |
| --- | --- | --- |
| AgentLoop / Swarm | 规划、调查、调用已注册工具、辩论和提出实验 | 把未经验证的结果宣布为正式证据 |
| 审查网关 | 检查公式语义、时序、工具策略和实验意图 | 凭空补造缺失证据 |
| 执行沙箱 | 对挂载数据运行已批准的研究代码 | 访问网络或逃离声明的工作区 |
| 证据服务 | 计算正式指标、数据指纹、覆盖和证据状态 | 为迎合智能体而改写结论 |
| 报告编译器 | 把结论绑定到证据并披露限制 | 隐藏失败或阻塞的研究分支 |

这套分工保留了智能体系统的创造力，但不会把金融权力或证据裁决权交给语言模型。

## 六个专业研究组

| 研究组 | 核心问题 | 典型产出 |
| --- | --- | --- |
| **基本面研究组** | 公司赚了什么、拥有什么、欠了什么，又如何再投资？ | 盈利、质量、估值、资产负债表因子 |
| **预期与事件组** | 什么相对预期发生了变化，变化发生在何时？ | 预期修正、超预期、事件窗口、催化剂假设 |
| **市场结构组** | 流动性、拥挤度和交易机制如何影响信号？ | 换手、冲击、微观结构、可交易性诊断 |
| **另类数据组** | 非传统观测能否提供及时增量信息？ | 数据质量说明、特征提案、可得性边界 |
| **宏观与状态组** | 信号何时增强、减弱或反转？ | 状态标签、条件结果、宏观敏感性 |
| **Meta-Alpha 组** | 所谓优势是否稳健、独立并且可复现？ | 泄漏审查、消融、相关性、稳健性与证据审计 |

每个研究组都有独立身份和研究方法，但所有被接受的结论必须汇聚到同一份证据契约。

## 产品界面

| 模块 | 用途 |
| --- | --- |
| **研究工作台** | 提交问题、观察各组活动、检查审查决定并跟踪研究进度 |
| **结果与报告** | 先读直接答案，再深入查看证据、限制和实验产物 |
| **证据浏览器** | 查看正式、辅助、阻塞证据，以及数据指纹和回放元数据 |
| **因子库** | 比较公式语义、质量、血缘和历史实验结果 |
| **CLI 与 API** | 自动化研究、运行可复现演示并接入外部工作流 |

## 快速开始

### 1. 先运行确定性的离线演示

这一步不需要模型密钥或行情数据凭证。

~~~powershell
git clone https://github.com/xxxhhbb/AlphaForge.git
Set-Location AlphaForge

python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -e ".[dev]"

alphaforge demo --offline --seed 7
~~~

macOS 或 Linux 请使用 <code>source .venv/bin/activate</code> 激活环境。

演示会在 <code>agent/runs/</code> 下生成一场可复现研究。建议先看：

- <code>report/research_answer.md</code>：直接研究答案；
- <code>report/research_report.md</code>：完整研究报告；
- <code>evidence/</code> 与 <code>experiments/</code>：已接受证据和可回放实验产物。

### 2. 启动 Web 产品

~~~powershell
Set-Location frontend
npm ci
npm run build
Set-Location ..

alphaforge serve --host 127.0.0.1 --port 8000
~~~

打开 <http://127.0.0.1:8000>。默认只监听本机回环地址。

### 3. 连接模型服务并运行真实研究

~~~powershell
Copy-Item agent\.env.example agent\.env
# 按照 docs/alphaforge/QUICKSTART.md 填写模型服务与数据配置

alphaforge research "哪些质量因子在行业中性化后仍然稳健？" --label-horizon 20 --print-report
~~~

Docker、数据挂载、模型供应商配置和继承的完整运行栈，请阅读 [Docker 指南](docs/alphaforge/DOCKER.md) 与 [快速开始](docs/alphaforge/QUICKSTART.md)。

<details>
<summary><strong>常用 CLI 入口</strong></summary>

~~~text
alphaforge demo       # 确定性离线研究
alphaforge research   # 自主研究活动
alphaforge serve      # API + 已构建前端
alphaforge version    # 版本与构建信息
~~~

使用 <code>alphaforge &lt;command&gt; --help</code> 查看完整参数。可直接复用的研究问题位于 [examples/questions.md](examples/questions.md)，代表性输出位于 [examples/sample-report.md](examples/sample-report.md)。
</details>

## 项目结构

~~~text
AlphaForge/
├── agent/
│   ├── alphaforge/              # 研究内核、契约、证据与报告
│   ├── vibe_trading/            # 继承的智能体运行时与兼容层
│   ├── alphaforge_cli.py        # CLI 入口
│   └── alphaforge_api_server.py # 产品 API 与前端服务
├── frontend/                    # React 19 研究工作台
├── docs/alphaforge/             # 用户、架构、API、可信边界与前端规格
├── examples/                    # 示例问题与示例报告
├── agent/tests/                 # 产品、契约、Harness 与回归测试
├── Dockerfile
├── docker-compose.yml
└── pyproject.toml
~~~

## 文档地图

| 目标 | 从这里开始 |
| --- | --- |
| 安装与运行 | [快速开始](docs/alphaforge/QUICKSTART.md) · [Docker](docs/alphaforge/DOCKER.md) · [故障排查](docs/alphaforge/TROUBLESHOOTING.md) |
| 使用产品 | [用户指南](docs/alphaforge/USER_GUIDE.md) · [示例问题](examples/questions.md) · [示例报告](examples/sample-report.md) |
| 理解系统 | [系统架构](docs/alphaforge/ARCHITECTURE.md) · [API 参考](docs/alphaforge/API_REFERENCE.md) · [配置指南](docs/alphaforge/CONFIGURATION.md) |
| 判断可信度与边界 | [当前状态](docs/alphaforge/CURRENT_STATUS.md) · [安全与权限](docs/alphaforge/SECURITY_AND_AUTHORITY.md) · [公式质量](docs/alphaforge/FORMULA_QUALITY_AND_REVISION.md) |
| 理解研究答案 | [Research Answer V3](docs/alphaforge/RESEARCH_ANSWER_V3.md) · [Harness 报告](docs/alphaforge/RESEARCH_ANSWER_HARNESS_REPORT.md) |
| 让 AI 开发前端 | [前端产品需求](docs/alphaforge/FRONTEND_PRODUCT_REQUIREMENTS.md) · [前端 AI Prompt](docs/alphaforge/FRONTEND_AI_PROMPT.md) |
| 安全参与开发 | [贡献指南](CONTRIBUTING.md) · [AI Agent 贡献指南](AGENT_CONTRIBUTOR_GUIDE.md) |

## 开发与验证

~~~powershell
python -m pytest -q
python scripts/check_alphaforge_docs.py

Set-Location frontend
npm ci
npm run lint
npm run build
~~~

专项 Harness 覆盖研究答案正确性、产品集成、公式语义、证据状态、可回放性和文档链接。新增能力应同时提供一个代表性问题、一套明确验收标准和一个稳定失败模式。

## 当前成熟度

AlphaForge 是持续开发中的研究产品，不是生产交易平台。

- 确定性离线路径、CLI、API、已构建前端、报告链路和六组研究契约已经实现。
- 正式证据必须使用固定输入并通过受控证据链。
- 动态工具和外部数据源在完成数据冻结、时序对齐与复现验证前只能作为辅助信息。
- 实盘执行、券商账户、订单路由、组合授权和投资建议流程被明确排除在范围之外。

最新能力矩阵与已知缺口请查看 [当前状态](docs/alphaforge/CURRENT_STATUS.md)。

## 路线图

- 持续提高答案质量、报告层级和研究结果可视化。
- 扩展研究组专属工具，同时维护统一证据契约。
- 接入更丰富、严格时序对齐的数据集和供应商适配器。
- 加强跨因子比较、市场状态分析与实验回放。
- 用产品级 Harness 保持前端、API、CLI 与报告一致。

## 参与贡献

欢迎参与研究工作流、证据质量、专业研究组、数据适配器、公式语义、报告和前端体验的建设。提交修改前请阅读 [CONTRIBUTING.md](CONTRIBUTING.md)；AI 开发者还应遵循 [AGENT_CONTRIBUTOR_GUIDE.md](AGENT_CONTRIBUTOR_GUIDE.md)。

## 致谢

AlphaForge 构建并学习于开源量化研究与智能体生态，包括 [Vibe-Trading](https://github.com/HKUDS/Vibe-Trading)、[Qlib](https://github.com/microsoft/qlib) 和 [RD-Agent](https://github.com/microsoft/RD-Agent)。这里的列出仅表示架构与社区层面的致谢，不代表上述项目对 AlphaForge 的背书。

## 许可证与免责声明

本项目采用 [MIT License](LICENSE)。

本仓库仅用于研究与教育。系统输出可能不完整、过时、有偏或错误。AlphaForge 的任何内容都不构成投资建议、推荐、要约或交易指令。数据权利、模型服务条款、结果验证、风险控制和所有现实决策均由使用者自行负责。

<p align="center">
  <strong>AlphaForge · 千因研究院</strong><br />
  千种假设，一套证据，一个更清晰的答案。
</p>
