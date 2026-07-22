import { useEffect, useMemo, useState } from "react";
import { Database, Filter, GitCompareArrows, Search, ShieldCheck } from "lucide-react";
import { Link, useParams } from "react-router-dom";
import { api, type AlphaSummary } from "@/lib/api";
import { EmptyState, MetricTile, PageHero, SectionHeading, StatusChip } from "@/components/veriquant/NexusUI";

const ZOO_NAMES: Record<string, string> = {
  qlib158: "Qlib 158 模板",
  alpha101: "Kakushadze 101 公式模板",
  gtja191: "国泰君安 191 模板",
  academic: "学术异象模板",
  fundamental: "PIT 基本面模板",
};

const THEME_NAMES: Record<string, string> = {
  momentum: "动量",
  reversal: "反转",
  volatility: "波动率",
  volume: "成交量",
  value: "价值",
  quality: "质量",
  liquidity: "流动性",
  microstructure: "微观结构",
  growth: "成长",
  sentiment: "情绪",
};

const FACTOR_ACTIONS = [
  ["验证", "验证我的想法"],
  ["改进", "改进现有因子"],
  ["诊断", "诊断因子/组合"],
  ["补缺口", "补充组合缺口"],
  ["监控", "监控、衰减与退役"],
] as const;

function themeName(value: string) {
  return THEME_NAMES[value.toLowerCase()] ?? value;
}

function factorActionLink(alphaId: string, mode: string) {
  return `/campaign/confirm?mode=${encodeURIComponent(mode)}&factor=${encodeURIComponent(alphaId)}`;
}

export function AlphaZoo() {
  const { alphaId } = useParams<{ alphaId?: string }>();
  const [items, setItems] = useState<AlphaSummary[]>([]);
  const [total, setTotal] = useState(0);
  const [query, setQuery] = useState("");
  const [zoo, setZoo] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  useEffect(() => {
    setLoading(true);
    setError("");
    api.listAlphas({ zoo: zoo || undefined, limit: 1000 })
      .then((response) => {
        setItems(response.alphas);
        setTotal(response.total);
      })
      .catch(() => {
        setItems([]);
        setTotal(0);
        setError("因子模板目录暂不可用。");
      })
      .finally(() => setLoading(false));
  }, [zoo]);

  const filtered = useMemo(() => {
    const keyword = query.trim().toLowerCase();
    return keyword
      ? items.filter((item) => item.id.toLowerCase().includes(keyword) || (item.nickname || "").toLowerCase().includes(keyword))
      : items;
  }, [items, query]);
  const selected = alphaId ? items.find((item) => item.id === alphaId) : undefined;
  const selectedLabel = selected?.nickname || selected?.id || alphaId || "选择一个因子";

  return (
    <>
      <PageHero
        eyebrow="Alpha 因子库"
        title="因子库是研究入口，不是手动改指标的地方。"
        description="在这里查看因子状态、版本、谱系、Evidence 和相关性，并从因子发起验证、改进、诊断、补缺口或监控 Campaign。"
      />

      <section className="vq-section vq-section-parchment">
        <div className="vq-container">
          <div className="vq-grid-4">
            <MetricTile label="可浏览模板" value={loading ? "…" : total} caption="经典公式与研究模板" icon={Database} />
            <MetricTile label="可信执行形式" value="FactorDAG" caption="禁止 eval/exec 与 factor.py 直跑" icon={ShieldCheck} />
            <MetricTile label="可发起动作" value="5" caption="验证、改进、诊断、补缺口、监控" icon={Filter} />
            <MetricTile label="谱系视角" value="版本化" caption="父子关系、证据与相关性追溯" icon={GitCompareArrows} />
          </div>
        </div>
      </section>

      <section className="vq-section vq-section-white">
        <div className="vq-container">
          <SectionHeading
            eyebrow="因子资产"
            title="从已有资产发起一个新的研究 Campaign。"
            description="页面只读展示因子信息；官方指标、Promotion、持仓和风险审批不可在因子库里编辑。"
          />
          <div className="vq-glass-panel vq-filter-panel">
            <div className="vq-operator-grid">
              <label className="vq-grow">
                <span className="vq-caption">搜索因子</span>
                <div className="vq-search-wrap">
                  <Search aria-hidden="true" />
                  <input
                    className="vq-search"
                    value={query}
                    onChange={(event) => setQuery(event.target.value)}
                    placeholder="输入因子 ID 或名称"
                  />
                </div>
              </label>
              <label>
                <span className="vq-caption">模板来源</span>
                <select className="vq-select" value={zoo} onChange={(event) => setZoo(event.target.value)}>
                  <option value="">全部来源</option>
                  {Object.entries(ZOO_NAMES).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
                </select>
              </label>
            </div>
          </div>

          {error ? <EmptyState title="模板目录不可用" description={error} /> : (
            <div className="vq-table-wrap">
              <table className="vq-table">
                <thead>
                  <tr>
                    <th>因子</th>
                    <th>来源</th>
                    <th>主题</th>
                    <th>适用范围</th>
                    <th>状态</th>
                    <th>Campaign 动作</th>
                  </tr>
                </thead>
                <tbody>
                  {filtered.slice(0, 100).map((item) => (
                    <tr key={`${item.zoo}:${item.id}`}>
                      <td>
                        <Link to={`/factors/${encodeURIComponent(item.id)}`} className="vq-code">{item.id}</Link>
                        {item.nickname ? <div className="vq-caption">{item.nickname}</div> : null}
                      </td>
                      <td>{ZOO_NAMES[item.zoo] ?? item.zoo}</td>
                      <td>{item.theme.map(themeName).join("、") || "未分类"}</td>
                      <td>{item.universe.join("、") || "需重新声明"}</td>
                      <td><StatusChip status="DRAFT" /></td>
                      <td>
                        <div className="vq-factor-actions">
                          {FACTOR_ACTIONS.map(([label, mode]) => (
                            <Link key={label} to={factorActionLink(item.id, mode)}>{label}</Link>
                          ))}
                        </div>
                      </td>
                    </tr>
                  ))}
                  {!loading && filtered.length === 0 ? (
                    <tr><td colSpan={6}>没有符合条件的因子模板。</td></tr>
                  ) : null}
                </tbody>
              </table>
            </div>
          )}
          {filtered.length > 100 ? (
            <p className="vq-caption vq-top-gap">当前显示前 100 条，共 {filtered.length} 条；请使用搜索缩小范围。</p>
          ) : null}
        </div>
      </section>

      {alphaId ? (
        <section className="vq-section vq-section-dark">
          <div className="vq-container">
            <SectionHeading
              eyebrow="因子详情"
              title={selectedLabel}
              description="详情页展示状态、版本、谱系、Evidence 摘要和相关性入口；任何研究动作都会创建新的 Campaign，而不是直接修改官方事实。"
            />
            <div className="vq-grid-3">
              <article className="vq-glass-panel">
                <p className="vq-caption">因子编号</p>
                <strong className="vq-code">{alphaId}</strong>
              </article>
              <article className="vq-glass-panel">
                <p className="vq-caption">当前状态</p>
                <StatusChip status="DRAFT" />
              </article>
              <article className="vq-glass-panel">
                <p className="vq-caption">版本与谱系</p>
                <strong>等待 Campaign 产物绑定</strong>
              </article>
            </div>
            <div className="vq-page-actions">
              {FACTOR_ACTIONS.map(([label, mode]) => (
                <Link className="vq-button-glass" key={label} to={factorActionLink(alphaId, mode)}>{label}这个因子</Link>
              ))}
            </div>
          </div>
        </section>
      ) : null}
    </>
  );
}
