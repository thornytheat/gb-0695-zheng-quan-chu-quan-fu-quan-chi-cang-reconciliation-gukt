const state = { active: false, run: JSON.parse(document.getElementById('conv-run').textContent) };
const errorBox = () => document.getElementById('error');

async function call(url, payload) {
  if (state.active) return;
  state.active = true;
  const box = errorBox(); if (box) box.textContent = '处理中…';
  for (const b of document.querySelectorAll('button')) b.disabled = true;
  try {
    const options = { method: 'POST', body: payload };
    if (!(payload instanceof FormData)) {
      options.headers = { 'Content-Type': 'application/json' };
      options.body = JSON.stringify(payload);
    }
    const resp = await fetch(url, options);
    const result = await resp.json();
    if (!resp.ok) throw Error(result.message || '请求失败');
    if (result.manifest) {
      location.href = '/conv/work/1?run_id=' + encodeURIComponent(result.manifest.run_id);
    } else {
      location.reload();
    }
  } catch (e) {
    if (box) box.textContent = e.message;
  } finally {
    state.active = false;
    for (const b of document.querySelectorAll('button')) b.disabled = false;
  }
}

document.getElementById('demo').addEventListener('click', () => call('/conv/api/run', {}));
document.getElementById('submit').addEventListener('click', () =>
  call('/conv/api/run', { csv: document.getElementById('conv-source').value }));
document.getElementById('upload').addEventListener('click', () => {
  const f = document.getElementById('sourcefile').files[0];
  if (!f) { errorBox().textContent = '请选择转股文件。'; return; }
  const data = new FormData(); data.append('file', f);
  call('/conv/api/run', data);
});
document.querySelectorAll('[data-check]').forEach(b => b.addEventListener('click', () =>
  call('/conv/api/review', { run_id: state.run.run_id, revision: state.run.revision, decision: b.dataset.check })));
document.getElementById('publish').addEventListener('click', () =>
  call('/conv/api/publish', { run_id: state.run.run_id }));
document.getElementById('print').addEventListener('click', () => print());
