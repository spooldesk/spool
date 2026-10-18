/* Offline fixture transport, loaded BEFORE the original dashboard scripts.
 * No request can fall through to a server. CSP also blocks connections/forms.
 * All user edits live in the parent frame's memory and vanish on reset/reload.
 */
(() => {
  'use strict';
  const cfg=window.SPOOLDEMO, ru=cfg.lang==='ru', tr=(en,r)=>ru?r:en;
  const origin=new URL(cfg.root).origin;
  const state=cfg.state, tickets=cfg.tickets, pageURL=new URL(cfg.route,origin);
  const post=(action,extra={})=>parent.postMessage({type:'spooldesk-demo',action,...extra},origin);
  const save=()=>post('state',{state});
  const note=text=>{post('notice',{text});window.showToast?.(text,'info');};
  const unavailable=()=>note(tr('Explore this form. Saving settings and external services are disabled in this demo.','Изучите форму. Сохранение настроек и внешние сервисы в этом демо отключены.'));
  function path(value){try{const u=new URL(value,origin);return u.origin===origin?u.pathname+u.search:null;}catch(_){return null;}}
  window.demoNavigate=value=>{const p=path(value);if(p)post('navigate',{route:p});else unavailable();};
  const json=(data,status=200)=>Promise.resolve(new Response(JSON.stringify(data),{status,headers:{'Content-Type':'application/json'}}));
  function changed(id){return state.changes[id]||(state.changes[id]={messages:[],updates:{}});}
  function ticket(id){return {...tickets.find(t=>t.id===Number(id)),...state.changes[id]};}
  function body(value){if(typeof value==='string'){try{return JSON.parse(value);}catch(_){return Object.fromEntries(new URLSearchParams(value));}}return value instanceof FormData||value instanceof URLSearchParams?Object.fromEntries(value):{};}
  function messages(id){const fixture=cfg.api[`/tickets/${id}/messages.json`]||{};const change=state.changes[id]||{};return [...(fixture.messages||[]),...(change.messages||[])].map(m=>({...m,...change.updates?.[m.id]}));}
  function updateTicket(id,changes){Object.assign(changed(id),changes);save();}
  function actionForm(form){
    const endpoint=path(form.action);if(!endpoint)return unavailable();
    const match=endpoint.match(/^\/tickets\/(\d+)\/(reply|note|stage|assign|close|category)$/);
    if(!match)return unavailable();
    const [,id,action]=match, values=Object.fromEntries(new FormData(form)), change=changed(id);
    if(action==='reply'||action==='note'){
      if([...form.querySelectorAll('input[type=file]')].some(input=>input.files.length))return note(tr('File previews are local. Remove the file to try a text reply; sending files requires your installation.','Файлы можно просмотреть локально. Уберите файл для пробного текстового ответа; отправка файлов доступна в вашей установке.'));
      if(!String(values.text||'').trim())return;
      if(messages(id).length>150)return unavailable();
      const parentId=Number(values.reply_to_id||values.reply_to_message_id||0), parentMessage=messages(id).find(m=>m.id===parentId);
      const message={id:100000+Date.now()%100000000,text:String(values.text).slice(0,4096),sender_type:'admin',sender_label:'Alex Morgan',operator_id:1,is_internal:action==='note',revision:0,can_reply:action!=='note',can_edit:true,can_delete:action!=='note',created_at:new Date().toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'}),reply:parentMessage?{id:parentMessage.id,text:parentMessage.text.slice(0,240),author:parentMessage.sender_type,deleted:false}:null};
      change.messages.push(message);if(action==='reply')change.stage='waiting';
      save();note(tr('Reply saved in this browser demo.','Ответ сохранён в демо браузера.'));window.demoNavigate(pageURL.pathname);return;
    }
    if(action==='stage')change.stage=values.stage;
    if(action==='assign'){change.assigned_admin_id=Number(values.admin_id||values.assigned_admin_id)||null;change.assigned_label=form.querySelector('select')?.selectedOptions[0]?.textContent.trim()||'—';}
    if(action==='close')change.status='closed';
    if(action==='category')change.category=values.category;
    save();window.demoNavigate(pageURL.pathname);
  }
  // Guard native form.submit() too (message withdrawal uses it).
  HTMLFormElement.prototype.submit=function(){const p=path(this.action)||'';const match=p.match(/^\/tickets\/(\d+)\/message\/(\d+)\/delete$/);if(match){const [_,id,mid]=match;changed(id).updates[mid]={deleted_at:true,text:tr('Message withdrawn in this demo.','Сообщение удалено в этом демо.'),can_edit:false,can_reply:false,can_delete:false};save();window.demoNavigate(pageURL.pathname);}else actionForm(this);};
  navigator.sendBeacon=()=>false;
  // about:srcdoc has no relative URL for the app's chat-history replaceState.
  // The parent demo router owns navigation; chat state stays in parent memory.
  window.history.replaceState=()=>{};
  window.XMLHttpRequest=function(){throw new Error('Network disabled in demo');};
  window.WebSocket=function(){throw new Error('Network disabled in demo');};
  window.EventSource=function(){throw new Error('Network disabled in demo');};
  window.fetch=async (input,init={})=>{
    const address=path(input instanceof Request?input.url:input);
    if(!address)return json({ok:false,error:'External requests are disabled in this demo.'},403);
    const u=new URL(address,origin), p=u.pathname, method=(init.method||'GET').toUpperCase(), values=body(init.body);
    let match;
    if((match=p.match(/^\/tickets\/(\d+)\/messages.json$/))){const id=match[1],rows=messages(id),after=Number(u.searchParams.get('after_id')||0), t=ticket(id);return json({...cfg.api[p],ok:true,status:t.status,messages:rows.filter(m=>m.id>after),latest_id:Math.max(0,...rows.map(m=>m.id)),updates:rows.filter(m=>state.changes[id]?.updates?.[m.id])});}
    if((match=p.match(/^\/api\/tickets\/(\d+)\/presence$/)))return json({ok:true,others:[]});
    if((match=p.match(/^\/api\/tickets\/(\d+)\/stage$/))){if(!['new','in_progress','waiting'].includes(values.stage))return json({ok:false},400);updateTicket(match[1],{stage:values.stage});return json({ok:true});}
    if((match=p.match(/^\/tickets\/(\d+)\/message\/(\d+)\/edit$/))){const [_,id,mid]=match;changed(id).updates[mid]={text:String(values.text||'').slice(0,4096),edited_at:new Date().toISOString(),revision:Number(values.revision||0)+1};save();return json({ok:true});}
    if(p==='/assistant/conversations')return json({conversations:Object.values(state.chats).map(c=>({id:c.id,title:c.title}))});
    if((match=p.match(/^\/assistant\/conversations\/([^/]+)$/))){if(method==='DELETE'){delete state.chats[match[1]];save();return json({ok:true});}return json(state.chats[match[1]]||{id:match[1],turns:[]});}
    if(p==='/assistant/chat')return assistant(values);
    if((match=p.match(/^\/assistant\/actions\/([^/]+)$/))){const a=state.actions[match[1]];if(!a||a.status!=='pending')return json({error:tr('This demo action has already been handled.','Это действие демо уже обработано.')},409);a.status=values.decision==='confirm'?'applied':'cancelled';if(a.status==='applied')updateTicket(a.ticket_id,{stage:a.after.stage});save();return json({ok:true,cancelled:a.status==='cancelled'});}
    if(method==='GET'&&Object.hasOwn(cfg.api,p))return json(cfg.api[p]);
    return json({ok:false,error:tr('This action needs your own installation. No data was sent.','Для этого действия нужна ваша установка. Данные не отправлялись.')},403);
  };
  function assistant(values){
    const question=String(values.message||'').slice(0,2000), id=values.conversation_id||'demo-'+Date.now();
    const chat=state.chats[id]||(state.chats[id]={id,title:question.slice(0,50),turns:[]});
    if(chat.turns.length>=20)return json({error:tr('Reset the demo to start again.','Сбросьте демо, чтобы начать снова.')},429);
    const requested=Number(question.match(/#(\d+)/)?.[1]), target=tickets.find(t=>t.id===requested)||tickets.find(t=>t.status==='active');
    const intro=tr('**Scripted demo example — no model was called.**\n\n','**Сценарный пример демо — модель не вызывалась.**\n\n');
    let answer=intro+tr('In your installation the assistant can search permitted tickets, read conversations, draft replies and propose changes. Try: “Show active tickets”, “Draft a reply”, or “Move #83 to in progress”.','В вашей установке ассистент ищет доступные тикеты, читает переписку, готовит черновики и предлагает изменения. Попробуйте: «Покажи активные тикеты», «Подготовь ответ» или «Переведи #83 в работу».');
    let proposal=null,sources=[];
    if(/progress|status|move|статус|работу|переведи/i.test(question)&&target){const before=ticket(target.id);proposal={id:'action-'+Date.now(),ticket_id:target.id,kind:'stage',before:{stage:before.stage},after:{stage:'in_progress'},status:'pending'};state.actions[proposal.id]=proposal;answer=intro+tr(`I propose moving ticket #${target.id} to In progress. Review the change below before confirming.`,`Предлагаю перевести тикет #${target.id} в работу. Проверьте изменение перед подтверждением.`);}
    else if(/need(?:s)? (?:a |an )?(?:reply|answer)|without (?:a )?(?:reply|answer)|unanswered|awaiting (?:a )?(?:reply|answer)|ждут ответа|нужен ответ|без ответа|не ответил/i.test(question)){const label={new:tr('New','Новое'),in_progress:tr('In progress','В работе'),waiting:tr('Waiting for customer','Ждёт клиента')};const open=tickets.filter(t=>{const x=ticket(t.id);return x.status==='active'&&x.stage!=='waiting';});answer=intro+tr(`**${open.length} active tickets are waiting on your team**\n\nTickets waiting for the customer are left out — the next move there is theirs.\n\n| Ticket | Stage | Category |\n|---|---|---|\n`,`**${open.length} активных обращений ждут ответа команды**\n\nОбращения, которые ждут клиента, не включены — там следующий шаг за ним.\n\n| Обращение | Стадия | Категория |\n|---|---|---|\n`)+open.map(t=>{const x=ticket(t.id);return `| #${t.id} | ${label[x.stage]||x.stage} | ${x.category||'—'} |`;}).join('\n');sources=open.slice(0,6).map(t=>({url:`/tickets/${t.id}`,title:`#${t.id} · ${ticket(t.id).category||''}`}));}
    else if(/\b(?:draft|write|prepare|compose)\b|черновик|подготов|напиши|составь/i.test(question)&&target){answer=intro+tr('**Draft reply**\n\nHi! Thanks for reporting this. Could you share the exact error message and when the issue started? That will help us investigate the next step.\n\nReview this draft before sending it to the customer.','**Черновик ответа**\n\nЗдравствуйте! Спасибо, что сообщили. Уточните, пожалуйста, текст ошибки и когда возникла проблема. Это поможет выбрать следующий шаг проверки.\n\nПроверьте черновик перед отправкой клиенту.');}
    else if(/workload|compare|period|30 days|нагруз|сравн|период/i.test(question)){const label={new:tr('New','Новое'),in_progress:tr('In progress','В работе'),waiting:tr('Waiting for customer','Ждёт клиента')};const open=tickets.filter(t=>ticket(t.id).status==='active');const by=key=>{const c={};for(const t of open){const v=ticket(t.id)[key]||'—';c[v]=(c[v]||0)+1;}return Object.entries(c);};answer=intro+tr(`**Current workload: ${open.length} active tickets**\n\n| Stage | Tickets |\n|---|---:|\n`,`**Текущая нагрузка: ${open.length} активных обращений**\n\n| Стадия | Обращения |\n|---|---:|\n`)+by('stage').map(([k,v])=>`| ${label[k]||k} | ${v} |`).join('\n')+tr('\n\n| Category | Tickets |\n|---|---:|\n','\n\n| Категория | Обращения |\n|---|---:|\n')+by('category').map(([k,v])=>`| ${k} | ${v} |`).join('\n')+tr('\n\nIn your installation the assistant compares periods from your own history. In this demo, the period comparison is on the Reports page.','\n\nВ вашей установке ассистент сравнивает периоды по вашей истории. В демо сравнение периодов — на странице «Отчёты».');sources=[{url:'/reports',title:tr('Reports','Отчёты')},{url:'/overview',title:tr('Overview','Обзор')}];}
    else if(/ticket|summar|analy|workload|тикет|обращени|анализ|нагруз|проблем/i.test(question)){const open=tickets.filter(t=>ticket(t.id).status==='active');const counts={};for(const t of open)counts[t.category||'Other']=(counts[t.category||'Other']||0)+1;answer=intro+tr('**Open tickets in this fictional dataset**\n\n| Category | Tickets |\n|---|---:|\n','**Активные тикеты в демонстрационных данных**\n\n| Категория | Тикеты |\n|---|---:|\n')+Object.entries(counts).map(([k,v])=>`| ${k} | ${v} |`).join('\n');sources=open.slice(0,4).map(t=>({url:`/tickets/${t.id}`,title:`#${t.id} · ${t.category}`}));}
    const result={ok:true,answer,action:proposal,sources,model:tr('Scripted demo','Сценарное демо'),tokens_in:0,tokens_out:0,conversation_id:id,ticket_id:target?.id};chat.turns.push({question,result,status:'complete',attachments:[]});save();return json(result);
  }
  document.addEventListener('click',event=>{
    if(event.target.closest('#assistant-attach')){event.preventDefault();event.stopImmediatePropagation();note(tr('File analysis is available in your installation. This demo uses fictional data only.','Анализ файлов доступен в вашей установке. Здесь используются только демонстрационные данные.'));return;}
    if(event.target.closest('#notify-toggle')){event.preventDefault();event.stopImmediatePropagation();unavailable();return;}
    const link=event.target.closest('a');if(!link)return;
    const href=link.getAttribute('href')||'';
    if(link.dataset.demoDownload==='true')return;
    if(href.startsWith('#'))return;
    if(href.startsWith('blob:')){event.preventDefault();unavailable();return;}
    event.preventDefault();event.stopImmediatePropagation();
    if(/\/demo\/attachments\/\d+\.png$/.test(href)){showImage(href);return;}
    if(href.includes('/export.'))return exportCSV();
    window.demoNavigate(href);
  },true);
  document.addEventListener('submit',event=>{
    const form=event.target;
    if(form.id==='assistant-form'||form.closest('.conversation-edit'))return;
    event.preventDefault();event.stopImmediatePropagation();
    if(form.method.toLowerCase()==='get'){const p=path(form.action)||pageURL.pathname;window.demoNavigate(p.split('?')[0]+'?'+new URLSearchParams(new FormData(form)));return;}
    actionForm(form);
  },true);
  function exportCSV(){const rows=[['Ticket','Status','Stage','Category'],...tickets.map(t=>{const r=ticket(t.id);return [r.id,r.status,r.stage,r.category];})];const csv=rows.map(r=>r.map(v=>'"'+String(v??'').replaceAll('"','""')+'"').join(',')).join('\r\n');const a=document.createElement('a');a.href=URL.createObjectURL(new Blob([csv],{type:'text/csv;charset=utf-8'}));a.download='spooldesk-fictional-tickets.csv';a.dataset.demoDownload='true';a.addEventListener('click',e=>e.stopPropagation());document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(a.href),1000);}
  function showImage(url){const d=document.createElement('dialog');d.style.cssText='max-width:90vw;background:#09090b;color:white;border:1px solid #444;border-radius:12px;padding:16px';const img=document.createElement('img');img.src=url;img.style.maxWidth='75vw';const b=document.createElement('button');b.textContent=tr('Close','Закрыть');b.onclick=()=>d.close();d.append(b,img);document.body.append(d);d.addEventListener('close',()=>d.remove());d.showModal();}
  window.addEventListener('DOMContentLoaded',()=>{
    // Never invite visitors to enter real passwords/API keys in a public demo.
    document.querySelectorAll('input[type=password]').forEach(n=>{n.value='';n.disabled=true;n.placeholder=tr('Disabled in public demo','Отключено в публичном демо');});
    const id=pageURL.pathname.match(/^\/tickets\/(\d+)$/)?.[1];
    const stageLabel=s=>({new:tr('New','Новое'),in_progress:tr('In progress','В работе'),waiting:tr('Waiting for customer','Ожидание клиента'),closed:tr('Closed','Закрыто')})[s]||s;
    function selectValue(n,value){n.value=value;const label=n.closest('.ui-select')?.querySelector('button>span');if(label)label.textContent=n.selectedOptions[0]?.textContent||'';}
    if(id){const t=ticket(id),change=state.changes[id];
      if(change){document.querySelectorAll('select[name=stage]').forEach(n=>selectValue(n,t.stage));document.querySelectorAll('select[name=admin_id]').forEach(n=>selectValue(n,t.assigned_admin_id||''));document.querySelectorAll('select[name=category]').forEach(n=>selectValue(n,t.category||''));const badge=document.getElementById('status-badge');if(badge){badge.firstElementChild.textContent=t.status==='closed'?stageLabel('closed'):tr('Active','Активный');const stage=badge.nextElementSibling;if(stage)stage.textContent=t.status==='closed'?'':stageLabel(t.stage);}if(t.status==='closed'){document.getElementById('composer')?.classList.add('hidden');document.getElementById('closed-note')?.classList.remove('hidden');}window.SpConversation?.updates(messages(id));}
    }
    document.querySelectorAll('a[href*="/export."]').forEach(a=>{a.textContent=tr('Download demo CSV','Скачать демо CSV');});
    // Reports are a fixed fixture snapshot, not a live reporting engine.
    if(['/overview','/reports','/ai'].includes(pageURL.pathname))note(tr('Sample report · fixed fictional period; filters require your installation.','Пример отчёта · фиксированный демонстрационный период; фильтры доступны в вашей установке.'));
    if(pageURL.pathname==='/board'){
      for(const card of document.querySelectorAll('.card[data-ticket-id]')){const t=ticket(card.dataset.ticketId),column=t.status==='closed'?'closed':t.stage;const zone=document.querySelector(`.drop-zone[data-column="${column}"]`);if(zone){zone.append(card);card.dataset.stage=column;}else if(t.status==='closed')card.remove();}
      for(const column of document.querySelectorAll('section[data-column]')){const count=column.querySelector('header span:last-child');if(count)count.textContent=column.querySelectorAll('.card').length;}
    }
    if(pageURL.pathname==='/tickets'){
      const params=pageURL.searchParams,q=(params.get('q')||'').toLowerCase(),stage=params.get('stage'),category=params.get('category'),team=params.get('team_id'),assigned=params.get('assigned');
      for(const n of document.querySelectorAll('input[type=date]'))if(params.has(n.name))n.value=params.get(n.name);
      const search=document.querySelector('input[name=q]');if(search)search.value=q;
      for(const n of document.querySelectorAll('form[method=get] select'))if(params.has(n.name))n.value=params.get(n.name);
      const closed=params.get('status')==='closed',tbody=document.querySelector('tbody'),columnCount=document.querySelectorAll('thead th').length;
      if(tbody&&Object.keys(cfg.rows).length){tbody.replaceChildren();for(const original of [...tickets].reverse()){
        const r=ticket(original.id);if((r.status==='closed')!==closed)continue;
        const template=document.createElement('template');template.innerHTML=cfg.rows[r.id]||'';const row=template.content.querySelector('tr');if(!row)continue;
        while(row.cells.length>columnCount)row.deleteCell(-1);while(row.cells.length<columnCount){const td=row.insertCell();td.className='px-4 py-2.5 text-zinc-500';td.textContent='—';}
        const status=row.cells[1].querySelector('span');if(status)status.textContent=stageLabel(r.status==='closed'?'closed':r.stage);
        if(state.changes[r.id]?.assigned_label)row.cells[4].textContent=r.assigned_label;
        if(state.changes[r.id]?.category)row.cells[5].textContent=r.category;
        row.hidden=Boolean((q&&!row.textContent.toLowerCase().includes(q)&&!messages(r.id).some(m=>(m.text||'').toLowerCase().includes(q)))||(stage&&r.stage!==stage)||(category&&r.category!==category)||(team&&String(r.team_id)!==team)||(assigned&&assigned!=='me'&&assigned!=='none'&&String(r.assigned_admin_id)!==assigned)||(assigned==='none'&&r.assigned_admin_id)||(assigned==='me'&&r.assigned_admin_id!==1));tbody.append(row);
        const date=row.cells[6].textContent.trim().slice(0,10);if((params.get('date_from')&&date<params.get('date_from'))||(params.get('date_to')&&date>params.get('date_to')))row.hidden=true;
      }}
      document.querySelectorAll('.ticket-status-tabs a').forEach(a=>{const isClosed=new URL(a.href).searchParams.get('status')==='closed';const count=tickets.filter(t=>(ticket(t.id).status==='closed')===isClosed).length;const badge=a.querySelector('span');if(badge)badge.textContent=count;});
      const total=document.querySelector('h1')?.parentElement.querySelector('p span');if(total&&tbody)total.textContent=[...tbody.rows].filter(r=>!r.hidden).length;
    }
    if(pageURL.pathname==='/assistant')note(tr('Real assistant interface · scripted examples · no model connection','Настоящий интерфейс ассистента · сценарные примеры · без подключения модели'));
  });
})();
