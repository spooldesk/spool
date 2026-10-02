/* Keep unsaved provider fields separate; credentials never follow a switch. */
(function () {
  'use strict';
  var selector = document.getElementById('aiProvider');
  if (!selector) return;
  var model = document.getElementById('aiModel');
  var host = document.getElementById('aiHost');
  var key = document.getElementById('aiKey');
  var clearKey = document.getElementById('aiClearKey');
  var keyInfo = document.getElementById('aiKeyInfo');
  var status = document.getElementById('aiSavedStatus');
  var initialProvider = selector.value;
  var previous = initialProvider;
  var placeholder = key.placeholder;
  var drafts = Object.create(null);

  selector.addEventListener('change', function () {
    drafts[previous] = {model: model.value, host: host.value};
    var provider = selector.value;
    document.querySelectorAll('[data-provider-note]').forEach(function (note) {
      note.hidden = note.dataset.providerNote !== provider;
    });
    var option = selector.options[selector.selectedIndex];
    var list = document.getElementById('models-' + provider);
    var first = list && list.querySelector('option');
    var draft = drafts[provider] || {model: first ? first.value : '', host: ''};
    model.value = draft.model;
    model.setAttribute('list', 'models-' + provider);
    host.value = draft.host;
    var hint = option.dataset.hostHint || '';
    host.placeholder = hint;
    host.disabled = !hint;
    document.getElementById('aiHostRow').hidden = !hint;
    var needsKey = option.dataset.needsKey === '1';
    document.getElementById('aiKeyRow').hidden = !needsKey;
    key.disabled = !needsKey;
    key.value = '';
    key.placeholder = provider === initialProvider ? placeholder : key.dataset.emptyPlaceholder;
    keyInfo.hidden = provider !== initialProvider;
    status.hidden = provider !== initialProvider;
    if (clearKey) {
      clearKey.checked = false;
      clearKey.disabled = provider !== initialProvider;
      clearKey.parentElement.hidden = provider !== initialProvider;
    }
    previous = provider;
  });

  // Opening an advanced section via a saved-form anchor keeps it visible.
  function revealAnchor() {
    var target = document.getElementById(location.hash.slice(1));
    if (target && target.tagName === 'DETAILS') target.open = true;
  }
  revealAnchor();
  window.addEventListener('hashchange', revealAnchor);
})();
