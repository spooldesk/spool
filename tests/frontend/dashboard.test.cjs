const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function source(name) {
  return fs.readFileSync(path.join(__dirname, '../../src/dashboard/static', name), 'utf8');
}

function fetchPage(status = 200) {
  const requests = [], notices = [];
  const location = {origin: 'https://help.example', pathname: '/tickets/7', search: '?queue=mine'};
  const document = {
    baseURI: location.origin + location.pathname,
    querySelector: () => ({content: 'test-csrf-token'}),
    getElementById: id => notices.find(n => n.id === id),
    createElement: () => ({setAttribute() {}, appendChild(child) {this.link = child;}}),
    body: {dataset: {sessionExpired: 'Session expired', signIn: 'Sign in'},
           appendChild: notice => notices.push(notice)},
  };
  const window = {fetch: async (...args) => {requests.push(args); return {status};}};
  vm.runInNewContext(source('dashboard-fetch.js'), {window, document, location, Headers, Request, URL});
  return {window, requests, notices, location};
}

test('same-origin mutating requests receive CSRF without changing caller options', async () => {
  const page = fetchPage();
  const options = {method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{"text":"draft"}'};
  await page.window.fetch('/tickets/7/note', options);
  const [, sent] = page.requests[0];
  assert.equal(sent.headers.get('X-CSRF-Token'), 'test-csrf-token');
  assert.equal(sent.headers.get('X-Requested-With'), 'Spooldesk');
  assert.equal(sent.body, options.body);
  assert.deepEqual(options.headers, {'Content-Type': 'application/json'});
  assert.equal(sent.headers.get('Content-Type'), 'application/json');
});

test('Request headers and URL inputs are handled; GET does not carry CSRF', async () => {
  const page = fetchPage();
  await page.window.fetch(new Request('https://help.example/tickets/7/note', {
    method: 'POST', headers: {'X-Custom': 'keep', 'X-CSRF-Token': 'explicit'}, body: 'draft',
  }));
  assert.equal(page.requests[0][1].headers.get('X-Custom'), 'keep');
  assert.equal(page.requests[0][1].headers.get('X-CSRF-Token'), 'explicit');
  await page.window.fetch(new URL('https://help.example/tickets'));
  assert.equal(page.requests[1][1].headers.has('X-CSRF-Token'), false);
});

test('external destinations never receive dashboard headers', async () => {
  const page = fetchPage();
  for (const url of ['https://other.example/upload', '//other.example/upload']) {
    const options = {method: 'POST', body: 'public data'};
    await page.window.fetch(url, options);
    assert.equal(page.requests.at(-1)[1], options);
    assert.equal(options.headers, undefined);
  }
});

test('401 keeps the page and response intact and offers one separate sign-in tab', async () => {
  const page = fetchPage(401);
  const response = await page.window.fetch('/tickets/7/note', {method: 'POST'});
  await page.window.fetch('/tickets/version.json');
  assert.equal(response.status, 401);
  assert.equal(page.location.pathname, '/tickets/7');
  assert.equal(page.notices.length, 1);
  assert.equal(page.notices[0].link.target, '_blank');
  assert.equal(new URL(page.notices[0].link.href, page.location.origin).searchParams.get('next'), '/tickets/7?queue=mine');
});

function providerPage() {
  const notes = ['ollama', 'anthropic', 'openrouter'].map(providerNote => ({dataset: {providerNote}}));
  const fields = {
    aiProvider: {value: 'ollama', selectedIndex: 0, options: [
      {dataset: {needsKey: '0', hostHint: 'http://localhost:11434'}},
      {dataset: {needsKey: '1', hostHint: ''}},
      {dataset: {needsKey: '1', hostHint: ''}},
    ], addEventListener(type, fn) {this.change = fn;}},
    aiModel: {value: 'my-local-model', setAttribute() {}},
    aiHost: {value: 'http://private.local:11434'},
    aiKey: {value: '', placeholder: 'saved key hint', dataset: {emptyPlaceholder: 'API key'}},
    aiClearKey: {checked: true, parentElement: {}},
    aiKeyInfo: {}, aiSavedStatus: {}, aiHostRow: {}, aiKeyRow: {},
    'models-ollama': {querySelector: () => ({value: 'local-default'})},
    'models-anthropic': {querySelector: () => ({value: 'remote-default'})},
    'models-openrouter': {querySelector: () => ({value: 'anthropic/claude-sonnet-5.5'})},
  };
  vm.runInNewContext(source('ai-settings.js'), {
    document: {getElementById: id => fields[id], querySelectorAll: () => notes},
    window: {addEventListener() {}}, location: {hash: ''},
  });
  return {fields, notes, select(value, index) {
    fields.aiProvider.value = value; fields.aiProvider.selectedIndex = index; fields.aiProvider.change();
  }};
}

test('provider switch keeps model and host drafts separate and clears pending credentials', () => {
  const page = providerPage(), fields = page.fields;
  page.select('anthropic', 1);
  assert.equal(fields.aiHost.value, '');
  assert.equal(fields.aiHost.disabled, true);
  assert.equal(fields.aiModel.value, 'remote-default');
  assert.equal(fields.aiClearKey.checked, false);
  assert.equal(fields.aiClearKey.disabled, true);
  assert.equal(fields.aiSavedStatus.hidden, true);
  fields.aiModel.value = 'custom-remote-model'; fields.aiKey.value = 'unsaved-test-key';
  page.select('ollama', 0);
  assert.equal(fields.aiModel.value, 'my-local-model');
  assert.equal(fields.aiHost.value, 'http://private.local:11434');
  assert.equal(fields.aiKey.value, '');
  assert.equal(fields.aiKey.disabled, true);
  page.select('anthropic', 1);
  assert.equal(fields.aiModel.value, 'custom-remote-model');
  assert.equal(fields.aiHost.value, '');
  assert.equal(fields.aiKey.value, '');
});

test('OpenRouter selection explains its key and model IDs without inheriting a host or credential', () => {
  const page = providerPage(), fields = page.fields;
  fields.aiKey.value = 'unsaved-other-provider-key';
  page.select('openrouter', 2);
  assert.equal(fields.aiModel.value, 'anthropic/claude-sonnet-5.5');
  assert.equal(fields.aiKey.value, '');
  assert.equal(fields.aiHost.value, '');
  assert.equal(fields.aiHost.disabled, true);
  assert.deepEqual(page.notes.filter(n => !n.hidden).map(n => n.dataset.providerNote), ['openrouter']);
});

function composerPage(response) {
  const navigations = [], requests = [];
  const form = {action: 'https://help.example/tickets/7/reply',
    text: 'unsent draft', files: ['screenshot.png'],
    addEventListener(type, fn) {this.submit = fn;}};
  const button = {}, notice = {}, window = {};
  vm.runInNewContext(source('composer-submit.js'), {
    window, URL,
    FormData: class {constructor(form) {this.text = form.text; this.files = form.files;}},
    fetch: async (...args) => {requests.push(args); return await response();},
    location: {origin: 'https://help.example', href: 'https://help.example/tickets/7',
               assign: url => navigations.push(url)},
  });
  window.SpComposerSubmit(form, button, notice, {sessionExpired: 'expired', failed: 'failed', uncertain: 'uncertain'});
  return {form, button, notice, navigations, requests, send: () => form.submit({preventDefault() {}})};
}

test('expired composer submission preserves text and selected files', async () => {
  const page = composerPage(async () => ({status: 401, ok: false}));
  await page.send();
  assert.equal(page.form.text, 'unsent draft');
  assert.deepEqual(page.form.files, ['screenshot.png']);
  assert.equal(page.notice.textContent, 'expired');
  assert.deepEqual(page.navigations, []);
  assert.equal(page.button.disabled, false);
});

test('composer follows a successful queue redirect but keeps server-rejected drafts', async () => {
  const success = composerPage(async () => ({status: 200, ok: true, redirected: true,
    url: 'https://help.example/tickets/8?queue=mine'}));
  await success.send();
  assert.deepEqual(success.navigations, ['https://help.example/tickets/8?queue=mine']);
  const failure = composerPage(async () => ({status: 200, ok: true, redirected: true,
    url: 'https://help.example/tickets/7?err=Ticket%20closed'}));
  await failure.send();
  assert.equal(failure.notice.textContent, 'Ticket closed');
  assert.deepEqual(failure.navigations, []);
  assert.equal(failure.form.text, 'unsent draft');
});

test('composer prevents duplicate in-flight submits and never retries network failures', async () => {
  let reject;
  const page = composerPage(() => new Promise((resolve, fail) => {reject = fail;}));
  const first = page.send();
  await page.send();
  assert.equal(page.requests.length, 1);
  reject(new Error('connection lost'));
  await first;
  assert.equal(page.notice.textContent, 'uncertain');
  assert.equal(page.requests.length, 1);
  assert.equal(page.button.disabled, false);
  assert.equal(page.form.text, 'unsent draft');
});
