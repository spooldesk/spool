/* Descriptions stay in HTML without JS; enhance marked text into compact help. */
(function () {
  'use strict';
  let active = null, closeTimer;
  function close() {
    clearTimeout(closeTimer);
    if (!active) return;
    active.button.setAttribute('aria-expanded', 'false');
    active.panel.hidden = true;
    active = null;
  }
  function place(panel, button) {
    const r = button.getBoundingClientRect();
    const width = panel.offsetWidth, height = panel.offsetHeight;
    panel.style.left = Math.max(8, Math.min(r.left, innerWidth - width - 8)) + 'px';
    panel.style.top = Math.max(8, r.bottom + height + 8 > innerHeight
      ? r.top - height - 8 : r.bottom + 8) + 'px';
  }
  document.querySelectorAll('[data-help]').forEach((description, index) => {
    if (!description.textContent.trim()) return;
    const button = document.createElement('button');
    button.type = 'button'; button.className = 'help-tip-trigger';
    button.textContent = 'i';
    button.setAttribute('aria-label', document.body.dataset.helpLabel || 'Help');
    button.setAttribute('aria-expanded', 'false');
    const panel = document.createElement('div');
    panel.id = 'help-tip-' + index; panel.className = 'help-tip-panel';
    panel.setAttribute('role', 'note'); panel.hidden = true;
    button.setAttribute('aria-controls', panel.id);
    button.setAttribute('aria-describedby', panel.id);
    panel.append(...description.childNodes);
    // Put help next to its title when one exists; do not disturb form fields.
    const previous = description.previousElementSibling;
    if (previous && (previous.matches('h1,h2,h3,h4,label')
        || (previous.matches('div') && previous.classList.contains('font-medium')))) {
      previous.append(button); description.remove();
    } else {
      description.replaceWith(button);
    }
    document.body.append(panel);
    function open() {
      clearTimeout(closeTimer);
      if (active?.button !== button) close();
      panel.hidden = false; button.setAttribute('aria-expanded', 'true');
      active = {button, panel}; place(panel, button);
    }
    function laterClose() { clearTimeout(closeTimer); closeTimer = setTimeout(close, 160); }
    button.addEventListener('pointerenter', open);
    button.addEventListener('pointerleave', laterClose);
    panel.addEventListener('pointerenter', () => clearTimeout(closeTimer));
    panel.addEventListener('pointerleave', laterClose);
    button.addEventListener('focus', open);
    button.addEventListener('blur', laterClose);
    panel.addEventListener('focusin', () => clearTimeout(closeTimer));
    panel.addEventListener('focusout', laterClose);
    button.addEventListener('click', event => {
      event.preventDefault(); event.stopPropagation(); open();
    });
  });
  document.addEventListener('pointerdown', event => {
    if (active && !active.button.contains(event.target) && !active.panel.contains(event.target)) close();
  });
  document.addEventListener('keydown', event => { if (event.key === 'Escape') close(); });
  window.addEventListener('resize', close);
  document.addEventListener('scroll', event => {
    if (active && !active.panel.contains(event.target)) close();
  }, true);
})();
