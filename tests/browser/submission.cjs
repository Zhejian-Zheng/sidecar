const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

// Run the real page controller with a minimal DOM and an unreliable HTTP boundary.
const elements = new Map();
const element = id => {
  if (!elements.has(id)) elements.set(id, {value:'', hidden:false, disabled:false,
    setAttribute(){}, removeAttribute(){}, replaceChildren(){}, append(){}});
  return elements.get(id);
};
const requests = [];
const jobs = new Map();
let failNext = true;
const context = vm.createContext({
  document: {getElementById:element, addEventListener(){}, createElement:()=>element('temp')},
  sessionStorage: {getItem:()=>null, setItem(){}, removeItem(){}},
  crypto: require('node:crypto').webcrypto,
  setInterval(){}, URL, console,
  fetch: async (url, options) => {
    assert.equal(url, '/api/tasks');
    const body = JSON.parse(options.body);
    requests.push(body);
    if (!jobs.has(body.request_id)) jobs.set(body.request_id, {id:body.request_id});
    if (failNext) { failNext = false; throw new Error('Response lost after server commit'); }
    return {ok:true, status:201, json:async()=>jobs.get(body.request_id)};
  },
});
vm.runInContext(fs.readFileSync('src/sidecar/static/app.js', 'utf8'), context);
vm.runInContext("config={mode:'demo'}; refresh=async()=>{};", context);
element('goal').value = 'task';
element('items').value = 'one';
const submit = () => element('task-form').onsubmit({preventDefault(){}});
(async () => {
  await submit();
  await submit();
  assert.equal(jobs.size, 1, 'lost response retry must not create a second task');
  assert.equal(requests[0].request_id, requests[1].request_id);
  await submit();
  assert.equal(jobs.size, 2, 'a new submission after success needs a new identity');
  failNext = true;
  await submit();
  element('goal').value = 'changed task';
  await submit();
  assert.notEqual(requests[3].request_id, requests[4].request_id,
    'edited content must not reuse the failed request identity');
  const before = requests.length;
  await Promise.all([submit(), submit()]);
  assert.equal(requests.length, before + 1, 'concurrent submits must be ignored');
})().catch(error => { console.error(error); process.exitCode = 1; });
