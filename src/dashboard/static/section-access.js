/* Server permissions are authoritative; make view mode explicit in controls. */
(function () {
  if (document.body.dataset.readonly !== 'true') return;
  document.querySelectorAll('form').forEach(function (form) {
    if ((form.method || 'get').toLowerCase() === 'get') return;
    var path = new URL(form.action, location.href).pathname;
    if (path === '/logout' || path === '/views' || path.startsWith('/views/')) return;
    if (path === '/assistant/chat') return; // Read-only chat; confirmations have their own permission gate.
    form.querySelectorAll('input,textarea,select,button').forEach(function (control) { control.disabled = true; });
    form.addEventListener('submit', function (event) { event.preventDefault(); });
  });
  document.querySelectorAll('[draggable="true"]').forEach(function (item) { item.draggable = false; });
  document.querySelectorAll('.move-btn').forEach(function (item) { item.hidden = true; item.disabled = true; });
})();
