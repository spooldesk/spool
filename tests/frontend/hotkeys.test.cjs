const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function page(enabled = true) {
  const source = fs.readFileSync(path.join(__dirname, '../../src/dashboard/templates/app_base.html'), 'utf8');
  const start = source.indexOf('/* Keyboard shortcuts.');
  const code = source.slice(start, source.indexOf('</script>', start))
    .replace(/{{[^}]+}}/g, String(enabled));
  let keydown;
  const hidden = new Set(['hidden']);
  const nodes = {
    'keys-overlay': {classList: {contains:k=>hidden.has(k), toggle(k,on){on?hidden.add(k):hidden.delete(k);}}, addEventListener(){}},
    'keys-close': {focus(){}, addEventListener(){}},
    'reply-text': {focus(){this.focused=true;}},
    'mode-reply': {click(){this.clicked=true;}},
    'mode-note': {click(){this.clicked=true;}},
  };
  const document = {activeElement:{tagName:'BODY'}, getElementById:id=>nodes[id],
    querySelector:()=>null, addEventListener(name, fn){if(name==='keydown')keydown=fn;}};
  const location={href:'/me'};
  vm.runInNewContext(code,{document,location,Date});
  function key(key, code, extra={}) {keydown?.({key,code,preventDefault(){},...extra});}
  return {key, nodes, document, location, hidden};
}

test('physical navigation and reply shortcuts work in Cyrillic layout', () => {
  const p=page(); p.key('п','KeyG'); p.key('и','KeyB');
  assert.equal(p.location.href,'/board');
  p.key('к','KeyR'); assert.equal(p.nodes['mode-reply'].clicked,true);
  assert.equal(p.nodes['reply-text'].focused,true);
});
test('typing, composition and disabled shortcuts do not navigate', () => {
  const p=page(); p.document.activeElement={tagName:'TEXTAREA'};
  p.key('g','KeyG'); p.key('t','KeyT'); assert.equal(p.location.href,'/me');
  p.document.activeElement={tagName:'BODY'};
  p.key('g','KeyG',{isComposing:true}); p.key('t','KeyT');
  assert.equal(p.location.href,'/me');
  const off=page(false); off.key('g','KeyG'); off.key('t','KeyT');
  assert.equal(off.location.href,'/me');
});
test('help opens with physical Shift Slash and blocks navigation until Escape', () => {
  const p=page(); p.key(',','Slash',{shiftKey:true}); assert.equal(p.hidden.has('hidden'),false);
  p.key('g','KeyG'); p.key('t','KeyT'); assert.equal(p.location.href,'/me');
  p.key('Escape','Escape'); assert.equal(p.hidden.has('hidden'),true);
  p.key('g','KeyG'); p.key('t','KeyT'); assert.equal(p.location.href,'/tickets');
});
