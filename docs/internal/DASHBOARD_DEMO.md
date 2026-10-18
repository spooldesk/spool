# Authentic dashboard demo and video

The public `/demo/` uses HTML rendered by the application routes, its actual
templates, icons, styles and JavaScript. It is not a second hand-designed UI.
English and Russian exports are committed so Cloudflare Pages needs no Python
backend or database. The product dashboard's design is unchanged.

## Regenerate after UI changes

```powershell
.venv/Scripts/python.exe scripts/build_dashboard_demo.py
.venv/Scripts/python.exe scripts/build_landing.py --check
.venv/Scripts/python.exe -m pytest tests/test_dashboard_demo.py tests/test_landing_build.py tests/test_landing_css.py tests/test_demo_scripts.py
node --test tests/frontend/dashboard-demo.test.cjs
```

The builder creates a disposable fixture database, clears inherited integration
credentials and disables dotenv and external transports. It never opens the
canonical preview database. `manifest.json` records source hashes and contains
only explicitly selected fictional data. Tests detect stale UI exports.

## Browser boundary

The outer shell loads static files; the inner frame intercepts API calls.
Content Security Policy denies network connections and form submissions.
Native settings/password/credential changes and channel connections are not
available. State lives in page memory and resets on reload. There is no public
login, backend or model connection.

Visitors can navigate actual pages, open tickets, try text replies and notes,
message editing/deletion, status changes and the board. AI examples are visibly
labelled as scripted. Confirmation changes only fixture state. Analytics show a
fixed sample period. Files can be previewed in the ticket composer, but sending
and AI file analysis require a real installation. The demo export is CSV.

`vendor/` includes the exact Chart.js and Tabler versions already used by the
dashboard, with their MIT licenses. No CDN is needed inside the public demo.

## Video source

The current walkthrough is edited from captures of actual browser interactions
with an isolated running application on port 8012. It is not a continuous screen
recording. Source frames, scene text, synthesized English narration, subtitle
timings and the final MP4 are in the ignored directory
`.agent-runs/authentic-demo-20261017/video/`. The release dashboard and current
development features must not be described as interchangeable. AI settings are
shown without making a real provider request. All customer and operator data
in the captures are fictional.

The revised English narration uses the synthetic male Andrew voice. Only the
public narration script is sent for speech generation. Per-word timings drive
subtitle changes; scene lengths follow the generated audio. The revised export
is in `video/export/male/`; the original narration is preserved for comparison.
