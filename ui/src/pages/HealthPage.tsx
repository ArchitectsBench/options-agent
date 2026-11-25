import { useCockpit } from "../store";
import type { EarningsState, FeedStatus, JobStatus, WatchdogHealth } from "../feed/types";

const panel: React.CSSProperties = {
  border: "1px solid var(--border-panel)",
  borderRadius: 10,
  background: "var(--bg-panel)",
};
const sectionTitle: React.CSSProperties = {
  fontSize: 11,
  fontWeight: 700,
  letterSpacing: "0.14em",
  color: "var(--text-2)",
};
const panelHeader: React.CSSProperties = {
  padding: "11px 15px",
  borderBottom: "1px solid var(--border-rail)",
};

const FEED_STATUS: Record<FeedStatus, { color: string; label: string }> = {
  live: { color: "var(--grn)", label: "LIVE" },
  standby: { color: "var(--text-7)", label: "STANDBY" },
  degraded: { color: "var(--amb)", label: "DEGRADED" },
  down: { color: "var(--red)", label: "DOWN" },
};

const EARNINGS_TAG: Record<EarningsState, { label: string; bg: string; color: string }> = {
  block: { label: "VETO-BLOCKED", bg: "rgba(255,90,95,0.1)", color: "var(--red)" },
  watch: { label: "IN WINDOW SOON", bg: "rgba(240,178,58,0.12)", color: "var(--amb)" },
  clear: { label: "clear", bg: "var(--bg-tile)", color: "var(--text-7)" },
  etf: { label: "ETF · none", bg: "var(--bg-tile)", color: "var(--text-7)" },
};

const JOB_STATUS: Record<JobStatus, { color: string; label: string }> = {
  ok: { color: "var(--grn)", label: "OK" },
  warn: { color: "var(--amb)", label: "WARN" },
  fail: { color: "var(--red)", label: "FAIL" },
};

const WATCHDOG: Record<WatchdogHealth, { color: string; label: string }> = {
  stream: { color: "var(--grn)", label: "STREAMING · ~1s" },
  degraded: { color: "var(--amb)", label: "DEGRADED · 5-min fallback" },
  offline: { color: "var(--red)", label: "OFFLINE" },
};

export function HealthPage() {
  const { feed } = useCockpit();
  const h = feed.health;
  const wd = WATCHDOG[h.watchdog];

  return (
    <div style={{ flex: 1, display: "flex", flexDirection: "column", gap: 14, padding: 14 }}>
      {/* header + error/warning tiles */}
      <div style={{ display: "flex", alignItems: "flex-end", justifyContent: "space-between" }}>
        <div>
          <h1 style={{ margin: 0, fontSize: 18, fontWeight: 700, letterSpacing: "-0.01em" }}>
            System Health
          </h1>
          <div className="mono" style={{ fontSize: 10.5, color: "var(--text-9)", marginTop: 3 }}>
            feeds · jobs · watchdog · {feed.dataAge}
          </div>
        </div>
        <div style={{ display: "flex", gap: 10 }}>
          <div style={{ ...panel, borderRadius: 8, padding: "8px 14px", textAlign: "center" }}>
            <div
              className="mono"
              style={{
                fontSize: 18,
                fontWeight: 700,
                color: h.errors24h > 0 ? "var(--red)" : "var(--grn)",
              }}
            >
              {h.errors24h}
            </div>
            <div style={{ fontSize: 9, letterSpacing: "0.08em", color: "var(--text-8)" }}>
              ERRORS 24h
            </div>
          </div>
          <div style={{ ...panel, borderRadius: 8, padding: "8px 14px", textAlign: "center" }}>
            <div className="mono" style={{ fontSize: 18, fontWeight: 700, color: "var(--amb)" }}>
              {h.warnings24h}
            </div>
            <div style={{ fontSize: 9, letterSpacing: "0.08em", color: "var(--text-8)" }}>
              WARNINGS 24h
            </div>
          </div>
        </div>
      </div>

      <div style={{ display: "flex", gap: 14, alignItems: "flex-start" }}>
        {/* data feeds */}
        <div style={{ ...panel, flex: 1.3, overflow: "hidden" }}>
          <div style={panelHeader}>
            <span style={sectionTitle}>DATA FEEDS</span>
          </div>
          {h.feeds.map((f) => {
            const st = FEED_STATUS[f.status];
            return (
              <div
                key={f.name}
                style={{
                  display: "grid",
                  gridTemplateColumns: "20px 1fr 92px 84px",
                  gap: 10,
                  padding: "11px 15px",
                  borderBottom: "1px solid var(--border-rail)",
                  alignItems: "center",
                }}
              >
                <span
                  style={{
                    width: 9,
                    height: 9,
                    borderRadius: "50%",
                    background: st.color,
                    boxShadow: `0 0 7px ${st.color}`,
                  }}
                />
                <div>
                  <div style={{ fontWeight: 600, fontSize: 13 }}>{f.name}</div>
                  <div style={{ fontSize: 10, color: "var(--text-7)" }}>{f.role}</div>
                </div>
                <div style={{ textAlign: "right" }}>
                  <div className="mono" style={{ fontSize: 11, color: "var(--text-3)" }}>
                    {f.latency}
                  </div>
                  <div className="mono" style={{ fontSize: 9, color: "var(--text-9)" }}>
                    {f.last}
                  </div>
                </div>
                <div style={{ textAlign: "right" }}>
                  <span
                    className="mono"
                    style={{
                      fontSize: 9.5,
                      fontWeight: 700,
                      letterSpacing: "0.06em",
                      color: st.color,
                    }}
                  >
                    {st.label}
                  </span>
                </div>
              </div>
            );
          })}
        </div>

        {/* host + watchdog */}
        <div style={{ flex: 1, display: "flex", flexDirection: "column", gap: 14 }}>
          <div style={{ ...panel, padding: "14px 16px" }}>
            <div style={{ ...sectionTitle, marginBottom: 12 }}>HOST</div>
            <div
              className="mono"
              style={{ display: "flex", flexDirection: "column", gap: 9, fontSize: 12 }}
            >
              <div style={{ display: "flex", justifyContent: "space-between" }}>
                <span style={{ color: "var(--text-6)" }}>Sleep-prevention</span>
                <span style={{ color: "var(--grn)", fontWeight: 600 }}>{h.host.sleepPrevention}</span>
              </div>
              <div style={{ display: "flex", justifyContent: "space-between" }}>
                <span style={{ color: "var(--text-6)" }}>Uptime</span>
                <span>{h.host.uptime}</span>
              </div>
              <div style={{ display: "flex", justifyContent: "space-between" }}>
                <span style={{ color: "var(--text-6)" }}>CPU / mem</span>
                <span>
                  {h.host.cpu} · {h.host.mem}
                </span>
              </div>
            </div>
          </div>

          <div style={{ ...panel, padding: "14px 16px" }}>
            <div style={{ ...sectionTitle, marginBottom: 10 }}>WATCHDOG STREAM</div>
            <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
              <span style={{ fontSize: 16, color: wd.color }}>⚡</span>
              <span className="mono" style={{ fontSize: 13, fontWeight: 700, color: wd.color }}>
                {wd.label}
              </span>
            </div>
            <div style={{ fontSize: 11, color: "var(--text-6)", marginTop: 8, lineHeight: 1.5 }}>
              Streaming exit monitor reacts in ~1s. Falls back to 5-min cycle exits if the stream
              degrades.
            </div>
          </div>
        </div>
      </div>

      <div style={{ display: "flex", gap: 14, alignItems: "flex-start" }}>
        {/* earnings calendar */}
        <div style={{ ...panel, flex: 1, overflow: "hidden" }}>
          <div style={panelHeader}>
            <span style={sectionTitle}>EARNINGS CALENDAR</span>{" "}
            <span className="mono" style={{ fontSize: 9.5, color: "var(--text-9)" }}>
              watchlist · next report
            </span>
          </div>
          {h.earnings.map((e) => {
            const tag = EARNINGS_TAG[e.state];
            return (
              <div
                key={e.ticker}
                style={{
                  display: "grid",
                  gridTemplateColumns: "60px 110px 1fr",
                  gap: 10,
                  padding: "9px 15px",
                  borderBottom: "1px solid var(--border-rail)",
                  alignItems: "center",
                }}
              >
                <span className="mono" style={{ fontWeight: 700, fontSize: 12 }}>
                  {e.ticker}
                </span>
                <span className="mono" style={{ fontSize: 11, color: "var(--text-4)" }}>
                  {e.date}
                </span>
                <span
                  className="mono"
                  style={{
                    justifySelf: "end",
                    fontSize: 9,
                    fontWeight: 700,
                    letterSpacing: "0.05em",
                    padding: "2px 8px",
                    borderRadius: 4,
                    background: tag.bg,
                    color: tag.color,
                  }}
                >
                  {tag.label}
                </span>
              </div>
            );
          })}
        </div>

        {/* scheduled jobs */}
        <div style={{ ...panel, flex: 1, overflow: "hidden" }}>
          <div style={panelHeader}>
            <span style={sectionTitle}>SCHEDULED JOBS</span>
          </div>
          {h.jobs.map((j) => {
            const st = JOB_STATUS[j.status];
            return (
              <div
                key={j.name}
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: 12,
                  padding: "11px 15px",
                  borderBottom: "1px solid var(--border-rail)",
                }}
              >
                <div style={{ flex: 1 }}>
                  <div style={{ fontWeight: 600, fontSize: 12.5 }}>{j.name}</div>
                  <div className="mono" style={{ fontSize: 10, color: "var(--text-7)", marginTop: 2 }}>
                    {j.detail}
                  </div>
                </div>
                <span className="mono" style={{ fontSize: 10, color: "var(--text-6)" }}>
                  {j.last}
                </span>
                <span
                  className="mono"
                  style={{
                    fontSize: 9.5,
                    fontWeight: 700,
                    padding: "2px 8px",
                    borderRadius: 4,
                    background: "rgba(0,0,0,0.25)",
                    color: st.color,
                  }}
                >
                  {st.label}
                </span>
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}
