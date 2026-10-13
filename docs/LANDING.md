# Bilingual public landing

The English page is `/`; Russian is `/ru/`. Edit `assets/landing/page.html`,
`assets/landing/en.json` and `assets/landing/ru.json`. The two generated HTML
files must stay in sync. Styles and progressive enhancement live in
`docs/style.css` and `docs/landing.js`. The interactive preview lives in
`docs/product-demo.js` and `docs/product-demo.css`. No npm installation or CDN is required.

```sh
python scripts/build_landing.py
python scripts/build_landing.py --check --export site
python -m http.server 8765 --bind 127.0.0.1 --directory site
```

Export requires a **new** output directory; choose a different directory for
another build. It copies only sixteen public assets, including both languages, legal-information pages, the current license text,
the interactive demo, shared brand tokens and fallback screenshots. Do not deploy `docs/`: it contains internal audits
and planning documents. CI validates generation and retains the public site as
an artifact named with the commit SHA; it does not publish it.

## Cloudflare Pages

For Git integration, use repository root, production branch `main`, no framework,
build command `python scripts/build_landing.py --check --export site`, and output
directory `site`. Use Python 3.12 or later. A `staging`/pull-request preview is a
preview of this **static website**, not a running helpdesk or a test database.
Cloudflare documents custom build commands and output directories in its
[static HTML guide](https://developers.cloudflare.com/pages/framework-guides/deploy-anything/).

For an existing Direct Upload project, upload the **contents** of the exported
directory or a ZIP whose root contains `index.html`; do not include the parent
directory. Direct Upload and Git integration have different project setup
paths; inspect the existing project before choosing a method. See Cloudflare's
[Direct Upload guide](https://developers.cloudflare.com/pages/get-started/direct-upload/).

After an approved deployment, verify `/`, `/ru/`, language links, screenshots,
mobile layout and contact links on the actual HTTPS domain. Check that
`/RELEASE_READINESS_2026-09-22.md` is not publicly served. The canonical domain is
`spooldesk.net` in the template; change it there before using another domain.

## Offer and content boundaries

- Self-hosted software under the existing BSL; optional setup and maintenance.
- $300 setup and $149/month maintenance are initial offers subject to agreed
  scope/readiness, not a checkout or a promise of immediate availability.
- Mailto links use `hello@spooldesk.net`; verify the mailbox before publication.
- Screenshots use synthetic data. No invented customer counts or testimonials.
- No hosted Cloud signup, included AI credits, hosted workspace availability or
  blanket claim that connected providers never receive data.
- Animations respect reduced motion. Text, links and FAQ work without JavaScript;
  channel examples and the interactive preview are optional enhancements.

## Interactive preview

The product section starts with an empty local queue. Visitors can add fictional
examples, switch list/board, search, open a conversation, try a reply or internal
note, close/reopen a ticket and reset the demo. Customer and report views reflect
only the demo state. Integration descriptions never collect credentials.
Nothing connects to the application API, sends messages or persists visitor text.
Reload/reset clears the demo. Both languages and themes are supported.
If JavaScript fails or is disabled, the original screenshots remain available.

Identity tokens are authored in `assets/brand/tokens.css` and copied identically
to `docs/brand.css` and `src/dashboard/static/brand.css` by `scripts/build_brand.py`.
The landing build also builds/checks them. Keep the public export allowlist in
`scripts/build_landing.py`; never publish the repository or internal `docs/` files.

## Sales and legal information

Pricing has four cards: self-hosted, setup, maintenance and Contact sales.
The last card opens the on-page sales section; its visible address opens the
visitor's mail app. There is no contact database, checkout or automatic email.
Use hello@spooldesk.net until the owner explicitly changes the public contact.
Prices remain indicative while the commercial service is being prepared.

Edit `assets/landing/legal.html` and the two `*-legal.json` files for `/legal/`
and `/ru/legal/`. These pages explain the existing license and data flows;
they do not substitute for completed seller details, commercial terms and a
privacy policy. See [business readiness](https://github.com/spooldesk/spool/blob/main/docs/internal/BUSINESS_READINESS.md) before paid launch.
The builder copies `LICENSE` to public `license.txt`; never hand-edit that copy.
Check four-card layout, EN/RU, both themes, mobile, sales anchors and legal links.
