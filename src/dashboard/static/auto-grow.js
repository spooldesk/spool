/* All multiline inputs grow after typing, insertion, reset and responsive reflow. */
window.SpAutoGrow = (() => {
  'use strict';
  const fields = new Set(), sizes = new WeakMap();
  let pending = false;
  function resize(field) {
    if (!field?.isConnected || !field.getClientRects().length || !field.clientWidth) return;
    const style = getComputedStyle(field);
    if (!sizes.has(field)) sizes.set(field, {minimum:parseFloat(style.height) || 40, width:field.clientWidth});
    const size = sizes.get(field), border = parseFloat(style.borderTopWidth) + parseFloat(style.borderBottomWidth);
    const max = parseFloat(style.maxHeight) || Infinity;
    field.style.height = '0px';
    const height = Math.min(max, Math.max(size.minimum, field.scrollHeight + border));
    field.style.height = height + 'px';
    field.style.overflowY = field.scrollHeight > field.clientHeight + 1 ? 'auto' : 'hidden';
    size.width = field.clientWidth;
  }
  function refresh() {
    pending = false;
    document.querySelectorAll('textarea').forEach(field => {
      if (!fields.has(field)) { fields.add(field); observer?.observe(field); }
    });
    fields.forEach(field => {
      if (!field.isConnected) { fields.delete(field); observer?.unobserve(field); }
      else resize(field);
    });
  }
  function schedule() { if (!pending) { pending = true; requestAnimationFrame(refresh); } }
  const observer = typeof ResizeObserver === 'function' ? new ResizeObserver(entries => {
    if (entries.some(({target}) => sizes.get(target)?.width !== target.clientWidth)) schedule();
  }) : null;
  ['input','change','focusin'].forEach(name => document.addEventListener(name, event => {
    if (event.target instanceof HTMLTextAreaElement) resize(event.target);
  }));
  document.addEventListener('reset', schedule);
  window.addEventListener('resize', schedule);
  new MutationObserver(schedule).observe(document.documentElement, {
    subtree:true, childList:true, attributes:true, attributeFilter:['class','hidden','open']
  });
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', schedule);
  else schedule();
  return {resize, refresh:schedule};
})();
