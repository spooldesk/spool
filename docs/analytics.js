/* Small, first-party conversion counter. Website only; never shipped in the dashboard. */
(() => {
  'use strict';
  if (!['spooldesk.net', 'www.spooldesk.net'].includes(location.hostname)) return;
  if (!['/', '/ru/'].includes(location.pathname)) return;
  if (navigator.doNotTrack === '1' || navigator.globalPrivacyControl === true) return;
  const sources = ['github', 'linkedin', 'hackernews', 'selfhosted', 'indiehackers', 'partner', 'outreach'];
  const mediums = ['social', 'community', 'referral', 'email'];
  const query = new URLSearchParams(location.search);
  let source = 'direct', medium = 'none';
  try {
    const ref = new URL(document.referrer);
    if (ref.hostname !== location.hostname) {
      source = ({'github.com':'github','www.linkedin.com':'linkedin','news.ycombinator.com':'hackernews',
        'www.reddit.com':'selfhosted','www.indiehackers.com':'indiehackers',
        'www.google.com':'google','www.bing.com':'bing'})[ref.hostname] || 'other';
      medium = ['google', 'bing'].includes(source) ? 'organic' : 'referral';
    }
  } catch (_) {}
  if (sources.includes(query.get('utm_source'))) source = query.get('utm_source');
  if (mediums.includes(query.get('utm_medium'))) medium = query.get('utm_medium');
  // Fixed company codes for this campaign; never accept arbitrary recipient data.
  const campaigns = ['first-pilots-2026-10', ...Array.from({length:100}, (_, i) =>
    'outreach-2026-10-16-c' + String(i + 1).padStart(2, '0'))];
  const campaign = campaigns.includes(query.get('utm_campaign')) ? query.get('utm_campaign') : 'none';
  const seen = new Set();
  function record(event, demo = 'none') {
    const key = event + ':' + demo;
    if (seen.has(key)) return; // One intent per action type per page load.
    seen.add(key);
    const body = JSON.stringify({event, page: location.pathname, source, medium, campaign, demo});
    try {
      fetch('/events', {method:'POST', headers:{'Content-Type':'application/json'},
        body, keepalive:true, credentials:'omit', referrerPolicy:'origin'}).catch(() => {});
    } catch (_) {} // Metrics never prevent navigation or using the demo.
  }
  // A tagged landing load is a visit signal, not proof that a person read the email.
  if (source === 'outreach' && ['email', 'referral'].includes(medium) && campaign.startsWith('outreach-2026-10-16-c')) {
    record('landing_visit');
  }
  document.addEventListener('click', event => {
    if (!event.isTrusted) return;
    const target = event.target.closest?.('a, button');
    if (!target) return;
    const action = target.dataset.analytics;
    if (action === 'download_click' || action === 'sales_contact') record(action);
    // Count an actual demo interaction, not scrolling past a screenshot.
    if (target.closest('#product-demo')) record('demo_open', 'product');
    if (target.closest('#ai') && target.matches('button')) record('demo_open', 'ai');
  });
})();
