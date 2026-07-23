# AlphaForge 快速开始

## 五分钟离线体验

需要 Python 3.11 或更高版本。

```powershell
git clone https://github.com/xxxhhbb/AlphaForge.git
Set-Location AlphaForge

python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -e .

alphaforge demo --offline --seed 7
```

离线演示无需模型密钥或网络数据，会完成一次从研究候选、机器评价到报告产物的完整流程。

## 发起真实研究

复制本地环境模板：

```powershell
Copy-Item agent\.env.example agent\.env
```

在 `agent/.env` 中填写你选择的模型服务配置。不要提交或分享该文件。

准备冻结研究数据后运行：

```powershell
alphaforge research "成交量确认能否增强 50 日动量，并预测未来 20 日收益？" `
  --data-root "<你的冻结数据目录>" `
  --label-horizon 20 `
  --print-report
```

运行过程中会持续输出研究计划、参与研究组、实验、机器评价和报告位置。

## 打开本地产品

Docker 会自动完成应用构建：

```powershell
docker compose up --build alphaforge
```

打开 <http://127.0.0.1:8000>。停止服务：

```powershell
docker compose down
```

## 常用命令

```text
alphaforge demo       完全离线体验
alphaforge research   发起 Alpha 研究
alphaforge serve      启动本地研究服务
alphaforge version    查看版本
```

更多内容：

- [用户指南](USER_GUIDE.md)
- [Docker 使用](DOCKER.md)
- [故障排查](TROUBLESHOOTING.md)
- [示例问题](../../examples/questions.md)
- [示例报告](../../examples/sample-report.md)
