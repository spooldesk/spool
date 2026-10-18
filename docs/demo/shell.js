/* Same rendered templates/styles as the product. Only the data transport is a sandbox. */
(() => {
  'use strict';
  const params=new URLSearchParams(location.search), lang=params.get('lang')==='ru'?'ru':'en';
  const ru=lang==='ru', tr=(en,ruText)=>ru?ruText:en;
  const frame=document.getElementById('dashboard'), notice=document.getElementById('demo-notice');
  let manifest, state={changes:{},chats:{},actions:{}}, navigation=0;
  const root=new URL('./',location.href), cache=new Map();
  document.documentElement.lang=lang;
  document.getElementById('demo-label').textContent=tr('Real dashboard · fictional data','Настоящий интерфейс · демоданные');
  document.getElementById('demo-reset').textContent=tr('Reset demo','Сбросить демо');
  const expand=document.getElementById('demo-expand');expand.textContent=tr('Open full screen ↗','Открыть отдельно ↗');expand.href=`?lang=${lang}`;
  const safeJSON=value=>JSON.stringify(value).replaceAll('<','\\u003c');
  function route(value){try{const u=new URL(value,'https://demo.invalid');if(u.origin!=='https://demo.invalid')return null;return u.pathname+u.search+u.hash;}catch(_){return null;}}
  function keyFor(value){const u=new URL(value,'https://demo.invalid');if(u.pathname==='/')return '/tickets';if(u.pathname==='/tickets'&&u.searchParams.get('status')==='closed')return '/tickets?status=closed';return u.pathname;}
  async function navigate(value,fromHistory=false){
    const requested=route(value);if(!requested)return;
    const key=manifest.languages[lang][requested]?requested:keyFor(requested);
    const filename=manifest.languages[lang][key];
    if(!filename){notice.textContent=tr('This action requires your own installation.','Для этого действия нужна ваша установка.');return;}
    const turn=++navigation;notice.textContent='';
    try{
      if(!cache.has(filename)){const response=await fetch(new URL(filename,root));if(!response.ok)throw new Error('page');cache.set(filename,await response.text());}
      if(turn!==navigation)return;
      const config={lang,route:requested,root:root.href,state,tickets:manifest.tickets,api:manifest.api,rows:manifest.rows?.[lang]||{}};
      const csp=`default-src 'none'; script-src 'unsafe-inline' ${location.origin}; style-src 'unsafe-inline' ${location.origin}; img-src ${location.origin} data: blob:; font-src ${location.origin}; connect-src 'none'; form-action 'none'; object-src 'none'; base-uri ${location.origin}`;
      const inject=`<base href="${location.origin}/"><meta http-equiv="Content-Security-Policy" content="${csp}"><meta name="robots" content="noindex"><script>window.SPOOLDEMO=${safeJSON(config)};<\/script><script src="${root.href}bridge.js"><\/script>`;
      let page=cache.get(filename).replace('<head>','<head>'+inject);
      // Fixture-only attachments; never link an authenticated attachment endpoint.
      page=page.replace(/\/tickets\/attachment\/(\d+)/g,'/demo/attachments/$1.png');
      frame.srcdoc=page;
      if(!fromHistory)history.replaceState(null,'','#'+encodeURIComponent(requested));
      expand.href=`?lang=${lang}#${encodeURIComponent(requested)}`;
      document.title=tr('Spooldesk — dashboard demo','Spooldesk — демо кабинета');
    }catch(_){const box=document.getElementById('demo-error');box.hidden=false;box.textContent=tr('The demo could not load. Reload this page to try again.','Не удалось загрузить демо. Обновите страницу.');}
  }
  window.addEventListener('message',event=>{
    if(event.source!==frame.contentWindow||event.origin!==location.origin||!event.data||event.data.type!=='spooldesk-demo')return;
    const message=event.data;
    if(message.action==='navigate')navigate(message.route);
    if(message.action==='state')state=message.state;
    if(message.action==='notice')notice.textContent=String(message.text||'').slice(0,180);
  });
  document.getElementById('demo-reset').addEventListener('click',()=>{state={changes:{},chats:{},actions:{}};navigate('/tickets');});
  window.addEventListener('hashchange',()=>navigate(decodeURIComponent(location.hash.slice(1))||'/tickets',true));
  fetch(new URL('manifest.json',root)).then(r=>{if(!r.ok)throw new Error('manifest');return r.json();}).then(data=>{manifest=data;navigate(decodeURIComponent(location.hash.slice(1))||'/tickets',true);}).catch(()=>{document.getElementById('demo-error').hidden=false;document.getElementById('demo-error').textContent=tr('Demo unavailable. Please reload.','Демо недоступно. Обновите страницу.');});
})();
