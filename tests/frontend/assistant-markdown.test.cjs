const {test} = require('node:test');
const assert = require('node:assert/strict');
const {render, cells} = require('../../src/dashboard/static/assistant-markdown.js');
function fixture() {
  const doc = {createElement: tag => new Node(tag), createTextNode: value => {const n = new Node('#text'); n.textContent=value; return n;}};
  class Node {
    constructor(tag) {this.tag=tag;this.ownerDocument=doc;this.children=[];this.attrs={};this.value='';}
    set textContent(value) {this.value=value;this.children=[];}
    get textContent() {return this.value+this.children.map(c=>c.textContent).join('');}
    appendChild(child) {this.children.push(child);return child;}
    replaceChildren() {this.children=[];this.value='';}
    setAttribute(k,v) {this.attrs[k]=v;}
  }
  const root = new Node('div');
  const all = (node=root) => [node,...node.children.flatMap(all)];
  return {root,all};
}
test('pipe comparisons render semantic headers and cells', () => {
  const {root,all}=fixture(); render(root,'| Metric | Previous | Current |\n|---|---:|---:|\n| Created | 24 | **56** |\n| Closed | 24 | 31 |');
  assert.equal(all().filter(n=>n.tag==='table').length,1);
  assert.equal(all().filter(n=>n.tag==='th'&&n.attrs.scope==='col').length,3);
  assert.equal(all().filter(n=>n.tag==='td').length,6);
  assert.equal(all().find(n=>n.tag==='strong').textContent,'56');
});
test('unsafe HTML, links and images stay inert text', () => {
  const {root,all}=fixture(); const text='<img src=x onerror=alert(1)>\n<script>bad()</script>\n[click](javascript:bad())\n![image](https://evil.test)';
  render(root,text); assert.ok(root.textContent.includes('<script>bad()</script>'));
  assert.ok(all().every(n=>!['img','script','a','iframe'].includes(n.tag)));
  assert.ok(all().every(n=>!Object.keys(n.attrs).some(k=>/^on|src|href/.test(k))));
});
test('escaped pipes, code and malformed tables retain their meaning', () => {
  assert.deepEqual(cells('| one\\|two | `a|b` |'),['one|two','`a|b`']);
  const {root,all}=fixture();render(root,'```js\nconst value = "<b>literal</b>";\n```\n| broken | header |\n|not a separator|ok|');
  assert.ok(all().find(n=>n.tag==='pre').textContent.includes('<b>literal</b>'));
  assert.equal(all().filter(n=>n.tag==='table').length,0);
});
test('headings, lists and renderer size limit', () => {
  const {root,all}=fixture();render(root,'## Overview\n- First\n- Second\n\n1. One\n2. Two\n'+'x'.repeat(20000));
  assert.equal(all().filter(n=>n.tag==='li').length,4);
  assert.ok(all().some(n=>n.tag==='h3')); assert.ok(root.textContent.length<=12000);
});
