"""Visual identity for the Self Search UI: an editorial, product-first look.

Warm ivory canvas, navy-charcoal text, one restrained cobalt accent, white product
surfaces with large uniform imagery, and a serif display face for headings.
No decorative background shapes or ambient animation: the products are the visuals.

One colour = one job:
  cobalt   -> actions, links, focus
  coral    -> problems: failed validation, errors
  mustard  -> caution: synthetic price estimates, fallback answers, "not applied"
  mint     -> verified: requirements met, supported claims, success
  lavender -> quiet accent
  cream    -> ivory canvas; white "paper" for cards and inputs; ink for text

Fonts are the ones Streamlit already bundles and serves locally (OFL):
Source Serif 4 (display), Source Sans 3 (body/controls), Source Code Pro (labels).

Every text/background pair is in ``TEXT_PAIRS`` (WCAG AA 4.5:1) and every
non-text UI pair in ``UI_PAIRS`` (3:1); both are enforced by the test suite.
"""

from __future__ import annotations

from zlib import crc32

PALETTE = {
    "ink": "#1B1F2B",
    "ink_muted": "#545A6B",
    "cream": "#F8F5EF",          # ivory canvas
    "cream_deep": "#F1ECE3",     # image wells, quiet panels
    "paper": "#FFFFFF",
    "line": "#E4DDD2",           # hairline card outlines (not a control boundary)
    "widget_border": "#867E72",  # input/control boundaries (>= 3:1)
    "disabled_bg": "#ECE7DE",
    "cobalt": "#2346D1",
    "cobalt_deep": "#1A35A3",
    "cobalt_tint": "#E9EDFB",
    "coral": "#E8735F",
    "coral_deep": "#AE3426",
    "coral_tint": "#FCE9E5",
    "mustard": "#E3AA2B",
    "mustard_deep": "#6B4B00",
    "mustard_tint": "#FBF0D6",
    "mint": "#6FC9A6",
    "mint_deep": "#0E6544",
    "mint_tint": "#E2F4EC",
    "lavender": "#A99AE6",
    "lavender_deep": "#5A3DB5",
    "lavender_tint": "#EFEBFB",
    "white": "#FFFFFF",
}

# (foreground, background) pairs that carry text -> must be >= 4.5:1.
TEXT_PAIRS = [
    ("ink", "cream"), ("ink", "cream_deep"), ("ink", "paper"), ("ink_muted", "cream"),
    ("ink_muted", "cream_deep"), ("ink_muted", "paper"), ("ink_muted", "disabled_bg"),
    ("white", "ink"), ("white", "cobalt"), ("white", "cobalt_deep"), ("white", "coral_deep"), ("cobalt_deep", "paper"),
    ("cobalt_deep", "cobalt_tint"), ("cobalt", "paper"), ("cobalt", "cream"),
    ("coral_deep", "coral_tint"), ("coral_deep", "paper"),
    ("mustard_deep", "mustard_tint"), ("mustard_deep", "cream"), ("mustard_deep", "paper"),
    ("mint_deep", "mint_tint"), ("mint_deep", "paper"),
    ("lavender_deep", "lavender_tint"), ("lavender_deep", "paper"), ("lavender_deep", "cream_deep"),
    ("ink", "cobalt_tint"), ("ink", "lavender_tint"), ("ink", "mustard_tint"), ("ink", "mint_tint"),
    ("ink", "coral_tint"),
]
# Non-text UI: control boundaries and the focus ring -> must be >= 3:1.
UI_PAIRS = [
    ("widget_border", "paper"), ("widget_border", "cream"), ("widget_border", "cream_deep"),
    ("cobalt_deep", "cream"), ("cobalt_deep", "paper"), ("cobalt_deep", "cream_deep"), ("cobalt", "paper"),
]

# Category -> accent name (small tints only). Fixed for known catalogue categories;
# anything else gets a stable choice from the rest.
CATEGORY_ACCENTS = {"electronics": "cobalt", "sports_and_outdoors": "mint"}
_ACCENT_CYCLE = ("lavender", "mustard", "coral", "cobalt", "mint")


def category_accent(category: str | None) -> str:
    """Accent colour name for a product category (stable, deterministic)."""
    key = (category or "").strip().lower().replace(" ", "_")
    if not key:
        return "cobalt"
    return CATEGORY_ACCENTS.get(key) or _ACCENT_CYCLE[crc32(key.encode()) % len(_ACCENT_CYCLE)]


# ---------------------------------------------------------------------------
# Contrast
# ---------------------------------------------------------------------------

def _rgb(hex_colour: str) -> tuple[int, int, int]:
    h = hex_colour.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _luminance(hex_colour: str) -> float:
    lin = []
    for c in _rgb(hex_colour):
        v = c / 255
        lin.append(v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4)
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def contrast_ratio(fg: str, bg: str) -> float:
    """WCAG 2.1 contrast ratio between two hex colours."""
    hi, lo = sorted((_luminance(fg), _luminance(bg)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def blend(fg: str, bg: str, alpha: float) -> str:
    """Colour of ``fg`` painted at ``alpha`` opacity over ``bg``."""
    f, b = _rgb(fg), _rgb(bg)
    return "#%02X%02X%02X" % tuple(round(alpha * x + (1 - alpha) * y) for x, y in zip(f, b))


P = PALETTE

_BUTTON = "button[data-testid^='stBaseButton']"
_PRIMARY = "button[data-testid='stBaseButton-primary'], button[data-testid='stBaseButton-primaryFormSubmit']"
_SECONDARY = ("button[data-testid='stBaseButton-secondary'], button[data-testid='stBaseButton-secondaryFormSubmit'], "
              "[data-testid='stDownloadButton'] button")


def _key(prefix: str) -> str:
    """Selector for buttons inside widgets whose key starts with ``prefix`` (Streamlit adds st-key-<key>)."""
    return f"[class*='st-key-{prefix}'] button"


CSS = f"""
<style>
:root {{
  --ss-ink: {P['ink']}; --ss-ink-muted: {P['ink_muted']}; --ss-cream: {P['cream']}; --ss-cream-deep: {P['cream_deep']};
  --ss-paper: {P['paper']}; --ss-line: {P['line']}; --ss-border: {P['widget_border']};
  --ss-cobalt: {P['cobalt']}; --ss-cobalt-deep: {P['cobalt_deep']}; --ss-cobalt-tint: {P['cobalt_tint']};
  --ss-coral: {P['coral']}; --ss-coral-deep: {P['coral_deep']}; --ss-coral-tint: {P['coral_tint']};
  --ss-mustard: {P['mustard']}; --ss-mustard-deep: {P['mustard_deep']}; --ss-mustard-tint: {P['mustard_tint']};
  --ss-mint: {P['mint']}; --ss-mint-deep: {P['mint_deep']}; --ss-mint-tint: {P['mint_tint']};
  --ss-lavender: {P['lavender']}; --ss-lavender-deep: {P['lavender_deep']}; --ss-lavender-tint: {P['lavender_tint']};
  --ss-display: "Source Serif", Georgia, Cambria, "Times New Roman", serif;
  --ss-body: "Source Sans", system-ui, -apple-system, "Segoe UI", Roboto, Arial, sans-serif;
  --ss-mono: "Source Code Pro", ui-monospace, Consolas, "Liberation Mono", monospace;
  --ss-radius: 8px; --ss-radius-card: 12px;
  --ss-shadow: 0 1px 2px rgba(27, 31, 43, .05), 0 8px 24px rgba(27, 31, 43, .06);
}}

/* ---------- Canvas: plain ivory, no decorative layer ---------------------- */
[data-testid="stApp"] {{ background-color: var(--ss-cream); font-family: var(--ss-body); }}
[data-testid="stAppViewContainer"], .stMain, [data-testid="stMainBlockContainer"] {{ background: transparent; }}
/* Streamlit's top bar only holds the developer menu; hidden so it never covers the Self Search header. */
[data-testid="stHeader"] {{ display: none; }}
[data-testid="stBottom"] > div, [data-testid="stBottomBlockContainer"] {{ background: var(--ss-cream); }}
.block-container {{ max-width: 1240px; padding-top: 1.1rem; padding-bottom: 2.5rem; }}

/* ---------- Typography ------------------------------------------------- */
.stApp, .stApp p, .stApp li, .stApp label, .stApp input, .stApp textarea, .stApp button {{ font-family: var(--ss-body); }}
.stApp h1, .stApp h2, .stApp h3, .ss-display {{
  font-family: var(--ss-display); font-optical-sizing: auto; letter-spacing: -0.015em; color: var(--ss-ink);
}}
.stMarkdown p, .stCaption, [data-testid="stCaptionContainer"] {{ line-height: 1.55; }}
/* Streamlit fades captions with opacity .6 (~3:1 on cream); the muted ink alone meets 4.5:1. */
[data-testid="stCaptionContainer"], .stCaption p {{ color: var(--ss-ink-muted); opacity: 1; }}
.ss-label {{ font-family: var(--ss-mono); font-size: .72rem; letter-spacing: .1em; text-transform: uppercase;
            color: var(--ss-ink-muted); font-weight: 600; }}
.ss-section {{ font-family: var(--ss-mono); font-size: .74rem; letter-spacing: .1em; text-transform: uppercase;
              font-weight: 600; color: var(--ss-ink-muted); margin: 1.4rem 0 .5rem; }}
.stMarkdown a {{ color: var(--ss-cobalt-deep); text-underline-offset: 3px; }}

/* ---------- Header ------------------------------------------------------- */
.ss-brand {{ font-family: var(--ss-display); font-size: 1.55rem; font-weight: 700; color: var(--ss-ink);
  letter-spacing: -0.02em; line-height: 1.2; }}
.ss-brand span {{ color: var(--ss-cobalt); }}
.ss-rule {{ height: 1px; background: var(--ss-line); margin: .35rem 0 0; }}

/* ---------- Hero (before the first search) ------------------------------- */
.ss-eyebrow {{ font-family: var(--ss-mono); font-size: .74rem; letter-spacing: .14em; text-transform: uppercase;
  color: var(--ss-cobalt-deep); font-weight: 600; margin: 2.2rem 0 .6rem; }}
.ss-hero-title {{ font-family: var(--ss-display); font-size: clamp(2rem, 4.2vw, 3.1rem); line-height: 1.08;
  font-weight: 650; color: var(--ss-ink); letter-spacing: -0.025em; max-width: 20ch; margin: 0 0 .7rem; }}
.ss-hero-sub {{ color: var(--ss-ink-muted); font-size: 1.08rem; max-width: 56ch; margin: 0 0 1.4rem; }}

/* ---------- Buttons ---------------------------------------------------- */
{_BUTTON} {{
  border-radius: var(--ss-radius); font-weight: 600; min-height: 2.5rem; height: auto;
  padding: .45rem 1rem; line-height: 1.25; white-space: normal; overflow-wrap: anywhere;
  transition: background-color .15s ease, border-color .15s ease, color .15s ease;
}}
{_BUTTON} p {{ white-space: normal; }}
{_PRIMARY} {{ background: var(--ss-ink); border: 1.5px solid var(--ss-ink); color: #fff; }}
{_PRIMARY.replace(", ", ":hover, ")}:hover {{ background: var(--ss-cobalt-deep); border-color: var(--ss-cobalt-deep); color: #fff; }}
{_SECONDARY} {{ background: var(--ss-paper); border: 1.5px solid var(--ss-border); color: var(--ss-ink); }}
{_SECONDARY.replace(", ", ":hover, ")}:hover {{ border-color: var(--ss-cobalt-deep); color: var(--ss-cobalt-deep); }}

/* Quiet (text) buttons: cancel, new search */
{_key('cancel-ask')}, {_key('new-search')} {{
  background: transparent; border: 1.5px solid transparent; color: var(--ss-ink-muted); min-height: 2.25rem;
  text-decoration: underline; text-underline-offset: 3px;
}}
{_key('cancel-ask')}:hover, {_key('new-search')}:hover {{ background: var(--ss-paper); border-color: var(--ss-border); color: var(--ss-ink); }}
/* Pills: example queries, filter chips, broaden suggestions */
{_key('example-')}, {_key('chip-')}, {_key('broaden-')} {{ border-radius: 999px; min-height: 2.2rem; padding: .3rem .95rem; }}
{_key('example-')} {{ background: var(--ss-paper); border: 1px solid var(--ss-line); color: var(--ss-ink); font-weight: 500; }}
{_key('example-')}:hover {{ border-color: var(--ss-ink); color: var(--ss-ink); }}
{_key('chip-')} {{ background: var(--ss-cobalt-tint); border: 1px solid var(--ss-cobalt-tint); color: var(--ss-cobalt-deep); }}

/* Disabled: muted fill + dashed border, label still readable */
{_BUTTON}:disabled, {_BUTTON}[disabled] {{
  background: var(--ss-disabled, {P['disabled_bg']}) !important; color: var(--ss-ink-muted) !important;
  border: 1.5px dashed var(--ss-border) !important; cursor: not-allowed; opacity: 1;
}}
/* While a request runs, primary actions look busy */
[data-testid="stApp"][data-test-script-state="running"] {_PRIMARY.split(", ")[0]},
[data-testid="stApp"][data-test-script-state="running"] {_PRIMARY.split(", ")[1]} {{ cursor: progress; opacity: .8; }}

/* ---------- Focus: always visible for keyboard users -------------------- */
.stApp :is(button, a, input, textarea, summary, [role="tab"], [role="checkbox"], [role="switch"], [role="option"]):focus-visible {{
  outline: 3px solid var(--ss-cobalt-deep) !important; outline-offset: 2px !important; box-shadow: none;
}}
/* Checkboxes/toggles visually hide their <input>; ring the visible label instead. */
.stApp label:has(input:focus-visible) {{ outline: 3px solid var(--ss-cobalt-deep); outline-offset: 2px; border-radius: 8px; }}
.stApp :is(.stMain, [data-testid="stTable"], [tabindex]:not(button, a, input, textarea, summary)):focus-visible {{
  outline: 3px solid var(--ss-cobalt-deep) !important; outline-offset: -3px !important;
}}

/* ---------- Inputs ---------------------------------------------------- */
[data-testid="stWidgetLabel"] p {{ font-weight: 600; color: var(--ss-ink); }}
.stApp [data-baseweb="input"], .stApp [data-baseweb="base-input"], .stApp [data-baseweb="select"] > div,
.stApp [data-baseweb="textarea"] {{ background: var(--ss-paper); border-radius: var(--ss-radius); }}
.stApp [data-baseweb="input"], .stApp [data-baseweb="select"] > div {{ border: 1.5px solid var(--ss-border); }}
.stApp [data-baseweb="input"]:focus-within, .stApp [data-baseweb="select"] > div:focus-within {{
  border-color: var(--ss-cobalt); box-shadow: 0 0 0 3px var(--ss-cobalt-tint);
}}
.stApp input::placeholder, .stApp textarea::placeholder {{ color: var(--ss-ink-muted); opacity: 1; }}
[class*="st-key-hero-query"] input {{ font-size: 1.12rem; min-height: 3.2rem; padding-left: 1rem; }}
[class*="st-key-hero-query"] [data-baseweb="input"] {{ border-radius: 10px; box-shadow: var(--ss-shadow); }}
[class*="st-key-hero-search"] {_PRIMARY.split(", ")[1]} {{ min-height: 3.2rem; border-radius: 10px; }}
/* Compare checkbox as a quiet pill */
[class*="st-key-cmp-"] label {{ border: 1.5px solid var(--ss-border); border-radius: 999px; padding: .3rem .8rem;
  background: var(--ss-paper); width: 100%; min-height: 2.5rem; align-items: center; }}
[class*="st-key-cmp-"] label:has(input:checked) {{ background: var(--ss-cobalt-tint); border-color: var(--ss-cobalt); }}
[class*="st-key-cmp-"] label p {{ white-space: normal; overflow: visible; text-overflow: clip; }}

/* ---------- Alerts: same colour jobs, plus a bold left rule ------------- */
[data-testid="stAlert"] > div {{ border-radius: 10px; border-left: 4px solid currentColor; }}
[data-testid="stAlertContentWarning"], [data-testid="stAlertContentError"], [data-testid="stAlertContentInfo"],
[data-testid="stAlertContentSuccess"] {{ font-weight: 500; }}

/* ---------- Filter row, asking bar, answer ------------------------------ */
.ss-filter {{ display: inline-block; padding: .3rem .85rem; border-radius: 999px; font-size: .88rem; font-weight: 600;
  background: var(--ss-paper); border: 1px solid var(--ss-line); color: var(--ss-ink); white-space: nowrap; }}
/* Filter chips sit in one compact wrapping row, each only as wide as its label */
[data-testid="stHorizontalBlock"]:has(> [data-testid="stColumn"] :is([class*="st-key-chip-"], .ss-filter, [class*="st-key-broaden-"])) {{
  flex-wrap: wrap; gap: .4rem !important; }}
[data-testid="stHorizontalBlock"]:has(> [data-testid="stColumn"] :is([class*="st-key-chip-"], .ss-filter, [class*="st-key-broaden-"])) > [data-testid="stColumn"] {{
  flex: 0 0 auto !important; width: auto !important; min-width: 0 !important; }}
.ss-asking {{ background: var(--ss-cobalt-tint); color: var(--ss-cobalt-deep); border-radius: 10px;
  padding: .5rem .85rem; font-size: .95rem; }}
.ss-results-head {{ display: flex; align-items: baseline; gap: .8rem; flex-wrap: wrap; margin: 1.6rem 0 .5rem; }}
.ss-query {{ font-family: var(--ss-display); font-size: clamp(1.45rem, 2.4vw, 1.9rem); font-weight: 650;
  color: var(--ss-ink); letter-spacing: -0.015em; }}
.ss-query::before {{ content: "“"; color: var(--ss-cobalt); }}
.ss-query::after {{ content: "”"; color: var(--ss-cobalt); }}
.ss-count {{ color: var(--ss-ink-muted); font-size: .95rem; }}
.ss-summary {{ background: var(--ss-paper); border: 1px solid var(--ss-line); border-left: 3px solid var(--ss-cobalt);
  border-radius: 10px; padding: .75rem 1rem; margin: .4rem 0 1.1rem; max-width: 80ch; }}
.ss-summary p {{ margin: .2rem 0 0; color: var(--ss-ink); font-size: 1.02rem; }}

/* ---------- Chips and badges ------------------------------------------ */
.ss-chips {{ display: flex; flex-wrap: wrap; gap: .3rem; margin: .25rem 0 .45rem; }}
.ss-chip {{ display: inline-block; padding: .12rem .6rem; border-radius: 999px; font-size: .78rem; font-weight: 600;
  border: 1px solid var(--ss-line); background: var(--ss-paper); color: var(--ss-ink);
  max-width: 100%; overflow-wrap: anywhere; }}  /* long source feature text wraps instead of overflowing */
.ss-chip-ok {{ background: var(--ss-mint-tint); border-color: var(--ss-mint-tint); color: var(--ss-mint-deep); }}
.ss-chip-warn {{ background: var(--ss-mustard-tint); border-color: var(--ss-mustard-tint); color: var(--ss-mustard-deep); }}
.ss-chip-bad {{ background: var(--ss-coral-tint); border-color: var(--ss-coral-tint); color: var(--ss-coral-deep); }}
.ss-chip-accent, .ss-chip-cobalt {{ background: var(--ss-cobalt-tint); border-color: var(--ss-cobalt-tint); color: var(--ss-cobalt-deep); }}
.ss-chip-mint {{ background: var(--ss-mint-tint); border-color: var(--ss-mint-tint); color: var(--ss-mint-deep); }}
.ss-chip-coral {{ background: var(--ss-coral-tint); border-color: var(--ss-coral-tint); color: var(--ss-coral-deep); }}
.ss-chip-mustard {{ background: var(--ss-mustard-tint); border-color: var(--ss-mustard-tint); color: var(--ss-mustard-deep); }}
.ss-chip-lavender {{ background: var(--ss-lavender-tint); border-color: var(--ss-lavender-tint); color: var(--ss-lavender-deep); }}
.ss-badge {{ display: inline-block; font-family: var(--ss-mono); font-size: .66rem; font-weight: 700; letter-spacing: .1em;
  text-transform: uppercase; padding: .18rem .5rem; border-radius: 4px; background: var(--ss-ink); color: #fff; }}

/* ---------- Product cards ---------------------------------------------- */
[class*="st-key-card-"] {{
  background: var(--ss-paper); border: 1px solid var(--ss-line) !important;
  border-radius: var(--ss-radius-card) !important; transition: box-shadow .2s ease, border-color .2s ease;
}}
[class*="st-key-card-"]:hover {{ box-shadow: var(--ss-shadow); border-color: #D8CFC1 !important; }}
.ss-media {{ border-radius: 8px; overflow: hidden; display: flex; align-items: center; justify-content: center;
  background: var(--ss-cream-deep); aspect-ratio: 4 / 5; width: 100%; }}
.ss-media img {{ display: block; width: 100%; height: 100%; object-fit: contain; mix-blend-mode: multiply; }}
.ss-title {{ font-family: var(--ss-body); font-size: 1rem; font-weight: 600; line-height: 1.35;
  margin: .55rem 0 .2rem; color: var(--ss-ink); display: -webkit-box; -webkit-line-clamp: 2; line-clamp: 2;
  -webkit-box-orient: vertical; overflow: hidden; min-height: 2.7em; max-height: 2.7em; overflow-wrap: anywhere; }}
.ss-price {{ display: inline-block; font-size: 1.2rem; font-weight: 700; color: var(--ss-ink);
  font-variant-numeric: tabular-nums; margin: .05rem .45rem .05rem 0; vertical-align: middle; }}
.ss-meta {{ color: var(--ss-ink-muted); font-size: .88rem; margin: .1rem 0 .15rem; }}
.ss-details {{ color: var(--ss-ink-muted); font-size: .85rem; margin: 0 0 .3rem; overflow-wrap: anywhere; }}
.ss-cite {{ font-family: var(--ss-mono); font-size: .72rem; color: var(--ss-ink-muted); }}
.ss-claim {{ margin: .15rem 0 .35rem; line-height: 1.45; color: var(--ss-ink); font-size: .9rem; }}
.ss-claim .ss-chip {{ margin-right: .25rem; }}
/* Card grids wrap by available width (tablet), not only below Streamlit's 640px breakpoint */
[data-testid="stHorizontalBlock"]:has(> [data-testid="stColumn"] [class*="st-key-card-"]) {{ flex-wrap: wrap; row-gap: 1.2rem; }}
[data-testid="stHorizontalBlock"]:has(> [data-testid="stColumn"] [class*="st-key-card-"]) > [data-testid="stColumn"] {{
  flex: 1 1 15rem !important; min-width: min(15rem, 100%) !important; }}
[data-testid="stHorizontalBlock"]:has(> [data-testid="stColumn"] [class*="st-key-card-"]) > [data-testid="stColumn"]:empty {{ display: none; }}
/* Button rows inside cards and the example row wrap instead of clipping */
[class*="st-key-card-"] [data-testid="stHorizontalBlock"],
[data-testid="stHorizontalBlock"]:has([class*="st-key-example-"]) {{ flex-wrap: wrap; row-gap: .5rem; }}
[class*="st-key-card-"] [data-testid="stHorizontalBlock"] > [data-testid="stColumn"] {{ flex: 1 1 7.5rem !important; min-width: min(7.5rem, 100%) !important; }}
[data-testid="stHorizontalBlock"]:has([class*="st-key-example-"]) > [data-testid="stColumn"] {{ flex: 0 0 auto !important; width: auto !important; min-width: 0 !important; }}

/* ---------- Loading placeholder grid (only while a request runs) -------- */
.ss-loading {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(14rem, 1fr)); gap: 1.2rem; margin: 1rem 0; }}
.ss-skel {{ background: var(--ss-paper); border: 1px solid var(--ss-line); border-radius: var(--ss-radius-card); padding: .8rem; }}
.ss-skel i {{ display: block; background: var(--ss-cream-deep); border-radius: 6px; animation: ss-pulse 1.6s ease-in-out infinite; }}
.ss-skel i:first-child {{ aspect-ratio: 4 / 5; margin-bottom: .7rem; }}
.ss-skel i + i {{ height: .8rem; margin: .45rem 0; width: 80%; }}
@keyframes ss-pulse {{ 0%, 100% {{ opacity: 1; }} 50% {{ opacity: .55; }} }}
.ss-progress {{ color: var(--ss-ink-muted); font-size: .95rem; }}

/* ---------- Evidence, tabs, expanders, tables -------------------------- */
.stApp [data-testid="stMarkdownContainer"] blockquote {{ background: var(--ss-cream); border-left: 3px solid var(--ss-mustard);
  border-radius: 6px; padding: .45rem .8rem; margin: .25rem 0 .7rem; color: var(--ss-ink); }}
[data-testid="stExpander"] details {{ background: var(--ss-paper); border: 1px solid var(--ss-line); border-radius: 8px; }}
[data-testid="stExpander"] summary p {{ font-weight: 600; color: var(--ss-ink); }}
[data-testid="stTable"] {{ overflow-x: auto; max-width: 100%; }}
[data-testid="stTable"] table {{ border-collapse: separate; border-spacing: 0; border: 1px solid var(--ss-line);
  border-radius: 10px; overflow: hidden; background: var(--ss-paper); }}
[data-testid="stTable"] thead th {{ background: var(--ss-cream-deep); color: var(--ss-ink); font-weight: 700; }}
[data-testid="stTable"] tbody tr:nth-child(even) td, [data-testid="stTable"] tbody tr:nth-child(even) th {{ background: var(--ss-cream); }}
[data-testid="stTable"] tbody td:first-child, [data-testid="stTable"] tbody th {{ font-weight: 650; color: var(--ss-ink); }}

/* ---------- Chat elements (kept accessible if used) ---------------------- */
[data-testid="stChatMessageAvatarUser"] {{ background-color: var(--ss-coral-deep) !important; color: #fff !important; }}
[data-testid="stChatMessageAvatarAssistant"] {{ background-color: var(--ss-cobalt) !important; color: #fff !important; }}

/* ---------- Footer: one small disclaimer -------------------------------- */
.ss-footer {{ margin-top: 2.5rem; padding-top: .7rem; border-top: 1px solid var(--ss-line);
  color: var(--ss-ink-muted); font-size: .8rem; text-align: center; }}

/* ---------- Small screens --------------------------------------------- */
@media (max-width: 640px) {{
  .block-container {{ padding-left: 1rem; padding-right: 1rem; }}
  .ss-eyebrow {{ margin-top: 1.2rem; }}
}}

/* ---------- Reduced motion: no pulsing, no transitions ------------------ */
@media (prefers-reduced-motion: reduce) {{
  .ss-skel i {{ animation: none !important; }}
  [class*="st-key-card-"], {_BUTTON} {{ transition: none !important; }}
  [class*="st-key-card-"]:hover, {_BUTTON}:hover {{ transform: none !important; }}
}}
</style>
"""


def chip(text: str, kind: str = "") -> str:
    """One chip; ``text`` must already be HTML-escaped."""
    cls = f"ss-chip ss-chip-{kind}" if kind else "ss-chip"
    return f'<span class="{cls}">{text}</span>'


def chips(items: list[str]) -> str:
    return f'<div class="ss-chips">{"".join(items)}</div>' if items else ""
