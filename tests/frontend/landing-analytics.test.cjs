const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const root = path.resolve(__dirname, '../..');
const code = fs.readFileSync(path.join(root, 'docs/analytics.js'), 'utf8');
const endpoint = import('data:text/javascript;base64,' + Buffer.from(fs.readFileSync(path.join(root, 'functions/events.js'))).toString('base64'));
const payload = {event:'download_click',page:'/',source:'direct',medium:'none',campaign:'none',demo:'none'};
async function send(data=payload, options={}) {
  const points=[];
  const {onRequest}=await endpoint;
  const headers={Origin:'https://spooldesk.net','Content-Type':'application/json',...options.headers};
  const request=new Request(options.url || 'https://spooldesk.net/events',{
    method:options.method || 'POST', headers,
    ...(options.method==='GET' ? {} : {body:options.raw ?? JSON.stringify(data)})
  });
  const env=options.env ?? {LANDING_EVENTS:{writeDataPoint(p){points.push(p);}}};
  const response=await onRequest({request,env});
  return {status:response.status,points,cache:response.headers.get('Cache-Control')};
}
test('endpoint writes only fixed event dimensions, not request identifiers',async()=>{
  const result=await send(payload,{headers:{Cookie:'private=value','CF-Connecting-IP':'127.0.0.9',Referer:'https://spooldesk.net/?email=private@example.com'}});
  assert.equal(result.status,204);
  assert.deepEqual(result.points,[{indexes:['download_click'],blobs:['download_click','/','direct','none','none','none'],doubles:[1]}]);
  assert.equal(result.cache,'no-store');
  for(const demo of ['product','ai']) assert.equal((await send({...payload,event:'demo_open',demo})).status,204);
  assert.equal((await send({...payload,event:'sales_contact',page:'/ru/'})).status,204);
});
test('endpoint rejects wrong origin, preview, method, content type and oversized bodies',async()=>{
  for(const [options,status] of [
    [{headers:{Origin:'https://evil.example'}},403],
    [{headers:{'Sec-Fetch-Site':'cross-site'}},403],
    [{url:'https://preview.spooldesk.pages.dev/events'},404],
    [{method:'GET'},405], [{headers:{'Content-Type':'text/plain'}},415],
    [{raw:'x'.repeat(1025)},413], [{raw:'{broken'},400]
  ]) { const r=await send(payload,options); assert.equal(r.status,status);assert.equal(r.points.length,0); }
});
test('endpoint rejects arbitrary data and inconsistent event/demo combinations',async()=>{
  for(const data of [null,[],{}, {...payload,email:'private@example.com'}, {...payload,source:'private'},
    {...payload,event:'demo_open'}, {...payload,demo:'ai'}, {...payload,page:'/tickets/1'}]) {
    const r=await send(data);assert.equal(r.status,400);assert.equal(r.points.length,0);
  }
});
test('privacy signals and binding failures do not create points',async()=>{
  for(const headers of [{DNT:'1'},{'Sec-GPC':'1'}]) {const r=await send(payload,{headers});assert.equal(r.status,204);assert.equal(r.points.length,0);}
  assert.equal((await send(payload,{env:{}})).status,503);
  assert.equal((await send(payload,{env:{LANDING_EVENTS:{writeDataPoint(){throw Error('offline');}}}})).status,503);
});
function browser(options={}) {
  const calls=[];let click;
  vm.runInNewContext(code,{
    location:{hostname:'spooldesk.net',pathname:'/',search:'',...options.location},
    navigator:options.navigator || {},
    document:{referrer:options.referrer || '',addEventListener(name,fn){assert.equal(name,'click');click=fn;}},
    URL,URLSearchParams,Set,fetch(url,settings){calls.push({url,...settings});return Promise.resolve();}
  });
  function act(action,demo,trusted=true){
    const target={dataset:{analytics:action},matches:()=>true,closest:selector=>(selector==='#product-demo'&&demo==='product')||(selector==='#ai'&&demo==='ai')};
    click?.({isTrusted:trusted,target:{closest:()=>target}});
  }
  return {calls,act,enabled:!!click};
}
test('client records three types of intent, deduplicates and ignores scripted clicks',()=>{
  const b=browser();b.act('download_click',null,false);assert.equal(b.calls.length,0);
  b.act('download_click');b.act('download_click');b.act('sales_contact');b.act(null,'product');b.act(null,'ai');b.act(null,'ai');
  assert.equal(b.calls.length,4);
  for(const call of b.calls){assert.equal(call.url,'/events');assert.equal(call.credentials,'omit');assert.equal(call.keepalive,true);}
  assert.deepEqual(b.calls.map(c=>JSON.parse(c.body).event),['download_click','sales_contact','demo_open','demo_open']);
});
test('client never counts previews, unrelated pages or opted-out browsers',()=>{
  for(const options of [{location:{hostname:'spooldesk.pages.dev'}},{location:{hostname:'localhost'}},{location:{pathname:'/404.html'}},{navigator:{doNotTrack:'1'}},{navigator:{globalPrivacyControl:true}}]) assert.equal(browser(options).enabled,false);
});
test('campaigns are allowlisted; raw URLs and text are never sent',()=>{
  const b=browser({location:{search:'?utm_source=github&utm_medium=community&utm_campaign=first-pilots-2026-10&email=private@example.com',pathname:'/ru/'}});
  b.act('sales_contact');assert.deepEqual(JSON.parse(b.calls[0].body),{...payload,event:'sales_contact',page:'/ru/',source:'github',medium:'community',campaign:'first-pilots-2026-10'});
  const invalid=browser({location:{search:'?utm_source=private@example.com&utm_campaign=secret'},referrer:'https://www.google.com/search?q=private'});
  invalid.act('download_click');const p=JSON.parse(invalid.calls[0].body);assert.equal(p.source,'google');assert.equal(p.medium,'organic');assert.equal(p.campaign,'none');assert.ok(!invalid.calls[0].body.includes('private'));
});
