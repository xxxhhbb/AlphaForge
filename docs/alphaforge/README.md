# AlphaForge 文档中心

这套文档面向四类读者：第一次运行产品的用户、研究/后端开发者、前端协作者，以及接手任务的 AI 编程代理。所有能力描述以当前代码和机器收据为准；规划能力会明确标记为“下一步”，不会与已实现能力混写。

## 推荐阅读顺序

### 产品使用者

1. [产品理念](PRODUCT_VISION.md)
2. [快速开始](QUICKSTART.md)
3. [用户指南](USER_GUIDE.md)
4. [当前状态](CURRENT_STATUS.md)
5. [故障排查](TROUBLESHOOTING.md)

### 后端与研究系统开发者

1. [系统架构](ARCHITECTURE.md)
2. [安全与权限](SECURITY_AND_AUTHORITY.md)
3. [API 参考](API_REFERENCE.md)
4. [配置指南](CONFIGURATION.md)
5. [开发与测试](DEVELOPMENT.md)
6. [路线图](ROADMAP.md)

### 前端开发者或前端 AI

1. [产品理念](PRODUCT_VISION.md)
2. [系统架构](ARCHITECTURE.md)
3. [API 参考](API_REFERENCE.md)
4. [前端产品需求](FRONTEND_PRODUCT_REQUIREMENTS.md)
5. [前端 AI Prompt](FRONTEND_AI_PROMPT.md)
6. [开发与测试](DEVELOPMENT.md)

### 项目维护者

1. [GitHub 协作](GITHUB_COLLABORATION.md)
2. [当前状态](CURRENT_STATUS.md)
3. [路线图](ROADMAP.md)
4. 根目录 [CONTRIBUTING.md](../../CONTRIBUTING.md)
5. 根目录 [AGENT_CONTRIBUTOR_GUIDE.md](../../AGENT_CONTRIBUTOR_GUIDE.md)

## 文档清单

| 文件 | 作用 |
| --- | --- |
| [PRODUCT_VISION.md](PRODUCT_VISION.md) | 为什么做 AlphaForge、目标用户、设计原则与非目标 |
| [ARCHITECTURE.md](ARCHITECTURE.md) | 控制流、数据流、状态流、权限流、组件证据与复用边界 |
| [QUICKSTART.md](QUICKSTART.md) | Windows/macOS/Linux 本地安装、首次启动和验证 |
| [DOCKER.md](DOCKER.md) | Docker Desktop/Engine、Compose、卷、安全、升级和排错 |
| [CONFIGURATION.md](CONFIGURATION.md) | LLM、数据源、环境变量、离线模式和密钥规则 |
| [USER_GUIDE.md](USER_GUIDE.md) | 从问题到报告的完整操作和结果解释 |
| [API_REFERENCE.md](API_REFERENCE.md) | Research OS V3 控制与只读报告端点 |
| [SECURITY_AND_AUTHORITY.md](SECURITY_AND_AUTHORITY.md) | Agent、Evidence、Promotion、风险、执行和前端权限边界 |
| [DEVELOPMENT.md](DEVELOPMENT.md) | 代码结构、环境、测试命令、变更验收与调试 |
| [GITHUB_COLLABORATION.md](GITHUB_COLLABORATION.md) | 新仓库初始化、推送、分支、Issue、PR 与保护规则 |
| [FRONTEND_PRODUCT_REQUIREMENTS.md](FRONTEND_PRODUCT_REQUIREMENTS.md) | 前端信息架构、交互、状态、视觉、测试和 DoD |
| [FRONTEND_AI_PROMPT.md](FRONTEND_AI_PROMPT.md) | 可直接复制给 AI 编程代理的完整前端任务 |
| [CURRENT_STATUS.md](CURRENT_STATUS.md) | 最新收据、测试口径、已知限制和不能声称的能力 |
| [ROADMAP.md](ROADMAP.md) | 从“跑通”到“研究质量与呈现质量”的分阶段路线图 |
| [TROUBLESHOOTING.md](TROUBLESHOOTING.md) | Python、Node、Docker、端口、API、数据和模型问题 |

## 命名规则

- 对外产品：`AlphaForge（千因研究院）`；
- 前端短名：`AlphaForge`；
- 内部确定性研究内核：`VeriQuant`；
- 上游兼容运行时：`Vibe-Trading`；
- Python 发布包与 CLI：当前仍为 `vibe-trading-ai` / `vibe-trading`，除非另有迁移计划；
- Research OS 公共产品路径：`Research OS V3`。

文档、页面标题、Issue 和 PR 应使用 AlphaForge。只有引用现有包、CLI、环境变量、目录或上游实现时才使用 Vibe/VeriQuant 名称。

## 文档维护规则

1. 用户可见行为变化必须更新根 README 和对应文档。
2. API 字段变化必须同时更新 `API_REFERENCE.md`、前端类型和合同测试。
3. 当前状态只引用带路径、哈希或测试命令的收据。
4. 不把“页面能展示”“报告已生成”写成“Alpha 已验证”。
5. 辅助研究数字必须显式标注，不得用颜色或文案暗示其为官方 Evidence。
6. 不在文档中放真实密钥、私有数据路径、账户信息或未脱敏报告。
