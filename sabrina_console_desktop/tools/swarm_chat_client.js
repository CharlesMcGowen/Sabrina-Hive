/* Shared by the standalone portal and the Control Center. */
(() => {
  const embedded = !!document.getElementById('sc-input');
  const field = document.getElementById(embedded ? 'sc-input' : 'msgBox');
  const button = document.getElementById(embedded ? 'sc-send-btn' : 'sendBtn');
  const status = document.getElementById(embedded ? 'sc-status-pill' : 'statusPill');
  const mode = document.getElementById(embedded ? 'sc-mode' : 'modeSel');
  const target = document.getElementById(embedded ? 'sc-target' : 'targetSel');
  const base = embedded ? 'http://127.0.0.1:8095' : '';
  const key = 'sabrina-chat-pending-v1';
  let busy = false;
  let pending = null;
  const notice = document.createElement('div');
  notice.style.cssText = 'padding:8px 22px;color:#93c5fd;white-space:pre-wrap';
  notice.setAttribute('role', 'status');
  field.parentElement.before(notice);
  const scope = document.createElement('div');
  scope.textContent = 'Discussion only: model replies do not execute repairs or launch missions.';
  scope.style.cssText = 'padding:6px 22px;color:#94a3b8;font-size:12px';
  notice.before(scope);
  const check = document.createElement('button');
  check.textContent = 'Check pending response';
  check.hidden = true;
  field.parentElement.append(check);
  async function api(path, options = {}) {
    const response = await fetch(base + '/api/swarm-chat' + path,
      {...options, signal: AbortSignal.timeout(15000)});
    const data = await response.json();
    if (!response.ok) {
      const error = new Error(data.detail || ('HTTP ' + response.status));
      error.status = response.status;
      throw error;
    }
    return data;
  }
  function release() {
    pending = null; busy = false; button.disabled = false; check.hidden = true;
    try { localStorage.removeItem(key); } catch (_) {}
  }
  async function poll() {
    if (!pending) return;
    busy = true; button.disabled = true; check.hidden = true;
    try {
      const job = await api('/jobs/' + pending.operation_id);
      const seconds = Math.max(0, Math.round(Date.now()/1000 - job.created));
      status.textContent = job.progress + ' · ' + seconds + 's';
      if (job.state === 'running') { setTimeout(poll, 1500); return; }
      if (job.result) {
        const hist = await api('/history');
        (embedded ? scRenderHistory : renderHistory)(hist.history || []);
        const synthesis = job.result.synthesis;
        if (synthesis && !synthesis.ok) {
          status.textContent = 'Member replies ready; synthesis failed: ' + synthesis.error;
        }
      }
      notice.textContent = job.state === 'interrupted' ? job.progress : '';
      release();
    } catch (error) {
      status.textContent = 'Status unavailable: ' + error.message;
      notice.textContent = 'Your message is retained. Check its status before sending again; a network timeout does not cancel model work.';
      check.hidden = false;
    }
  }
  async function send() {
    if (busy || pending) return;
    const message = field.value.trim();
    if (!message) return;
    pending = {operation_id: crypto.randomUUID(), message, mode: mode.value, target: target.value || 'local:qwen3:14b'};
    try { localStorage.setItem(key, JSON.stringify(pending)); } catch (_) {}
    busy = true; button.disabled = true;
    notice.textContent = 'You: ' + message;
    status.textContent = 'Submitting message…';
    try {
      await api('/jobs', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(pending)});
      if (field.value.trim() === message) field.value = '';
      await poll();
    } catch (error) {
      status.textContent = 'Submission: ' + error.message;
      if (error.status && error.status < 500) {
        release();
      } else {
        notice.textContent = 'Submission outcome unknown. Your draft is retained; check the receipt before retrying.';
        check.hidden = false;
      }
    }
  }
  check.onclick = poll;
  if (embedded) window.scSend = send; else window.sendMsg = send;
  try { pending = JSON.parse(localStorage.getItem(key)); } catch (_) {}
  if (pending) { notice.textContent = 'Pending message: ' + pending.message; poll(); }
})();
