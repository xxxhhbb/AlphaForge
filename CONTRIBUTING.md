# Contributing to AlphaForge

感谢你帮助改进 AlphaForge。提交前请先搜索现有 Issue，确保问题尚未被报告或正在处理。

## 提交 Issue

请说明：

- 你希望完成的研究任务；
- 实际结果与预期结果；
- 可复现步骤；
- 操作系统和 Python/Docker 版本；
- 已脱敏的错误信息或截图。

不要提交模型密钥、数据服务 Token、私有数据、账户信息或完整环境文件。

## 提交 Pull Request

1. 从 `main` 创建短生命周期分支；
2. 只修改实现目标所需的文件；
3. 为行为变化增加测试；
4. 更新受影响的用户说明；
5. 报告实际运行的测试；
6. 使用 DCO sign-off 提交。

```powershell
git switch -c fix/short-description
git commit -s -m "fix: describe the change"
```

## 本地安装

按照 [快速开始](docs/alphaforge/QUICKSTART.md) 创建环境。常用检查：

```powershell
python scripts\check_alphaforge_docs.py
python -m pytest -q
```

前端发生变化时同时运行：

```powershell
Set-Location frontend
npm run test:run
npm run build
```

## 安全边界

涉及研究代码执行、数据时间语义、Evidence、模型工具权限、密钥或本地服务暴露的修改，必须包含失败关闭测试。研究功能不得引入真实账户或真实订单入口。

安全问题不要公开提交 Issue，请按照 [安全策略](SECURITY.md) 私下报告。

## 许可证

贡献代码须与仓库许可证兼容，并保留 `LICENSE`、`NOTICE` 及适用的第三方归属。
