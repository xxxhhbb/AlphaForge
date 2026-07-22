import { useEffect, useState } from "react";
import { AlertTriangle, CircleDashed } from "lucide-react";
import { useParams } from "react-router-dom";
import {
  defaultResearchOSV3Client,
  ResearchOSV3Result,
  type ResearchOSV3ResultClient,
  type ResearchOSV3RunView,
} from "@/features/research-os-v3";

export interface ResearchOSV3ResultPageProps {
  client?: ResearchOSV3ResultClient;
  runId?: string;
}

export function ResearchOSV3ResultPage({ client = defaultResearchOSV3Client, runId: explicitRunId }: ResearchOSV3ResultPageProps) {
  const params = useParams<{ runId?: string }>();
  const runId = explicitRunId ?? params.runId ?? "";
  const [run, setRun] = useState<ResearchOSV3RunView>();
  const [error, setError] = useState("");

  useEffect(() => {
    const controller = new AbortController();
    setRun(undefined);
    setError("");
    client.getResearchResult(runId, controller.signal)
      .then(setRun)
      .catch((reason: unknown) => {
        if (reason instanceof DOMException && reason.name === "AbortError") return;
        setError(reason instanceof Error ? reason.message : "暂时无法读取研究结果。");
      });
    return () => controller.abort();
  }, [client, runId]);

  if (error) {
    return <main className="ros3-shell"><section className="ros3-report-pending" role="alert"><AlertTriangle aria-hidden="true" /><div><strong>研究结果暂不可用</strong><p>{error}</p></div></section></main>;
  }
  if (!run) {
    return <main className="ros3-shell"><section className="ros3-report-pending" role="status"><CircleDashed aria-hidden="true" /><div><strong>正在读取研究结果</strong><p>正在核对运行清单、专业研究组结果和机器验证状态。</p></div></section></main>;
  }
  return (
    <main>
      <h1 className="sr-only">专业研究小组与机器验证</h1>
      <ResearchOSV3Result run={run} />
    </main>
  );
}

export default ResearchOSV3ResultPage;
