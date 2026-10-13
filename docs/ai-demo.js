/* A guided product illustration. No model calls, storage, forms or network I/O. */
(() => {
  'use strict';
  const root = document.getElementById('ai-tour');
  if (!root) return;
  const stage = root.querySelector('#ai-stage');
  const next = root.querySelector('#ai-next');
  const announcement = root.querySelector('#ai-announcement');
  const modes = [...root.querySelectorAll('[data-ai-mode]')];
  const initial = document.createElement('template');
  initial.content.append(...[...stage.childNodes].map(node => node.cloneNode(true)));
  initial.dataset.announcement = stage.dataset.announcement;
  const scenes = {
    customer: [initial, root.querySelector('#ai-customer-1'), root.querySelector('#ai-customer-2')],
    team: [0, 1, 2].map(n => root.querySelector(`#ai-team-${n}`)),
  };
  if (Object.values(scenes).some(list => list.some(scene => !scene))) return;
  let mode = 'customer';
  let step = 0;
  function render() {
    const scene = scenes[mode][step];
    stage.replaceChildren(scene.content.cloneNode(true));
    modes.forEach(button => button.setAttribute('aria-pressed', String(button.dataset.aiMode === mode)));
    root.querySelector('#ai-progress').textContent = `0${step + 1} / 03`;
    const label = step === 2 ? next.dataset.replay : mode === 'team' && step === 1 ? next.dataset.confirm : next.dataset.next;
    next.replaceChildren(document.createTextNode(label + ' '));
    const arrow = document.createElement('span');
    arrow.setAttribute('aria-hidden', 'true');
    arrow.textContent = step === 2 ? '↻' : '→';
    next.append(arrow);
    announcement.textContent = modes.find(button => button.dataset.aiMode === mode).textContent + ': ' + scene.dataset.announcement;
  }
  modes.forEach(button => button.addEventListener('click', () => {
    if (button.dataset.aiMode === mode) return;
    mode = button.dataset.aiMode;
    step = 0;
    render();
  }));
  next.addEventListener('click', () => {
    step = (step + 1) % 3;
    render();
    const conversation = stage.querySelector('.ai-conversation');
    const headerBottom = document.querySelector('.site-header')?.getBoundingClientRect().bottom || 0;
    if (conversation.getBoundingClientRect().top < headerBottom + 16) {
      conversation.scrollIntoView({block: 'start', behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'instant' : 'smooth'});
    }
  });
  root.querySelector('.ai-mode-switch').hidden = false;
  root.querySelector('.ai-tour-actions').hidden = false;
})();
