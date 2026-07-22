# AlphaForge Research OS V3 API

## 基础信息

AlphaForge 专用服务器默认地址：

```text
http://127.0.0.1:8000
```

Research OS V3 前缀：

```text
/api/veriquant/research-os/v3
```

专用服务器只允许回环 Host/Origin，关闭 OpenAPI/Swagger。所有 ID 都应使用 URL 编码；无效或路径穿越 ID 返回 400/409，不得尝试拼接本地路径。

## 端点总览

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/healthz` | 专用服务健康检查 |
| GET | `/veriquant/status` | 产品和内核状态 |
| POST | `/api/veriquant/research-os/v3/plans` | 创建研究计划 |
| POST | `/api/veriquant/research-os/v3/plans/{plan_id}/confirm` | 显式确认计划 |
| POST | `/api/veriquant/research-os/v3/plans/{plan_id}/prepare-data` | 显式联网准备真实数据 |
| POST | `/api/veriquant/research-os/v3/runs` | 启动已确认计划 |
| GET | `/api/veriquant/research-os/v3/runs/{run_id}/status` | 查询运行状态 |
| POST | `/api/veriquant/research-os/v3/runs/{run_id}/cancel` | 取消运行 |
| POST | `/api/veriquant/research-os/v3/runs/{run_id}/resume` | 从检查点恢复 |
| GET | `/api/veriquant/research-os/v3/runs` | 列出公开运行 |
| GET | `/api/veriquant/research-os/v3/runs/{run_id}` | 完整公开运行 envelope |
| GET | `/api/veriquant/research-os/v3/runs/{run_id}/report` | JSON 公开报告 |
| GET | `/api/veriquant/research-os/v3/runs/{run_id}/report.md` | Markdown 报告 |

Workbench 只读端点见本文末尾。

## 1. 创建计划

```http
POST /api/veriquant/research-os/v3/plans
Content-Type: application/json
```

请求：

```json
{
  "question": "检验盈利质量和换手率对未来20日收益的关系，并说明成本和增量性。",
  "depth": "标准研究",
  "interactionStyle": "标准式"
}
```

允许深度：`快速探索`、`标准研究`、`严格验证`。

允许协作方式：`引导式`、`标准式`、`批处理式`。

前端消费的计划视图：

```json
{
  "campaignId": "campaign.xxxxx",
  "campaignHash": "sha256:...",
  "state": "待确认",
  "question": "...",
  "researchMode": "...",
  "researchSubject": "...",
  "depth": "标准研究",
  "interactionStyle": "标准式",
  "selectedSpecialists": ["fundamental", "market_structure"],
  "plannedResearch": ["..."],
  "dataScope": {
    "sourceLabel": "...",
    "universe": "...",
    "dateRange": "...",
    "availability": "可用",
    "limitations": []
  },
  "budget": {
    "specialistGroups": 2,
    "candidateLimit": 12,
    "trialLimit": 6,
    "modelCallLimit": 8
  },
  "checkpoints": ["..."],
  "stopConditions": ["..."]
}
```

前端必须运行时校验字段，不能直接信任未知 JSON。

## 2. 准备真实数据

这是显式操作者动作，默认计划不会获得网络权限。

```http
POST /api/veriquant/research-os/v3/plans/{plan_id}/prepare-data
Content-Type: application/json
```

最小请求：

```json
{
  "allowNetwork": true,
  "endDate": "20241121"
}
```

可选字段：`indexCode`、`targetSymbols`、`maxAcquisitionSymbols`、`maxApiCalls`、`maxRows`、`maxStorageBytes`。

没有 `allowNetwork=true` 或 `endDate` 时失败。前端不得静默调用该接口。

## 3. 确认计划

```http
POST /api/veriquant/research-os/v3/plans/{plan_id}/confirm
```

无歧义计划可以发空 body。需要显式答案时：

```json
{
  "answers": {
    "universe": "沪深300",
    "horizon": "20个交易日"
  }
}
```

`answers` 必须是键值对象。确认返回 `state=已确认` 的计划视图。

## 4. 启动运行

```http
POST /api/veriquant/research-os/v3/runs
Content-Type: application/json
```

```json
{
  "plan_id": "campaign.xxxxx"
}
```

注意此字段为后端合同的 snake_case `plan_id`。

## 5. 查询状态

```http
GET /api/veriquant/research-os/v3/runs/{run_id}/status
```

后端 manifest 的关键字段：

```json
{
  "run_id": "run-...",
  "campaign_hash": "sha256:...",
  "current_stage": "RESEARCHING",
  "selected_group_ids": ["fundamental", "market_structure"],
  "specialist_summaries": [
    {
      "specialist_id": "fundamental",
      "status": "COMPLETED_DRAFT",
      "candidate_count": 1,
      "typed_failures": [],
      "missing_capabilities": [],
      "message": "..."
    }
  ],
  "candidate_count": 1,
  "trial_count": 1,
  "remaining_candidate_slots": 5,
  "typed_failures": []
}
```

阶段映射：

| 后端 | 前端 |
| --- | --- |
| `PLANNING` | 排队中 |
| `RESEARCHING` | 运行中 |
| `CANDIDATE_PREFLIGHT` | 运行中 |
| `AWAITING_EVIDENCE` | 等待确认 |
| `COMPLETED` | 已完成 |
| `PARTIAL` | 部分完成 |
| `BLOCKED` | 已阻断 |
| `FAILED` | 失败 |
| `CANCELLED` | 已取消 |

轮询建议：运行中每 1–2 秒；页面隐藏后降频；终态立即停止；使用 `AbortController` 取消旧请求。不要对 4xx 无限重试。

## 6. 取消与恢复

取消：

```http
POST /api/veriquant/research-os/v3/runs/{run_id}/cancel
```

恢复：

```http
POST /api/veriquant/research-os/v3/runs/{run_id}/resume
Content-Type: application/json

{"plan_id":"campaign.xxxxx"}
```

恢复必须使用原计划身份。前端应防止双击，并在返回前保持按钮 busy。

## 7. 读取报告

JSON：

```http
GET /api/veriquant/research-os/v3/runs/{run_id}/report
```

公开字段包括：问题、一句话结论、合法 outcome、数据范围、模型脱敏标识、候选计数、可见机器证据状态、公开候选、限制、下一动作和 report hash。

Markdown：

```http
GET /api/veriquant/research-os/v3/runs/{run_id}/report.md
Accept: text/markdown
```

前端只在合法终态读取报告。Markdown 必须当不可信内容渲染：禁用原始 HTML，链接安全处理，不执行脚本。

## 8. Workbench 只读端点

```text
GET /veriquant/workbench/runs
GET /veriquant/workbench/runs/{run_id}
GET /veriquant/workbench/runs/{run_id}/lineage
GET /veriquant/workbench/runs/{run_id}/evidence
GET /veriquant/workbench/runs/{run_id}/critics
GET /veriquant/workbench/runs/{run_id}/promotion
GET /veriquant/workbench/runs/{run_id}/citations
GET /veriquant/workbench/runs/{run_id}/shadow-portfolio
GET /veriquant/workbench/runs/{run_id}/nav-attribution
GET /veriquant/workbench/runs/{run_id}/drift-alerts
```

这些接口是公开投影，不授予写权限。未知嵌套字段必须丢弃，不能递归展示整个 payload。

## 9. 错误处理

| 状态 | 典型含义 | 前端行为 |
| --- | --- | --- |
| 400 | ID/日期/显式授权无效 | 展示可修正输入，不重试 |
| 403 | 非回环 Host/Origin | 停止请求，说明安全限制 |
| 404 | 计划、运行或报告不存在 | 提供返回研究首页 |
| 409 | 产物损坏/状态冲突 | fail closed，保留 ID 供排查 |
| 422 | 请求合同无效 | 显示字段错误 |
| 5xx | 基础设施异常 | 有界重试或恢复，不伪造结果 |

前端错误文案不得包含本地绝对路径、secret、sealed 内容或完整原始响应。

## 10. TypeScript 合同来源

- 启动流程：`frontend/src/features/research-os-v3-launch/types.ts`；
- 启动 HTTP client：`frontend/src/features/research-os-v3-launch/client.ts`；
- V3 结果：`frontend/src/features/research-os-v3/`；
- 后端路由：`agent/src/api/research_os_v3_routes.py`；
- 产品服务：`agent/src/veriquant/research_os_v3/product_service.py`。

任何合同变更必须同时更新后端测试、前端运行时校验、TypeScript 类型和本文。
