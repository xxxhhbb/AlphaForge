import type { LucideIcon } from "lucide-react";
import type { ReactNode } from "react";
import { ArrowRight, CircleAlert } from "lucide-react";
import { Link } from "react-router-dom";
import { statusZh } from "@/lib/veriquant";

export function StatusChip({ status }: { status: string }) {
  const safe = status || "INCOMPLETE";
  return <span className={`vq-status vq-status-${safe.toLowerCase()}`}>{statusZh(safe)}</span>;
}

export function PageHero({ eyebrow, title, description, action, tone = "plain", visual }: {
  eyebrow: string;
  title: string;
  description: string;
  action?: ReactNode;
  tone?: "plain" | "ice" | "night" | "violet";
  visual?: string;
}) {
  return (
    <section
      className={`vq-page-hero vq-page-hero-${tone}${visual ? " vq-page-hero-has-visual" : ""}`}
      style={visual ? { backgroundImage: `url(${visual})` } : undefined}
    >
      <div className="vq-container vq-hero-grid">
        <div>
          <p className="vq-eyebrow">{eyebrow}</p>
          <h1>{title}</h1>
          <p className="vq-lead">{description}</p>
        </div>
        {action ? <div className="vq-hero-action">{action}</div> : null}
      </div>
    </section>
  );
}

export function GlassPanel({ children, className = "" }: { children: ReactNode; className?: string }) {
  return <section className={`vq-glass-panel ${className}`}>{children}</section>;
}

export function SectionHeading({ eyebrow, title, description }: { eyebrow?: string; title: string; description?: string }) {
  return (
    <div className="vq-section-heading">
      {eyebrow ? <p className="vq-eyebrow">{eyebrow}</p> : null}
      <h2>{title}</h2>
      {description ? <p>{description}</p> : null}
    </div>
  );
}

export function MetricTile({ label, value, caption, icon: Icon }: {
  label: string;
  value: string | number;
  caption?: string;
  icon?: LucideIcon;
}) {
  return (
    <article className="vq-utility-card">
      <div className="vq-metric-label">{Icon ? <Icon aria-hidden="true" /> : null}<span>{label}</span></div>
      <strong className="vq-metric-value">{value}</strong>
      {caption ? <p className="vq-caption">{caption}</p> : null}
    </article>
  );
}

export function EmptyState({ title, description, code }: { title: string; description: string; code?: string }) {
  return (
    <div className="vq-empty-state">
      <CircleAlert aria-hidden="true" />
      <div><strong>{title}</strong><p>{description}</p>{code ? <code>{code}</code> : null}</div>
    </div>
  );
}

export function PrimaryLink({ to, children }: { to: string; children: ReactNode }) {
  return <Link className="vq-button-primary" to={to}>{children}<ArrowRight aria-hidden="true" /></Link>;
}

export function SecondaryLink({ to, children }: { to: string; children: ReactNode }) {
  return <Link className="vq-button-secondary" to={to}>{children}</Link>;
}
