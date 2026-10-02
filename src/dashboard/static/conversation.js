/* Message actions and live revisions. Provider permissions are checked again by the server. */
window.SpConversation = (() => {
  'use strict';
  let config, dialog, editor, error, selected;
  const el = (tag, cls, text) => {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    return node;
  };
  function button(label, action) {
    const b = el('button', 'message-action', label); b.type = 'button';
    b.addEventListener('click', action); return b;
  }
  function resetReply() {
    document.getElementById('reply-parent').value = '';
    document.getElementById('reply-context').replaceChildren();
    document.getElementById('reply-context').hidden = true;
  }
  function reply(m) {
    document.getElementById('mode-reply')?.click();
    const box = document.getElementById('reply-context');
    document.getElementById('reply-parent').value = m.id;
    box.hidden = false;
    box.replaceChildren(el('span', '', `${config.labels.reply}: ${(m.text || m.file_name || '').slice(0, 240)}`),
                        button(config.labels.cancel, resetReply));
    document.getElementById('reply-text').focus();
  }
  function edit(m) {
    selected = m; editor.value = m.text; error.textContent = '';
    dialog.showModal(); window.SpAutoGrow?.resize(editor); editor.focus();
  }
  function confirmWithdrawal() {
    return new Promise(resolve => {
      const prompt = el('dialog', 'conversation-edit');
      const title = el('h2', '', config.labels.confirmRemove);
      title.id = 'withdraw-message-title'; prompt.setAttribute('aria-labelledby', title.id);
      const actions = el('div', 'conversation-actions');
      let approved = false;
      const cancel = button(config.labels.cancel, () => prompt.close());
      const remove = button(config.labels.remove, () => { approved = true; prompt.close(); });
      remove.classList.add('danger'); actions.append(cancel, remove);
      prompt.append(title, actions); document.body.append(prompt);
      prompt.addEventListener('close', () => { prompt.remove(); resolve(approved); }, {once:true});
      prompt.showModal(); cancel.focus();
    });
  }
  function reconcile(m) {
    const prompt = el('dialog','conversation-edit');
    const title = el('h2','',config.labels.reconcile); title.id='reconcile-message-title';
    prompt.setAttribute('aria-labelledby',title.id);
    const notice=el('p','mutation-notice',config.labels.reconcileHint);
    const errors=el('p','mutation-notice'); errors.setAttribute('role','alert');
    const actions=el('div','conversation-actions');
    async function submit(applied) {
      actions.querySelectorAll('button').forEach(b=>b.disabled=true);
      try {
        const response=await fetch(`/tickets/${config.ticket}/message/${m.id}/reconcile`,{
          method:'POST',body:new URLSearchParams({revision:m.revision,applied,csrf_token:config.csrf})
        });
        const data=await response.json();
        if (!response.ok || !data.ok) { errors.textContent=data.error || config.labels.failed; return; }
        prompt.close(); location.reload();
      } catch (_) { errors.textContent=config.labels.failed; }
      finally { actions.querySelectorAll('button').forEach(b=>b.disabled=false); }
    }
    actions.append(button(config.labels.cancel,()=>prompt.close()),
      button(config.labels.reconcileApplied,()=>submit('yes')),button(config.labels.reconcileUnchanged,()=>submit('no')));
    prompt.append(title,notice,errors,actions);document.body.append(prompt);
    prompt.addEventListener('close',()=>prompt.remove(),{once:true});prompt.showModal();
  }
  function decorate(wrap, m) {
    if (!config || !wrap) return;
    const fingerprint = JSON.stringify(m);
    if (wrap.dataset.chatFingerprint === fingerprint) return;
    wrap.dataset.chatFingerprint = fingerprint;
    wrap.classList.add('message-card');
    wrap.dataset.sender = m.sender_type; wrap.dataset.internal = String(m.is_internal);
    wrap.dataset.revision = m.revision || 0;
    const bubble = wrap.firstElementChild;
    let content = bubble.querySelector('.message-text');
    if (content) {
      content.textContent = m.text || '';
      content.classList.toggle('line-through', Boolean(m.deleted_at));
    }
    if (bubble.querySelector('.message-menu-trigger[aria-expanded="true"]')) window.SpMessageMenu.close();
    bubble.querySelectorAll('.message-menu-trigger,.conversation-actions,.reply-quote,.mutation-notice,.revision-label').forEach(n => n.remove());
    // The original server-rendered form is replaced with one shared action implementation.
    bubble.querySelectorAll('form[action$="/delete"]').forEach(n => n.remove());
    if (m.reply) {
      const quote = button(`${m.reply.author}: ${m.reply.deleted ? config.labels.withdrawn : m.reply.text}`, () => {
        document.querySelector(`[data-message-id="${Number(m.reply.id)}"]`)?.scrollIntoView({block:'center',behavior:'smooth'});
      });
      quote.className = 'reply-quote';
      bubble.insertBefore(quote, content || bubble.firstChild);
    }
    if (m.edited_at) bubble.append(el('span', 'revision-label', config.labels.edited));
    if (m.deleted_at) bubble.append(el('span', 'revision-label', config.labels.withdrawn));
    if (m.mutation_status) bubble.append(el('div', 'mutation-notice',
      m.mutation_status === 'uncertain' ? config.labels.uncertain : config.labels.pending));
    const actions = [];
    if (m.text) actions.push({label:config.labels.copy, action:async () => {
      try { await navigator.clipboard.writeText(m.text); }
      catch (_) { window.showToast?.(config.labels.failed, 'error'); }
    }});
    if (m.mutation_status==='uncertain' && ['owner','admin'].includes(config.role)) {
      actions.push({label:config.labels.reconcile,action:()=>reconcile(m)});
    }
    if (config.canWrite && m.can_reply) actions.push({label:config.labels.reply,action:()=>reply(m)});
    const owner = ['owner', 'admin'].includes(config.role) || m.operator_id === config.operator;
    if (config.canWrite && owner && m.can_edit) actions.push({label:config.labels.edit,action:()=>edit(m)});
    if (config.canWrite && owner && m.sender_type === 'admin' && !m.is_internal && !m.deleted_at) actions.push({
      label:config.labels.remove, danger:true, disabled:!m.can_delete,
      hint: m.mutation_status ? config.labels.uncertain : config.labels.removeUnavailable,
      action: async () => {
      if (!await confirmWithdrawal()) return;
      const form = el('form'); form.method = 'post';
      form.action = `/tickets/${config.ticket}/message/${Number(m.id)}/delete`;
      const token = el('input'); token.type = 'hidden'; token.name = 'csrf_token'; token.value = config.csrf;
      form.append(token); document.body.append(form); form.submit();
    }});
    window.SpMessageMenu.attach(bubble, actions, config.labels.actions);
  }
  function boot(c) {
    config = c;
    dialog = el('dialog', 'conversation-edit');
    const form = el('form');
    const title = el('h2', '', c.labels.edit); title.id = 'edit-message-title';
    dialog.setAttribute('aria-labelledby', title.id);
    editor = el('textarea'); editor.rows = 3; editor.maxLength = 4096;
    editor.setAttribute('aria-label', c.labels.edit);
    error = el('p', 'mutation-notice'); error.setAttribute('role', 'alert');
    const footer = el('div', 'conversation-actions');
    const save = el('button', 'message-action primary', c.labels.save); save.type = 'submit';
    footer.append(button(c.labels.cancel, () => dialog.close()), save);
    form.append(title, editor, error, footer); dialog.append(form); document.body.append(dialog);
    form.addEventListener('submit', async event => {
      event.preventDefault(); if (!selected || save.disabled) return;
      save.disabled = true; error.textContent = '';
      try {
        const response = await fetch(`/tickets/${c.ticket}/message/${selected.id}/edit`, {
          method:'POST', body:new URLSearchParams({text:editor.value, revision:selected.revision || 0, csrf_token:c.csrf})
        });
        const data = await response.json();
        if (!response.ok || !data.ok) { error.textContent = data.error || c.labels.failed; return; }
        dialog.close();
        selected.text = editor.value.trim(); selected.revision = (selected.revision || 0) + 1;
        selected.edited_at = new Date().toISOString();
        decorate(document.querySelector(`[data-message-id="${selected.id}"]`), selected);
      } catch (_) { error.textContent = c.labels.uncertain; }
      finally { save.disabled = false; }
    });
    document.querySelectorAll('[data-chat]').forEach(w => {
      try { decorate(w, JSON.parse(w.dataset.chat)); } catch (_) { /* Keep the readable server transcript. */ }
    });
    document.getElementById('mode-note')?.addEventListener('click', resetReply);
  }
  function updates(rows) {
    rows.forEach(m => decorate(document.querySelector(`[data-message-id="${Number(m.id)}"]`), m));
  }
  function sources(rows) {
    const box = document.getElementById('knowledge-sources'); if (!box) return;
    box.replaceChildren();
    if (!rows?.length) return;
    box.append(el('span', '', config.labels.sources));
    rows.forEach(s => {
      if (!s.url?.startsWith('/help/')) return;
      const a = el('a', '', s.title); a.href = s.url; a.target = '_blank'; a.rel = 'noopener'; box.append(a);
    });
  }
  return {boot, decorate, updates, sources};
})();
