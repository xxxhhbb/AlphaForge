# AlphaForge Docker 指南

## 两种 Docker 用途不要混淆

### 用途 A：运行项目的一体化兼容镜像

根 `Dockerfile` 构建 React 前端和 Python 后端，默认命令为：

```text
vibe-trading serve --host 0.0.0.0 --port 8899
```

它托管 AlphaForge 页面/API，同时保留上游 Vibe 兼容路由。Compose 将宿主机端口限制到 `127.0.0.1:8899`，但该进程本身不是 AlphaForge-only 服务。

### 用途 B：作为 Research Sandbox 隔离 Agent 研究代码

`DockerResearchRunner` 从本地主机调用 Docker daemon，以固定 image ID、无网络、只读根文件系统、非 root 用户和资源限制运行一次性研究容器。它不会自动 pull 或 build 镜像。

当前仓库没有提交可从零重建 `veriquant-m2-test:local` 的专用 Dockerfile；新协作者没有该固定本地镜像时，完整 generated-Python 路径会合法返回 `OS_SANDBOX_UNAVAILABLE`。这是已知可复现性缺口，不应通过关闭隔离或退回主机 Python 来绕过。

## Windows 安装 Docker Desktop

官方指南：<https://docs.docker.com/desktop/setup/install/windows-install/>

推荐 WSL 2 backend：

1. 在管理员 PowerShell 中安装/更新 WSL：

   ```powershell
   wsl --install
   wsl --update
   ```

2. 重启系统（如果安装器要求）。
3. 安装 Docker Desktop，选择 WSL 2 backend。
4. 启动 Docker Desktop，等待 Engine Ready。
5. 验证：

   ```powershell
   wsl --version
   docker version
   docker compose version
   docker run --rm hello-world
   ```

Docker daemon 等价于高权限主机能力，只给需要的用户访问。

## 首次启动一体化服务

```powershell
Set-Location "C:\path\to\AlphaForge"
Copy-Item agent\.env.example agent\.env
docker compose build
docker compose up -d
docker compose ps
```

打开 <http://127.0.0.1:8899>。

健康检查：

```powershell
Invoke-RestMethod http://127.0.0.1:8899/live
```

查看日志：

```powershell
docker compose logs --tail 200 vibe-trading
docker compose logs --follow vibe-trading
```

## Compose 安全配置

当前服务包含：

- 宿主机只绑定 `127.0.0.1:8899`；
- 容器删除全部 capabilities，只补回研究子进程降权所需的 `SETUID/SETGID`；
- `no-new-privileges:true`；
- 只读根文件系统；
- `/tmp` 和缓存使用 `tmpfs`；
- 内存 4 GiB、CPU 2、PID 512；
- 非 root `vibe` 用户；
- pinned Python/Node 基础镜像 digest；
- named volumes 持久化运行和状态。

不要为了“方便”移除这些设置或挂载整个宿主机目录。

## 持久卷

| 卷 | 容器路径 | 内容 |
| --- | --- | --- |
| `vibe-runs` | `/app/agent/runs` | 兼容运行产物 |
| `vibe-sessions` | `/app/agent/sessions` | 会话 |
| `vibe-home` | `/home/vibe/.vibe-trading` | 用户记忆、配置、索引等 |
| `vibe-swarm-runs` | `/app/agent/.swarm/runs` | Swarm 运行 |
| `vibe-uploads` | `/app/agent/uploads` | 上传文件 |
| bind mount | `/app/agent/.env` | 本地环境文件 |

普通停止不会删除卷：

```powershell
docker compose down
```

只有确认要删除所有容器状态时才执行：

```powershell
docker compose down -v
```

删除卷通常不可恢复。先用 `docker volume ls` 确认目标。

## 可选 Vite 开发容器

```powershell
docker compose --profile frontend up --build
```

页面在 <http://127.0.0.1:5899>，后端在 8899。该 profile 挂载本地 `frontend/` 并运行 `npm install`，适合临时开发；可复现 CI 和正式构建仍使用 `npm ci`。

前端主流程访问 `/api/veriquant/...` 时，应确认 Vite 代理已包含该前缀。

## 重新构建与升级

代码或依赖变化后：

```powershell
docker compose build --pull=false
docker compose up -d --force-recreate
```

项目镜像使用 digest-pinned 基础镜像。升级基础镜像必须更新 digest、构建、运行健康检查和安全回归，不要只改 tag。

查看磁盘：

```powershell
docker system df
docker compose images
```

不要在不知道范围时运行广泛的 prune；它可能删除其他项目的镜像、缓存或卷。

## Research Sandbox 的运行条件

完整 OS sandbox 要求：

1. `docker` CLI 在运行 AlphaForge Python 服务的主机 PATH；
2. daemon 可用；
3. 操作者显式安装本地镜像；
4. 配置同时提供安全镜像名和 `sha256:<64 hex>` image ID；
5. image tag 当前解析到完全相同 ID；
6. Runner 能创建唯一临时目录和一次性容器。

检查本地镜像：

```powershell
docker image inspect veriquant-m2-test:local --format '{{.Id}}'
```

缺失镜像时不要随便用 `python:latest` 替代，因为运行时身份、依赖和验收收据会失效。

Runner 强制参数包括：

```text
--network none
--read-only
--cap-drop ALL
--security-opt no-new-privileges:true
--user 65534:65534
--pids-limit 32
--memory 256m
--cpus 0.5
--ipc none
--tmpfs /tmp:rw,noexec,nosuid,nodev,size=16m
--pull never
```

## Docker 模式的已知限制

- 默认项目容器不等于 AlphaForge 专用服务器；
- 默认容器不挂 Docker socket，也不应默认拥有启动 sibling container 的主机权限；
- 因此“整个应用在容器中”与“宿主机 Docker 作为研究沙箱”当前不是同一条已验收路径；
- 真实模型/数据需要本地 `agent/.env`；不要把 `docker compose config` 的完整输出贴到 Issue，因为它可能展开秘密；
- PDF 渲染依赖在 runtime 镜像中安装的 Pango/Cairo/Fontconfig；如果报告只生成 HTML，先查容器日志。

## 常用诊断

```powershell
docker compose ps
docker compose logs --tail 200
docker inspect vibe-trading-main-vibe-trading-1
docker volume ls
Test-NetConnection 127.0.0.1 -Port 8899
```

更多见 [故障排查](TROUBLESHOOTING.md)。
