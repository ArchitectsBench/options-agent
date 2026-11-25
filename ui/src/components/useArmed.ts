import { useCallback, useEffect, useRef, useState } from "react";

/**
 * Two-step-confirm state for a destructive action (kill-switch RELEASE).
 * First activation arms; a second within `timeoutMs` confirms. Auto-disarms
 * so a stray first click can't leave the control primed indefinitely.
 * Keyboard-friendly by design (two discrete Enter/Space presses), which is why
 * two-step confirm was chosen over press-and-hold for RELEASE.
 */
export function useArmed(timeoutMs = 3000) {
  const [armed, setArmed] = useState(false);
  const timer = useRef<number | undefined>(undefined);

  const disarm = useCallback(() => {
    if (timer.current) window.clearTimeout(timer.current);
    setArmed(false);
  }, []);

  const arm = useCallback(() => {
    setArmed(true);
    if (timer.current) window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => setArmed(false), timeoutMs);
  }, [timeoutMs]);

  useEffect(() => () => { if (timer.current) window.clearTimeout(timer.current); }, []);

  return { armed, arm, disarm };
}
