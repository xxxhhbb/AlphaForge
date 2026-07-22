# AlphaForge AI Contributor Guide

本指南面向 AI 编程代理和自动化协作者。它补充 `CONTRIBUTING.md`、`SECURITY.md` 与 `.github/pull_request_template.md`。

## 开始前

1. 阅读本文件和 `CONTRIBUTING.md`；
2. 阅读任务对应的 `docs/alphaforge/` 文档；
3. 检查当前工作区改动并保留用户内容；
4. 以实际代码和机器收据为事实，不把 README 声明当实现证据；
5. 选择最小纵向切片，写清非目标；
6. 未获授权不要访问真实 Provider、数据网络、账户、Broker 或部署。

前端任务必须完整阅读：

- `docs/alphaforge/FRONTEND_PRODUCT_REQUIREMENTS.md`
- `docs/alphaforge/API_REFERENCE.md`
- `docs/alphaforge/SECURITY_AND_AUTHORITY.md`
- `docs/alphaforge/CURRENT_STATUS.md`

可直接使用 `docs/alphaforge/FRONTEND_AI_PROMPT.md`。

## 仓库形状

- `agent/src/agent/`：ReAct AgentLoop 与上下文；
- `agent/src/tools/`：工具构建和权限；
- `agent/src/swarm/`：DAG/Swarm；
- `agent/src/veriquant/`：AlphaForge 可信研究内核；
- `agent/src/api/`：API 路由；
- `agent/alphaforge_api_server.py`：AlphaForge-only 回环入口；
- `frontend/`：React/Vite 产品；
- `docs/alphaforge/`：公开用户/协作者/AI 文档；
- `.autobuild/`：本地运行收据，不提交；
- `agent/.env`：真实配置，不读取到输出、不提交。

## 权限边界

Agent 研究自由不等于系统权限。任何实现都必须保持：

- LLM/Research Agent 只能提议 DRAFT 或辅助研究；
- 官方 Evidence 只由独立确定性服务生成；
- proposer 不能 approve 自己；
- sealed 信息不能进入下一轮研究上下文；
- generated Python 只在机械审查后的无网络 Research Sandbox；
- 前端不能写 Evidence、Promotion、组合、Risk、订单或 Ledger；
- 缺失/损坏/歧义 fail closed；
- 部分、负向和数据缺口是合法结果，不得改成伪成功。

## 默认允许的本地动作

- 只读搜索代码、文档和测试；
- 运行离线单元/集成测试；
- 运行 TypeScript/build；
- 启动 loopback-only AlphaForge 专用服务做本地 smoke；
- 修改任务范围内的代码、测试和文档；
- 读取脱敏 fixture 和公开机器收据；
- 使用 fake/deterministic provider。

## 需要明确授权的动作

- 真实模型或付费 API 调用；
- 真实数据联网采集；
- 启动公网服务或部署；
- 修改 CI Secrets/云资源；
- Broker/OAuth/钱包/支付/账户授权；
- 下单、撤单、平仓、改变 Mandate 或清除 Halt；
- 删除卷、运行产物、数据库或 Git 历史；
- force push、release、publish package；
- 放宽 Sandbox、PIT、Evidence、sealed 或 Risk 权限。

## 文件规则

- 编辑使用小型、可审查 patch；
- 不覆盖无关用户改动；
- 不使用 `git reset --hard`/`git checkout --` 恢复文件；
- 不删除 `.autobuild` 来“修复测试”；
- 不修改机器收据以迎合文档/UI；
- 不提交 `agent/.env`、runs、sessions、uploads、token cache、私有数据或报告；
- 不在代码/文档硬编码个人绝对路径；
- 依赖锁和大规模生成文件仅在任务需要时更新。

## 真实路径优先

对产品 bug：

1. 从用户问题追到计划；
2. 追到选组和 AgentLoop；
3. 追到工具/冻结数据；
4. 追到 review/runner/standard table；
5. 追到 evaluator/Evidence；
6. 追到 report/API/UI；
7. 修复影响问题家族的共享阻断；
8. 复跑同一 case 和相邻回归。

不要为具体问句添加关键字特判，也不要增加第二套运行时解决路由漂移。

## 状态诚实性

分别记录：

- execution status；
- evidence authority；
- research verdict；
- report status。

`report built` 不等于 `Alpha validated`。测试集合失败、未收集或环境不可用不能计入 passed。

## 前端专用规则

- V3 是新的唯一产品合同；
- 计划必须显式确认；
- UI 从 API 重建状态，不自创状态；
- 未选研究组不显示失败；
- official/auxiliary/none 文字化显示；
- Markdown 禁用 raw HTML；
- 不递归显示未知 payload；
- 生产 API 失败不回退成功 fixture；
- 360px、键盘、focus、aria 和 reduced motion 是验收项；
- 浏览器验收不得调用真实模型/数据/交易。

## 安全验证

高风险区域需要相应负向测试：

- 未声明工具拒绝；
- 路径穿越拒绝；
- network/shell/subprocess/env/dynamic import 拒绝；
- sealed/secret 不进入输出；
- auxiliary 不可 Promotion；
- Risk 缺失/陈旧/歧义拒绝；
- API 路由不出现 Broker/Order/OAuth/Mandate；
- 前端未知字段丢弃。

## 常用检查

```powershell
python scripts\check_alphaforge_docs.py
python -m compileall -q agent\src\veriquant agent\src\api
python -m pytest -q -p no:cacheprovider --confcutdir=agent/tests/veriquant agent/tests/veriquant/research_os_v3

Set-Location frontend
npm run test:run
npm run build
```

使用与改动最接近的窄集；全量过慢时说明没跑什么。

## PR 和最终汇报

每个 AI PR/最终汇报必须包含：

- 结果先行；
- 改动文件；
- 真实测试命令与精确结果；
- 浏览器/运行验收；
- 权限、网络、Secret、数据和交易影响；
- 非目标；
- 已知限制；
- 回滚方式。

不要只报告“已完成”“全部通过”，也不要把计划、静态扫描或 UI 截图当真实研究收据。

## 提交

社区提交需要 DCO：

```powershell
git commit -s -m "feat(frontend): improve AlphaForge report view"
```

不添加 AI `Co-Authored-By` trailer。
