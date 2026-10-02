const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {describe} = require('../../src/dashboard/static/ticket-presence.js');
const labels = {replying:'Reply: {who}',note:'Note: {who}',customer:'Customer: {who}',here:'Here: {who}',hereMany:'Viewing: {who}'};
test('reply, internal note and customer typing remain distinct and names are plain text', () => {
  assert.equal(describe([{name:'<img>',typing:true,replying:true},{name:'Nina',typing:true,replying:false},{name:'Client',typing:true,customer:true}],labels),
    'Reply: <img> Note: Nina Customer: Client');
  assert.equal(describe([],labels),'');
});
function fixture() {
  let now=10000, interval, posted=[];
  const events={};
  const node = () => ({textContent:'',hidden:false,value:'',action:'/tickets/1/reply',classList:{toggle(){}},listeners:{},addEventListener(k,f){this.listeners[k]=f;}});
  const nodes=Object.fromEntries(['reply-form','reply-text','presence','presence-text','composer-presence','mode-note','mode-reply'].map(k=>[k,node()]));
  const document={hidden:false,activeElement:nodes['reply-text'],getElementById:id=>nodes[id],addEventListener(k,f){events[k]=f;}};
  const window={addEventListener(k,f){events[k]=f;}};
  vm.runInNewContext(fs.readFileSync('src/dashboard/static/ticket-presence.js','utf8'),{
    window,document,crypto:{randomUUID:()=> 'a'.repeat(32)},Date:{now:()=>now},URLSearchParams,queueMicrotask,
    navigator:{sendBeacon:(_url,body)=>posted.push(body)},setInterval:f=>interval=f,
    fetch:async(_url,opts)=>{posted.push(opts.body);return {ok:true,json:async()=>({ok:true,others:[]})};}
  });
  window.SpTicketPresence.boot({ticket:1,csrf:'dummy',canWrite:true,labels});
  return {nodes,document,events,posted,tick:()=>interval(),advance:n=>now+=n};
}
const settle=()=>new Promise(resolve=>setImmediate(resolve));
test('periodic heartbeat keeps real typing, clears idle typing and never transmits draft',async()=>{
  const f=fixture(); await settle();
  f.nodes['reply-text'].value='private draft'; f.nodes['reply-text'].listeners.input(); await settle();
  assert.equal(f.posted.at(-1).get('typing'),'1');
  f.advance(3000);f.tick();await settle();
  assert.equal(f.posted.at(-1).get('typing'),'1');
  f.advance(4000);f.tick();await settle();
  assert.equal(f.posted.at(-1).get('typing'),'0');
  assert.ok(f.posted.every(body=>!body.toString().includes('private')));
});
test('hidden tab immediately leaves its own lease; note mode stays internal',async()=>{
  const f=fixture();await settle();
  f.nodes['reply-form'].action='/tickets/1/note';f.nodes['reply-text'].value='Note';
  f.nodes['reply-text'].listeners.input();await settle();
  assert.equal(f.posted.at(-1).get('audience'),'internal');
  f.document.hidden=true;f.events.visibilitychange();
  assert.equal(f.posted.at(-1).get('leaving'),'1');
  assert.equal(f.posted.at(-1).get('client_id'),'a'.repeat(32));
});
