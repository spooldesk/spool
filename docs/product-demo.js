/* Launch the actual generated dashboard inside an isolated frame. */
(() => {
  'use strict';
  const root=document.getElementById('product-demo');if(!root)return;
  const ru=document.documentElement.lang==='ru', tr=(en,r)=>ru?r:en;
  const url=new URL((ru?'../':'')+'demo/?lang='+(ru?'ru':'en'),document.baseURI);
  const launch=document.createElement('button');launch.type='button';launch.className='dashboard-demo-launch';
  const poster=document.createElement('img');poster.src=new URL('poster.jpg',url).href;poster.alt=tr('Actual Spooldesk ticket queue with fictional customers','Настоящая очередь Spooldesk с вымышленными клиентами');poster.className='dashboard-demo-poster';poster.loading='lazy';
  const title=document.createElement('strong');title.textContent=tr('Explore the real dashboard','Откройте настоящий кабинет');
  const copy=document.createElement('span');copy.textContent=tr('Tickets, conversations, customers, reports and settings. The same interface you install.','Тикеты, переписки, клиенты, отчёты и настройки. Тот же интерфейс, который вы устанавливаете.');
  const cta=document.createElement('span');cta.className='dashboard-demo-cta';cta.textContent=tr('Open interactive demo →','Открыть интерактивное демо →');
  const overlay=document.createElement('div');overlay.className='dashboard-demo-overlay';overlay.append(title,copy,cta);launch.append(poster,overlay);root.replaceChildren(launch);root.hidden=false;
  const fallback=document.getElementById('demo-fallback');if(fallback)fallback.hidden=true;
  launch.addEventListener('click',()=>{const frame=document.createElement('iframe');frame.src=url.href;frame.title=tr('Real Spooldesk dashboard demo','Демо настоящего кабинета Spooldesk');frame.className='dashboard-demo-frame';frame.referrerPolicy='no-referrer';root.replaceChildren(frame);});
})();
