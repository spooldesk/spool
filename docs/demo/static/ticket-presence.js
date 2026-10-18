/* Shared PostgreSQL presence. No draft text is sent, only short-lived activity. */
(function (scope) {
  'use strict';
  function describe(others, labels) {
    const replies = others.filter(p => !p.customer && p.typing && p.replying);
    const notes = others.filter(p => !p.customer && p.typing && !p.replying);
    const customers = others.filter(p => p.customer && p.typing);
    const viewers = others.filter(p => !p.customer && !p.typing);
    const line = (people, label) => people.length ? label.replace('{who}', people.map(p => p.name).join(', ')) : '';
    return [line(replies, labels.replying), line(notes, labels.note), line(customers, labels.customer),
      !replies.length && !notes.length ? line(viewers, viewers.length > 1 ? labels.hereMany : labels.here) : ''].filter(Boolean).join(' ');
  }
  function boot(config) {
    const form = document.getElementById('reply-form'), input = document.getElementById('reply-text');
    const header = document.getElementById('presence'), label = document.getElementById('presence-text');
    const composer = document.getElementById('composer-presence');
    if (!header || !label) return;
    const client = crypto.randomUUID().replaceAll('-', '');
    let lastInput = -Infinity, lastSent = -Infinity, inFlight = false, dirty = false, stopped = false;
    let known = [], knownAt = 0;
    const now = () => Date.now();
    const isTyping = () => config.canWrite && !document.hidden && !stopped &&
      document.activeElement === input && Boolean(input?.value.trim()) && now() - lastInput < 6000;
    function render(people) {
      const description = describe(people, config.labels);
      label.textContent = description;
      header.classList.toggle('hidden', !description); header.classList.toggle('flex', Boolean(description));
      const operators = people.filter(p => !p.customer && p.typing);
      if (composer) {
        composer.textContent = describe(operators, config.labels);
        composer.hidden = !operators.length;
      }
    }
    function body(leaving = false) {
      return new URLSearchParams({csrf_token:config.csrf, client_id:client,
        typing:isTyping() ? '1' : '0', leaving:leaving ? '1' : '',
        audience:form?.action.endsWith('/note') ? 'internal' : 'public'});
    }
    async function beat() {
      if (document.hidden) return;
      if (inFlight) {dirty = true; return;}
      inFlight = true; lastSent = now();
      try {
        const response = await fetch(`/api/tickets/${config.ticket}/presence`, {method:'POST',body:body()});
        if (!response.ok) throw new Error('presence unavailable');
        const data = await response.json();
        if (data.ok) {known = data.others || []; knownAt = now(); render(known);}
      } catch (_) {
        // Never claim a colleague is still typing when our connection has gone stale.
        if (now() - knownAt > 6000) render([]);
      } finally {
        inFlight = false;
        if (dirty) {dirty = false; beat();}
      }
    }
    function leave() {
      stopped = true; known = []; render([]);
      navigator.sendBeacon(`/api/tickets/${config.ticket}/presence`, body(true));
    }
    input?.addEventListener('input', () => {
      const wasTyping = isTyping(); stopped = false; lastInput = now();
      if (!wasTyping || !input.value.trim() || now()-lastSent >= 1500) beat();
    });
    input?.addEventListener('blur', () => {lastInput = -Infinity; beat();});
    form?.addEventListener('submit', () => {lastInput = -Infinity; stopped = true; beat();});
    // Run after the existing mode handler has changed the form action.
    ['mode-note','mode-reply'].forEach(id => document.getElementById(id)?.addEventListener('click', () => {
      lastInput = -Infinity; queueMicrotask(beat);
    }));
    document.addEventListener('visibilitychange', () => {
      if (document.hidden) leave(); else {stopped = false; lastInput = -Infinity; beat();}
    });
    window.addEventListener('pagehide', leave);
    window.addEventListener('pageshow', () => {stopped = false; beat();});
    setInterval(() => {
      if (now()-knownAt > 6000) render([]);
      beat();
    }, 3000);
    beat();
  }
  const api = {boot, describe};
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else scope.SpTicketPresence = api;
})(typeof window !== 'undefined' ? window : {});
