// Website conversion counts only. No ticket data, visitor IDs, cookies or raw URLs.
const origins = new Set(['https://spooldesk.net', 'https://www.spooldesk.net']);
const allowed = {
  event: ['download_click', 'demo_open', 'sales_contact', 'landing_visit'],
  page: ['/', '/ru/'],
  source: ['direct', 'github', 'linkedin', 'hackernews', 'selfhosted', 'indiehackers', 'partner', 'outreach', 'google', 'bing', 'other'],
  medium: ['none', 'social', 'community', 'referral', 'email', 'organic'],
  campaign: ['none', 'first-pilots-2026-10', ...Array.from({length:100}, (_, i) =>
    'outreach-2026-10-16-c' + String(i + 1).padStart(2, '0'))],
  demo: ['none', 'product', 'ai'],
};
function response(status) {
  return new Response(null, {status, headers: {'Cache-Control': 'no-store', 'X-Robots-Tag': 'noindex'}});
}
export async function onRequest({request, env}) {
  const origin = new URL(request.url).origin;
  if (!origins.has(origin)) return response(404); // Preview deployments never count.
  if (request.method !== 'POST') return response(405);
  if (request.headers.get('Origin') !== origin) return response(403);
  if (request.headers.get('Sec-Fetch-Site') && request.headers.get('Sec-Fetch-Site') !== 'same-origin') return response(403);
  if (request.headers.get('DNT') === '1' || request.headers.get('Sec-GPC') === '1') return response(204);
  if (request.headers.get('Content-Type')?.split(';')[0].trim() !== 'application/json') return response(415);
  if (Number(request.headers.get('Content-Length')) > 1024) return response(413);
  // Bound streamed bodies too; Content-Length can be absent or inaccurate.
  const reader = request.body?.getReader();
  if (!reader) return response(400);
  let size = 0;
  const chunks = [];
  try {
    while (true) {
      const {done, value} = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > 1024) { await reader.cancel(); return response(413); }
      chunks.push(value);
    }
  } catch { return response(400); }
  let data;
  try { data = JSON.parse(await new Blob(chunks).text()); } catch { return response(400); }
  if (!data || Array.isArray(data) || Object.keys(data).length !== Object.keys(allowed).length) return response(400);
  if (!Object.entries(allowed).every(([key, values]) => values.includes(data[key]))) return response(400);
  if ((data.event === 'demo_open') !== (data.demo !== 'none')) return response(400);
  if (data.event === 'landing_visit' && (data.source !== 'outreach' || !['email', 'referral'].includes(data.medium) ||
      !data.campaign.startsWith('outreach-2026-10-16-c'))) return response(400);
  if (!env.LANDING_EVENTS) return response(503);
  try {
    env.LANDING_EVENTS.writeDataPoint({
      indexes: [data.event],
      blobs: [data.event, data.page, data.source, data.medium, data.campaign, data.demo],
      doubles: [1],
    });
  } catch { return response(503); }
  return response(204);
}
