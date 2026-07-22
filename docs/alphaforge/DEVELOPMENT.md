# AlphaForge 开发与测试

## 开发原则

1. 先找到一条真实产品路径，再修改共享阻断；
2. 不为单个问题写硬编码 `if/else`；
3. 研究 Agent 输出保持 DRAFT，确定性服务保留权威；
4. UI 是状态投影，不是第二套状态机；
5. 测试验证不变量，不只验证 happy path；
6. 保留用户现有修改，不使用破坏性 Git 命令；
7. 任何“通过”必须对应真实命令和收据。

## 代码结构

```text
agent/
  alphaforge_api_server.py
  api_server.py
  src/agent/
  src/swarm/
  src/tools/
  src/veriquant/
  tests/
frontend/
  src/pages/
  src/features/research-os-v3-launch/
  src/features/research-os-v3/
  src/components/
scripts/
docs/alphaforge/
```

## Python 环境

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -e ".[dev]"
```

依赖源为 `pyproject.toml`；容器的可复现运行依赖为 `requirements-lock.txt`。修改依赖时必须按锁文件头部流程重建 hash lock，并验证 Docker build。

## 前端环境

```powershell
Set-Location frontend
npm ci
npm run dev
```

技术栈：React 19、TypeScript、Vite、React Router、Zustand、ECharts、React Markdown、Vitest 和 Testing Library。

## 常用窄集测试

### Research OS V3 API/控制

```powershell
python -m pytest -q -p no:cacheprovider --confcutdir=agent/tests/veriquant `
  agent/tests/veriquant/research_os_v3/test_u6_api.py `
  agent/tests/veriquant/research_os_v3/test_u6_control.py
```

### Research Agent 产品

```powershell
python -m pytest -q -p no:cacheprovider --confcutdir=agent/tests/veriquant `
  agent/tests/veriquant/agent_research_product
```

### Research Sandbox

```powershell
python -m pytest -q -p no:cacheprovider --confcutdir=agent/tests/veriquant `
  agent/tests/veriquant/agent_research_sandbox
```

没有固定 Docker 镜像时，真实 Docker smoke 必须验证 typed unavailable，不得 skip 后宣称通过。

### Workbench/API

```powershell
python -m pytest -q `
  agent/tests/api/parallel_veriquant_workbench/test_workbench_routes.py `
  agent/tests/api/parallel_veriquant_workbench/test_workbench_frontend_static.py
```

### 前端

```powershell
Set-Location frontend
npm run test:run
npm run build
```

前端主流程至少覆盖：计划、显式确认、运行轮询、部分/阻断/失败、取消、恢复、报告、合同损坏和辅助/官方 Evidence 显示。

## 较大回归

```powershell
python -m pytest --ignore=agent/tests/e2e_backtest `
  --ignore=agent/tests/test_e2e_harness_v2.py --tb=short -q
```

该命令可能较慢。报告结果时区分通过、失败、跳过、未收集和环境不可用。

## 静态检查

```powershell
python -m compileall -q agent\src\veriquant agent\src\api
python -m py_compile agent\alphaforge_api_server.py
python scripts\check_alphaforge_docs.py

Set-Location frontend
npx tsc -b --pretty false
```

## 前端变更验收

每个前端 PR 应说明：

- 改变了哪个用户决策；
- 使用了哪个 API 合同；
- 加载、空、运行、部分、阻断、失败和取消状态；
- Evidence 权威标签如何保持；
- 键盘和窄屏行为；
- 是否增加未知字段或 secret/sealed 泄漏风险；
- 测试命令和截图。

不得：

- 在组件中直接拼接本地 artifact 路径；
- 用 Markdown 文本推断官方 Evidence；
- 递归渲染未知 JSON；
- 在 API 失败时自动展示成功 fixture；
- 为了 UI 好看把 PARTIAL/BLOCKED 变成 COMPLETED。

## 后端变更验收

每个实现 wave 写清：输入、输出、持久状态、失败行为、测试和非目标。

涉及 Research OS 的共享变更至少验证：

- 计划保留 subject/horizon/universe；
- DAG 循环、缺失上游和失败上游 fail closed；
- 每组独立工具视图和 artifact namespace；
- 取消与恢复幂等；
- Trial 失败也消耗预算；
- 产物路径和 run ID 防穿越；
- official/auxiliary/sealed 权威不混淆；
- 报告数字绑定机器证据。

## 文档变更验收

```powershell
python scripts\check_alphaforge_docs.py
```

并人工确认：

- 所有命令使用真实入口和端口；
- 新文档未被 `.gitignore` 忽略；
- 没有真实秘密、私有运行数据或绝对用户路径；
- 已实现与路线图清楚分开；
- 产品名为 AlphaForge，Vibe/VeriQuant 只用于正确的内部/上游上下文。

## CI

`.github/workflows/test.yml` 当前安装 Python 3.11、运行环境变量 Gate、Python 测试、前端 build 和 Vitest。AlphaForge 文档检查也应作为 CI 步骤运行。

建议为前端收敛增加独立 job：

- 只在 `frontend/**`、V3 API 合同和前端文档变化时运行；
- `npm ci`；
- `npm run test:run`；
- `npm run build`；
- 上传失败截图/测试结果，不上传秘密；
- 后续增加 Playwright 的 loopback-only smoke。

## 提交约定

```text
feat(frontend): converge AlphaForge research flow
fix(api): preserve partial research status
test(evidence): reject auxiliary promotion
docs(alphaforge): add Docker sandbox limitations
```

社区提交使用 DCO：

```powershell
git commit -s -m "feat(frontend): add explicit plan confirmation"
```

## 变更风险分层

| 风险 | 示例 | 最低验证 |
| --- | --- | --- |
| 低 | 文案、公开文档 | 链接检查、人工预览 |
| 中 | 前端组件、只读 API 投影 | Vitest、build、后端合同窄集 |
| 高 | Tool 权限、PIT、Sandbox、Evidence、Promotion | 正负测试、相关回归、安全扫描、回滚 |
| 极高 | Broker、订单、认证、Secret、部署公网 | 维护者批准、隔离环境、明确授权；不作为常规 PR 验证 |
