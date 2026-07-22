from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from .harness import QlibProductCapabilityHarness


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run AlphaForge Qlib product capability acceptance")
    parser.add_argument("--catalog-result", required=True)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()
    report = QlibProductCapabilityHarness(catalog_result_path=args.catalog_result).run()
    output = Path(args.output_dir)
    payload = report.to_dict()
    _atomic_write(output / "capability_report.json", json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
    markdown = "\n".join([
        "# AlphaForge Qlib 产品能力验收",
        "",
        f"- 状态：**{payload['status']}**",
        f"- 固定版本官方算子：{report.classified_operator_count}/{report.pinned_operator_count} 已分类",
        f"- 可进入静态认证的算子：{report.trusted_operator_count}",
        f"- 明确排除的内部/不安全算子：{report.excluded_operator_count}",
        f"- 语义模板：{report.semantic_template_count}；认证成功 {report.certified_template_count}；已知差异待隔离复核 {report.known_difference_template_count}；失败 {report.failed_template_count}",
        f"- 因子目录：{report.catalog_record_count or '不可用'} 项；完整分类：{report.catalog_classification_complete}",
        f"- 报告哈希：`{report.report_hash}`",
        "",
        "说明：模板认证证明表达式语言和静态安全边界可用，不等于因子有效，也不授予官方指标或晋级权限。",
        "",
    ])
    _atomic_write(output / "capability_report.md", markdown)
    print(json.dumps({"status": payload["status"], "report_hash": report.report_hash, "templates": report.semantic_template_count}, ensure_ascii=False))
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
