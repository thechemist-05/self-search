#!/usr/bin/env python
"""
Browser-level UI checks for the Self Search frontend (Stage 1), in headless Chrome.

Drives Chrome over the DevTools protocol with packages already installed
(``websockets``, ``requests``); no Playwright/Selenium. Run it against an API and
UI that are already running (it only reads; chat sessions live in memory).

At every viewport it:
  * runs a real-click scenario: search, compare two products, ask-about +
    cancel, then checks the layout (no sidebar, compact filter row under the
    search box, no profile controls) and counts real product images;
  * audits each state: no horizontal overflow; every visible control is the
    top-most element at its own centre (nothing - including the decorative
    layer - covers it); no control clipped off-screen; no overlapping buttons;
    no truncated button labels;
  * walks keyboard focus with Tab and checks every focused control shows a
    visible focus indicator;
  * checks there is no decorative layer over content (none since the 2026-10-22
    redesign) and that prefers-reduced-motion stops animation;
  * on desktop, measures idle main-thread CPU with and without the animation;
  * saves screenshots.

Usage:
    python scripts/ui_check.py --url http://127.0.0.1:8602 --out <dir>
Exit code 1 if any check fails.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

import requests
import websockets

CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    "/usr/bin/google-chrome", "/usr/bin/chromium", "/usr/bin/chromium-browser",
]

# name, CSS width, CSS height, device pixel ratio, mobile
VIEWPORTS = [
    ("desktop-1440", 1440, 900, 1, False),
    ("tablet-834", 834, 1112, 2, False),
    ("mobile-390", 390, 844, 3, True),
    ("reflow-320", 320, 760, 2, True),
    ("zoom200-1280", 640, 450, 2, False),  # a 1280x900 window at 200% browser zoom = 640x450 CSS px
]

HELPERS_JS = r"""
window.__ss = {
  idle() { const a = document.querySelector('[data-testid="stApp"]');
           return !!a && a.getAttribute('data-test-script-state') !== 'running'; },
  visible(el) {
    if (!el || !el.isConnected) return false;
    const r = el.getBoundingClientRect(); if (r.width < 2 || r.height < 2) return false;
    if (el.checkVisibility && !el.checkVisibility({visibilityProperty: true})) return false;
    return true;
  },
  sidebarHidden(el) { const sb = el.closest('[data-testid="stSidebar"]');
    return !!sb && (sb.getAttribute('aria-expanded') === 'false' || sb.getBoundingClientRect().right <= 1); },
  overlaySidebar() {
    // Below Streamlit's sidebar breakpoint an open sidebar overlays the main pane.
    const sb = document.querySelector('[data-testid="stSidebar"]'), main = document.querySelector('.stMain');
    if (!sb || !main || sb.getAttribute('aria-expanded') === 'false') return false;
    const r = sb.getBoundingClientRect(); return r.right > 50 && main.getBoundingClientRect().left < r.right - 1;
  },
  // Scroll el to the centre, then (if something sticky covers it) to 30% and 70% of the viewport.
  reach(el) {
    const H = innerHeight;
    for (const frac of [null, 0.3, 0.7]) {
      if (frac === null) el.scrollIntoView({block: 'center', inline: 'nearest'});
      else { const r0 = el.getBoundingClientRect(); const sc = el.closest('[data-testid="stSidebarContent"]') || document.querySelector('.stMain');
             if (sc) sc.scrollBy(0, r0.top + r0.height / 2 - H * frac); }
      const r = el.getBoundingClientRect();
      const x = Math.min(Math.max(r.left + r.width / 2, 1), innerWidth - 1), y = Math.min(Math.max(r.top + r.height / 2, 1), H - 1);
      const hit = document.elementFromPoint(x, y);
      if (hit && (hit === el || el.contains(hit))) return {x, y, hit: true, r};
      if (frac === 0.7) return {x, y, hit: false, r, by: hit};
    }
  },
  controls() {
    const overlay = this.overlaySidebar();
    const sel = 'button, input:not([type=hidden]):not([type=file]), textarea, summary, [role="tab"]';
    const out = [];
    for (let el of document.querySelectorAll(sel)) {
      if (el.matches('input[type=checkbox]')) el = el.closest('label') || el;   // the visible part
      if (out.includes(el) || !this.visible(el) || this.sidebarHidden(el)) continue;
      if (overlay && !el.closest('[data-testid="stSidebar"]')) continue;
      out.push(el);
    }
    return out;
  },
  name(el) { return ((el.getAttribute('aria-label') || el.innerText || el.value || el.placeholder ||
                      el.getAttribute('data-testid') || el.tagName) + '').replace(/\s+/g, ' ').trim().slice(0, 70); },
  audit() {
    const W = innerWidth, H = innerHeight, problems = [];
    const de = document.documentElement;
    const docOver = Math.max(de.scrollWidth, document.body.scrollWidth) - W;
    if (docOver > 1) problems.push(`page overflows horizontally by ${docOver}px`);
    for (const sc of document.querySelectorAll('.stMain, [data-testid="stMainBlockContainer"]')) {
      const o = sc.scrollWidth - sc.clientWidth; if (o > 1) problems.push(`${sc.dataset.testid || sc.className.split(" ")[0]} overflows horizontally by ${o}px`);
    }
    const ctrls = this.controls();
    // Overlap: measured in one pass, no scrolling in between.
    const btns = ctrls.filter(e => e.tagName === 'BUTTON').map(e => [e, e.getBoundingClientRect()]);
    for (let i = 0; i < btns.length; i++) for (let j = i + 1; j < btns.length; j++) {
      const [a, ra] = btns[i], [b, rb] = btns[j];
      if (a.contains(b) || b.contains(a)) continue;
      // The fixed header sits above scrolled content by design; real covering is caught below.
      if (a.closest('[data-testid="stHeader"]') || b.closest('[data-testid="stHeader"]')) continue;
      const w = Math.min(ra.right, rb.right) - Math.max(ra.left, rb.left), h = Math.min(ra.bottom, rb.bottom) - Math.max(ra.top, rb.top);
      if (w > 2 && h > 2) problems.push(`overlap: "${this.name(a)}" / "${this.name(b)}"`);
    }
    for (const el of ctrls) {
      const {r, hit: reached, by: hit} = this.reach(el), nm = this.name(el);
      if (r.left < -1 || r.right > W + 1) problems.push(`clipped: "${nm}" spans ${Math.round(r.left)}..${Math.round(r.right)} of ${W}`);
      if (!reached)
        problems.push(`covered: "${nm}" -> ${hit ? (hit.getAttribute('data-testid') || hit.tagName + '.' + (hit.className + '').slice(0, 40)) : 'nothing'}`);
      if (el.tagName === 'BUTTON') {
        if (!nm) problems.push('button without an accessible label');
      }
      if (el.tagName === 'BUTTON' || el.tagName === 'LABEL')
        for (const t of el.querySelectorAll('p')) if (t.scrollWidth > t.clientWidth + 1) problems.push(`label truncated: "${nm}"`);
    }
    problems.push(...this.contrast());
    const app = document.querySelector('[data-testid="stApp"]'), decor = {};
    // A decorative layer, if one is ever added back, must never sit above or capture content.
    for (const p of ['::before', '::after']) {
      const cs = getComputedStyle(app, p);
      if (cs.content === 'none' || cs.content === 'normal') { decor[p] = 'none'; continue; }
      decor[p] = {pointerEvents: cs.pointerEvents, zIndex: cs.zIndex, position: cs.position, animation: cs.animationName, userSelect: cs.userSelect};
      if (cs.pointerEvents !== 'none') problems.push(`decor ${p} captures pointer events`);
      if (cs.zIndex !== '-1') problems.push(`decor ${p} z-index is ${cs.zIndex}`);
      if (cs.position !== 'fixed') problems.push(`decor ${p} is not fixed`);
    }
    return {controls: ctrls.length, problems, decor};
  },
  // Rendered text contrast: text colour x every ancestor's opacity, over the nearest opaque background.
  // Disabled controls are exempt (WCAG 1.4.3). The decorative layer is covered by the unit tests.
  contrast() {
    const rgb = c => { const m = c.match(/[\d.]+/g); return m ? m.map(Number) : null; };
    const lum = ([r, g, b]) => [r, g, b].map(v => { v /= 255; return v <= .03928 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4; })
                                     .reduce((a, v, i) => a + v * [.2126, .7152, .0722][i], 0);
    const out = new Set(), seen = new Set();
    for (const el of document.querySelectorAll('p, span, label, td, th, h1, h2, h3, h4, li, summary, a, small, code')) {
      if (!this.visible(el) || this.sidebarHidden(el) || el.closest('[disabled], [aria-disabled="true"], [data-testid="stHeader"]')) continue;
      if (![...el.childNodes].some(n => n.nodeType === 3 && n.textContent.trim())) continue;
      const cs = getComputedStyle(el), fg = rgb(cs.color); if (!fg) continue;
      let alpha = fg[3] ?? 1, bg = null;
      for (let x = el; x; x = x.parentElement) {
        const xs = getComputedStyle(x); alpha *= parseFloat(xs.opacity);
        const b = rgb(xs.backgroundColor); if (!bg && b && (b[3] ?? 1) > .99) bg = b;
      }
      bg = bg || [255, 248, 236];
      const mix = fg.slice(0, 3).map((v, i) => v * alpha + bg[i] * (1 - alpha));
      const [l1, l2] = [lum(mix), lum(bg)].sort((a, b) => b - a), ratio = (l1 + .05) / (l2 + .05);
      const large = parseFloat(cs.fontSize) >= 24 || (parseFloat(cs.fontSize) >= 18.66 && parseInt(cs.fontWeight) >= 700);
      const key = this.name(el).slice(0, 40);
      if (ratio < (large ? 3 : 4.5) && !seen.has(key)) { seen.add(key); out.add(`low text contrast ${ratio.toFixed(2)}: "${key}"`); }
    }
    return [...out];
  },
  focusInfo() {
    const el = document.activeElement;
    if (!el || el === document.body) return {none: true};
    const cands = [el, el.closest('label'), el.closest('[data-baseweb="input"]'), el.closest('[data-baseweb="select"] > div'),
                   el.closest('[data-testid="stChatInput"] > div')].filter(Boolean);
    let ok = false;
    for (const c of cands) { const cs = getComputedStyle(c);
      if ((cs.outlineStyle !== 'none' && parseFloat(cs.outlineWidth) >= 2) || (cs.boxShadow && cs.boxShadow !== 'none')) ok = true; }
    return {name: this.name(el), tag: el.tagName, ok, inSidebar: !!el.closest('[data-testid="stSidebar"]')};
  },
  // First *visible* match: on touch devices Streamlit also renders a hidden copy of buttons with tooltips.
  q(sel) { return [...document.querySelectorAll(sel)].find(e => this.visible(e)) || null; },
  byText(sel, text) { return [...document.querySelectorAll(sel)].find(e => this.visible(e) && !this.sidebarHidden(e) && e.innerText.replace(/\s+/g, ' ').includes(text)); },
};
"""


class CheckFailed(Exception):
    pass


class Page:
    def __init__(self, ws: Any) -> None:
        self.ws, self.n, self.pending = ws, 0, {}
        self.reader = asyncio.create_task(self._read())

    async def _read(self) -> None:
        async for msg in self.ws:
            data = json.loads(msg)
            fut = self.pending.pop(data.get("id"), None)
            if fut and not fut.done():
                fut.set_result(data)

    async def send(self, method: str, **params: Any) -> dict:
        self.n += 1
        fut = asyncio.get_running_loop().create_future()
        self.pending[self.n] = fut
        await self.ws.send(json.dumps({"id": self.n, "method": method, "params": params}))
        res = await asyncio.wait_for(fut, 90)
        if "error" in res:
            raise RuntimeError(f"{method}: {res['error']}")
        return res.get("result", {})

    async def js(self, expr: str) -> Any:
        r = await self.send("Runtime.evaluate", expression=expr, returnByValue=True, awaitPromise=True)
        if "exceptionDetails" in r:
            raise RuntimeError(f"JS error: {r['exceptionDetails'].get('exception', {}).get('description', r['exceptionDetails'])}")
        return r["result"].get("value")

    async def wait(self, expr: str, what: str, timeout: float = 45) -> None:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                if await self.js(f"(() => {{ try {{ return !!window.__ss && __ss.idle() && !!({expr}); }} catch (e) {{ return false; }} }})()"):
                    await asyncio.sleep(0.4)
                    return
            except RuntimeError:
                pass
            await asyncio.sleep(0.25)
        raise CheckFailed(f"timed out waiting for: {what}")

    async def click(self, el_expr: str, what: str) -> None:
        info = None
        for _ in range(8):
            info = await self.js(f"(() => {{ const el = ({el_expr}); if (!el) return null; const i = __ss.reach(el);"
                                 " return {x: i.x, y: i.y, hit: i.hit}; })()")
            if info and info["hit"]:
                break
            await asyncio.sleep(0.4)
        if not info:
            raise CheckFailed(f"{what}: control not found")
        if not info["hit"]:
            raise CheckFailed(f"{what}: covered at its centre (a real click would miss)")
        for kind in ("mouseMoved", "mousePressed", "mouseReleased"):
            await self.send("Input.dispatchMouseEvent", type=kind, x=info["x"], y=info["y"], button="left", clickCount=1)
        await asyncio.sleep(0.5)

    async def type(self, text: str) -> None:
        await self.send("Input.insertText", text=text)

    async def key(self, key: str, code: str, vk: int) -> None:
        for kind in ("keyDown", "keyUp"):
            await self.send("Input.dispatchKeyEvent", type=kind, key=key, code=code, windowsVirtualKeyCode=vk)

    async def shot(self, path: Path) -> None:
        await asyncio.sleep(0.6)
        data = await self.send("Page.captureScreenshot", format="png")
        path.write_bytes(base64.b64decode(data["data"]))


async def open_expander(page: Page, label: str) -> None:
    await page.click(f"[...document.querySelectorAll('summary')].find(s => __ss.visible(s) && s.innerText.includes({json.dumps(label)}))",
                     f"open expander '{label}'")


def button(text: str) -> str:
    return f"__ss.byText('button', {json.dumps(text)})"


# Seconds to wait for an answer (search results, comparison). With a real CPU-only model an
# answer can take up to the API's model budget, so pass --answer-wait 130 (the UI waits 120 s).
ANSWER_WAIT = 45.0


async def run_viewport(page: Page, url: str, vp: tuple, out: Path, report: dict, query: str = "wireless") -> None:
    name, w, h, dpr, mobile = vp
    res: dict[str, Any] = {"viewport": f"{w}x{h}@{dpr}x{' mobile' if mobile else ''}", "steps": [], "audits": {}, "focus": {}}
    report[name] = res
    await page.send("Emulation.setDeviceMetricsOverride", width=w, height=h, deviceScaleFactor=dpr, mobile=mobile)
    await page.send("Emulation.setEmulatedMedia", features=[{"name": "prefers-reduced-motion", "value": "no-preference"}])
    await page.send("Page.navigate", url=url)
    await asyncio.sleep(1.0)
    await page.wait("__ss.q('.ss-brand') && __ss.q('[class*=\"st-key-hero-query\"] input') "
                    "&& document.fonts.status === 'loaded'", "empty state")

    async def step(label: str, coro: Any) -> None:
        try:
            await coro
            res["steps"].append([label, "ok"])
        except Exception as e:  # record and continue with a screenshot of the failure
            res["steps"].append([label, f"FAILED: {e}"])
            await page.shot(out / f"{name}-FAILED-{len(res['steps'])}.png")

    async def audit(label: str) -> None:
        res["audits"][label] = await page.js("__ss.audit()")

    # 1. Empty state
    await audit("empty")
    await page.js("document.querySelector('.stMain')?.scrollTo(0, 0)")
    await page.shot(out / f"{name}-1-empty.png")

    # 2. Keyboard focus walk from the top of the page
    await page.js("document.activeElement && document.activeElement.blur()")
    seen, missing = [], []
    for _ in range(28):
        await page.key("Tab", "Tab", 9)
        await asyncio.sleep(0.12)
        info = await page.js("__ss.focusInfo()")
        if info.get("none"):
            continue
        seen.append(info["name"])
        if not info["ok"]:
            missing.append(info["name"])
    res["focus"] = {"tab_stops_visited": len(seen), "without_visible_indicator": sorted(set(missing))}

    # 3. Hero search with real typing and clicking
    async def search() -> None:
        await page.click("__ss.q('[class*=\"st-key-hero-query\"] input')", "hero search box")
        await page.type(query)
        await page.click(button("Search"), "Search button")
        await page.wait("document.querySelectorAll('[class*=\"st-key-card-\"]').length >= 2", "two result cards",
                        timeout=ANSWER_WAIT)
    started = time.perf_counter()
    await step("hero search -> results", search())
    res["seconds_to_cards"] = round(time.perf_counter() - started, 1)
    await audit("results")

    # 3a. Optional AI summary (desktop only: each viewport is a new session, so a new model call)
    async def summarise() -> None:
        if await page.js("!!document.querySelector('.ss-summary')"):
            raise CheckFailed("an AI summary was shown before it was requested")
        await page.click("__ss.q('[class*=\"st-key-summarise-\"] button')", "Summarise reviews")
        await page.wait("[...document.querySelectorAll('.ss-progress')].some(e => e.textContent.includes('Summarising'))",
                        "summary progress")
        if await page.js("document.querySelectorAll('[class*=\"st-key-card-\"]').length") < 2:
            raise CheckFailed("results disappeared while the summary runs")
        # The page stays usable: tick and untick a Compare box while the model works.
        await page.click("document.querySelectorAll('[class*=\"st-key-cmp-\"] label')[0]", "Compare checkbox (during summary)")
        await page.wait("document.querySelectorAll('[class*=\"st-key-cmp-\"] input:checked').length >= 1",
                        "checkbox ticked during summary")
        await page.click("document.querySelectorAll('[class*=\"st-key-cmp-\"] label')[0]", "untick")
        await page.wait("document.querySelectorAll('[class*=\"st-key-cmp-\"] input:checked').length == 0", "unticked")
        t0 = time.perf_counter()
        await page.wait("!!document.querySelector('.ss-summary') || !!document.querySelector('[class*=\"st-key-summarise-retry-\"]')",
                        "summary or retry", timeout=ANSWER_WAIT)
        res["summary_seconds_after_interaction"] = round(time.perf_counter() - t0, 1)
        if not await page.js("!!document.querySelector('.ss-summary')"):
            raise CheckFailed("the summary failed (retry shown)")
    if name.startswith("desktop"):
        await step("optional AI summary while the page stays usable", summarise())
        await audit("summary")
    await page.js("document.querySelector('[class*=\"st-key-card-\"]')?.scrollIntoView({block: 'start'})")
    await page.shot(out / f"{name}-2-results.png")

    # 4. Compare two products
    async def compare() -> None:
        await page.click("document.querySelectorAll('[class*=\"st-key-cmp-\"] label')[0]", "Compare checkbox 1")
        await page.wait("document.querySelectorAll('[class*=\"st-key-cmp-\"] input:checked').length >= 1", "first checkbox ticked")
        await page.click("document.querySelectorAll('[class*=\"st-key-cmp-\"] label')[1]", "Compare checkbox 2")
        await page.wait("__ss.q('[class*=\"st-key-compare-selected\"] button:not([disabled])')", "compare button enabled")
        await page.click("__ss.q('[class*=\"st-key-compare-selected\"] button')", "Compare selected")
        await page.wait("__ss.q('[data-testid=\"stTable\"]')", "comparison table", timeout=ANSWER_WAIT)
    await step("compare two products", compare())
    await audit("compare")
    await page.js("document.querySelector('[data-testid=\"stTable\"]')?.scrollIntoView({block: 'center'})")
    await page.shot(out / f"{name}-3-compare.png")

    # 5. Ask about a product, then cancel
    async def ask() -> None:
        await page.click("__ss.q('[class*=\"st-key-ask-\"] button')", "Ask about this")
        await page.wait("__ss.q('[class*=\"st-key-cancel-ask\"] button')", "ask-about banner")
        await page.click("__ss.q('[class*=\"st-key-cancel-ask\"] button')", "Cancel ask-about")
        await page.wait("!__ss.q('[class*=\"st-key-cancel-ask\"]')", "banner removed")
    await step("ask about + cancel", ask())

    # 6. Layout: no sidebar, filter chips directly under the search box, no profile controls
    async def layout() -> None:
        if await page.js("(() => { const sb = document.querySelector('[data-testid=\"stSidebar\"]');"
                         " return !!sb && sb.getBoundingClientRect().width > 1; })()"):
            raise CheckFailed("a sidebar is rendered")
        if await page.js("!!document.querySelector('[data-testid=\"stChatInput\"]')"):
            raise CheckFailed("a second (chat) input is rendered")
        if await page.js("!!document.querySelector('[class*=\"st-key-create-profile\"], [class*=\"st-key-open-profile\"]')"):
            raise CheckFailed("profile controls are still rendered")
        await page.js("document.querySelector('.stMain')?.scrollTo(0, 0)")
        order = await page.js(
            "(() => { const top = e => e ? e.getBoundingClientRect().top : null;"
            " const box = document.querySelector('[class*=\"st-key-hero-query\"]');"
            " const chip = document.querySelector('.ss-filter, [class*=\"st-key-chip-\"]');"
            " const answer = document.querySelector('.ss-query');"
            " return [top(box), top(chip), top(answer)]; })()")
        if order[1] is None:
            raise CheckFailed("no filter chips after a typed search")
        if not (order[0] < order[1] < order[2]):
            raise CheckFailed(f"filter row is not between the search box and the results: {order}")
        res["images"] = await page.js(
            "(() => { const imgs = [...document.querySelectorAll('[class*=\"st-key-card-\"] img')];"
            " return {product: imgs.filter(i => i.alt.startsWith('Product image of') && i.naturalWidth > 1).length,"
            " placeholder: imgs.filter(i => i.alt.startsWith('Image unavailable')).length}; })()")
    await step("layout: no sidebar, filter row under search", layout())
    await audit("layout")
    await page.shot(out / f"{name}-4-layout.png")

    # 7. Reduced motion
    await page.send("Emulation.setEmulatedMedia", features=[{"name": "prefers-reduced-motion", "value": "reduce"}])
    await asyncio.sleep(0.4)
    res["reduced_motion_animation"] = await page.js(
        "['::before','::after'].map(p => getComputedStyle(document.querySelector('[data-testid=\"stApp\"]'), p).animationName)")
    await page.send("Emulation.setEmulatedMedia", features=[{"name": "prefers-reduced-motion", "value": "no-preference"}])
    res["motion_animation"] = await page.js(
        "['::before','::after'].map(p => getComputedStyle(document.querySelector('[data-testid=\"stApp\"]'), p).animationName)")


async def idle_cpu(page: Page, seconds: float = 6.0) -> dict:
    async def task_duration() -> float:
        metrics = (await page.send("Performance.getMetrics"))["metrics"]
        return next(m["value"] for m in metrics if m["name"] == "TaskDuration")
    out = {}
    for label, value in (("animated", "no-preference"), ("reduced_motion", "reduce")):
        await page.send("Emulation.setEmulatedMedia", features=[{"name": "prefers-reduced-motion", "value": value}])
        await asyncio.sleep(1.0)
        a = await task_duration()
        await asyncio.sleep(seconds)
        out[label] = round(100 * (await task_duration() - a) / seconds, 2)
    await page.send("Emulation.setEmulatedMedia", features=[{"name": "prefers-reduced-motion", "value": "no-preference"}])
    return out  # % of one core spent on main-thread tasks while idle


def problems_of(report: dict) -> list[str]:
    out = []
    for vp, r in report.items():
        if not isinstance(r, dict) or "steps" not in r:
            continue
        out += [f"{vp}: step '{s}' {st}" for s, st in r["steps"] if st != "ok"]
        for state, a in r["audits"].items():
            out += [f"{vp}/{state}: {p}" for p in a["problems"]]
        out += [f"{vp}: focus without visible indicator: {n}" for n in r["focus"].get("without_visible_indicator", [])]
        if r.get("reduced_motion_animation") != ["none", "none"]:
            out.append(f"{vp}: reduced motion still animates: {r.get('reduced_motion_animation')}")
    return out


async def main_async(url: str, out: Path, chrome: str, query: str = "wireless") -> int:
    out.mkdir(parents=True, exist_ok=True)
    profile = Path(tempfile.mkdtemp(prefix="ss-chrome-"))
    port = 9333
    proc = subprocess.Popen([chrome, "--headless=new", f"--remote-debugging-port={port}", f"--user-data-dir={profile}",
                             "--no-first-run", "--no-default-browser-check", "--disable-extensions", "--hide-scrollbars",
                             "about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    report: dict[str, Any] = {"url": url}
    try:
        for _ in range(100):
            try:
                targets = requests.get(f"http://127.0.0.1:{port}/json/list", timeout=1).json()
                ws_url = next(t["webSocketDebuggerUrl"] for t in targets if t.get("type") == "page")
                break
            except Exception:
                time.sleep(0.2)
        else:
            raise RuntimeError("Chrome DevTools endpoint did not come up")
        async with websockets.connect(ws_url, max_size=None) as ws:
            page = Page(ws)
            for domain in ("Page", "Runtime", "Performance"):
                await page.send(f"{domain}.enable")
            await page.send("Page.addScriptToEvaluateOnNewDocument", source=HELPERS_JS)
            for vp in VIEWPORTS:
                print(f"-> {vp[0]} ...", flush=True)
                try:
                    await run_viewport(page, url, vp, out, report, query)
                except Exception as e:
                    report[vp[0]] = {"viewport": vp[0], "steps": [["viewport run", f"FAILED: {e}"]], "audits": {}, "focus": {}}
                if vp[0] == "desktop-1440":
                    await page.send("Emulation.setDeviceMetricsOverride", width=1440, height=900, deviceScaleFactor=1, mobile=False)
                    report["idle_cpu_percent_desktop"] = await idle_cpu(page)
    finally:
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(profile, ignore_errors=True)

    problems = problems_of(report)
    report["problems"] = problems
    (out / "ui_check_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    for vp, r in report.items():
        if isinstance(r, dict) and "steps" in r:
            ok = sum(1 for _, s in r["steps"] if s == "ok")
            counts = {k: v["controls"] for k, v in r["audits"].items()}
            print(f"{vp:14} steps {ok}/{len(r['steps'])} ok | controls audited {counts} | "
                  f"tab stops {r['focus'].get('tab_stops_visited')} | reduced-motion {r.get('reduced_motion_animation')} | "
                  f"images {r.get('images')} | cards in {r.get('seconds_to_cards')} s"
                  + (f" | summary +{r['summary_seconds_after_interaction']} s" if 'summary_seconds_after_interaction' in r else ""))
    print("idle CPU % (desktop):", report.get("idle_cpu_percent_desktop"))
    print(f"{len(problems)} problem(s)" + (":" if problems else ""))
    for p in problems:
        print("  -", p)
    return 1 if problems else 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Headless-Chrome UI checks for Self Search")
    parser.add_argument("--url", default="http://127.0.0.1:8602")
    parser.add_argument("--query", default="wireless", help="hero search text (must return >= 2 cards)")
    parser.add_argument("--answer-wait", type=float, default=ANSWER_WAIT,
                        help="seconds to wait for results and comparisons (real model: 130)")
    parser.add_argument("--out", default="ui_check_output")
    parser.add_argument("--chrome", default=None)
    args = parser.parse_args()
    chrome: Optional[str] = args.chrome or next((c for c in CHROME_CANDIDATES if Path(c).exists()), None)
    if not chrome:
        sys.exit("Chrome/Edge not found; pass --chrome")
    globals()["ANSWER_WAIT"] = args.answer_wait
    sys.exit(asyncio.run(main_async(args.url, Path(args.out), chrome, args.query)))


if __name__ == "__main__":
    main()
