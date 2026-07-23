# AlphaForge 故障排查

## 找不到 `alphaforge` 命令

确认虚拟环境已激活并重新安装：

```powershell
.\.venv\Scripts\Activate.ps1
pip install -e .
alphaforge --version
```

## Python 依赖缺失

```powershell
python --version
python -m pip install --upgrade pip
pip install -e .
```

建议使用 Python 3.11。

## Docker 无法启动

```powershell
docker version
docker compose ps
docker compose logs --tail 200 alphaforge
```

Windows 用户应确认 Docker Desktop 已启动并处于 Linux containers 模式。

## 页面打不开

确认服务健康：

```powershell
Invoke-RestMethod http://127.0.0.1:8000/healthz
```

若端口被占用，先停止占用 8000 端口的本地程序，再重新启动 AlphaForge。

## 模型调用失败

检查：

1. `agent/.env` 是否存在；
2. Provider 地址、模型名称和密钥是否匹配；
3. 当前网络是否能访问所配置的 Provider；
4. Provider 是否返回限流、余额或权限错误。

不要把密钥、完整请求头或 `agent/.env` 内容粘贴到 Issue。

## 研究数据不可用

确认 `--data-root` 指向真实存在的冻结数据目录，并包含本次问题所需的行情、财务、事件或宏观字段。数据目录缺失时，系统会停止机器评价并给出具体的数据需求。

## 运行看起来没有响应

为终端研究设置更短的进度间隔：

```powershell
alphaforge research "你的研究问题" --progress-interval 5 --print-report
```

长时间研究会持续输出当前研究组、实验和评价阶段。不要重复启动同一个问题；优先使用已有运行的恢复入口。

## 报告尚未生成

报告只会在研究达到合法终态后发布。先查看终端输出中的运行状态和报告路径；数据不足、模型不可用或实验审查未通过时，系统会生成相应的研究说明。

## 提交问题时请提供

- 操作系统、Python 和 Docker 版本；
- 执行的命令；
- 脱敏后的错误码和最后 50 行日志；
- 问题是否涉及模型服务、数据目录或 Docker；
- 不含密钥和私有数据的截图。
