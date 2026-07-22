# AlphaForge 安全与权限边界

## 安全目标

AlphaForge 允许 Agent 有足够研究自由，但不允许其拥有金融事实或主机权限。安全依赖代码边界、隔离、数据身份和独立评价，不依赖“请勿越权”的提示词。

## 权威等级

### `DRAFT`

假设、计划、公式、研究代码、候选和解释的初始状态。可被审查、运行、拒绝或修订，不能直接进入组合与交易。

### `AUXILIARY_RESEARCH_EVIDENCE`

来自模型、Qlib、研究沙箱、临时诊断或非官方数据的研究线索。可以帮助决定下一实验，但不能伪装成官方指标或直接晋级。

### `OFFICIAL_EVIDENCE`

只能由独立确定性 Evidence Service 从冻结、内容寻址、时点合格的输入计算。相同输入必须复现相同身份。

官方 Evidence 仍不等于 Promotion，也不等于交易授权。

## Research Agent 可以做什么

- 分解问题和选择专业研究路径；
- 使用每组获准的只读工具；
- 查看脱敏后的冻结数据描述和 schema；
- 选择并披露代理变量；
- 产生多个研究草案和可证伪实验；
- 在私有 workspace 写受限研究 Python；
- 根据机械审查或运行诊断进行预算内修订；
- 解释后端允许公开的机器结果。

## Research Agent 不能做什么

- 读取环境变量、密钥、任意路径或其他分支 workspace；
- 使用网络、shell、subprocess、动态 import、`eval`/`exec`；
- 读取 sealed 数据、隐藏排名、封闭失败原因或其衍生物；
- 声称自己计算了官方指标；
- 自己批准、晋级或选择自己提出的候选；
- 设置组合、绕过 Risk Gate、释放真实订单或修改账本；
- 将会话、Trace、Swarm `run.json` 或模型记忆当成 Trial Ledger。

## 研究沙箱

构建宪法 A-02 只授权独立研究路径中的 generated Python：

1. 源码是 DRAFT；
2. 先经过 AST/import/path/authority 机械审查；
3. 在固定、无网络、资源受限的容器中运行；
4. 只读取冻结只读数据 Handle；
5. 固定随机种子，限制 CPU、内存、磁盘、输出和时间；
6. 只产生声明的标准表；
7. 源码、review、runner、数据和输出身份绑定；
8. 输出上限为辅助研究，除非独立评价器能从合格原始量重算。

标准表包括 `ScoreTable`、`RegressionTable`、`RegimeComparisonTable`、`EventTable`、`DiagnosticTable` 和 `DatasetAssessmentTable`。

## 前端权限

前端可以：

- 创建计划；
- 展示数据范围、预算、研究组和停止条件；
- 显式确认计划；
- 启动、查询、取消和恢复研究；
- 读取公开报告与 Workbench 投影；
- 展示官方、辅助、无证据和数据缺口状态。

前端不可以：

- 传入任意路径或任意代码；
- 自定义工具白名单；
- 修改 Evidence、Promotion、风险、组合、订单、成交或账本；
- 显示未知响应字段；
- 从 Markdown、模型文本或文件名推断官方状态；
- 把 sealed/secret/order/broker/ledger 等字段记录到浏览器日志或分析平台；
- 在生产不可用时自动回退为伪成功 fixture。

## 数据和时点

每个可评价字段至少要有：

- 来源和版本；
- entity、事件时间、发布时间、可用时间和决策时间；
- 非空、实体和时间覆盖；
- 修订谱系；
- universe/calendar 策略；
- 原始响应和冻结 Handle；
- 语义映射版本与派生字段谱系。

字段存在但有效覆盖为零时必须视为不可用。当前新闻/研报工具数据晚于冻结历史 Evidence 窗口时，只能保持辅助研究，不能回填成历史官方特征。

## 密钥规则

- 只在本地 `agent/.env`、用户密钥存储或 CI Secrets 中保存真实凭据；
- `agent/.env.example` 只能放占位符；
- 不提交 API Key、Token、OAuth 缓存、Cookie、私钥、助记词、Broker 凭据或私有导出；
- 不把密钥粘贴到 Issue、PR、Trace、报告或截图；
- 如果密钥曾进入 Git 历史，删除文件不够，必须立即轮换密钥并清理历史；
- 前端只接收“已配置/未配置”等脱敏状态，不能读取实际值。

## 网络与服务暴露

- AlphaForge 专用服务器只允许 `127.0.0.1`/`localhost`/`::1` Host 与 Origin；
- Docker Compose 默认把 8899/5899 映射到宿主机 `127.0.0.1`；
- 不要把端口改为公网 `0.0.0.0` 后仍关闭认证；
- 默认研究与 CI 离线；真实数据准备必须显式 `allowNetwork=true` 并受资源预算约束；
- 外部 MCP、Broker、OAuth 和 webhook 都是独立信任面，不进入常规前端任务。

## PR 安全检查

涉及以下任一目录或概念时，PR 必须包含风险说明、负向测试和回滚方案：

- `agent/src/veriquant/` 中的 PIT、Evidence、Promotion、Sandbox、Risk、Ledger；
- `agent/src/agent/tools.py`、`agent/src/tools/` 的工具权限；
- `agent/src/swarm/` 的隔离、依赖和状态；
- `agent/src/api/` 的新写接口；
- `frontend/` 中的公开字段投影和 Evidence 标签；
- Docker 能力、卷、网络、用户、只读文件系统；
- `.github/workflows/` 的 Secrets 与部署权限。

常规 PR 验证不得启动真实交易、授权账户、发送订单或访问真实资金。

## 事件响应

如果发现密钥、sealed 内容、路径穿越、任意执行或交易权限泄漏：

1. 停止复述敏感内容；
2. 关闭相关服务或功能开关；
3. 轮换受影响凭据；
4. 保存脱敏证据和影响范围；
5. 增加失败关闭测试；
6. 采用小型回滚或安全热修，不在事故中做大重构；
7. 按根目录 `SECURITY.md` 的报告流程处理。
