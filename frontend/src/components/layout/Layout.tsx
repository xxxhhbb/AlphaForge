import { useState } from "react";
import { Link, Outlet, useLocation } from "react-router-dom";
import {
  BookOpenCheck,
  Database,
  Home,
  Menu,
  ShieldCheck,
  X,
} from "lucide-react";

const NAV = [
  { to: "/research", label: "开始研究", icon: Home },
  { to: "/factors", label: "因子库", icon: Database },
  { to: "/evidence", label: "报告中心", icon: BookOpenCheck },
  { to: "/system", label: "安全边界", icon: ShieldCheck },
];

function isActive(pathname: string, to: string) {
  return pathname === to || (to !== "/" && pathname.startsWith(to));
}

export function Layout() {
  const { pathname } = useLocation();
  const [open, setOpen] = useState(false);

  return (
    <div className="vq-app-shell">
      <header className="vq-global-nav">
        <div className="vq-global-nav-inner">
          <Link to="/" className="vq-brand" aria-label="AlphaForge 千因研究院首页">
            <span className="vq-brand-mark">AF</span>
            <span>AlphaForge 千因研究院</span>
          </Link>
          <nav className="vq-desktop-nav" aria-label="主导航">
            {NAV.map(({ to, label }) => (
              <Link key={to} to={to} className={isActive(pathname, to) ? "active" : ""}>{label}</Link>
            ))}
          </nav>
          <button
            className="vq-mobile-menu-button"
            type="button"
            onClick={() => setOpen((value) => !value)}
            aria-label={open ? "关闭导航" : "打开导航"}
          >
            {open ? <X /> : <Menu />}
          </button>
        </div>
        {open ? (
          <nav className="vq-mobile-nav" aria-label="移动导航">
            {NAV.map(({ to, label, icon: Icon }) => (
              <Link key={to} to={to} onClick={() => setOpen(false)}>
                <Icon aria-hidden="true" />
                {label}
              </Link>
            ))}
          </nav>
        ) : null}
      </header>
      <div className="vq-subnav">
        <div className="vq-subnav-inner">
          <div>
            <strong>输入问题，生成研究报告</strong>
            <span>专业小组自动分工 · 候选机器验证 · 中文结论可追溯</span>
          </div>
          <span className="vq-offline-badge"><i />只读研究 · 无真实下单</span>
        </div>
      </div>
      <main className="vq-main"><Outlet /></main>
      <footer className="vq-footer">
        <div className="vq-container">
          <strong>AlphaForge 千因研究院</strong>
          <p>
            页面负责发起研究、展示过程和报告。模型只提出候选与解释；机器证据、晋级、持仓和订单权限始终隔离。
          </p>
        </div>
      </footer>
    </div>
  );
}
