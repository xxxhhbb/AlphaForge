# AlphaForge 当前状态

更新日期：2026-07-22（Asia/Shanghai）

## 结论

AlphaForge 当前可以作为“有界、多专业、可恢复、可出中文报告”的研究产品使用；不能声称任意 Alpha 问题都会形成官方可交易结论。最新总门禁为 `QUALIFIED_FOR_RESEARCH_PRODUCT_WITH_AUXILIARY_LIMITATION`。

## 最新产品收敛收据

收据：`.autobuild/veriquant/product_stabilization_harness/final_product_v1/final_verdict.json`

| 门禁 | 结果 |
| --- | --- |
| 静态问题目录 | 500/500 可解析、可路由；未声称真实逐题执行 |
| 代表研究家族 | 25 个通过 |
| 六专业组资格 | 通过；官方路径 5 组，辅助上限 1 组，无阻断组 |
| 动态工具数据 | 通过但状态 `PARTIAL`，权威上限 `AUXILIARY_ONLY` |
| 安全 | 动态执行、禁用路由和 secret 扫描通过 |
| 测试口径 | 产品脊柱 192 + 最终化 2，共 194 项通过 |
| 恢复游标 | `COMPLETE` |

动态数据限制：当前真实新闻/研报工具结果晚于冻结历史 Evidence 窗口，36 份文档已冻结为辅助数据，不能回填为历史官方特征。

## 最新三组专业报告

产物：`.autobuild/veriquant/professional_report_v2_run/78b8874b2b3708bd/research_runs/agent_product_canary_0dd4344d7c87bfc4/report.md`

已完成研究组：

- 基本面研究组；
- 市场行为与交易可行性研究组；
- 组合增量研究组。

三组均完成：计划、只读数据调查、研究草案、机械代码审查、无网络隔离运行、标准回归表、独立一致性检查和面向用户的中文报告。

报告发现换手率、ROE、ROA、Shibor 等统计线索，但所有三组实验均为 `AUXILIARY_ONLY`。当前没有完成：

- 明确每日股票评分；
- 滚动样本外 RankIC/ICIR；
- 交易成本、价差、冲击和容量；
- 与 50 日动量的直接相关和残差增量；
- 高低 Shibor 状态的独立表现表；
- 独立 sealed Promotion。

因此正确处置是“进入下一轮验证”，不是“可以直接交易”。

## 已实现能力

- Vibe ReAct AgentLoop、ToolRegistry、Provider/Trace 复用；
- Swarm/DAG 研究组调度、隔离上下文和失败阻断；
- 研究计划、确认、启动、状态、取消、恢复和报告 API；
- 六专业组路由和资格收据；
- 冻结/PIT 数据身份与代理数据路径；
- 认证 Qlib 表达式与旧 FactorDAG 兼容路径；
- generated Python 机械审查和 Research Sandbox；
- 六类标准研究表与相应评价方向；
- 官方/辅助/无证据等级分离；
- 面向用户的中文报告和 Workbench 投影；
- React/Vite 前端的研究启动、结果、报告、Evidence、因子和状态页面；
- AlphaForge 专用回环服务器，不注册交易权限路由；
- 默认 Docker 一体化构建和持久卷。

## 部分实现或仍需收敛

### 前端

- V3 启动页当前创建计划后自动确认，应改成清晰的显式确认步骤；
- V3、旧 Campaign 和 Workbench 存在多套客户端/页面语义，应以 V3 为唯一主流程；
- 手写 Markdown 解析器不完整，已有 `react-markdown`/`remark-gfm` 应用于正式报告；
- Vite 开发代理未显式包含 `/api/veriquant/...`；
- System Status 仍包含“只允许 FactorDAG、禁止任意研究 Python”的过时描述；
- Evidence/辅助研究的视觉区分还需在所有页面统一；
- 页面需要更强的响应式、键盘、错误、取消/恢复和空状态验收。

### 报告与研究质量

- 报告已从 artifact dump 变为 answer-first，但跨方法比较、反对证据、稳健性和下一实验仍需结构化；
- 最新三组回归只是研究线索，未形成成本后 Alpha 验证；
- 各研究组需要更多 native data/native method 的真实代表任务；
- 新闻/研报与历史 Evidence 窗口的时间对齐仍是辅助限制。

### 状态与部署

- `.autobuild/veriquant/state.json` 的 `current_prompt` 仍为旧 `IN_PROGRESS`，与更晚的 `final_product_v1` 收据不一致；
- `.git` 目录为空，当前不是有效 Git 仓库；
- Docker 默认启动完整 Vibe 兼容服务器，不是 AlphaForge-only 权限面；
- Node/npm 不在当前系统 PATH，但项目已有锁文件和 Docker Node 构建阶段。

## 不能声称的能力

- 500 个问题全部真实运行通过；
- 六组在所有问题上都有官方 Evidence；
- 最新三组报告已经验证成本后可交易收益；
- 单个 p 值、RankIC 或报告结论可以直接晋级；
- Qlib long-only 回测完成了市场中性组合、风险、执行和账本链；
- 前端显示的状态可以替代后台收据；
- 默认 Docker 服务只包含研究 API；
- 系统已授权真实交易。

## 状态证据优先级

出现冲突时按以下顺序判断：

1. 更晚、同一真实产品路径生成的最终 Harness 收据；
2. 运行级 manifest、review、runner、standard table、Evidence 和 report receipt；
3. 可复现的测试命令和测试结果；
4. `.autobuild/veriquant/state.json` 恢复快照；
5. 文档和 UI 文案。

文档和页面永远不能覆盖机器收据。
