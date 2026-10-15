# Website analytics

The marketing website has two separate measurements. Neither is installed in the self-hosted dashboard.

## Visits and performance

Use Cloudflare Web Analytics for page views, visits, referrers, countries and Core Web Vitals. Its configured hostnames must include **spooldesk.net** (not only www.spooldesk.net and spooldesk.pages.dev). Filter production reports to spooldesk.net/www.spooldesk.net and exclude bots where the UI offers that option. Requests in zone HTTP analytics include assets, crawlers and scans; they are not people or leads.

Pages manages its injected beacon. After custom-domain changes, inspect Workers & Pages → spooldesk → Metrics → Web Analytics and the linked Manage site page. Keep exactly one injected beacon per page. Configuration changes may require a new deployment. Do not alter DNS/mail records to fix analytics.

## Conversion intent

`docs/analytics.js` sends a small JSON POST to `/events`. `functions/events.js` writes the validated point to Cloudflare Analytics Engine. Configure Pages → spooldesk → Settings → Bindings → Production → Add → Analytics Engine:

- Variable: `LANDING_EVENTS`
- Dataset: `spooldesk_landing_events`

Redeploy after changing bindings. Git integration builds Functions from the repository's `functions/` directory; a dashboard static ZIP upload does not. `_routes.json` restricts Function execution to `/events`. No provider key is exposed or required by this code.

| event | Meaning | Does not prove |
| --- | --- | --- |
| download_click | First installer-link click per page load | A completed download or installation |
| demo_open / product | First interaction with the product demo | A real workspace created |
| demo_open / ai | First interaction with the guided AI demo | An actual model call |
| sales_contact | First mailto contact click per page load | An email sent or a sale |

No cookies, persistent visitor IDs, IP fields, arbitrary query values or demo text are written to the dataset. Only allowlisted event, language path, coarse source, medium, campaign and demo type are accepted. DNT/GPC opt-outs, preview hosts and scripted clicks are ignored. Reloading can create another count. Ad blockers, failed requests, bots and sampling make these directional metrics, not audited unique-user counts. The public endpoint's origin checks limit browser misuse but do not authenticate humans or prevent a non-browser client from forging events.

Analytics Engine retains data for three months. It can sample; use `SUM(_sample_interval)` instead of `COUNT(*)`. The website's legal-information pages describe the collection. Hosting providers process their own request metadata under their terms.

### Queries

Use Cloudflare Analytics Engine SQL API (dataset name below), or Analytics SQL in the dashboard with the dataset name shown by its schema browser (`events.analyticsEngine.spooldesk_landing_events` when available).

```sql
SELECT blob1 AS event, blob6 AS demo, SUM(_sample_interval) AS actions
FROM spooldesk_landing_events
WHERE timestamp >= NOW() - INTERVAL '7' DAY
GROUP BY event, demo ORDER BY actions DESC
```

```sql
SELECT blob3 AS source, blob4 AS medium, blob5 AS campaign,
       blob1 AS event, SUM(_sample_interval) AS actions
FROM spooldesk_landing_events
WHERE timestamp >= NOW() - INTERVAL '14' DAY
GROUP BY source, medium, campaign, event ORDER BY actions DESC
```

Recognized campaign: `first-pilots-2026-10`; recognized UTM sources: `github`, `linkedin`, `hackernews`, `selfhosted`, `indiehackers`, `partner`; mediums: `social`, `community`, `referral`, `email`. Unknown text is never stored. Referrers can additionally be classified as direct, google, bing or other. For a new campaign extend both allowlists and tests deliberately.

Example: `https://spooldesk.net/?utm_source=linkedin&utm_medium=social&utm_campaign=first-pilots-2026-10#product`

### Verification and recovery

1. Run `python scripts/build_landing.py --check`, landing pytest tests and `node --test tests/frontend/landing-analytics.test.cjs`.
2. On production confirm `/` and `/ru/` return 200; an unknown path, `/.env` and `/.git/config` return **404** with the branded page. Its noindex tag excludes it from search. A local Python file server checks rendering, not Pages fallback behavior.
3. Interact once with each demo. `/events` should return 204; subsequent clicks in the same demo/page load should not send extra points. Use a controlled QA click and account for it in the baseline. Do not send real sales mail just to test a click.
4. A response of 503 indicates a missing/broken Analytics Engine binding. A 403 indicates origin mismatch; 400 invalid data; 404 preview hostname; 405 wrong method. The website remains usable if counting fails.
5. Query received events after propagation. Counts can lag. Compare only periods after the fix; old missing visits cannot be reconstructed.
6. To stop action counting, remove the analytics script include and redeploy. Do not delete the dataset to disable collection. Never add tracking to customers' self-hosted installations as a workaround.

## Sources

- [Web Analytics FAQ: no custom events or UTM tracking](https://developers.cloudflare.com/web-analytics/faq/)
- [Pages bindings](https://developers.cloudflare.com/pages/functions/bindings/)
- [Analytics Engine SQL](https://developers.cloudflare.com/analytics/analytics-engine/sql-api/)
- [Retention and limits](https://developers.cloudflare.com/analytics/analytics-engine/limits/)
- [Pricing](https://developers.cloudflare.com/analytics/analytics-engine/pricing/)
- [Pages 404 handling](https://developers.cloudflare.com/pages/configuration/serving-pages/)
