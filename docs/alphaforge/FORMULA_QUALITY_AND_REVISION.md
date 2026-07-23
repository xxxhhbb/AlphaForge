# AlphaForge 公式质量与自主修订

## 结论

公式质量审查已经接入唯一产品主链：Agent 仍然自由提出公式和研究代码；程序只在机械安全审查之后、隔离运行之前，利用冻结输入判断公式是否真的具有横截面含义。

当前实现不再通过 `shibor_`、`macro_` 等字段名猜测语义。审查依据是每个决策时点的实际横截面分布。因此：

- 任意改名但同日对所有股票恒定的字段，被直接作为加减项时要求修订；
- 名为 Shibor 的字段如果实际具有横截面差异，不会被误杀；
- 宏观变量用于交互或状态划分时允许继续研究；
- 等权组合本身不被禁止；
- 高相关特征只产生提示，不自动否决候选。

## 产品主链

```text
AgentLoop 生成 DRAFT 公式与代码
→ 既有机械安全审查
→ FormulaSemanticReviewer（冻结数据驱动）
→ 需要修订时返回脱敏 finding code
→ 复用既有 AgentLoop 最多修订一次
→ Docker 无网络隔离运行
→ 标准结果表
→ 独立 Evidence
→ ResearchAnswer 与中文客户报告
```

原公式、语义审查收据、修订原因、新公式及两次源码哈希全部保留。报告正文显示具名公式和中文修订理由，内部哈希保留在机器收据，不作为客户正文重点。

## 权限边界

语义审查不计算官方指标、不接触 sealed 数据、不决定 Promotion。Agent 的修订只改变研究 DRAFT；冻结数据身份、PIT、Evidence、晋级、风险和交易权限保持不变。

## Harness

运行：

```powershell
$env:PYTHONPATH='agent/src'
python -m veriquant.formula_quality_harness.harness
```

机器产物位于 `.autobuild/veriquant/formula_quality_harness/`，包括计划、逐案例内容寻址收据、事件流、进度和最终 verdict。代表案例覆盖改名恒定项、字段名误报、宏观交互、高相关提示和 `sum(z_scores)` 公式。
