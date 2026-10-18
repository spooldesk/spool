const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
test('exported inline JavaScript remains syntactically intact',()=>{
  const unique=new Set();
  for(const language of ['en','ru'])for(const file of fs.readdirSync('docs/demo/'+language)){
    const html=fs.readFileSync('docs/demo/'+language+'/'+file,'utf8');
    for(const [,attributes,body] of html.matchAll(/<script\b([^>]*)>([\s\S]*?)<\/script>/gi)){
      if(attributes.includes('application/json')||!body.trim()||unique.has(body))continue;
      new vm.Script(body,{filename:language+'/'+file});unique.add(body);
    }
  }
  assert.ok(unique.size>20);
});
function demo({lang='en',tickets=[{id:1,status:'active',stage:'new'}]}={}){
  const changes={}; const events=[];
  const window={SPOOLDEMO:{lang,root:'https://spooldesk.net/demo/',route:'/tickets/1',state:{changes,chats:{},actions:{}},tickets,api:{'/tickets/1/messages.json':{messages:[{id:1,text:'Example request'}]}},rows:{}},history:{replaceState(){}},addEventListener(){}};
  const sandbox={window,parent:{postMessage:message=>events.push(message)},document:{addEventListener(){}},navigator:{},HTMLFormElement:function(){},URL,URLSearchParams,Response,Request,FormData,Blob,Date,console};
  vm.runInNewContext(fs.readFileSync('docs/demo/bridge.js','utf8'),sandbox);
  return {window,changes,events};
}
test('unknown and external fetches are denied; fixture reads stay local',async()=>{
  const {window}=demo();
  for(const url of ['https://external.example/collect','/manage/ai','/assistant/uploads?filename=secret.txt'])assert.equal((await window.fetch(url,{method:'POST',body:'secret'})).status,403);
  const response=await window.fetch('/tickets/1/messages.json');
  assert.equal((await response.json()).messages[0].text,'Example request');
});
test('AI demo labels its example and cannot change a ticket before confirmation',async()=>{
  const {window,changes}=demo();
  const response=await window.fetch('/assistant/chat',{method:'POST',body:JSON.stringify({message:'Move #1 to in progress'})});
  const data=await response.json();
  assert.match(data.answer,/Scripted demo example/);
  assert.equal(changes[1],undefined);
  await window.fetch('/assistant/actions/'+data.action.id,{method:'POST',body:JSON.stringify({decision:'confirm'})});
  assert.equal(changes[1].stage,'in_progress');
  assert.equal((await window.fetch('/assistant/actions/'+data.action.id,{method:'POST',body:JSON.stringify({decision:'confirm'})})).status,409);
  const history=await (await window.fetch('/assistant/conversations/'+data.conversation_id)).json();
  assert.equal(history.turns[0].result.action.status,'applied');
});
test('stage changes stay in demo state and reject unsupported stages',async()=>{
  const {window,changes}=demo();
  const response=await window.fetch('/api/tickets/1/stage',{method:'POST',body:JSON.stringify({stage:'waiting'})});
  assert.equal(response.status,200);assert.equal(changes[1].stage,'waiting');
  assert.equal((await window.fetch('/api/tickets/1/stage',{method:'POST',body:'{"stage":"anything"}'})).status,400);
});
// The two suggestion cards are the first thing a visitor clicks in the demo
// assistant. "Need a reply" used to fall into the draft-a-reply branch, because
// that branch matched any "reply" or "ответ", so the first answer anyone saw
// was a customer draft in response to a request for a list of tickets.
const QUEUE=[{id:1,status:'active',stage:'new',category:'Network'},{id:2,status:'active',stage:'waiting',category:'Billing'},
  {id:3,status:'active',stage:'in_progress',category:'Access'},{id:4,status:'closed',stage:'new',category:'Other'}];
async function ask(question,lang='en'){
  const {window}=demo({lang,tickets:QUEUE});
  return (await (await window.fetch('/assistant/chat',{method:'POST',body:JSON.stringify({message:question})})).json()).answer;
}
for(const [lang,question] of [['en','Find active tickets that still need a reply.'],['ru','Найди активные обращения, которые ещё ждут ответа оператора.']]){
  test(`"needs a reply" lists the team's tickets, not a draft (${lang})`,async()=>{
    const answer=await ask(question,lang);
    assert.doesNotMatch(answer,/Draft reply|Черновик ответа/);
    assert.match(answer,/#1\b/);assert.match(answer,/#3\b/);
    assert.doesNotMatch(answer,/#2\b/, 'waiting for the customer: the next move is theirs');
    assert.doesNotMatch(answer,/#4\b/, 'closed tickets need nothing');
  });
}
for(const [lang,question] of [['en','Analyze the workload and compare the last 30 days with the previous period.'],['ru','Проанализируй нагрузку и сравни последние 30 дней с предыдущим периодом.']]){
  test(`workload is a breakdown that points to the real period comparison (${lang})`,async()=>{
    const answer=await ask(question,lang);
    assert.doesNotMatch(answer,/Draft reply|Черновик ответа/);
    assert.match(answer,/Reports|Отчёты/);
  });
}
test('an explicit request still gets a draft',async()=>{
  assert.match(await ask('Draft a reply for #1'),/Draft reply/);
  assert.match(await ask('Подготовь ответ для #1','ru'),/Черновик ответа/);
});
