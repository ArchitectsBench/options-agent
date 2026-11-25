// E2E responsiveness test (real Chromium, real click semantics).
// Run: node e2e/toggle-responsiveness.mjs [url]   (default http://localhost:8421/)
// MUST also be run against the DEV server (:8420): the 2026-07-31 StrictMode
// stuck-busy bug ONLY manifested in dev — prod certified a broken cockpit.
// Origin: real-browser repro of the user's report: "click FUNNEL/BLENDED one after
// the other 2-3 times and it stops responding."
// Drives actual Chromium against the served app with REAL click semantics
// (true e.detail counting, real event timing). Captures console errors.
import { chromium } from "playwright";

const URL = process.argv[2] || "http://localhost:8421/";
const browser = await chromium.launch();
const page = await browser.newPage();

const consoleErrors = [];
page.on("console", (m) => { if (m.type() === "error") consoleErrors.push(m.text().slice(0, 300)); });
page.on("pageerror", (e) => consoleErrors.push("PAGEERROR: " + String(e).slice(0, 300)));

await page.goto(URL, { waitUntil: "networkidle" });
await page.waitForTimeout(1500); // let WS connect + first feed land

const funnel = page.getByRole("button", { name: "FUNNEL ONLY" }).first();
const blended = page.getByRole("button", { name: "BLENDED" }).first();
console.log("buttons found:", await funnel.count(), await blended.count());

async function pressedState() {
  return {
    funnel: await funnel.getAttribute("aria-pressed"),
    blended: await blended.getAttribute("aria-pressed"),
    funnelDisabled: await funnel.isDisabled(),
    blendedDisabled: await blended.isDisabled(),
  };
}

console.log("initial:", await pressedState());

// The user's repro: alternate fast, 3 rounds (6 clicks, ~120ms apart — well
// inside the double-click interval, exactly the "rapid" case).
let failures = 0;
for (let round = 1; round <= 3; round++) {
  await blended.click({ delay: 20 });
  await page.waitForTimeout(120);
  let s = await pressedState();
  if (s.blended !== "true") { failures++; console.log(`round ${round}: BLENDED click DID NOT take`, s); }
  else console.log(`round ${round}: BLENDED ok`, s);

  await funnel.click({ delay: 20 });
  await page.waitForTimeout(120);
  s = await pressedState();
  if (s.funnel !== "true") { failures++; console.log(`round ${round}: FUNNEL click DID NOT take`, s); }
  else console.log(`round ${round}: FUNNEL ok`, s);
}

// then truly rapid: no waits at all, 6 alternating clicks back-to-back
for (let i = 0; i < 3; i++) { await blended.click(); await funnel.click(); }
await page.waitForTimeout(300);
const final = await pressedState();
console.log("after burst (ended on FUNNEL):", final);
if (final.funnel !== "true") { failures++; console.log("BURST: final FUNNEL state wrong"); }

// is anything now stuck-disabled?
if (final.funnelDisabled || final.blendedDisabled) { failures++; console.log("STUCK DISABLED detected"); }

console.log("\nconsole errors during run:", consoleErrors.length ? consoleErrors : "none");
console.log(failures === 0 ? "\nVERDICT: PASS — toggles respond in a real browser" : `\nVERDICT: FAIL — ${failures} unresponsive interactions REPRODUCED`);
await browser.close();
process.exit(failures === 0 ? 0 : 1);
