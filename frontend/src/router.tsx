import { Suspense, lazy, type ComponentType } from "react";
import { Navigate, createBrowserRouter } from "react-router-dom";
import { Layout } from "@/components/layout/Layout";

const ResearchHome = lazy(() => import("@/pages/ResearchOSV3LaunchPage").then((m) => ({ default: m.ResearchOSV3LaunchPage })));
const CampaignConfirm = lazy(() => import("@/pages/CampaignConfirm").then((m) => ({ default: m.CampaignConfirm })));
const CampaignDashboard = lazy(() => import("@/pages/CampaignDashboard").then((m) => ({ default: m.CampaignDashboard })));
const Workbench = lazy(() => import("@/pages/VeriquantWorkbench").then((m) => ({ default: m.VeriquantWorkbench })));
const FactorCenter = lazy(() => import("@/pages/AlphaZoo").then((m) => ({ default: m.AlphaZoo })));
const Evidence = lazy(() => import("@/pages/Evidence").then((m) => ({ default: m.Evidence })));
const Shadow = lazy(() => import("@/pages/Shadow").then((m) => ({ default: m.Shadow })));
const Reports = lazy(() => import("@/pages/Reports").then((m) => ({ default: m.Reports })));
const FormalReport = lazy(() => import("@/pages/FormalReportPage").then((m) => ({ default: m.FormalReportPage })));
const SystemStatus = lazy(() => import("@/pages/SystemStatus").then((m) => ({ default: m.SystemStatus })));
const ResearchOSV3Result = lazy(() => import("@/pages/ResearchOSV3ResultPage").then((m) => ({ default: m.ResearchOSV3ResultPage })));

function PageLoader() {
  return <div className="vq-page-loader">正在打开 AlphaForge 研究空间…</div>;
}

function wrap(Component: ComponentType) {
  return <Suspense fallback={<PageLoader />}><Component /></Suspense>;
}

export const router = createBrowserRouter([{ element: <Layout />, children: [
  { path: "/", element: wrap(ResearchHome) },
  { path: "/research", element: wrap(ResearchHome) },
  { path: "/campaign", element: <Navigate to="/research" replace /> },
  { path: "/campaign/confirm", element: wrap(CampaignConfirm) },
  { path: "/campaign/dashboard", element: wrap(CampaignDashboard) },
  { path: "/veriquant/workbench", element: wrap(Workbench) },
  { path: "/factors", element: wrap(FactorCenter) },
  { path: "/factors/:alphaId", element: wrap(FactorCenter) },
  { path: "/evidence", element: wrap(Evidence) },
  { path: "/shadow", element: wrap(Shadow) },
  { path: "/reports", element: wrap(Reports) },
  { path: "/reports/:runId", element: wrap(FormalReport) },
  { path: "/research/results/:runId", element: wrap(ResearchOSV3Result) },
  { path: "/system", element: wrap(SystemStatus) },
  { path: "/alpha-zoo/*", element: <Navigate to="/factors" replace /> },
  { path: "/veriquant/campaign", element: <Navigate to="/research" replace /> },
  { path: "/agent", element: <Navigate to="/campaign/dashboard" replace /> },
  { path: "/runtime", element: <Navigate to="/system" replace /> },
  { path: "/settings", element: <Navigate to="/system" replace /> },
  { path: "/correlation", element: <Navigate to="/factors" replace /> },
  { path: "/compare", element: <Navigate to="/reports" replace /> },
  { path: "/runs/:runId", element: <Navigate to="/veriquant/workbench" replace /> },
  { path: "*", element: <Navigate to="/" replace /> },
]}]);
