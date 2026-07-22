# AlphaForge GitHub 上传与协作指南

## 当前本地情况

项目已经使用 Git 管理。向新的 GitHub 仓库首次发布时，如果远端已经包含 `README` 或 `LICENSE`，必须先抓取并保留远端历史，禁止使用 force push 覆盖。

推荐仓库名：`AlphaForge`。

## 1. 决定公开还是私有

首次协作建议先创建 **Private** 仓库，完成秘密扫描、许可证/上游归属、运行产物检查和 CI 后再考虑公开。

GitHub 官方创建仓库文档：<https://docs.github.com/en/repositories/creating-and-managing-repositories/creating-a-new-repository>

创建空仓库时不要让 GitHub 自动生成 README、`.gitignore` 或 License，避免和本地文件产生初始冲突。

## 2. 上传前必须检查

### 忽略规则

以下内容不得提交：

- `agent/.env` 和任何真实 `.env`；
- `.autobuild/` 机器产物和可能含本地研究数据的收据；
- `agent/runs/`、`agent/sessions/`、uploads、memory、token cache；
- `node_modules/`、`.pnpm-store/`、`.venv/`；
- Broker/账户导出、私有报告、截图中的密钥；
- `.codex/` 本地应用状态。

AlphaForge 公共文档 `docs/alphaforge/**` 是有意放行的例外。

### 秘密扫描

在初始化或提交前运行：

```powershell
rg -n --hidden -g '!agent/.env' -g '!.autobuild/**' -g '!node_modules/**' `
  -g '!.pnpm-store/**' -g '!.git/**' `
  '(sk-[A-Za-z0-9_-]{12,}|BEGIN (RSA|OPENSSH|EC) PRIVATE KEY|api[_-]?key\s*[:=]\s*[^ <]+|token\s*[:=]\s*[^ <]+)'
```

示例文件中的明显占位符可保留；任何可能真实的值都应先轮换，再清理文件和历史。

### 大文件

```powershell
Get-ChildItem -Recurse -File | Where-Object { $_.Length -gt 50MB } | `
  Select-Object Length,FullName
```

不要把模型、行情数据库、Docker 导出、日志、PDF 批量产物或大视频放进普通 Git。真正需要版本管理的大二进制再评估 Git LFS。

## 3. 初始化本地仓库

确认当前目录精确无误：

```powershell
Set-Location "C:\path\to\AlphaForge"
Get-Location
```

初始化：

```powershell
git init -b main
git config user.name "YOUR_NAME"
git config user.email "YOUR_GITHUB_EMAIL"
git status --short
```

先检查忽略结果：

```powershell
git check-ignore -v agent/.env
git check-ignore -v .autobuild/veriquant/state.json
git check-ignore -v docs/alphaforge/README.md
```

前两项应被忽略；最后一项不应被忽略（命令对它应无输出）。

## 4. 首次提交

```powershell
git add .
git status --short
git diff --cached --stat
git diff --cached -- . ':!requirements-lock.txt' ':!frontend/package-lock.json' ':!frontend/pnpm-lock.yaml'
```

人工检查 staged 文件，特别是 `.env`、`.autobuild`、日志、数据和个人路径。确认后：

```powershell
git commit -s -m "chore: initialize AlphaForge research OS"
```

不要把 AI 助手加入 `Co-Authored-By`；项目使用 DCO `Signed-off-by`。

## 5. 连接 GitHub

HTTPS：

```powershell
git remote add origin https://github.com/<YOUR_ORG>/alphaforge-research-os.git
git remote -v
git push -u origin main
```

SSH：

```powershell
git remote add origin git@github.com:<YOUR_ORG>/alphaforge-research-os.git
git remote -v
git push -u origin main
```

GitHub SSH 官方文档：<https://docs.github.com/en/authentication/connecting-to-github-with-ssh>

本指南不代替登录授权。不要把 Personal Access Token 写进 remote URL、脚本或文档。

## 6. 添加协作者

在 GitHub 仓库：

1. Settings → Collaborators and teams；
2. 为维护者、前端开发者、研究系统开发者分配最小权限；
3. 开启 Discussions（可选）用于产品讨论；
4. 使用 Issues 管任务，PR 做代码审查；
5. 不通过共享个人账号协作。

建议角色：

| 角色 | 权限/职责 |
| --- | --- |
| Maintainer | 合并、发布、权限和安全边界 |
| Frontend | `frontend/**`、前端文档和只读合同 |
| Research kernel | `agent/src/veriquant/**`、Evidence/PIT/Sandbox |
| Runtime | AgentLoop、Swarm、Provider、Docker |
| Reviewer | 合同、安全、报告和测试审查 |

## 7. 分支与 PR

建议：

```text
main                       可演示、CI 通过
feat/frontend-research-flow
fix/report-evidence-labels
docs/docker-onboarding
```

工作流：

```powershell
git switch -c feat/frontend-research-flow
# 修改与测试
git add frontend docs/alphaforge
git commit -s -m "feat(frontend): converge research plan flow"
git push -u origin feat/frontend-research-flow
```

在 GitHub 开 PR，不直接 push `main`。

## 8. Branch Protection 建议

为 `main` 设置：

- 必须通过 PR；
- 至少 1 位审批者，高风险模块建议 2 位；
- 新提交后撤销旧审批；
- 必须通过 CI；
- 禁止 force push 和删除分支；
- 要求 conversation resolved；
- 可选要求 signed commits；
- 对外部贡献检查 DCO。

初始 required checks 建议：

- Python tests；
- Frontend build；
- Frontend tests；
- AlphaForge docs check；
- secret/env gate。

## 9. Issue 拆分建议（前端）

不要创建一个“做完所有前端”的无限 Issue。按可验收纵切拆分：

1. 统一品牌和导航；
2. 计划预览与显式确认；
3. 运行轮询、六组状态、取消与恢复；
4. 正式报告 GFM 渲染；
5. Evidence 权威标签和候选漏斗；
6. 响应式与可访问性；
7. 错误/损坏合同 fail closed；
8. Vite proxy、测试和 E2E。

每个 Issue 写输入、输出、非目标、测试和截图要求。

## 10. 上游归属与许可证

AlphaForge 建立在 HKUDS/Vibe-Trading 上。公开仓库必须保留：

- `LICENSE`；
- `NOTICE`；
- 因子 zoo 下的许可证与 NOTICE；
- README 中的上游说明；
- 适用的 Apache-2.0 归属头。

不要删除上游版权，也不要把现有上游代码说成完全从零原创。产品品牌和新增实现可以使用 AlphaForge 名称。

## 11. CI 与 Secrets

- 第一次 push 后查看 Actions；
- Fork PR 默认不得获得生产 secrets；
- 真实 LLM、数据或部署 smoke 放在手动、受保护环境；
- 常规 PR 测试使用 fake/offline provider；
- 不在 CI 日志打印环境变量；
- Wiki deploy 仍指向上游 Cloudflare project，除非明确迁移，否则不要配置对应 secret 或触发发布。

## 12. 发布前清单

- [ ] README 使用 AlphaForge 名称并保留上游归属；
- [ ] `docs/alphaforge/**` 已提交；
- [ ] `agent/.env`、`.autobuild/`、runs/sessions 未 staged；
- [ ] secret 扫描无真实命中；
- [ ] 大文件已审查；
- [ ] `python scripts/check_alphaforge_docs.py` 通过；
- [ ] 前端 build/test 通过；
- [ ] 后端相关窄集通过；
- [ ] GitHub Actions 通过；
- [ ] Branch Protection 开启；
- [ ] 协作者权限最小化；
- [ ] 当前限制没有被 README 隐藏。
