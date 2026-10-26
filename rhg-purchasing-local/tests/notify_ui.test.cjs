// Run with node --test tests/notify_ui.test.cjs. No network or email is used.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {join} = require('node:path');
const vm = require('node:vm');

const html = readFileSync(join(__dirname, '../static/index.html'), 'utf8');
const section = html.slice(html.indexOf('let ntStatus = null;'), html.indexOf('function renderSmtp'));

function setup(fetch){
  const nodes = {};
  const timers = new Map();
  let nextTimer = 0;
  const context = vm.createContext({
    fetch, AbortController,
    $: id => nodes[id] ||= {}, can: () => true,
    setTimeout: (fn, delay) => { timers.set(++nextTimer, {fn, delay}); return nextTimer; },
    clearTimeout: id => timers.delete(id),
  });
  vm.runInContext(section, context);
  return {context, nodes, timers, run: code => vm.runInContext(code, context)};
}

test('HTML gateway errors give an HTTP error, not a JSON parser error', async () => {
  const ui = setup(async () => new Response('<html><h1>Gateway Timeout</h1>', {status:504}));
  await assert.rejects(ui.run('notifyRequest("/api/notify/test")'), /HTTP 504/);
  assert.equal(ui.timers.size, 0);
});

test('HTML with status 200 is handled without exposing a JSON parser error', async () => {
  const ui = setup(async () => new Response('<html>Sign in</html>'));
  await assert.rejects(ui.run('notifyRequest("/api/notify/test")'), /unexpected response/);
});

test('expired sessions show a sign-in instruction', async () => {
  const ui = setup(async () => new Response('<html>Login</html>', {status:401}));
  await assert.rejects(ui.run('notifyRequest("/api/notify/test")'), /Sign in again/);
});

test('JSON application errors preserve the real reason', async () => {
  const ui = setup(async () => Response.json({error:'Your role cannot send email tests.'}, {status:403}));
  await assert.rejects(ui.run('notifyRequest("/api/notify/test")'), /Your role cannot send/);
});

test('background tests disable sending until their final result', () => {
  const ui = setup();
  ui.run('trackNotifyTest({id:"job1", status:"queued", company:"SALT"})');
  assert.equal(ui.nodes.ntTest.disabled, true);
  assert.equal([...ui.timers.values()][0].delay, 2000);
  ui.run('trackNotifyTest({id:"job1", status:"sent", result:{status:"sent", to:["test@example.com"]}})');
  assert.equal(ui.nodes.ntTest.disabled, false);
  assert.equal(ui.nodes.ntMsg.textContent, 'Test sent to test@example.com.');
  assert.equal(ui.timers.size, 0);
});

test('polling retries a gateway failure without submitting another email', async () => {
  const requests = [];
  const ui = setup(async (url, options) => {
    requests.push({url, options});
    return new Response('<html>Bad gateway</html>', {status:502});
  });
  ui.run('trackNotifyTest({id:"job1", status:"running", company:"SALT"})');
  ui.timers.clear();
  await ui.run('pollNotifyTest("job1")');
  assert.equal(requests.length, 1);
  assert.match(requests[0].url, /test\?id=job1$/);
  assert.equal(requests[0].options.method, undefined);
  assert.equal(ui.nodes.ntTest.disabled, true);
  assert.equal([...ui.timers.values()][0].delay, 5000);
  assert.match(ui.nodes.ntMsg.textContent, /HTTP 502/);
});

test('SMTP failures are shown instead of being reported as successful sends', () => {
  const ui = setup();
  ui.run('trackNotifyTest({id:"job1", status:"failed", result:{status:"failed", detail:"SMTP authentication rejected"}})');
  assert.equal(ui.nodes.ntMsg.textContent, 'Not sent: SMTP authentication rejected');
  assert.equal(ui.nodes.ntTest.disabled, false);
});

test('refreshing settings resumes the current background test', async () => {
  const ui = setup(async () => Response.json({test:{id:'job1', status:'running', company:'SALT'}}));
  ui.run('function renderNotify(){}');
  await ui.run('loadNotify()');
  assert.equal(ui.run('ntTestId'), 'job1');
  assert.equal(ui.nodes.ntTest.disabled, true);
  assert.equal([...ui.timers.values()][0].delay, 2000);
});

test('successful polling completes and reloads history', async () => {
  const requests = [];
  const job = {id:'job1', status:'sent', result:{status:'sent', to:['test@example.com']}};
  const ui = setup(async url => {
    requests.push(url);
    return Response.json(url.includes('?id=') ? job : {test:job});
  });
  ui.run('function renderNotify(){}');
  ui.run('trackNotifyTest({id:"job1", status:"running", company:"SALT"})');
  await ui.run('pollNotifyTest("job1")');
  assert.equal(ui.run('ntTestId'), null);
  assert.equal(ui.nodes.ntMsg.textContent, 'Test sent to test@example.com.');
  assert.deepEqual(requests, ['/api/notify/test?id=job1', '/api/notify/status']);
  assert.equal(ui.timers.size, 0);
});
