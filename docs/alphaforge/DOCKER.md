# AlphaForge Docker 使用

## 前置要求

安装 Docker Desktop 或 Docker Engine，并确认：

```powershell
docker version
docker compose version
```

## 启动

在仓库根目录运行：

```powershell
Copy-Item agent\.env.example agent\.env
docker compose up --build alphaforge
```

打开：

- 产品页面：<http://127.0.0.1:8000>
- 健康检查：<http://127.0.0.1:8000/healthz>

服务仅绑定本机回环地址。模型与数据配置保存在本机 `agent/.env`，不要提交该文件。

## 查看运行状态

```powershell
docker compose ps
docker compose logs -f alphaforge
```

## 停止与重新启动

```powershell
docker compose down
docker compose up -d alphaforge
```

研究产物由命名卷持久保存，常规停止和重新创建容器不会清空这些卷。

## 更新

```powershell
git pull
docker compose build --pull alphaforge
docker compose up -d alphaforge
```

## 完全移除本地数据

下面的命令会删除 Compose 命名卷中的本地研究产物和状态，请先备份需要保留的内容：

```powershell
docker compose down --volumes
```

遇到启动问题请查看 [故障排查](TROUBLESHOOTING.md)。
