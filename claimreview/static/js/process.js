(function () {
  const btn = document.getElementById('process-btn');
  if (!btn) return;
  const claimId = btn.dataset.claimId;
  const statusEl = document.getElementById('process-status');
  const outer = document.getElementById('progress-outer');
  const inner = document.getElementById('progress-inner');
  let pollTimer = null;

  function render(state) {
    if (!state || state.status === 'not_started') {
      statusEl.textContent = '';
      outer.style.display = 'none';
      return;
    }
    outer.style.display = 'block';
    const done = (state.processed || 0) + (state.cached || 0) + (state.failed || 0);
    const total = state.total || 1;
    // Prefer page-level progress (finer-grained, updates as each concurrent
    // Textract call finishes) when there's any in-flight page work; falls
    // back to document-level once nothing is left to analyze concurrently.
    const pct = state.total_pages
      ? Math.min(100, Math.round(((state.pages_done || 0) / state.total_pages) * 100))
      : Math.min(100, Math.round((done / total) * 100));
    inner.style.width = pct + '%';

    if (state.status === 'running') {
      const pageProgress = state.total_pages ? ` — ${state.pages_done || 0}/${state.total_pages} page(s) analyzed` : '';
      const inFlight = (state.current_files || []).length
        ? ` (in progress: ${state.current_files.join(', ')})` : '';
      statusEl.textContent = `Processing... ${done}/${total} document(s)${pageProgress}${inFlight}`;
      btn.disabled = true;
    } else if (state.status === 'completed') {
      statusEl.textContent = `Done. ${state.processed} processed, ${state.cached} already cached.`;
      btn.disabled = false;
      clearInterval(pollTimer);
      refreshDocList();
    } else if (state.status === 'failed') {
      statusEl.textContent = `Finished with ${state.failed} failure(s). ${state.processed} processed, ${state.cached} cached.`;
      btn.disabled = false;
      clearInterval(pollTimer);
      refreshDocList();
    }
  }

  function poll() {
    fetch(`/api/claims/${claimId}/process/status`)
      .then((r) => r.json())
      .then(render);
  }

  function refreshDocList() {
    fetch(`/api/claims/${claimId}/documents`)
      .then((r) => r.json())
      .then((data) => {
        const list = document.getElementById('doc-list');
        if (!list) return;
        list.querySelectorAll('li[data-doc-id]').forEach((li) => {
          const doc = data.documents.find((d) => d.doc_id === li.dataset.docId);
          if (!doc) return;
          const statusSpan = li.querySelector('.doc-status');
          statusSpan.className = 'doc-status ' + (doc.cached ? 'cached' : 'pending');
          statusSpan.textContent = doc.cached ? 'processed' : 'not processed';
        });
      });
  }

  function startPolling() {
    if (pollTimer) clearInterval(pollTimer);
    pollTimer = setInterval(poll, 1500);
  }

  btn.addEventListener('click', () => {
    fetch(`/api/claims/${claimId}/process`, { method: 'POST' })
      .then((r) => r.json().then((data) => ({ ok: r.ok, data })))
      .then(({ ok, data }) => {
        if (!ok) {
          statusEl.textContent = data.error || 'Could not start processing';
          return;
        }
        startPolling();
        poll();
      });
  });

  // resume polling if a run is already in progress (e.g. page reload)
  fetch(`/api/claims/${claimId}/process/status`).then((r) => r.json()).then((state) => {
    render(state);
    if (state.status === 'running') startPolling();
  });
})();
