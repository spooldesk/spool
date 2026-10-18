/* Shared keyboard-accessible message menu. Portalled out of scrolling transcripts. */
window.SpMessageMenu = (() => {
  'use strict';
  let active = null, sequence = 0;
  function close(restore = false) {
    if (!active) return;
    const {trigger, panel} = active;
    active = null; panel.remove(); trigger.setAttribute('aria-expanded', 'false');
    if (restore && trigger.isConnected) trigger.focus();
  }
  function attach(parent, items, label) {
    if (!items.length) return;
    const trigger = document.createElement('button');
    trigger.type = 'button'; trigger.className = 'message-menu-trigger';
    trigger.textContent = '⋯'; trigger.title = label;
    trigger.setAttribute('aria-label', label); trigger.setAttribute('aria-haspopup', 'menu');
    trigger.setAttribute('aria-expanded', 'false');
    const id = 'message-menu-' + (++sequence); trigger.setAttribute('aria-controls', id);
    function open(last = false) {
      if (active?.trigger === trigger) { close(true); return; }
      close();
      const panel = document.createElement('div'); panel.id = id;
      panel.className = 'message-menu'; panel.setAttribute('role', 'menu');
      panel.setAttribute('aria-label', label);
      items.forEach(item => {
        const button = document.createElement('button'); button.type = 'button';
        button.className = 'message-menu-item' + (item.danger ? ' is-danger' : '');
        button.setAttribute('role', 'menuitem'); button.tabIndex = -1;
        button.textContent = item.label;
        if (item.disabled) {
          button.setAttribute('aria-disabled', 'true');
          if (item.hint) {
            const hint = document.createElement('small'); hint.textContent = item.hint;
            button.append(hint);
          }
        }
        button.addEventListener('click', () => {
          if (item.disabled) return;
          close(true); item.action();
        });
        panel.append(button);
      });
      document.body.append(panel); active = {trigger, panel};
      trigger.setAttribute('aria-expanded', 'true');
      const rect = trigger.getBoundingClientRect(), size = panel.getBoundingClientRect();
      const viewport = window.visualViewport;
      const width = viewport?.width || innerWidth, height = viewport?.height || innerHeight;
      panel.style.left = Math.max(8, Math.min(rect.right - size.width, width - size.width - 8)) + 'px';
      panel.style.top = Math.max(8, rect.bottom + size.height + 6 > height
        ? rect.top - size.height - 6 : rect.bottom + 6) + 'px';
      const buttons = [...panel.querySelectorAll('button')];
      buttons[last ? buttons.length - 1 : 0]?.focus();
      panel.addEventListener('keydown', event => {
        const i = buttons.indexOf(document.activeElement);
        let next;
        if (event.key === 'ArrowDown') next = (i + 1) % buttons.length;
        if (event.key === 'ArrowUp') next = (i - 1 + buttons.length) % buttons.length;
        if (event.key === 'Home') next = 0;
        if (event.key === 'End') next = buttons.length - 1;
        if (next !== undefined) { event.preventDefault(); buttons[next].focus(); }
        if (event.key === 'Tab') close(true);
      });
    }
    trigger.addEventListener('click', () => open());
    trigger.addEventListener('keydown', event => {
      if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
        event.preventDefault(); open(event.key === 'ArrowUp');
      }
    });
    parent.append(trigger);
    return trigger;
  }
  document.addEventListener('pointerdown', event => {
    if (active && !active.panel.contains(event.target) && !active.trigger.contains(event.target)) close();
  });
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && active) { event.preventDefault(); close(true); }
  });
  window.addEventListener('resize', () => close());
  document.addEventListener('scroll', event => {
    if (active && !active.panel.contains(event.target)) close();
  }, true);
  return {attach, close};
})();
