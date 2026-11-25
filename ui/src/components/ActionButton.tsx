import { forwardRef, useCallback, useEffect, useRef, useState } from "react";

/**
 * The single interactive-control primitive for the cockpit. Bakes in, once, the
 * things every control needs so the class of bug (double-click text selection →
 * macOS/Outlook data-detector, double-fire on mutations) can't recur:
 *
 *  - semantic <button> with a full CSS reset (.ui-control) for pixel parity
 *  - non-selectable + mousedown-default suppressed on 2nd+ click (no dbl-click
 *    text selection), while single-click focus and keyboard focus still work
 *  - drops the 2nd+ click of a multi-click at the onClick level (event.detail>1)
 *  - disables itself while an async onAct is in flight (returns a Promise)
 *  - optional post-action cooldown for state-toggling controls (e.g. kill=750ms)
 *  - keyboard: real button → Tab reaches it, Enter/Space activate, :focus-visible
 *
 * onAct may be sync or async. If it returns a Promise the button stays disabled
 * until it settles; then (and for sync actions) an optional cooldownMs lockout
 * applies before it can fire again.
 */
type ActionButtonProps = {
  onAct: (e: React.MouseEvent<HTMLButtonElement>) => void | Promise<void>;
  /** post-action lockout in ms — use on state toggles to stop flip-flop (kill = 750) */
  cooldownMs?: number;
  /**
   * Allow rapid successive clicks (2026-07-31): the default multi-click guard
   * drops any click the browser counts as part of a double/triple-click
   * (e.detail > 1) — right for MUTATION buttons (double-fire protection), but
   * on instant view toggles (FUNNEL/BLENDED, tabs, theme) rapid re-clicking is
   * legitimate and the guard read as "button stopped responding". Set true on
   * pure view/state toggles; leave false on anything that mutates or POSTs.
   */
  allowRapid?: boolean;
  disabled?: boolean;
  title?: string;
  className?: string;
  style?: React.CSSProperties;
  children: React.ReactNode;
  /** toggle semantics for AT: aria-pressed for on/off controls */
  "aria-pressed"?: boolean;
  "aria-label"?: string;
  "aria-current"?: boolean | "page";
  "aria-expanded"?: boolean;
};

export const ActionButton = forwardRef<HTMLButtonElement, ActionButtonProps>(
  function ActionButton({ onAct, cooldownMs = 0, allowRapid = false, disabled, className, style, children, ...aria }, ref) {
    const [busy, setBusy] = useState(false);
    const lockRef = useRef(false); // synchronous re-entry guard (same-tick double fire)
    const mounted = useRef(true);
    // StrictMode-safe (fixed 2026-07-31, THE "every button dies after one
    // click" bug): the effect must re-arm the flag in its BODY. The previous
    // cleanup-only form left mounted.current=false after StrictMode's dev-only
    // mount→unmount→remount, so release() skipped setBusy(false) and every
    // ActionButton in the dev cockpit (:8420) stuck disabled after its first
    // click. Prod builds don't double-invoke, which is why :8421 was immune.
    useEffect(() => {
      mounted.current = true;
      return () => { mounted.current = false; };
    }, []);

    const handleClick = useCallback(
      (e: React.MouseEvent<HTMLButtonElement>) => {
        if (!allowRapid && e.detail > 1) return;        // drop 2nd+ click of a multi-click (mutation guard)
        if (disabled || busy || lockRef.current) return; // in-flight / cooldown guard
        lockRef.current = true;
        setBusy(true);

        const release = () => {
          lockRef.current = false;
          if (mounted.current) setBusy(false);
        };
        const finish = () => (cooldownMs > 0 ? window.setTimeout(release, cooldownMs) : release());

        let result: void | Promise<void>;
        try {
          result = onAct(e);
        } catch {
          finish();
          return;
        }
        if (result && typeof (result as Promise<void>).then === "function") {
          (result as Promise<void>).then(finish, finish);
        } else {
          finish();
        }
      },
      [onAct, cooldownMs, allowRapid, disabled, busy],
    );

    return (
      <button
        ref={ref}
        type="button"
        disabled={disabled || busy}
        // Block text-selection on the 2nd+ click without stealing single-click
        // focus; user-select:none (.ui-control) covers the rest.
        onMouseDown={(e) => { if (e.detail > 1) e.preventDefault(); }}
        onClick={handleClick}
        className={`ui-control${className ? " " + className : ""}`}
        style={style}
        {...aria}
      >
        {children}
      </button>
    );
  },
);
