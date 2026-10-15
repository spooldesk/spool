# Bilingual public landing

The English page is `/`; Russian is `/ru/`. Edit `assets/landing/page.html`,
`assets/landing/en.json` and `assets/landing/ru.json`. The two generated HTML
files must stay in sync. Styles and progressive enhancement live in
`docs/style.css` and `docs/landing.js`. The interactive preview lives in
`docs/product-demo.js` and `docs/product-demo.css`; the guided AI showcase uses
`docs/ai-demo.js` and `docs/ai-demo.css`. No npm installation or CDN is required.

```sh
python scripts/build_landing.py
python scripts/build_landing.py --check --export site
python -m http.server 8765 --bind 127.0.0.1 --directory site
```

Export requires a **new** output directory; choose a different directory for
another build. It copies only twenty-four public assets, including both languages, legal-information pages, the current license text, search discovery files,
the interactive demo, shared brand tokens and fallback screenshots. Do not deploy `docs/`: it contains internal audits
and planning documents. CI validates generation and retains the public site as
an artifact named with the commit SHA; it does not publish it.

## Cloudflare Pages

For Git integration, use repository root, production branch `main`, no framework,
build command `python scripts/build_landing.py --check --export site`, and output
directory `site`. Set `PYTHON_VERSION=3.12` and `SKIP_DEPENDENCY_INSTALL=1` so the
static build does not install the helpdesk's Python dependencies. A `staging`/pull-request preview is a
preview of this **static website**, not a running helpdesk or a test database.
Cloudflare documents custom build commands and output directories in its
[static HTML guide](https://developers.cloudflare.com/pages/framework-guides/deploy-anything/).

For an existing Direct Upload project, upload the **contents** of the exported
directory or a ZIP whose root contains `index.html` for static pages only. Dashboard drag-and-drop does not deploy Pages Functions; use the Git integration for `/events`. Do not include the parent
directory. Direct Upload and Git integration have different project setup
paths; inspect the existing project before choosing a method. See Cloudflare's
[Direct Upload guide](https://developers.cloudflare.com/pages/get-started/direct-upload/).

After an approved deployment, verify `/`, `/ru/`, language links, screenshots,
mobile layout and contact links on the actual HTTPS domain. Check that
`/RELEASE_READINESS_2026-09-22.md` is not publicly served. The canonical domain is
`spooldesk.net` in the template; change it there before using another domain.

## Search discovery

Edit `assets/landing/robots.txt` and `assets/landing/sitemap.xml`; the build copies
them to `docs/` and includes them in the public export. The sitemap lists the
four public HTML pages using their canonical HTTPS URLs. Language alternatives
remain in each page's HTML. Update both search files when changing the domain
or adding public pages; do not list internal documentation or preview domains.

In Google Search Console, add the Domain property `spooldesk.net`, verify it
with the Google-provided TXT record at the DNS root, and submit
`https://spooldesk.net/sitemap.xml`. Keep that TXT record after verification;
add it alongside existing mail TXT records, never replace them. Use URL
Inspection for `/` and `/ru/` to check live accessibility and request indexing.
Verification and submission do not guarantee indexing or a search position.
After deployment, check that `/robots.txt` is plain text and `/sitemap.xml` is
XML, rather than the host's HTML fallback page.

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

The AI section offers two guided, fictional scenarios: a customer conversation
with clarification, a knowledge-based answer and handoff; and a team assistant
with a ticket overview and a status change confirmed by the visitor. The scenes
are static illustrations, not live model responses. They make no AI/application API calls, change no real tickets and retain no conversation data. The optional first-party counter records the first demo interaction without its text. Buttons advance/replay each scenario;
the initial customer scene remains readable without JavaScript. Keep the demo
notice visible and the content synchronized in both locales.
The build versions the AI script and stylesheet URLs by their content hashes
so browsers fetch changed resources after a deployment.

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

## Analytics and real 404 responses

See [website analytics](WEBSITE_ANALYTICS.md) for configuration, event definitions, SQL and verification. `assets/landing/404.html` generates root and Russian 404 documents; the root file disables Pages SPA fallback. Missing URLs must return HTTP 404, not the home page with HTTP 200. No error page is in the sitemap.

`functions/events.js` is deployed from repository root by Pages Git integration. The public `_routes.json` invokes it only for `/events`. The dashboard and self-hosted installation have no dependency on this endpoint. The AI showcase explicitly separates capabilities available in rc.9 from next-release customer dialogue and diagnostics.
