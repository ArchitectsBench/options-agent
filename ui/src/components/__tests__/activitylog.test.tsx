/**
 * Activity-log kinds (2026-09-14). The server added a "v2" log kind on 09-12
 * (SCALP-v2 flow events) that the UI had no style for; the first v2 event of
 * the next session threw "reading 'accent'" and blanked the whole dashboard
 * panel. These tests pin both halves: v2 has its own style, and a kind the UI
 * has never heard of degrades to a neutral style instead of crashing.
 */
import { describe, expect, it } from "vitest";
import { tagStyle } from "../ActivityLog";

describe("activity log tag styles", () => {
  it("styles the SCALP-v2 kind the server emits", () => {
    expect(tagStyle("v2").accent).toBeTruthy();
    expect(tagStyle("v2")).not.toEqual(tagStyle("safety")); // flow, not an incident
  });

  it("never throws on a kind the UI does not know yet", () => {
    const s = tagStyle("some_future_kind");
    expect(s.accent).toBeTruthy();
    expect(s).toEqual(tagStyle("cycle"));
  });
});
