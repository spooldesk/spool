/* Preserve the composer when a session expires or a submission is rejected. */
window.SpComposerSubmit = function (form, button, notice, labels) {
  if (!form || !button || !notice) return;
  let pending = false;
  form.addEventListener('submit', async function (event) {
    event.preventDefault();
    if (pending) return;
    pending = true;
    button.disabled = true;
    notice.textContent = '';
    try {
      const response = await fetch(form.action, {method: 'POST', body: new FormData(form)});
      if (response.status === 401) {
        notice.textContent = labels.sessionExpired;
        return;
      }
      if (!response.ok) {
        notice.textContent = labels.failed;
        return;
      }
      const destination = new URL(response.url, location.href);
      const error = destination.searchParams.get('err');
      if (error) {
        notice.textContent = error;
        return;
      }
      if (response.redirected && destination.origin === location.origin
          && destination.pathname !== '/login') {
        location.assign(destination.href);
        return;
      }
      notice.textContent = labels.failed;
    } catch (_) {
      // Do not automatically retry: the server may have accepted the message
      // before the connection dropped. Polling still updates the transcript.
      notice.textContent = labels.uncertain;
    } finally {
      pending = false;
      button.disabled = false;
    }
  });
};
