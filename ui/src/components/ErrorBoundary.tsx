import { Component, type ReactNode } from "react";

// Guards each page so a single render exception degrades to a visible fallback
// instead of unmounting the whole cockpit (the "frozen, blank" failure mode).
// Keyed by `page` in App so switching tabs resets a tab that had errored.
type Props = { children: ReactNode };
type State = { error: Error | null };

export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: unknown) {
    // Surface it in the console for diagnosis; the UI stays alive.
    console.error("[cockpit] page render error:", error, info);
  }

  render() {
    if (this.state.error) {
      return (
        <div style={{ flex: 1, padding: 24 }}>
          <div
            style={{
              border: "1px solid var(--red)",
              borderRadius: 10,
              background: "rgba(255,90,95,0.06)",
              padding: "18px 20px",
              maxWidth: 620,
            }}
          >
            <div style={{ fontSize: 13, fontWeight: 700, color: "var(--red)", marginBottom: 6 }}>
              This panel hit a render error
            </div>
            <div className="mono" style={{ fontSize: 11, color: "var(--text-4)", lineHeight: 1.5 }}>
              {this.state.error.message}
            </div>
            <div style={{ fontSize: 11, color: "var(--text-7)", marginTop: 10 }}>
              The rest of the cockpit is unaffected — switch tabs and back, or reload.
            </div>
          </div>
        </div>
      );
    }
    return this.props.children;
  }
}
