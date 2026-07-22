# 可直接交给前端 AI 的任务 Prompt

把下面整段复制给 AI 编程代理。它已经把范围、事实源、实现顺序和验收写死，避免 Agent 把时间花在重建后端或做一套新 UI。

---

你现在负责完善 `AlphaForge（千因研究院）` 的前端产品。仓库根目录是：

```text
<repository-root>
```

目标不是重写前端，而是在现有 React 19 + TypeScript + Vite 代码上，把 Research OS V3 收敛成一条清晰、可信、可验收的用户路径：

```text
输入 Alpha 问题 → 查看研究计划 → 用户显式确认 → 运行研究 → 查看各专业组 → 阅读结论 → 展开 Evidence
```

## 一、开始前必须完整阅读

按顺序读：

1. `README.md`
2. `docs/alphaforge/PRODUCT_VISION.md`
3. `docs/alphaforge/ARCHITECTURE.md`
4. `docs/alphaforge/API_REFERENCE.md`
5. `docs/alphaforge/SECURITY_AND_AUTHORITY.md`
6. `docs/alphaforge/FRONTEND_PRODUCT_REQUIREMENTS.md`
7. `docs/alphaforge/CURRENT_STATUS.md`
8. `CONTRIBUTING.md`
9. `AGENT_CONTRIBUTOR_GUIDE.md`

然后只读检查当前工作区改动，保存用户已有修改。不要运行 `git reset --hard`、`git checkout --` 或删除本地运行产物。

## 二、代码事实源

优先复用：

- `frontend/src/router.tsx`
- `frontend/src/pages/ResearchOSV3LaunchPage.tsx`
- `frontend/src/pages/ResearchOSV3ResultPage.tsx`
- `frontend/src/pages/FormalReportPage.tsx`
- `frontend/src/pages/Reports.tsx`
- `frontend/src/pages/Evidence.tsx`
- `frontend/src/pages/AlphaZoo.tsx`
- `frontend/src/pages/SystemStatus.tsx`
- `frontend/src/features/research-os-v3-launch/`
- `frontend/src/features/research-os-v3/`
- `frontend/src/components/layout/`
- `frontend/src/components/veriquant/`
- `frontend/src/styles/`
- `agent/src/api/research_os_v3_routes.py`
- `agent/alphaforge_api_server.py`

Research OS V3 是唯一新的产品合同。`frontend/src/features/alphaforge/` 和旧 Campaign 页面只能作为兼容参考，不得在生产 API 失败时回退成伪成功 fixture。

## 三、产品命名

- 对外统一：`AlphaForge（千因研究院）`；
- 简称：`AlphaForge`；
- `VeriQuant` 只作为内部可信研究内核出现在高级系统状态；
- `Vibe-Trading` 只作为上游/兼容实现名，不得出现在产品主品牌和主导航。

## 四、必须实现

### 1. 统一导航和路由

主导航只保留：研究、报告、Evidence、因子中心、系统。

主路由：

- `/`、`/research`
- `/research/results/:runId`
- `/reports`
- `/reports/:runId`
- `/evidence`
- `/factors`
- `/system`

Workbench 保留为高级详情入口。旧路由安全重定向，不在主导航展示。

### 2. 拆开计划、确认、启动

当前 `ResearchOSV3LaunchPage` 会 create plan 后自动 confirm/start。改为：

1. 输入问题并创建计划；
2. 展示原问题、研究目标、选中研究组、每组计划、数据范围/限制、预算、检查点和停止条件；
3. 用户可以返回修改；
4. 用户显式点击“确认并开始研究”；
5. 防止 confirm/start 双提交；
6. 真实数据准备必须是独立、显式的联网动作。

### 3. 运行页

将 run ID 放进 URL。展示：

- 研究问题；
- 总状态和当前阶段；
- 选中的专业组（只显示选中组）；
- 每组状态、候选数和消息；
- 已用/总预算；
- typed failures；
- 取消；
- 合法条件下恢复；
- 终态后的结果摘要和报告入口。

轮询必须 Abortable，页面隐藏降频，终态停止。4xx 不无限重试，网络/5xx 只做有界重试。不要虚构百分比。

### 4. 正式报告

删除或替换逐行手写 Markdown 渲染。使用项目已有：

```text
react-markdown + remark-gfm
```

禁止 raw HTML/脚本。正确展示标题、列表、表格、引用和 code。表格在手机上横向滚动，不造成整页横向滚动。

报告先显示一句话结论、研究处置、outcome、Evidence 等级、关键限制，再显示全文。不要从 Markdown 文字推断 Evidence；使用结构化 API 字段。

### 5. Evidence 权威统一

全站统一三类：

- 官方 Evidence；
- 辅助研究；
- 未形成 Evidence。

文字、图标和颜色同时表达。辅助研究不能使用“已验证”“已晋级”等成功文案。sealed 只显示允许状态，不展示指标、排名、路径或具体封闭原因。

### 6. 系统状态修正

修正过时文案：系统允许构建宪法 A-02 下的受限 generated research Python，但只能经机械审查进入无网络 Research Sandbox；仍禁止主机任意 Python。区分 certified Qlib expression、legacy FactorDAG 和 Research Sandbox。

健康状态来自 API，不要把静态卡片全部写成 `COMPLETED`。

### 7. 响应式和可访问性

- 360px、768px、1440px；
- 主流程全键盘可用；
- skip link、`main`、唯一 H1、表单 label/error；
- 清晰 focus；
- 状态更新适度 `aria-live`；
- modal 焦点管理；
- `prefers-reduced-motion`；
- 颜色不是唯一状态信号。

## 五、视觉方向

专业、克制、答案优先的研究工作台。使用白、浅暖灰、近黑、Apple 蓝和少量状态色。液态玻璃只用于导航、状态和操作面板；正文、报告和表格保持清晰大留白。

不要做：霓虹交易大屏、K 线主视觉、每张卡片都玻璃、夸张渐变、无意义动画、虚假实时百分比。

优先复用现有 CSS/token/component；不要引入大型 UI 框架。

## 六、安全硬约束

- 前端只读公开字段，未知字段丢弃；
- 不渲染 sealed、secret、broker、order、ledger、position 等未知嵌套内容；
- 不使用 `dangerouslySetInnerHTML`；
- 不把密钥放进 `VITE_*`；
- 不打印完整 API payload、Prompt、代码或数据；
- 不新增交易、账户、Broker、OAuth、Mandate 页面；
- 不修改 Evidence、Promotion、风险、组合、订单或账本；
- 不在后端不可用时展示成功 fixture；
- PARTIAL/BLOCKED/FAILED/CANCELLED 必须原样保留。

## 七、按阶段实施

一次只推进一个可审查阶段，并在每阶段后跑相关测试：

1. F0：记录现有测试/build 基线，检查类型和路由；
2. F1：品牌、导航、路由和全局布局；
3. F2：问题、计划预览、显式确认和数据准备；
4. F3：运行页、研究组、预算、取消/恢复和轮询；
5. F4：结果摘要、正式 GFM 报告和报告中心；
6. F5：Evidence、系统状态、旧路径收敛；
7. F6：响应式、可访问性、错误状态和完整验收。

不要先做大规模 CSS 重写。先让合同和用户流程正确，再统一视觉。

## 八、必须新增/更新测试

至少覆盖：

- 计划生成后不会自动确认；
- 返回修改保留问题；
- confirm/start 只发生一次；
- 显式 prepare-data；
- 完成、部分、阻断、失败、取消；
- 只显示选中研究组；
- 一组失败、其他完成时是部分结果；
- 取消/恢复；
- 终态停止轮询、旧请求 abort；
- API 非 JSON、缺字段、未知状态、负预算、used > total；
- GFM 表格、列表、code；
- raw HTML 不执行；
- 官方/辅助/无 Evidence；
- sealed 不泄漏；
- 360px 没有整页横向滚动；
- 键盘和基本 aria；
- `frontend/vite.config.ts` 包含 `/api/veriquant` proxy。

优先使用现有 client dependency injection 和 Testing Library，不为测试引入真实 Provider、网络或数据。

## 九、验证命令

在 PowerShell 7：

```powershell
Set-Location "C:\path\to\AlphaForge\frontend"
npm ci
npm run test:run
npm run build
```

如果修改/依赖 V3 合同，再跑：

```powershell
Set-Location "C:\path\to\AlphaForge"
python -m pytest -q -p no:cacheprovider --confcutdir=agent/tests/veriquant `
  agent/tests/veriquant/research_os_v3/test_u6_api.py `
  agent/tests/veriquant/research_os_v3/test_u6_control.py
```

文档：

```powershell
python scripts\check_alphaforge_docs.py
```

如果环境缺 Node/依赖，明确报告 `NOT_RUN` 和原因，不能把 TypeScript 静态阅读写成 build 通过。

## 十、浏览器验收

启动：

```powershell
# 终端 A
python agent\alphaforge_api_server.py --host 127.0.0.1 --port 8000

# 终端 B
Set-Location frontend
$env:VITE_VERIQUANT_API_URL='http://127.0.0.1:8000'
npm run dev
```

逐页检查 `/`、运行结果、报告、Evidence、因子中心、系统；截图桌面和 360px 手机。不得调用真实模型、真实数据网络或交易接口做前端验收。

## 十一、完成标准

严格按照 `docs/alphaforge/FRONTEND_PRODUCT_REQUIREMENTS.md` 的 Definition of Done。特别是：显式确认、V3 单一合同、合法负向/部分/缺口页面、安全 GFM、Evidence 等级一致、360px、键盘、测试和 build。

## 十二、最终汇报格式

最终回答必须包含：

1. 用户现在能完成什么完整流程；
2. 关键修改文件；
3. 测试命令和精确结果；
4. 浏览器验收页面和截图路径；
5. 没有做什么；
6. 仍需后端/数据支持的缺口；
7. 是否涉及权限、网络、密钥或交易面（正常应为否）。

不要只说“前端已完成”。用可复核的文件、测试和行为说明。

---
