/* Private server-backed history. Every data read and confirmation is authorized again. */
(() => {
  'use strict';
  const root = document.getElementById('assistant');
  const form = document.getElementById('assistant-form');
  if (!root || !form) return;
  const words = JSON.parse(document.getElementById('assistant-strings').textContent);
  const input = document.getElementById('assistant-input');
  const chat = document.getElementById('assistant-chat');
  const thread = document.getElementById('assistant-thread');
  const start = document.getElementById('assistant-start');
  const status = document.getElementById('assistant-status');
  const send = document.getElementById('assistant-send');
  const fresh = document.getElementById('assistant-new');
  const errorSettings = document.getElementById('assistant-error-settings');
  const history = document.getElementById('assistant-history');
  const historyList = document.getElementById('assistant-history-list');
  const historyToggle = document.getElementById('assistant-history-toggle');
  const fileInput = document.getElementById('assistant-file-input');
  const attach = document.getElementById('assistant-attach');
  const filesBox = document.getElementById('assistant-files');
  const preview = document.getElementById('assistant-file-preview');
  let busy = false, ticketId = null, conversationId = null, pendingFiles = [], lastAttempt = null;
  const text = (tag, value, cls) => {const n = document.createElement(tag); n.textContent = value; if (cls) n.className = cls; return n;};
  const button = (label, action, cls) => {const b = text('button', label, cls); b.type = 'button'; b.addEventListener('click', action); return b;};
  function notice(message, error=false, code='') {
    status.textContent = message; status.dataset.error = String(error);
    if (errorSettings) errorSettings.hidden = !['assistant_provider_auth','assistant_provider_permission','assistant_provider_model','assistant_provider_request','assistant_provider_billing','assistant_not_configured'].includes(code);
  }
  function scroll() {chat.scrollTop = chat.scrollHeight;}
  function grow() {input.style.height = 'auto'; input.style.height = Math.min(input.scrollHeight, 180) + 'px';}
  function message(role, content) {
    start.hidden = true;
    const box = text('article', '', `assistant-message assistant-message-${role}`);
    const author = text('div', role === 'user' ? words.assistant_you : words.assistant_title, 'assistant-message-author');
    if (role === 'assistant') {const icon = document.createElement('img'); icon.src = '/demo/static/icons/favicon-96x96.png'; icon.alt = ''; icon.width = 22; icon.height = 22; author.prepend(icon);}
    const body = text('div', '', 'assistant-message-text');
    if (role === 'assistant' && window.SpooldeskMarkdown) window.SpooldeskMarkdown.render(body, content);
    else body.textContent = content;
    box.append(author, body);
    thread.append(box); scroll(); return box;
  }
  async function responseBody(response) {
    if (!response.headers.get('content-type')?.includes('application/json')) throw new Error(words.assistant_unavailable);
    const data = await response.json();
    if (!response.ok) {const error = new Error(data.error || words.assistant_unavailable); error.code = data.code || ''; error.conversationId = data.conversation_id; error.httpStatus = response.status; throw error;}
    return data;
  }
  function proposal(action, parent) {
    const card = text('section', '', 'assistant-action');
    const pending = text('p', words.assistant_no_execution);
    const expiry = text('small', words.assistant_expiry);
    card.append(text('h3', `${words.assistant_proposal} · #${action.ticket_id}`), pending);
    const assigned = id => id == null ? words.assistant_unassigned : `#${id}`;
    let change;
    if (action.kind === 'stage') change = `${words.assistant_stage}: ${words['stage_'+action.before.stage] || action.before.stage} → ${words['stage_'+action.after.stage] || action.after.stage}`;
    else if (action.kind === 'assign') change = `${words.assistant_assignee}: ${assigned(action.before.assignee_id)} → ${action.after.assignee_name || assigned(action.after.assignee_id)}`;
    else change = words.assistant_close;
    card.append(text('p', change, 'assistant-action-change'));
    if (action.kind === 'close') card.append(text('p', words.assistant_close_notice));
    const link = text('a', words.assistant_open_ticket); link.href = `/tickets/${action.ticket_id}`; link.target = '_blank'; link.rel = 'noopener'; card.append(link);
    const controls = text('div', '', 'assistant-action-buttons');
    const state = text('p', ''); state.setAttribute('role', 'status');
    async function decide(decision) {
      controls.querySelectorAll('button').forEach(b => b.disabled = true);
      const payload = new FormData(); payload.set('decision', decision);
      try {
        const result = await responseBody(await fetch(`/assistant/actions/${action.id}`, {method:'POST', body:payload, headers:{Accept:'application/json'}}));
        state.textContent = result.cancelled ? words.assistant_cancelled : words.assistant_applied;
        controls.remove(); pending.remove(); expiry.remove();
      } catch (error) {
        state.textContent = error.message || words.assistant_unavailable;
        // A lost response may already have applied the action. Server tokens are single-use.
        controls.querySelectorAll('button').forEach(b => b.disabled = false);
      }
      scroll();
    }
    controls.append(button(words.assistant_confirm, () => decide('confirm'), 'assistant-primary'), button(words.assistant_cancel, () => decide('cancel')));
    card.append(controls, state, expiry); parent.append(card);
    if (action.status && action.status !== 'pending') {
      controls.remove(); pending.remove(); expiry.remove();
      state.textContent = action.status === 'applied' ? words.assistant_applied : action.status === 'cancelled' ? words.assistant_cancelled : words.assistant_confirmation_expired;
    }
  }
  function setBusy(value) {
    busy = value; send.disabled = value; fresh.disabled = value; attach.disabled = value; input.readOnly = value;
    chat.setAttribute('aria-busy', String(value));
  }
  function remember(id) {
    conversationId = id;
    const url = new URL(location.href);
    if (id) url.searchParams.set('chat', id); else url.searchParams.delete('chat');
    window.history.replaceState({}, '', url.pathname + url.search);
  }
  function reset() {
    thread.querySelectorAll('.assistant-message,.assistant-waiting').forEach(n=>n.remove());
    start.hidden = false; ticketId = null; pendingFiles = []; lastAttempt = null; input.value = ''; paintFiles(); grow(); notice('');
  }
  async function showFile(file) {
    try {
      const data = await responseBody(await fetch(`/assistant/uploads/${file.id}`, {headers:{Accept:'application/json'}}));
      document.getElementById('assistant-file-title').textContent = data.filename;
      document.getElementById('assistant-file-text').textContent = data.text;
      preview.showModal();
    } catch (error) {notice(error.message, true);}
  }
  function fileChips(files, parent, removable=false) {
    files.forEach(file => {
      const chip = text('div', '', 'assistant-file-chip');
      chip.append(button(file.filename, () => showFile(file)));
      if (removable) {
        const remove = button('×', () => {if (!busy) {pendingFiles = pendingFiles.filter(f=>f.id !== file.id); paintFiles();}});
        remove.setAttribute('aria-label', `${words.assistant_remove_file}: ${file.filename}`); chip.append(remove);
      }
      parent.append(chip);
    });
  }
  function paintFiles() {filesBox.replaceChildren(); fileChips(pendingFiles, filesBox, true);}
  function renderReply(result) {
    const box = message('assistant', result.answer);
    if (result.sources?.length) {
      const sources = text('div', '', 'assistant-sources'); sources.setAttribute('aria-label', words.assistant_sources);
      result.sources.forEach(ref => {
        if (!/^\/(tickets|users|manage|overview|reports|board|ai)(\/|$)/.test(ref.url)) return;
        const a = text('a', ref.title); a.href = ref.url; a.target = '_blank'; a.rel = 'noopener'; sources.append(a);
      }); box.append(sources);
    }
    box.append(button(words.assistant_copy, async event => {
      const control = event.currentTarget;
      try {await navigator.clipboard.writeText(result.answer); control.textContent = words.assistant_copied;}
      catch (_) {notice(words.assistant_unavailable, true);}
    }, 'assistant-copy'));
    box.append(text('div', `${result.model} · ${result.tokens_in} / ${result.tokens_out} tokens`, 'assistant-usage'));
    if (result.action) proposal(result.action, box);
    if (result.action_error) {const warning = text('p', words[result.action_error] || result.action_error, 'assistant-status'); warning.dataset.error='true'; box.append(warning);}
    ticketId = result.ticket_id || null;
  }
  async function refreshHistory() {
    const data = await responseBody(await fetch('/assistant/conversations', {headers:{Accept:'application/json'}}));
    historyList.replaceChildren();
    if (!data.conversations.length) historyList.append(text('p', words.assistant_history_empty));
    data.conversations.forEach(item => {
      const row = text('div', '', 'assistant-history-item');
      const open = button(item.title, () => {if (!busy) openChat(item.id);});
      open.setAttribute('aria-current', item.id === conversationId ? 'true' : 'false'); open.title = item.title;
      const remove = button('×', async () => {
        if (busy || !window.confirm(words.assistant_delete_confirm)) return;
        setBusy(true);
        try {
          await responseBody(await fetch(`/assistant/conversations/${item.id}`, {method:'DELETE',headers:{Accept:'application/json'}}));
          if (conversationId === item.id) {reset(); remember(null);}
          await refreshHistory();
        } catch (error) {notice(error.message, true);} finally {setBusy(false);}
      });
      remove.setAttribute('aria-label', `${words.assistant_delete_chat}: ${item.title}`);
      row.append(open, remove); historyList.append(row);
    });
    return data.conversations;
  }
  async function openChat(id) {
    setBusy(true); notice('');
    try {
      const data = await responseBody(await fetch(`/assistant/conversations/${id}`, {headers:{Accept:'application/json'}}));
      reset(); remember(data.id);
      data.turns.forEach(turn => {
        if (turn.status === 'redacted') {message('assistant', words[turn.error]); return;}
        const user = message('user', turn.question); fileChips(turn.attachments || [], user);
        if (turn.result) renderReply(turn.result);
        else message('assistant', words[turn.error] || words.assistant_history_running);
      });
      await refreshHistory(); scroll();
      if (window.innerWidth < 960) showHistory(false);
    } catch (error) {notice(error.message, true);} finally {setBusy(false);}
  }
  function showHistory(value) {history.hidden = !value; historyToggle.setAttribute('aria-expanded', String(value));}
  async function submit(event) {
    event.preventDefault();
    const question = input.value.trim(); if (!question || busy) return;
    setBusy(true);
    notice('');
    const attemptKey = JSON.stringify([conversationId, question, pendingFiles.map(f=>f.id)]);
    const attempt = lastAttempt?.key === attemptKey ? lastAttempt : {key:attemptKey, id:crypto.randomUUID().replaceAll('-','')};
    lastAttempt = attempt;
    const userBox = message('user', question); fileChips(pendingFiles, userBox);
    const waiting = text('div', '', 'assistant-waiting'); waiting.setAttribute('role', 'status');
    for (let i = 0; i < 3; i++) {const dot = text('span', ''); dot.setAttribute('aria-hidden', 'true'); waiting.append(dot);}
    waiting.append(text('p', words.assistant_thinking)); thread.append(waiting); scroll();
    try {
      const result = await responseBody(await fetch('/assistant/chat', {method:'POST', headers:{'Content-Type':'application/json', Accept:'application/json'},
        body:JSON.stringify({message:question, conversation_id:conversationId, request_id:attempt.id,
          attachment_ids:pendingFiles.map(f=>f.id), ticket_id:ticketId, context:root.dataset.context || ''})}));
      remember(result.conversation_id); renderReply(result); lastAttempt = null; pendingFiles = []; paintFiles();
      input.value = ''; grow(); notice(''); scroll();
    } catch (error) {
      if (error.conversationId) remember(error.conversationId);
      if (error.httpStatus) lastAttempt = null;
      userBox.remove(); notice(error.message || words.assistant_unavailable, true, error.code);
    } finally {
      waiting.remove(); setBusy(false); input.focus();
      refreshHistory().catch(()=>{});
    }
  }
  form.addEventListener('submit', submit);
  input.addEventListener('input', grow);
  input.addEventListener('keydown', event => {
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing && !busy) {event.preventDefault(); form.requestSubmit();}
  });
  root.querySelectorAll('[data-prompt]').forEach(b => b.addEventListener('click', () => {if (!busy) {input.value = b.dataset.prompt; grow(); input.focus();}}));
  fresh.addEventListener('click', () => {if (busy) return; reset(); remember(null); refreshHistory().catch(()=>{}); input.focus();});
  historyToggle.addEventListener('click', () => showHistory(history.hidden));
  showHistory(window.innerWidth >= 960);
  document.getElementById('assistant-file-close').addEventListener('click', () => preview.close());
  attach.addEventListener('click', () => fileInput.click());
  fileInput.addEventListener('change', async () => {
    const selected = Array.from(fileInput.files || []); fileInput.value = '';
    if (busy || !selected.length) return;
    if (selected.length + pendingFiles.length > 3) {notice(words.assistant_file_count, true); return;}
    setBusy(true); notice(words.assistant_uploading);
    try {
      for (const file of selected) {
        if (file.size > 2 * 1024 * 1024) throw new Error(words.assistant_file_size);
        pendingFiles.push(await responseBody(await fetch(`/assistant/uploads?filename=${encodeURIComponent(file.name)}`,
          {method:'POST',headers:{'Content-Type':'application/octet-stream',Accept:'application/json'},body:file})));
        paintFiles();
      }
      notice('');
    } catch (error) {notice(error.message, true);} finally {setBusy(false);}
  });
  setBusy(true);
  refreshHistory().then(async rows => {
    const selected = new URL(location.href).searchParams.get('chat');
    if (selected && /^[a-f0-9]{32}$/.test(selected)) await openChat(selected);
    else if (!root.dataset.context && rows.length) await openChat(rows[0].id);
  }).catch(error => notice(error.message, true)).finally(() => setBusy(false));
})();
