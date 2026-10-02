/* Small text-only Markdown renderer: no HTML parsing, links, images or remote assets. */
(function (scope) {
  'use strict';
  function cells(line) {
    line = line.trim().replace(/^\|/, '').replace(/\|$/, '');
    const result = []; let cell = '', code = false;
    for (let i = 0; i < line.length; i++) {
      if (line[i] === '\\' && line[i + 1] === '|') {cell += '|'; i++; continue;}
      if (line[i] === '`') code = !code;
      if (line[i] === '|' && !code) {result.push(cell.trim()); cell = '';}
      else cell += line[i];
    }
    result.push(cell.trim()); return result;
  }
  function inline(node, value) {
    const doc = node.ownerDocument;
    const tokens = /(`[^`\n]+`|\*\*[^*\n]+\*\*|\*[^*\n]+\*)/g;
    let last = 0, match;
    while ((match = tokens.exec(value))) {
      node.appendChild(doc.createTextNode(value.slice(last, match.index)));
      const kind = match[0][0] === '`' ? 'code' : match[0].startsWith('**') ? 'strong' : 'em';
      const child = doc.createElement(kind), trim = kind === 'strong' ? 2 : 1;
      child.textContent = match[0].slice(trim, -trim); node.appendChild(child);
      last = tokens.lastIndex;
    }
    node.appendChild(doc.createTextNode(value.slice(last)));
  }
  function render(container, input) {
    const doc = container.ownerDocument;
    container.replaceChildren();
    const lines = String(input || '').slice(0, 12000).replace(/\r\n?/g, '\n').split('\n');
    const add = (tag, value, parent = container) => {
      const node = doc.createElement(tag); if (value !== undefined) inline(node, value);
      parent.appendChild(node); return node;
    };
    let i = 0;
    while (i < lines.length) {
      const line = lines[i];
      if (!line.trim()) {i++; continue;}
      if (/^\s*```/.test(line)) {
        const block = []; i++;
        while (i < lines.length && !/^\s*```/.test(lines[i])) block.push(lines[i++]);
        if (i < lines.length) i++;
        const pre = add('pre'); pre.className = 'assistant-md-code';
        const code = doc.createElement('code'); code.textContent = block.join('\n'); pre.appendChild(code); continue;
      }
      const head = cells(line), separator = i + 1 < lines.length ? cells(lines[i + 1]) : [];
      if (line.includes('|') && head.length >= 2 && head.length <= 12 && separator.length === head.length && separator.every(c => /^:?-{3,}:?$/.test(c))) {
        const wrap = add('div'); wrap.className = 'assistant-md-table-wrap'; wrap.tabIndex = 0;
        const table = add('table', undefined, wrap); table.className = 'assistant-md-table';
        const tr = add('tr', undefined, add('thead', undefined, table));
        head.forEach(c => {const th = add('th', c, tr); th.setAttribute('scope', 'col');});
        const body = add('tbody', undefined, table); i += 2; let rows = 0;
        while (i < lines.length && lines[i].includes('|') && lines[i].trim() && rows < 100) {
          const values = cells(lines[i]); if (values.length !== head.length) break;
          const row = add('tr', undefined, body); values.forEach(c => add('td', c, row)); i++; rows++;
        }
        continue;
      }
      const heading = /^(#{1,4})\s+(.+)$/.exec(line);
      if (heading) {add(heading[1].length <= 2 ? 'h3' : 'h4', heading[2]); i++; continue;}
      const list = /^\s*(?:([-*+])|\d+[.)])\s+(.+)$/.exec(line);
      if (list) {
        const ordered = !list[1], node = add(ordered ? 'ol' : 'ul');
        while (i < lines.length) {
          const item = /^\s*(?:([-*+])|\d+[.)])\s+(.+)$/.exec(lines[i]);
          if (!item || !item[1] !== ordered) break;
          add('li', item[2], node); i++;
        }
        continue;
      }
      add('p', line); i++;
    }
  }
  const api = {render, cells};
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else scope.SpooldeskMarkdown = api;
})(typeof window !== 'undefined' ? window : {});
