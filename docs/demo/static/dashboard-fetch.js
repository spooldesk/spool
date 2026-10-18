/* Same-origin CSRF and session expiry handling shared by dashboard requests. */
(function () {
  'use strict';
  if (!window.fetch) return;
  var meta = document.querySelector('meta[name="csrf-token"]');
  var token = meta ? meta.content : '';
  var originalFetch = window.fetch.bind(window);

  function showExpiredSession() {
    if (document.getElementById('session-expired') || !document.body) return;
    var notice = document.createElement('div');
    notice.id = 'session-expired';
    notice.className = 'session-expired';
    notice.setAttribute('role', 'alert');
    notice.textContent = document.body.dataset.sessionExpired + ' ';
    var link = document.createElement('a');
    link.href = '/login?next=' + encodeURIComponent(location.pathname + location.search);
    link.target = '_blank';
    link.rel = 'noopener';
    link.textContent = document.body.dataset.signIn;
    notice.appendChild(link);
    document.body.appendChild(notice);
  }

  window.fetch = function (input, init) {
    var address = input instanceof Request ? input.url : input;
    var url = new URL(address, document.baseURI);
    if (url.origin !== location.origin) return originalFetch(input, init);
    var options = Object.assign({}, init);
    var method = (options.method || (input instanceof Request && input.method) || 'GET').toUpperCase();
    var headers = new Headers(options.headers || (input instanceof Request ? input.headers : undefined));
    headers.set('X-Requested-With', 'Spooldesk');
    if (token && !['GET', 'HEAD', 'OPTIONS'].includes(method) && !headers.has('X-CSRF-Token')) {
      headers.set('X-CSRF-Token', token);
    }
    options.headers = headers;
    return originalFetch(input, options).then(function (response) {
      if (response.status === 401) showExpiredSession();
      return response;
    });
  };
})();
