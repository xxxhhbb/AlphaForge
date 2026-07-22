# Contributing to AlphaForge

AlphaForge（千因研究院）是构建在 Vibe-Trading 运行时之上的可信多专业 Alpha 研究操作系统。欢迎前端、研究内核、数据、评估、报告、文档和测试贡献。

提交前请先读：

- [项目 README](README.md)
- [AlphaForge 文档中心](docs/alphaforge/README.md)
- [架构](docs/alphaforge/ARCHITECTURE.md)
- [安全与权限](docs/alphaforge/SECURITY_AND_AUTHORITY.md)
- [当前状态](docs/alphaforge/CURRENT_STATUS.md)
- AI/自动化贡献还要读 [AGENT_CONTRIBUTOR_GUIDE.md](AGENT_CONTRIBUTOR_GUIDE.md)

## 产品命名

- 对外产品统一使用 `AlphaForge（千因研究院）`；
- `VeriQuant` 是内部确定性内核；
- `Vibe-Trading` 是上游与兼容包/CLI 名；
- 不要为了品牌一次性重命名 Python 包、环境变量和所有 import，这需要独立迁移计划。

## 开发环境

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -e ".[dev]"

Set-Location frontend
npm ci
npm run build
```

完整说明见 [开发与测试](docs/alphaforge/DEVELOPMENT.md)。

## 工作流

1. 先创建或认领一个边界清晰的 Issue；
2. 从 `main` 创建短生命周期分支；
3. 只修改实现该纵向切片所需文件；
4. 添加正向和负向测试；
5. 更新用户/AI 文档；
6. 运行最窄相关测试和必要回归；
7. 使用 DCO sign-off 提交；
8. 打开 PR，说明目标、范围、风险、测试、截图和回滚。

```powershell
git switch -c feat/frontend-research-flow
git commit -s -m "feat(frontend): add explicit plan confirmation"
git push -u origin feat/frontend-research-flow
```

## DCO

所有社区提交必须包含 `Signed-off-by:`：

```powershell
git commit -s -m "fix(report): preserve auxiliary evidence label"
```

这表示你有权按项目许可证提交该贡献。不要添加 AI 助手的 `Co-Authored-By` 或其他自动署名 trailer；DCO 是所需 trailer。

## 代码风格

### Python

- Python 3.11+；
- 公共函数/方法添加类型；
- Google-style docstring；
- Ruff 配置见 `pyproject.toml`；
- 避免硬编码路径、密钥和 Provider；
- 文件尽量低于 400 行，800 行硬上限；
- 删除废弃代码，不长期注释。

### TypeScript/React

- TypeScript strict 合同和运行时响应校验；
- 函数组件与清晰数据所有权；
- 后端是状态事实源；
- 可访问语义、键盘和响应式是验收项；
- 不渲染未知 JSON，不使用 `dangerouslySetInnerHTML`；
- API 失败不能回退为伪成功 fixture；
- 视觉遵循 [前端产品需求](docs/alphaforge/FRONTEND_PRODUCT_REQUIREMENTS.md)。

## 研究与金融权限

Research Agent 可以规划、调查、写 DRAFT 实验和解释公开结果。它不能计算官方指标、批准自己、读取 sealed、绕过 Risk、下单或修改账本。

涉及以下区域的 PR 属于高风险：

- PIT/availability；
- Research Sandbox；
- Evidence/Promotion；
- Tool 白名单；
- Risk/portfolio/execution/ledger；
- Broker/OAuth/Secret；
- API 写权限；
- sealed 投影。

高风险 PR 必须有失败关闭测试、权限说明和回滚方案。常规 PR 验证不得访问真实资金或下单。

## 因子贡献

修改 `agent/src/factors/zoo/**/*.py` 时至少验证：

```powershell
pytest agent/tests/factors/test_alpha_purity.py agent/tests/factors/test_lookahead.py -q
```

要求：

- `__alpha_meta__` 满足 registry 合同；
- `compute(panel)` 保持形状、warmup NaN 和有限输出；
- 无负 shift/未来泄漏；
- 只使用 allowlist import；
- LaTeX 与实现一致；
- 更新相应 `LICENSE.md`/NOTICE；
- 适用的 Apache-2.0 来源保留 commit/path/版权归属。

## 测试

通用后端：

```powershell
pytest --ignore=agent/tests/e2e_backtest --ignore=agent/tests/test_e2e_harness_v2.py --tb=short -q
```

AlphaForge V3 窄集：

```powershell
python -m pytest -q -p no:cacheprovider --confcutdir=agent/tests/veriquant `
  agent/tests/veriquant/research_os_v3 `
  agent/tests/veriquant/agent_research_product
```

前端：

```powershell
Set-Location frontend
npm run test:run
npm run build
```

文档：

```powershell
python scripts\check_alphaforge_docs.py
```

报告精确结果，区分 passed、failed、skipped、not run 和 environment unavailable。

## PR 必填内容

- Summary：用户/系统结果；
- Why：问题与证据；
- Scope / Non-goals；
- Architecture/authority impact；
- Tests：精确命令和结果；
- UI：桌面/移动截图；
- Data/model/network impact；
- Security：secret/sealed/sandbox/trading；
- Rollback；
- Known limitations。

## 文档

- 用户可见行为更新根 README 和 `docs/alphaforge/`；
- API 变化更新 `API_REFERENCE.md`、前端类型和合同测试；
- 前端变化更新需求或当前状态；
- 已实现与路线图分开；
- 不提交 `.autobuild`、真实报告数据或密钥。

## 上游与许可

保留 `LICENSE`、`NOTICE`、因子目录许可和上游归属。产品使用 AlphaForge 品牌不改变 HKUDS/Vibe-Trading 及其他来源的版权义务。
