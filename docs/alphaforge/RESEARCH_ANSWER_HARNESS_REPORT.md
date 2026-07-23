# ResearchAnswer v3 Harness 报告

## 当前结论

当前状态为 **PARTIAL**，不是 QUALIFIED。

已完成：

- 500/500 静态问题目录可解析并编译 AnswerSpec；
- 6/6 标准输出适配器可用；
- 12 个数据缺口/安全负向案例通过；
- 4 个本机既有真实产品案例完成零网络 v3 重放：
  - `ScoreTable` 正向；
  - `ScoreTable` 负向；
  - `RegressionTable` 正向；
  - `RegimeComparisonTable` 正向。

尚未完成的 8 个真实案例：

- `DatasetAssessmentTable` 正向、负向；
- `DiagnosticTable` 正向、负向；
- `EventTable` 正向、负向；
- `RegressionTable` 负向；
- `RegimeComparisonTable` 负向。

## 机器收据

产物根：`.autobuild/veriquant/research_answer_v3_harness/`

- `baseline.json`
- `architecture_map.json`
- `answer_contract_matrix.json`
- `representative_plan.json`
- `case_results.jsonl`
- `blocker_clusters.json`
- `progress.json`
- `events.jsonl`
- `final_verdict.json`

最新机器状态：

```text
catalog_static_coverage: 500/500
standard_output_adapters: 6/6
qualified_representative_cases: 16/24
qualified_real_cases: 4/12
status: PARTIAL
recovery_cursor: RA-01-01
```

## 为什么没有标为完成

真实模型代表案例会把冻结数据的字段、覆盖和研究摘要发送给已配置的外部模型端点。当前执行环境的安全审查要求用户在知情后对这项数据外发给出本次明确授权。在获得授权前，Harness 不会：

- 绕过审查调用外部端点；
- 用合成 Evidence 冒充真实模型案例；
- 把 Event 数据缺口误登记为负向事件结论；
- 把未评价候选登记为 REJECT；
- 为达到 24/24 而放宽 Evidence 或报告规则。

## 恢复方式

授权后从 `RA-01-01` 恢复，每批最多 3 个真实案例。每条案例必须实际完成：

```text
真实模型计划/源码
→ 静态审查
→ pinned Docker 无网络运行
→ 标准表
→ 独立 Evidence
→ ResearchAnswer v3
→ 客户中文报告
→ 原子案例收据
```

全部 24 个案例完成、真实案例达到 12 个且回归/安全扫描通过后，`final_verdict.json` 才能变为 `QUALIFIED`。

