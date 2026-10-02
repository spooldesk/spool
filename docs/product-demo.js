/* In-memory product tour. No API, storage, credentials or outgoing messages. */
(() => {
  'use strict';
  const root = document.getElementById('product-demo');
  if (!root) return;
  const ru = document.documentElement.lang === 'ru';
  const tr = (en, russian) => ru ? russian : en;
  const words = {
    tickets: tr('Tickets', 'Обращения'), customers: tr('Customers', 'Клиенты'),
    reports: tr('Reports', 'Отчёты'), integrations: tr('Integrations', 'Интеграции'),
    new: tr('New', 'Новое'), in_progress: tr('In progress', 'В работе'),
    waiting: tr('Waiting for customer', 'Ждём клиента'), closed: tr('Closed', 'Закрыто'),
    active: tr('Active', 'Активные'), closedQueue: tr('Closed', 'Закрытые'), list: tr('List', 'Список'), board: tr('Board', 'Доска'),
    add: tr('Add sample tickets', 'Добавить примеры'), reset: tr('Reset demo', 'Сбросить демо'),
    search: tr('Search tickets', 'Поиск обращений'), back: tr('Back to tickets', 'К обращениям'),
    reply: tr('Reply', 'Ответ'), note: tr('Internal note', 'Заметка'),
    close: tr('Close ticket', 'Закрыть обращение'), reopen: tr('Reopen ticket', 'Открыть снова'),
  };
  const state = {tickets: [], view: 'tickets', mode: 'list', queue: 'active', search: '', selected: null, compose: 'reply'};
  const drafts = new Map();
  const node = (tag, cls, text) => {
    const element = document.createElement(tag);
    if (cls) element.className = cls;
    if (text != null) element.textContent = text;
    return element;
  };
  const button = (text, action, cls = '') => {
    const element = node('button', `sd-demo-button ${cls}`, text);
    element.type = 'button'; element.addEventListener('click', action); return element;
  };
  const badge = status => node('span', `sd-demo-badge sd-demo-status-${status}`, words[status]);
  const draftKey = () => `${state.selected}:${state.compose}`;
  const active = ticket => ticket.status !== 'closed';
  const initials = name => name.split(' ').map(p => p[0]).join('').slice(0, 2);
  let heading, body, reset, add, announce, navigation;

  function notify(text) { announce.textContent = text; }
  function focusHeading() { heading.focus({preventScroll: true}); }
  function counts() {
    return {active: state.tickets.filter(active).length, closed: state.tickets.filter(t => !active(t)).length};
  }
  function samples() {
    if (state.tickets.length) return;
    state.tickets = [
      {id: 1041, name: tr('Alex Morgan', 'Алексей Морозов'), channel: 'Email', status: 'new',
        subject: tr('Help with account access', 'Помогите восстановить доступ'),
        messages: [{kind: 'customer', text: tr('Hi! I cannot sign in after changing my email. Can you help?', 'Здравствуйте! После смены почты не могу войти в аккаунт. Поможете?')}]},
      {id: 1042, name: tr('Sam Lee', 'Мария Ли'), channel: 'Slack', status: 'in_progress',
        subject: tr('Invite a teammate', 'Приглашение в команду'),
        messages: [{kind: 'customer', text: tr('Where can I invite a new teammate to our workspace?', 'Где пригласить нового сотрудника в наш кабинет?')}]},
      {id: 1043, name: tr('Jordan Ellis', 'Антон Белов'), channel: tr('Website', 'Сайт'), status: 'waiting',
        subject: tr('Finding an invoice', 'Как найти счёт'),
        messages: [{kind: 'customer', text: tr('I need a copy of our last invoice.', 'Мне нужна копия последнего счёта.')},
          {kind: 'reply', text: tr('Of course. Which month do you need?', 'Конечно. За какой месяц нужен счёт?')}]},
    ];
    state.view = 'tickets'; state.selected = null; state.queue = 'active'; state.search = '';
    render(); notify(tr('Three fictional tickets added.', 'Добавлены три вымышленных обращения.')); focusHeading();
  }
  function clear() {
    state.tickets = []; state.view = 'tickets'; state.selected = null; state.mode = 'list';
    state.queue = 'active'; state.search = ''; state.compose = 'reply'; drafts.clear(); render();
    notify(tr('Demo cleared. Nothing was saved or sent.', 'Демо очищено. Ничего не сохранено и не отправлено.')); focusHeading();
  }
  function navigate(view) {
    state.view = view; state.selected = null; render(); focusHeading();
  }
  function open(ticket) {
    state.selected = ticket.id; state.compose = 'reply'; render(); focusHeading();
  }
  function empty(title, description, seed = false) {
    const box = node('div', 'sd-demo-empty');
    const mark = node('span', 'sd-demo-empty-mark', '↳'); mark.setAttribute('aria-hidden', 'true');
    box.append(mark, node('h4', '', title), node('p', '', description));
    if (seed) box.append(button(words.add, samples, 'sd-demo-primary'));
    return box;
  }
  function chrome() {
    const shell = node('div', 'sd-demo'); shell.setAttribute('aria-label', tr('Interactive Spooldesk demo', 'Интерактивное демо Spooldesk'));
    const strip = node('div', 'sd-demo-strip');
    strip.append(node('span', 'sd-demo-kicker', tr('PRODUCT DEMO', 'ДЕМО ПРОДУКТА')),
      node('span', 'sd-demo-disclaimer', tr('Fictional data. Nothing is sent.', 'Вымышленные данные. Ничего не отправляется.')));
    reset = button(words.reset, clear, 'sd-demo-text-button'); strip.append(reset);
    const layout = node('div', 'sd-demo-layout');
    const aside = node('aside', 'sd-demo-sidebar');
    const brand = node('div', 'sd-demo-brand');
    const image = node('img', 'sd-brand-icon'); image.src = root.dataset.brandMark; image.alt = ''; image.width = 28; image.height = 28;
    const name = node('span', '', 'spooldesk'); name.append(node('span', 'sd-brand-dot', '.')); brand.append(image, name);
    navigation = node('nav', 'sd-demo-nav'); navigation.setAttribute('aria-label', tr('Demo sections', 'Разделы демо'));
    const symbols = {tickets: '▤', customers: '◎', reports: '▥', integrations: '⊞'};
    for (const view of ['tickets', 'customers', 'reports', 'integrations']) {
      const b = button('', () => navigate(view), 'sd-demo-nav-button'); b.dataset.view = view;
      const icon = node('span', 'sd-demo-nav-icon', symbols[view]); icon.setAttribute('aria-hidden', 'true');
      b.append(icon, node('span', '', words[view])); navigation.append(b);
    }
    aside.append(brand, navigation, node('p', 'sd-demo-sidebar-note', tr('Your workspace.\nYour way of working.', 'Ваш кабинет.\nВаш порядок работы.')));
    const main = node('div', 'sd-demo-main'); const top = node('div', 'sd-demo-top');
    heading = node('h3', 'sd-demo-heading'); heading.tabIndex = -1;
    add = button(words.add, samples, 'sd-demo-primary'); top.append(heading, add);
    body = node('div', 'sd-demo-body');
    announce = node('p', 'sd-demo-announce'); announce.setAttribute('role', 'status'); announce.setAttribute('aria-live', 'polite');
    main.append(top, body, announce); layout.append(aside, main); shell.append(strip, layout); root.append(shell);
  }
  function ticketRows(target) {
    target.replaceChildren();
    const needle = state.search.trim().toLocaleLowerCase();
    const selected = state.tickets.filter(t => (state.queue === 'active' ? active(t) : !active(t)) &&
      `${t.id} ${t.name} ${t.subject} ${t.channel}`.toLocaleLowerCase().includes(needle));
    if (!selected.length) {
      target.append(empty(state.tickets.length ? tr('No matching tickets', 'Обращений не найдено') : tr('A clear start for your team', 'Чистый лист для вашей команды'),
        state.tickets.length ? tr('Try another search or switch the queue.', 'Измените поиск или переключите очередь.') : tr('Explore the workspace, or add a few examples to try a conversation.', 'Посмотрите разделы или добавьте примеры, чтобы попробовать переписку.'), !state.tickets.length)); return;
    }
    const row = ticket => {
      const b = button('', () => open(ticket), 'sd-demo-ticket');
      b.setAttribute('aria-label', `#${ticket.id} ${ticket.subject}`);
      const identity = node('div', 'sd-demo-ticket-person'); identity.append(node('span', 'sd-demo-avatar', initials(ticket.name)), node('span', '', ticket.name));
      const content = node('div', 'sd-demo-ticket-copy'); content.append(node('strong', '', ticket.subject), identity);
      const meta = node('div', 'sd-demo-ticket-meta'); meta.append(node('span', '', `#${ticket.id} · ${ticket.channel}`), badge(ticket.status));
      b.append(content, meta); return b;
    };
    if (state.mode === 'board') {
      const board = node('div', 'sd-demo-board');
      const stages = state.queue === 'closed' ? ['closed'] : ['new', 'in_progress', 'waiting'];
      for (const status of stages) {
        const col = node('section', 'sd-demo-column'); const group = selected.filter(t => t.status === status);
        const title = node('h4', '', words[status]); title.append(node('span', '', String(group.length))); col.append(title);
        for (const ticket of group) col.append(row(ticket));
        if (!group.length) col.append(node('p', 'sd-demo-column-empty', tr('No tickets', 'Нет обращений')));
        board.append(col);
      }
      target.append(board);
    } else {
      const list = node('div', 'sd-demo-list'); for (const ticket of selected) list.append(row(ticket)); target.append(list);
    }
  }
  function tickets() {
    const tools = node('div', 'sd-demo-tools'); const tabs = node('div', 'sd-demo-tabs');
    for (const queue of ['active', 'closed']) {
      const b = button(`${queue === 'closed' ? words.closedQueue : words.active} ${counts()[queue]}`, () => {
        state.queue = queue; render(); body.querySelector(`button[data-queue="${queue}"]`)?.focus({preventScroll:true});
      }, 'sd-demo-tab');
      b.dataset.queue = queue;
      b.setAttribute('aria-pressed', String(state.queue === queue)); tabs.append(b);
    }
    const modes = node('div', 'sd-demo-segment'); modes.setAttribute('role', 'group'); modes.setAttribute('aria-label', tr('Ticket view', 'Вид обращений'));
    for (const mode of ['list', 'board']) {
      const b = button(words[mode], () => {
        state.mode = mode; render(); body.querySelector(`button[data-mode="${mode}"]`)?.focus({preventScroll:true});
      }, 'sd-demo-segment-button'); b.dataset.mode = mode; b.setAttribute('aria-pressed', String(state.mode === mode)); modes.append(b);
    }
    tools.append(tabs, modes);
    const search = node('input', 'sd-demo-search'); search.type = 'search'; search.placeholder = words.search;
    search.setAttribute('aria-label', words.search); search.maxLength = 200; search.value = state.search;
    const results = node('div', 'sd-demo-results');
    search.addEventListener('input', () => {state.search = search.value; ticketRows(results);});
    body.append(tools, search, results); ticketRows(results);
  }
  function detail(ticket) {
    heading.textContent = `#${ticket.id}`;
    const top = node('div', 'sd-demo-detail-top');
    top.append(button(`← ${words.back}`, () => {state.selected = null; render(); focusHeading();}, 'sd-demo-text-button'), badge(ticket.status));
    const title = node('h4', 'sd-demo-subject', ticket.subject);
    const identity = node('p', 'sd-demo-person', `${ticket.name} · ${ticket.channel}`);
    const thread = node('div', 'sd-demo-thread'); thread.setAttribute('aria-label', tr('Conversation', 'Переписка'));
    for (const message of ticket.messages) {
      const bubble = node('div', `sd-demo-message sd-demo-message-${message.kind}`);
      bubble.append(node('span', 'sd-demo-message-author', message.kind === 'customer' ? ticket.name : message.kind === 'note' ? tr('You · internal note', 'Вы · внутренняя заметка') : tr('You · demo reply', 'Вы · ответ в демо')),
        node('p', '', message.text)); thread.append(bubble);
    }
    const composer = node('form', 'sd-demo-composer'); const controls = node('div', 'sd-demo-compose-tabs');
    const changeStatus = () => {ticket.status = active(ticket) ? 'closed' : 'in_progress'; render(); notify(words[ticket.status]); focusHeading();};
    if (active(ticket)) {
      for (const mode of ['reply', 'note']) {
        const b = button(words[mode], () => {state.compose = mode; render(); body.querySelector('textarea')?.focus({preventScroll:true});}, 'sd-demo-tab');
        b.setAttribute('aria-pressed', String(state.compose === mode)); controls.append(b);
      }
      const close = button(words.close, changeStatus, 'sd-demo-text-button'); close.classList.add('sd-demo-close'); controls.append(close);
      const label = node('label', 'sd-demo-compose-label', state.compose === 'note' ? tr('Private note for your team', 'Заметка для команды') : tr('Try a reply — it stays in this demo', 'Попробуйте ответить — это останется в демо')); label.htmlFor = 'sd-demo-reply';
      const input = node('textarea', 'sd-demo-reply'); input.id = 'sd-demo-reply'; input.rows = 2; input.maxLength = 2000;
      input.value = drafts.get(draftKey()) || '';
      const submit = node('button', 'sd-demo-button sd-demo-primary', state.compose === 'note' ? tr('Add note', 'Добавить заметку') : tr('Send in demo', 'Ответить в демо'));
      submit.type = 'submit'; submit.disabled = !input.value.trim();
      const grow = () => {input.style.height = 'auto'; input.style.height = `${Math.min(input.scrollHeight, 140)}px`;};
      input.addEventListener('input', () => {drafts.set(draftKey(), input.value); submit.disabled = !input.value.trim(); grow();});
      const bottom = node('div', 'sd-demo-compose-bottom'); bottom.append(node('span', '', tr('Local preview only', 'Только внутри демо')), submit);
      composer.append(controls, label, input, bottom);
      composer.addEventListener('submit', event => {
        event.preventDefault(); const text = input.value.trim(); if (!text) return;
        ticket.messages.push({kind: state.compose, text}); if (state.compose === 'reply') ticket.status = 'waiting';
        drafts.delete(draftKey()); render(); body.querySelector('.sd-demo-thread')?.scrollTo({top: 100000, behavior:'instant'});
        notify(tr('Added to the demo. Nothing was sent.', 'Добавлено в демо. Ничего не отправлено.'));
        body.querySelector('textarea')?.focus({preventScroll:true});
      });
      requestAnimationFrame(grow);
    } else {
      composer.append(node('p', '', tr('This conversation is closed.', 'Обращение закрыто.')), button(words.reopen, changeStatus));
    }
    body.classList.add('sd-demo-detail'); body.append(top, title, identity, thread, composer);
  }
  function customers() {
    if (!state.tickets.length) {body.append(empty(tr('Your customers will appear here', 'Здесь появятся ваши клиенты'), tr('Add sample tickets to explore a customer list.', 'Добавьте примеры обращений, чтобы посмотреть список клиентов.'), true)); return;}
    body.append(node('p', 'sd-demo-intro', tr('People and conversations, together.', 'Клиенты и их обращения — рядом.')));
    for (const ticket of state.tickets) {
      const row = button('', () => {state.view = 'tickets'; open(ticket);}, 'sd-demo-customer');
      const text = node('span', 'sd-demo-customer-copy'); text.append(node('strong', '', ticket.name), node('small', '', ticket.channel));
      row.append(node('span', 'sd-demo-avatar', initials(ticket.name)), text, node('span', '', `#${ticket.id} →`)); body.append(row);
    }
  }
  function reports() {
    body.append(node('p', 'sd-demo-intro', tr('A snapshot of the tickets in this demo.', 'Сводка по обращениям в этом демо.')));
    const metrics = node('div', 'sd-demo-metrics');
    for (const [title, value] of [[tr('Total', 'Всего'), state.tickets.length], [words.active, counts().active], [words.closed, counts().closed]]) {
      const item = node('div', 'sd-demo-metric'); item.append(node('span', '', title), node('strong', '', String(value))); metrics.append(item);
    }
    body.append(metrics, node('h4', 'sd-demo-report-title', tr('By status', 'По статусам')));
    for (const status of ['new', 'in_progress', 'waiting', 'closed']) {
      const count = state.tickets.filter(t => t.status === status).length;
      const row = node('div', 'sd-demo-report-row'); const track = node('div', 'sd-demo-track'); const fill = node('span');
      fill.style.width = `${state.tickets.length ? count / state.tickets.length * 100 : 0}%`; track.append(fill);
      row.append(node('span', '', words[status]), track, node('strong', '', String(count))); body.append(row);
    }
    if (!state.tickets.length) body.append(button(words.add, samples, 'sd-demo-report-seed'));
  }
  function integrations() {
    body.append(node('p', 'sd-demo-intro', tr('Four ways to start a conversation. Configure your channels in your own installation.', 'Четыре способа начать разговор. Подключение каналов настраивается в вашей установке.')));
    const grid = node('div', 'sd-demo-integrations');
    const entries = [
      ['Email', '@', tr('Turn incoming emails into tickets. Reply from the workspace.', 'Входящие письма становятся обращениями. Отвечайте из кабинета.'), tr('Connect your support mailbox via IMAP and SMTP with an app password.', 'Подключите ящик поддержки через IMAP и SMTP с паролем приложения.')],
      ['Slack', '#', tr('Keep support conversations in their original threads.', 'Сохраняйте переписку в исходных тредах.'), tr('Connect your Slack app and configure its events endpoint.', 'Подключите приложение Slack и настройте адрес приёма событий.')],
      [tr('Website chat', 'Чат на сайте'), '◌', tr('Let visitors ask for help where they already are.', 'Помогайте посетителям прямо на сайте.'), tr('Add the widget to your site and allow your website origin.', 'Добавьте виджет на сайт и разрешите его домен.')],
      ['Telegram', '↗', tr('Receive customer messages through your support bot.', 'Принимайте сообщения клиентов через бота поддержки.'), tr('Add a bot token in your installation. Telegram is optional.', 'Добавьте токен бота в своей установке. Telegram необязателен.')],
    ];
    for (const [name, icon, text, more] of entries) {
      const card = node('details', 'sd-demo-integration'); const summary = node('summary'); const symbol = node('span', 'sd-demo-channel-icon', icon); symbol.setAttribute('aria-hidden', 'true');
      summary.append(symbol, node('strong', '', name), node('span', 'sd-demo-chevron', '+'));
      card.append(summary, node('p', '', text), node('p', 'sd-demo-integration-more', more)); grid.append(card);
    }
    body.append(grid, node('p', 'sd-demo-intro', tr('No connections are made in the demo.', 'В демо подключения не выполняются.')));
  }
  function render() {
    body.replaceChildren(); body.classList.remove('sd-demo-detail'); heading.textContent = words[state.view];
    add.hidden = state.tickets.length > 0 || state.selected !== null; reset.disabled = !state.tickets.length;
    navigation.querySelectorAll('button').forEach(b => {
      if (b.dataset.view === state.view) b.setAttribute('aria-current', 'page'); else b.removeAttribute('aria-current');
    });
    const ticket = state.tickets.find(t => t.id === state.selected);
    if (ticket) detail(ticket);
    else ({tickets, customers, reports, integrations})[state.view]();
  }
  try {
    chrome(); render(); root.hidden = false;
    const fallback = document.getElementById('demo-fallback'); if (fallback) fallback.hidden = true;
  } catch (error) {
    root.hidden = true; // The static product screenshots remain usable.
    console.warn('Product demo could not initialize. Static preview retained.');
  }
})();
