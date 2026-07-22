# AlphaForge 配置指南

## 配置文件

开发环境默认读取 `agent/.env`：

```powershell
Copy-Item agent\.env.example agent\.env
```

真实 `agent/.env` 不得提交。修改示例文件时只能使用明显占位符。

## AlphaForge LLM 配置

AlphaForge 研究路径优先使用这些变量：

```dotenv
ENABLE_LLM_RESEARCH=1
ALPHAFORGE_LLM_ENDPOINT_URL=https://your-approved-host.example/v1/chat/completions
ALPHAFORGE_LLM_API_KEY=replace-me
ALPHAFORGE_LLM_MODEL=your-model-id
ALPHAFORGE_LLM_APPROVED_HOSTS=your-approved-host.example
ALPHAFORGE_LLM_TIMEOUT_SECONDS=45
ALPHAFORGE_LLM_MAX_TOKENS=2048
ALPHAFORGE_LLM_MAX_RETRIES=1
ALPHAFORGE_LLM_TEMPERATURE=0
ALPHAFORGE_LLM_TOP_P=1
```

规则：

- Endpoint host 必须在 `ALPHAFORGE_LLM_APPROVED_HOSTS`；
- 不把凭据、完整请求或未脱敏响应写入前端；
- 研究组有模型调用、迭代和 token 预算；
- Provider 失败必须形成 `PROVIDER_FAILED`/`ADAPTER_UNAVAILABLE`，不能回退为伪造模型结果；
- 重复性代码修复应使用轻量、严格有界的修复循环，避免重新跑完整研究 AgentLoop。

代码也兼容部分上游变量（如 `OPENAI_BASE_URL`、`OPENAI_API_KEY`、`LANGCHAIN_MODEL_NAME`），但新部署建议显式使用 `ALPHAFORGE_*`，减少 Vibe 兼容配置和研究配置的歧义。

## Vibe 兼容 Provider 配置

根包仍支持 `LANGCHAIN_PROVIDER`/`LANGCHAIN_MODEL_NAME` 及 OpenRouter、OpenAI、DeepSeek、Ollama 等兼容设置。它们主要服务上游 Vibe CLI/Agent 功能。

示例文件当前默认展示 OpenRouter 占位符。占位符不是有效密钥；在真实调用前必须替换或选择离线模式。

Ollama 示例：

```dotenv
LANGCHAIN_PROVIDER=ollama
LANGCHAIN_MODEL_NAME=qwen2.5:32b
OLLAMA_BASE_URL=http://localhost:11434
```

Docker 中默认 Ollama 地址为 `http://host.docker.internal:11434`。

## 数据源

### 冻结/离线数据

无凭据环境优先使用已冻结的数据仓和 deterministic fixtures。它们适合合同测试、前端开发和复放，不应被描述为当前实时市场。

### Tushare/代理数据

示例变量：

```dotenv
TUSHARE_TOKEN=replace-me
TUSHARE_PROXY_URL=https://your-approved-proxy.example
```

真实数据准备必须由显式用户动作触发，API 请求包含 `allowNetwork=true`、`endDate` 和可选资源预算。准备成功仍需冻结、时点检查和 Provenance；来源可用不代表官方 Evidence 可用。

### 动态文档数据

新闻、研报和文档工具返回必须记录：查询、来源、抓取时间、文档哈希、实体映射和时间窗口。若内容晚于研究决策窗口，只能作为辅助研究。

## Research Sandbox 配置

核心字段：

- `sandbox_root`：每次运行临时根；
- `image_name`：显式本地镜像名；
- `expected_image_id`：不可变 image ID；
- `docker_path`：`docker` 或绝对可执行文件；
- timeout、memory、cpu、pids、tmpfs、输入/输出大小限制。

Runner 不 pull、不 build。缺少镜像或 ID 不匹配时返回 `OS_SANDBOX_UNAVAILABLE`。

## 前端环境

```dotenv
VITE_API_URL=http://127.0.0.1:8899
VITE_VERIQUANT_API_URL=http://127.0.0.1:8000
```

- `VITE_VERIQUANT_API_URL`：AlphaForge V3、状态与 Workbench；
- `VITE_API_URL`：上游 Vibe 兼容接口；
- AlphaForge-only 前端工作优先把 VeriQuant target 指向 `agent/alphaforge_api_server.py`；
- 不使用 `VITE_*` 保存秘密，因为 Vite 会把它们打入浏览器 bundle。

## API 网络和认证

- 专用 AlphaForge 服务回环限定，不开放 Swagger；
- 上游一体服务如果超出回环网络，必须设置并验证 `API_AUTH_KEY`；
- `VIBE_TRADING_TRUST_DOCKER_LOOPBACK=1` 只适用于 Compose 已将端口绑定到宿主机 loopback 的场景；
- 不要在公网 `0.0.0.0` 暴露且关闭认证。

## 默认预算建议

前端不应直接写死后端权限，但可以把计划预算清楚展示：

| 深度 | 研究组 | 候选/Trial/模型调用 | 适用 |
| --- | --- | --- | --- |
| 快速探索 | 1–2 | 小预算 | 数据和方向确认 |
| 标准研究 | 1–4 | 中预算 | 正式研究报告 |
| 严格验证 | 依问题 | 更强门禁和复放 | 稳健性与独立评价 |

具体数值以计划 API 返回为准，前端不能擅自扩容。

## 配置验证

只检查是否配置，不输出秘密：

```powershell
python agent\alphaforge_api_server.py --help
Invoke-RestMethod http://127.0.0.1:8000/veriquant/status
```

提交前：

```powershell
git check-ignore agent/.env
git grep -n -I -E '(sk-[A-Za-z0-9_-]{12,}|BEGIN (RSA|OPENSSH|EC) PRIVATE KEY|TUSHARE_TOKEN=.+)' -- .
```

如果搜索命中示例占位符，应人工确认它明显无效；如果命中真实值，立即停止提交并轮换。
