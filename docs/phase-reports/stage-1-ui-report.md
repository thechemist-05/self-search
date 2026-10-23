# Stage 1 UI Report: Visual Design

Scope: a visual redesign of the Streamlit frontend only. No backend, API, database, retrieval, ranking, evidence-validation or security code was changed. Design details are in [frontend.md](../frontend.md#layout-and-theme-redesign), and remaining limits in [limitations.md](../limitations.md#frontend-limits-phase-9).

## Files changed

| File | Change |
|---|---|
| `requirements.txt` | Pins `streamlit==1.66.0` (verified installed; `pip check` clean). |
| `.streamlit/config.toml` | Palette, bundled fonts, radii, heading scale, alert colours, sidebar theme. |
| `frontend/theme.py` | Palette, contrast helpers, accents, decorative layer and all CSS. |
| `frontend/view_models.py` | `claim_tone`, `html_text`, `media_html`; a transparent image placeholder. |
| `frontend/app.py` | Hero, search icon, example pills, cancel row, compare button, footer disclaimer. |
| `frontend/components/turn.py` | Card markup: accent, badges, claim chips, icons on actions. Same keys and callbacks. |
| `frontend/components/sidebar.py` | Disclaimer, profile header, aligned removal rows, icons. Same keys and callbacks. |
| `scripts/ui_check.py` | New: headless-Chrome browser checks (no new dependencies). |
| `tests/unit/test_frontend_view_models.py` | `TestThemeAccessibility`: contrast, decoration safety, reduced motion, fonts, captions, avatars. |
| `tests/integration/test_frontend_app.py` | `TestControlCoverage`: 8 tests for flows that were not covered before. |
| `docs/frontend.md`, `docs/limitations.md` | Updated. |

## Verification

**Backend unchanged.** sha256 of all 63 files under `src/` was identical before and after (list hash `37024045edbb6693…`).

**Test suite.** The full suite ran three times; each run had 582 passed, 1 skipped, 0 failed. The skip is the live Ollama test, because the daemon is not installed. The 34 warnings come from existing backend code and the test client.

**Browser checks** (`scripts/ui_check.py`, headless Chrome, final run):
- 0 problems across 5 viewports;
- 8 of 8 scenario steps at each viewport;
- reduced motion stopped the animation everywhere;
- idle CPU was about 5.6 % animated and about 1.8 % with reduced motion.

## Defects the browser checks found and fixed

- Captions were faded by Streamlit (`opacity: .6`) to about 3.3:1. They now use the muted ink at full opacity, which is at least 4.5:1.
- The chat avatar glyphs were white on light coral (3.3:1) and amber (2.0:1). They now use coral-deep and cobalt (5.96 and 7.32).
- Keyboard-focusable scroll regions showed no focus ring. They now have an inset cobalt ring.
- The Compare label was truncated to "Comp…" on phones. It now wraps.
- The CSS used a non-existent `stMain` test id. It now uses the `.stMain` class.

## Not verified

- Screen readers;
- real touch devices;
- Firefox and Safari;
- Windows high-contrast mode;
- a person looking at the site. The screenshots were produced by the script, not inspected by the user.
