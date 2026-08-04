/* Styled replacements for the two controls the browser draws itself.
 *
 * A <select>'s dropdown and a date input's calendar are rendered by the
 * operating system: the blue highlight, the font, the little arrow are all
 * outside CSS's reach. Every other surface in this dashboard is themed, so
 * these two stood out on every page that has a filter.
 *
 * Both are progressive enhancement, not replacement. The native element stays
 * in the DOM, keeps the value and submits with the form; the widget is a skin
 * on top that writes back to it and fires a bubbling `change`, which is what
 * the `onchange="this.requestSubmit()"` filters rely on. With JavaScript off,
 * or if this file fails to load, the page keeps working exactly as before.
 */
(function () {
  'use strict';

  const CLOSE_EVENTS = ['click', 'focusin'];
  let openWidget = null;

  function closeOpen(except) {
    if (openWidget && openWidget !== except) openWidget.close();
    openWidget = except || null;
  }

  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') closeOpen(null);
  });
  CLOSE_EVENTS.forEach(function (evt) {
    document.addEventListener(evt, function (e) {
      if (openWidget && !openWidget.root.contains(e.target)) closeOpen(null);
    });
  });

  /* Tell the page a value changed the way a real interaction would, so form
   * handlers that listen for `change` cannot tell the difference. Several
   * filters are wired as onchange="this.requestSubmit()", so a swallowed event
   * does not look like a missing event — it looks like a control that does
   * nothing when you use it. */
  function fire(el) {
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
  }

  function commit(input, value) {
    if (input.value === value) return;
    input.value = value;
    fire(input);
  }

  // ---------------------------------------------------------------- select

  /* The widget replaces a control the page already sized. Padding classes
   * here are a starting point, not the answer: a select styled px-3 py-2 on
   * one page and px-2 py-1.5 on another must keep its own height, or it ends
   * up a few pixels short of the inputs beside it and the whole row looks
   * off. Take the height from the element being replaced, before hiding it.
   */
  function matchHeight(button, original) {
    const h = original.getBoundingClientRect().height;
    if (h > 4) button.style.minHeight = h + 'px';
  }

  function enhanceSelect(select) {
    if (select.dataset.enhanced || select.multiple || select.size > 1) return;
    select.dataset.enhanced = '1';

    const root = document.createElement('div');
    root.className = 'ui-select relative';
    const button = document.createElement('button');
    button.type = 'button';
    button.className =
      'w-full flex items-center gap-2 px-2 py-1.5 rounded-md bg-black border ' +
      'border-zinc-800 text-sm text-left text-zinc-100 hover:border-zinc-700 ' +
      'focus:border-brand-500 focus:outline-none';
    const label = document.createElement('span');
    label.className = 'flex-1 truncate';
    const caret = document.createElement('i');
    caret.className = 'ti ti-chevron-down text-zinc-500 shrink-0';
    caret.style.fontSize = '14px';
    caret.setAttribute('aria-hidden', 'true');
    button.append(label, caret);

    const panel = document.createElement('div');
    panel.className =
      'hidden absolute z-40 mt-1 w-full min-w-[10rem] max-h-72 overflow-y-auto ' +
      'bg-zinc-950 border border-zinc-800 rounded-lg shadow-2xl scroll-pretty py-1';
    panel.setAttribute('role', 'listbox');

    // A search box only earns its place once scanning the list stops being
    // instant; below that it is one more thing to tab past.
    let search = null;
    if (select.options.length > 8) {
      search = document.createElement('input');
      search.type = 'text';
      search.className =
        'w-full px-2 py-1.5 mb-1 bg-black border-b border-zinc-800 text-sm ' +
        'text-zinc-100 placeholder:text-zinc-600 focus:outline-none';
      search.placeholder = '…';
      panel.appendChild(search);
    }

    const list = document.createElement('div');
    panel.appendChild(list);

    matchHeight(button, select);
    select.classList.add('sr-only-select');
    select.parentNode.insertBefore(root, select);
    root.append(select, button, panel);

    let items = [];

    function paint() {
      label.textContent = select.options[select.selectedIndex]
        ? select.options[select.selectedIndex].textContent.trim()
        : '';
      button.setAttribute('aria-expanded', panel.classList.contains('hidden') ? 'false' : 'true');
    }

    function build() {
      list.innerHTML = '';
      items = [];
      const needle = (search && search.value.trim().toLowerCase()) || '';
      Array.prototype.forEach.call(select.options, function (opt, index) {
        const text = opt.textContent.trim();
        if (needle && text.toLowerCase().indexOf(needle) === -1) return;
        const item = document.createElement('div');
        item.className =
          'px-2.5 py-1.5 text-sm cursor-pointer text-zinc-300 hover:bg-zinc-900 ' +
          (index === select.selectedIndex ? 'bg-brand-600/20 text-white' : '');
        item.textContent = text;
        item.setAttribute('role', 'option');
        item.setAttribute('aria-selected', index === select.selectedIndex ? 'true' : 'false');
        item.addEventListener('click', function () {
          const changed = select.selectedIndex !== index;
          select.selectedIndex = index;
          // Announce it after the index moves, not through commit(): by then
          // select.value already equals the option, so a value comparison
          // would decide nothing had happened and stay silent.
          if (changed) fire(select);
          paint();
          widget.close();
          button.focus();
        });
        list.appendChild(item);
        items.push(item);
      });
      if (!items.length) {
        const empty = document.createElement('div');
        empty.className = 'px-2.5 py-2 text-xs text-zinc-600';
        empty.textContent = '—';
        list.appendChild(empty);
      }
    }

    let cursor = -1;
    function highlight(next) {
      if (!items.length) return;
      cursor = (next + items.length) % items.length;
      items.forEach(function (el, i) {
        el.classList.toggle('bg-zinc-900', i === cursor);
      });
      items[cursor].scrollIntoView({ block: 'nearest' });
    }

    const widget = {
      root: root,
      close: function () {
        panel.classList.add('hidden');
        paint();
      },
      open: function () {
        build();
        panel.classList.remove('hidden');
        cursor = select.selectedIndex;
        paint();
        if (search) { search.value = ''; build(); search.focus(); }
      },
    };

    button.addEventListener('click', function (e) {
      e.stopPropagation();
      const wasOpen = !panel.classList.contains('hidden');
      closeOpen(null);
      if (!wasOpen) { widget.open(); closeOpen(widget); }
    });

    if (search) search.addEventListener('input', build);

    root.addEventListener('keydown', function (e) {
      const isOpen = !panel.classList.contains('hidden');
      if (e.key === 'ArrowDown') {
        e.preventDefault();
        if (!isOpen) { widget.open(); closeOpen(widget); } else highlight(cursor + 1);
      } else if (e.key === 'ArrowUp') {
        e.preventDefault();
        if (isOpen) highlight(cursor - 1);
      } else if (e.key === 'Enter' && isOpen) {
        e.preventDefault();
        if (items[cursor]) items[cursor].click();
      } else if (e.key === 'Escape' && isOpen) {
        e.preventDefault();
        widget.close();
        button.focus();
      }
    });

    // The value can change from outside — a reset link, another script.
    select.addEventListener('change', paint);
    paint();
  }

  // ------------------------------------------------------------------ date

  const MONTHS_FALLBACK = ['January', 'February', 'March', 'April', 'May', 'June',
    'July', 'August', 'September', 'October', 'November', 'December'];

  function monthName(date) {
    try {
      return date.toLocaleDateString(document.documentElement.lang || 'en',
        { month: 'long', year: 'numeric' });
    } catch (e) {
      return MONTHS_FALLBACK[date.getMonth()] + ' ' + date.getFullYear();
    }
  }

  function iso(date) {
    const m = String(date.getMonth() + 1).padStart(2, '0');
    const d = String(date.getDate()).padStart(2, '0');
    return date.getFullYear() + '-' + m + '-' + d;
  }

  function enhanceDate(input) {
    if (input.dataset.enhanced) return;
    input.dataset.enhanced = '1';

    const root = document.createElement('div');
    root.className = 'ui-date relative';
    const button = document.createElement('button');
    button.type = 'button';
    button.className =
      'w-full flex items-center gap-2 px-2 py-1.5 rounded-md bg-black border ' +
      'border-zinc-800 text-sm text-left hover:border-zinc-700 ' +
      'focus:border-brand-500 focus:outline-none';
    const label = document.createElement('span');
    label.className = 'flex-1 truncate';
    const icon = document.createElement('i');
    icon.className = 'ti ti-calendar text-zinc-500 shrink-0';
    icon.style.fontSize = '14px';
    icon.setAttribute('aria-hidden', 'true');
    button.append(label, icon);

    const panel = document.createElement('div');
    panel.className =
      'hidden absolute z-40 mt-1 right-0 w-[17rem] bg-zinc-950 border ' +
      'border-zinc-800 rounded-lg shadow-2xl p-3';

    matchHeight(button, input);
    input.classList.add('sr-only-select');
    input.parentNode.insertBefore(root, input);
    root.append(input, button, panel);

    let shown = new Date();

    function paint() {
      if (input.value) {
        label.textContent = input.value;
        label.className = 'flex-1 truncate text-zinc-100';
      } else {
        label.textContent = input.placeholder || (input.dataset.empty || '—');
        label.className = 'flex-1 truncate text-zinc-600';
      }
    }

    function build() {
      panel.innerHTML = '';

      const head = document.createElement('div');
      head.className = 'flex items-center justify-between mb-2';
      const prev = document.createElement('button');
      prev.type = 'button';
      prev.className = 'px-1.5 text-zinc-500 hover:text-zinc-100';
      prev.innerHTML = '<i class="ti ti-chevron-left" style="font-size:14px"></i>';
      const title = document.createElement('div');
      title.className = 'text-sm text-zinc-200';
      title.textContent = monthName(shown);
      const next = document.createElement('button');
      next.type = 'button';
      next.className = 'px-1.5 text-zinc-500 hover:text-zinc-100';
      next.innerHTML = '<i class="ti ti-chevron-right" style="font-size:14px"></i>';
      prev.addEventListener('click', function () {
        shown = new Date(shown.getFullYear(), shown.getMonth() - 1, 1); build();
      });
      next.addEventListener('click', function () {
        shown = new Date(shown.getFullYear(), shown.getMonth() + 1, 1); build();
      });
      head.append(prev, title, next);
      panel.appendChild(head);

      const grid = document.createElement('div');
      grid.className = 'grid grid-cols-7 gap-0.5 text-center';
      // Monday-first: the working week is what a support rota is planned on.
      (window.UI_WEEKDAYS || ['M', 'T', 'W', 'T', 'F', 'S', 'S']).forEach(function (d) {
        const cell = document.createElement('div');
        cell.className = 'text-[10px] text-zinc-600 py-1';
        cell.textContent = d;
        grid.appendChild(cell);
      });

      const first = new Date(shown.getFullYear(), shown.getMonth(), 1);
      const lead = (first.getDay() + 6) % 7;   // shift Sunday=0 to Monday-first
      const days = new Date(shown.getFullYear(), shown.getMonth() + 1, 0).getDate();
      const todayIso = iso(new Date());

      for (let i = 0; i < lead; i++) grid.appendChild(document.createElement('div'));
      for (let day = 1; day <= days; day++) {
        const date = new Date(shown.getFullYear(), shown.getMonth(), day);
        const value = iso(date);
        const cell = document.createElement('button');
        cell.type = 'button';
        cell.textContent = day;
        cell.className =
          'py-1 rounded text-sm hover:bg-zinc-800 ' +
          (value === input.value ? 'bg-brand-600 text-white ' : 'text-zinc-300 ') +
          (value === todayIso && value !== input.value ? 'ring-1 ring-brand-500/40 ' : '');
        cell.addEventListener('click', function () {
          commit(input, value);
          paint();
          widget.close();
          button.focus();
        });
        grid.appendChild(cell);
      }
      panel.appendChild(grid);

      const foot = document.createElement('div');
      foot.className = 'flex items-center justify-between mt-2 pt-2 border-t border-zinc-900';
      const today = document.createElement('button');
      today.type = 'button';
      today.className = 'text-xs text-brand-400 hover:text-brand-300';
      today.textContent = window.UI_TODAY || 'Today';
      today.addEventListener('click', function () {
        commit(input, todayIso); paint(); widget.close();
      });
      const clear = document.createElement('button');
      clear.type = 'button';
      clear.className = 'text-xs text-zinc-500 hover:text-zinc-200';
      clear.textContent = window.UI_CLEAR || 'Clear';
      clear.addEventListener('click', function () {
        commit(input, ''); paint(); widget.close();
      });
      foot.append(today, clear);
      panel.appendChild(foot);
    }

    const widget = {
      root: root,
      close: function () { panel.classList.add('hidden'); },
      open: function () {
        shown = input.value ? new Date(input.value + 'T00:00:00') : new Date();
        if (isNaN(shown)) shown = new Date();
        build();
        panel.classList.remove('hidden');
      },
    };

    button.addEventListener('click', function (e) {
      e.stopPropagation();
      const wasOpen = !panel.classList.contains('hidden');
      closeOpen(null);
      if (!wasOpen) { widget.open(); closeOpen(widget); }
    });

    input.addEventListener('change', paint);
    paint();
  }

  /* ---------------- where "back" goes ----------------
   * A ticket can be opened from the list or from the board, and the arrow
   * used to always return to the list. Remember the last list-like page and
   * send it there instead, filters and all.
   *
   * sessionStorage rather than the server: it is per tab, so someone working
   * the board in one tab and the list in another gets the right answer in
   * both, and nothing has to be stored for them between visits.
   */
  var LIST_KEY = 'dash-list-page';
  var LIST_PAGES = ['/tickets', '/board'];

  function rememberList() {
    if (LIST_PAGES.indexOf(window.location.pathname) === -1) return;
    try {
      sessionStorage.setItem(LIST_KEY, window.location.pathname + window.location.search);
    } catch (e) { /* private mode — the fallback href still works */ }
  }

  function restoreBackLink() {
    var link = document.getElementById('back-link');
    if (!link) return;
    try {
      var target = sessionStorage.getItem(LIST_KEY);
      // Same-origin path only: this value ends up in an href.
      if (target && target.charAt(0) === '/' && target.charAt(1) !== '/') {
        link.setAttribute('href', target);
      }
    } catch (e) { /* keep the server-rendered fallback */ }
  }

  function enhanceAll(scope) {
    (scope || document).querySelectorAll('select').forEach(enhanceSelect);
    (scope || document).querySelectorAll('input[type=date]').forEach(enhanceDate);
    if (!scope) { rememberList(); restoreBackLink(); }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () { enhanceAll(); });
  } else {
    enhanceAll();
  }
  window.enhanceControls = enhanceAll;

  /* ---------------- live refresh ----------------
   * Keeps a list page current without re-rendering it on a blind timer.
   * Asks a ~40-byte endpoint whether anything moved, and only then pays for
   * the page. A slow backstop covers the changes the token can't see
   * (a re-assignment) and the case where a check simply failed.
   *
   * apply(doc) is page-specific: it gets the parsed new document and is
   * responsible for swapping in whatever part of it matters.
   */
  window.liveRefresh = function (opts) {
    const checkMs = opts.checkMs || 10000;
    // The token catches everything that matters, so this is a safety net for
    // a missed check or a change too subtle to fingerprint — not the mechanism.
    const backstopMs = opts.backstopMs || 300000;
    const isBusy = opts.isBusy || function () { return false; };
    let version = null;
    let lastFull = Date.now();
    let running = false;

    async function pull() {
      const r = await fetch(window.location.href, {
        cache: 'no-store',
        headers: { 'X-Requested-With': 'live' },
      });
      if (!r.ok) return;
      opts.apply(new DOMParser().parseFromString(await r.text(), 'text/html'));
      lastFull = Date.now();
    }

    async function tick() {
      // Don't fight the user: a swap mid-drag or mid-typing would be hostile.
      if (running || document.hidden || isBusy()) return;
      running = true;
      try {
        const r = await fetch('/api/tickets-version', { cache: 'no-store' });
        if (r.ok) {
          const next = (await r.json()).version;
          const changed = version !== null && next !== version;
          version = next;
          if (changed || Date.now() - lastFull >= backstopMs) await pull();
        }
      } catch (e) { /* transient — the next tick retries */ }
      finally { running = false; }
    }

    setInterval(tick, checkMs);
    // Coming back to the tab is exactly when the page is most likely stale.
    document.addEventListener('visibilitychange', function () {
      if (!document.hidden) tick();
    });
  };
})();
