/* Full-page POST keeps server-side access, CSRF and result handling in charge. */
(function () {
  document.querySelectorAll('[data-connection-check]').forEach(function (form) {
    form.addEventListener('submit', function (event) {
      if (form.dataset.pending === 'true') { event.preventDefault(); return; }
      form.dataset.pending = 'true';
      form.setAttribute('aria-busy', 'true');
      var button = form.querySelector('button[type="submit"]');
      button.dataset.originalLabel = button.textContent;
      button.textContent = button.dataset.checkingLabel;
      button.disabled = true;
    });
  });
  window.addEventListener('pageshow', function (event) {
    if (!event.persisted) return;
    document.querySelectorAll('[data-connection-check][data-pending="true"]').forEach(function (form) {
      delete form.dataset.pending;
      form.removeAttribute('aria-busy');
      var button = form.querySelector('button[type="submit"]');
      button.textContent = button.dataset.originalLabel;
      button.disabled = false;
    });
  });
})();
