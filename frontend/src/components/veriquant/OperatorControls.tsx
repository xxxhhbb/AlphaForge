import { useEffect, useMemo, useRef, useState } from "react";
import { statusZh } from "@/lib/veriquant";

type OperatorProfile = {
  profile_id: string;
  stage_count: number;
  max_budget: number;
  offline_only: boolean;
  requires_provider_key: boolean;
};

type OperatorJob = {
  job_id: string;
  profile_id: string;
  status: string;
  completed_stages: string[];
  artifact_hashes: Record<string, string>;
  failure_code?: string | null;
};

type OperatorEvent = {
  sequence: number;
  event_type: string;
  status: string;
  stage_id?: string | null;
  failure_code?: string | null;
};

async function operatorJson<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, {
    ...init,
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json",
      ...(init?.method && init.method !== "GET" ? { "X-VeriQuant-Operator": "offline-ui-v1" } : {}),
      ...(init?.headers || {}),
    },
  });
  if (!response.ok) {
    throw new Error(`当前 AlphaForge 独立服务未启用固定流程操作接口（HTTP ${response.status}）`);
  }
  const contentType = response.headers.get("content-type") || "";
  if (!contentType.toLowerCase().includes("application/json")) {
    throw new Error("当前 AlphaForge 独立服务未启用固定流程操作接口");
  }
  return response.json() as Promise<T>;
}

function idempotencyKey() {
  return typeof crypto !== "undefined" && "randomUUID" in crypto
    ? crypto.randomUUID()
    : `ui-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

const TERMINAL = new Set(["COMPLETED", "FAILED", "CANCELLED", "BLOCKED"]);

export function OperatorControls() {
  const [profiles, setProfiles] = useState<OperatorProfile[]>([]);
  const [profileId, setProfileId] = useState("");
  const [job, setJob] = useState<OperatorJob | null>(null);
  const [events, setEvents] = useState<OperatorEvent[]>([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const cursor = useRef(0);

  useEffect(() => {
    operatorJson<{ profiles: OperatorProfile[] }>("/veriquant/operator/profiles")
      .then(({ profiles: raw }) => {
        const next = Array.isArray(raw) ? raw : [];
        setProfiles(next);
        setProfileId((current) => current || next[0]?.profile_id || "");
      })
      .catch((reason) => setError((reason as Error).message));
  }, []);

  useEffect(() => {
    if (!job || TERMINAL.has(job.status)) return undefined;
    const timer = window.setInterval(async () => {
      try {
        const [statusPayload, eventPayload] = await Promise.all([
          operatorJson<{ job: OperatorJob }>(`/veriquant/jobs/${encodeURIComponent(job.job_id)}`),
          operatorJson<{ events: OperatorEvent[]; next_cursor: number }>(
            `/veriquant/jobs/${encodeURIComponent(job.job_id)}/events?after=${cursor.current}&limit=200`,
          ),
        ]);
        setJob(statusPayload.job);
        if (eventPayload.events.length) setEvents((current) => [...current, ...eventPayload.events]);
        cursor.current = eventPayload.next_cursor;
      } catch (reason) {
        setError((reason as Error).message);
      }
    }, 750);
    return () => window.clearInterval(timer);
  }, [job]);

  const selected = useMemo(
    () => profiles.find((profile) => profile.profile_id === profileId),
    [profileId, profiles],
  );

  async function createRun() {
    if (!selected) return;
    setBusy(true);
    setError("");
    setEvents([]);
    cursor.current = 0;
    try {
      const payload = await operatorJson<{ job: OperatorJob }>("/veriquant/jobs", {
        method: "POST",
        body: JSON.stringify({
          profile_id: selected.profile_id,
          seed: 7,
          budget: selected.max_budget,
          idempotency_key: idempotencyKey(),
        }),
      });
      setJob(payload.job);
    } catch (reason) {
      setError((reason as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function control(action: "cancel" | "resume") {
    if (!job) return;
    setBusy(true);
    setError("");
    try {
      const payload = await operatorJson<{ job: OperatorJob }>(
        `/veriquant/jobs/${encodeURIComponent(job.job_id)}/${action}`,
        { method: "POST", body: "{}" },
      );
      setJob(payload.job);
    } catch (reason) {
      setError((reason as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <section aria-label="离线研究操作台" className="vq-glass-panel">
      <div className="vq-panel-bar">
        <div>
          <p className="vq-eyebrow">固定研究方案</p>
          <h2 className="vq-panel-title">只读证据详情操作台</h2>
          <p className="vq-caption">
            这里用于触发已批准的离线演示流程和查看产物，不给页面、LLM 或文档任何 Evidence / Promotion / 风险审批权限。
          </p>
        </div>
        <span className="vq-offline-badge"><i />固定种子 7 · 离线优先</span>
      </div>
      <div className="vq-operator-grid">
        <label>
          <span className="vq-caption">批准的研究流程</span>
          <select
            aria-label="批准的研究流程"
            value={profileId}
            onChange={(event) => setProfileId(event.target.value)}
            className="vq-select"
          >
            {profiles.map((profile) => <option key={profile.profile_id} value={profile.profile_id}>{profile.profile_id}</option>)}
          </select>
        </label>
        <button type="button" disabled={!selected || busy} onClick={createRun} className="vq-button-primary">
          {busy ? "提交中…" : "运行固定流程"}
        </button>
        {job && !TERMINAL.has(job.status) ? (
          <button type="button" disabled={busy} onClick={() => control("cancel")} className="vq-button-secondary">取消</button>
        ) : null}
        {job && ["INTERRUPTED", "CANCELLED", "BLOCKED"].includes(job.status) ? (
          <button type="button" disabled={busy} onClick={() => control("resume")} className="vq-button-secondary">恢复</button>
        ) : null}
      </div>
      {error ? <p role="status" className="vq-danger-text">研究操作台不可用：{error}</p> : null}
      {job ? (
        <div className="vq-grid-3 vq-job-grid">
          <div className="vq-utility-card vq-mini-card">
            <p className="vq-caption">状态</p>
            <strong>{statusZh(job.status)}</strong>
          </div>
          <div className="vq-utility-card vq-mini-card">
            <p className="vq-caption">任务 ID</p>
            <code className="vq-code">{job.job_id}</code>
          </div>
          <div className="vq-utility-card vq-mini-card">
            <p className="vq-caption">完成阶段</p>
            <strong>{job.completed_stages.length}/{selected?.stage_count ?? "?"}</strong>
            {job.failure_code ? <p className="vq-danger-text">失败：{job.failure_code}</p> : null}
          </div>
        </div>
      ) : null}
      {events.length ? (
        <ol aria-label="研究运行事件" className="vq-events vq-code">
          {events.map((event) => (
            <li key={event.sequence}>
              #{event.sequence} {event.event_type} {event.stage_id || ""} → {statusZh(event.status)}
            </li>
          ))}
        </ol>
      ) : null}
    </section>
  );
}
