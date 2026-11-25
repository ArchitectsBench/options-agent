import { useEffect } from "react";
import { NavRail } from "./components/NavRail";
import { StampStrip, KillBanner, SimulatedBanner } from "./components/Banners";
import { TopBar } from "./components/TopBar";
import { ForwardClock } from "./components/ForwardClock";
import { ResearchStrip } from "./components/ResearchStrip";
import { ResearchPage } from "./pages/ResearchPage";
import { TodayStrip } from "./components/TodayStrip";
import { PositionsTable } from "./components/PositionsTable";
import { RecsQueue } from "./components/RecsQueue";
import { ActivityLog } from "./components/ActivityLog";
import { PerfPage } from "./pages/PerfPage";
import { TradesPage } from "./pages/TradesPage";
import { TradeViewPage } from "./pages/TradeViewPage";
import { HealthPage } from "./pages/HealthPage";
import { ReportsPage } from "./pages/ReportsPage";
import { ErrorBoundary } from "./components/ErrorBoundary";
import { useCockpit } from "./store";

export default function App() {
  const { page, theme, tick, connectLive } = useCockpit();

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
  }, [theme]);

  useEffect(() => {
    const t = setInterval(tick, 1000);
    return () => clearInterval(t);
  }, [tick]);

  useEffect(() => {
    connectLive(); // live feed via /api + /ws; mock + SIMULATED banner until it answers
  }, [connectLive]);

  return (
    <div style={{ minHeight: "100vh", display: "flex", background: "var(--bg-app)" }}>
      <NavRail />
      <div style={{ flex: 1, minWidth: 0, display: "flex", flexDirection: "column" }}>
        <SimulatedBanner />
        <StampStrip />
        <TopBar />
        <KillBanner />
        <ErrorBoundary key={page}>
        {page === "dash" && (
          <div
            style={{
              flex: 1,
              display: "flex",
              gap: 14,
              padding: 14,
              alignItems: "flex-start",
            }}
          >
            <div style={{ flex: 1, minWidth: 0, display: "flex", flexDirection: "column", gap: 14 }}>
              {/* Session surfaces first; research is a two-line summary that
                  links to the RSCH page (2026-09-13 reorganization). */}
              <ForwardClock />
              <TodayStrip />
              <PositionsTable />
              <RecsQueue />
              <ResearchStrip />
            </div>
            <ActivityLog />
          </div>
        )}
        {page === "trades" && <TradesPage />}
        {page === "tradeview" && <TradeViewPage />}
        {page === "perf" && <PerfPage />}
        {page === "research" && <ResearchPage />}
        {page === "health" && <HealthPage />}
        {page === "reports" && <ReportsPage />}
        </ErrorBoundary>
      </div>
    </div>
  );
}
