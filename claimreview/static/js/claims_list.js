(function () {
  const list = document.getElementById('claim-list');
  const textInput = document.getElementById('claim-filter-text');
  const statusBar = document.getElementById('claim-filter-status');
  const tagBar = document.getElementById('claim-filter-tags');
  const emptyMsg = document.getElementById('claim-filter-empty');
  if (!list || !textInput || !statusBar) return;

  let activeStatus = 'all';
  const activeTags = new Set();
  // Set by the "Show only these" button on the ?fetch_run= banner: narrows the
  // list to the claims a fetch just landed.
  let fetchRunOnly = false;
  // Run id selected in the "All fetches" dropdown, or '' for no run filter.
  let runFilter = '';

  function applyFilter() {
    const text = textInput.value.trim().toLowerCase();
    let visibleCount = 0;
    list.querySelectorAll('li[data-claim-id]').forEach((li) => {
      const matchesStatus = activeStatus === 'all' || li.dataset.status === activeStatus;
      const matchesText = !text || li.dataset.claimId.toLowerCase().includes(text);
      const matchesTags = Array.from(activeTags).every((tag) => li.dataset[tag] === '1');
      const matchesFetchRun = !fetchRunOnly || li.dataset.fetchRun === '1';
      // A claim can belong to several runs; selecting any of them shows it.
      const matchesRun = !runFilter
        || (li.dataset.runIds || '').split(' ').includes(runFilter);
      const visible = matchesStatus && matchesText && matchesTags && matchesFetchRun && matchesRun;
      li.style.display = visible ? '' : 'none';
      if (visible) visibleCount += 1;
    });
    emptyMsg.style.display = visibleCount === 0 ? '' : 'none';
  }

  statusBar.querySelectorAll('.filter-btn').forEach((btn) => {
    btn.addEventListener('click', () => {
      statusBar.querySelectorAll('.filter-btn').forEach((b) => b.classList.remove('active'));
      btn.classList.add('active');
      activeStatus = btn.dataset.status;
      applyFilter();
    });
  });

  if (tagBar) {
    tagBar.querySelectorAll('.tag-toggle').forEach((btn) => {
      btn.addEventListener('click', () => {
        const tag = btn.dataset.tag;
        if (activeTags.has(tag)) {
          activeTags.delete(tag);
          btn.classList.remove('active');
        } else {
          activeTags.add(tag);
          btn.classList.add('active');
        }
        applyFilter();
      });
    });
  }

  textInput.addEventListener('input', applyFilter);

  // ------------------------------------------------------------ batch select/process

  const selectAllCheckbox = document.getElementById('select-all-checkbox');
  const countEl = document.getElementById('batch-selected-count');
  const processBtn = document.getElementById('batch-process-btn');
  const batchStatusEl = document.getElementById('batch-status');

  function checkboxes() {
    return Array.from(list.querySelectorAll('.claim-checkbox'));
  }

  function selectedIds() {
    return checkboxes().filter((cb) => cb.checked).map((cb) => cb.value);
  }

  function updateBatchUI() {
    const n = selectedIds().length;
    countEl.textContent = `${n} selected`;
    processBtn.disabled = n === 0;
  }

  checkboxes().forEach((cb) => cb.addEventListener('change', updateBatchUI));
  // Claims arriving from a fetch run are rendered already checked, so the
  // count and the Process button have to reflect that before any click.
  updateBatchUI();

  // Filtering by fetch run is done client-side so switching between batches is
  // instant and does not reload (or re-run the rollup for) the whole folder.
  const runSelect = document.getElementById('fetch-run-select');
  if (runSelect) {
    runFilter = runSelect.value || '';
    runSelect.addEventListener('change', () => {
      runFilter = runSelect.value || '';
      applyFilter();
    });
  }

  const fetchRunOnlyBtn = document.getElementById('fetch-run-only');
  if (fetchRunOnlyBtn) {
    fetchRunOnlyBtn.addEventListener('click', () => {
      fetchRunOnly = !fetchRunOnly;
      fetchRunOnlyBtn.classList.toggle('active', fetchRunOnly);
      fetchRunOnlyBtn.textContent = fetchRunOnly ? 'Show all claims' : 'Show only these';
      applyFilter();
    });
  }

  if (selectAllCheckbox) {
    selectAllCheckbox.addEventListener('change', () => {
      checkboxes().forEach((cb) => {
        const li = cb.closest('li');
        if (li.style.display !== 'none') cb.checked = selectAllCheckbox.checked;
      });
      updateBatchUI();
    });
  }

  function statusLabel(state) {
    switch (state.status) {
      case 'not_started': return 'Not processed';
      case 'running': {
        const docs = `${state.processed + state.cached + state.failed}/${state.total}`;
        const pages = state.total_pages ? `, ${state.pages_done || 0}/${state.total_pages} pages` : '';
        return `Processing ${docs}${pages}`;
      }
      case 'completed': return `Processed (${state.processed + state.cached}/${state.total})`;
      case 'failed': return `Failed (${state.failed} error${state.failed !== 1 ? 's' : ''})`;
      case 'interrupted': return 'Interrupted - retry';
      default: return state.status;
    }
  }

  function pollClaim(claimId, onDone) {
    const li = list.querySelector(`li[data-claim-id="${CSS.escape(claimId)}"]`);
    const badge = li ? li.querySelector('[data-badge]') : null;
    const timer = setInterval(() => {
      fetch(`/api/claims/${claimId}/process/status`).then((r) => r.json()).then((state) => {
        if (badge) {
          badge.textContent = statusLabel(state);
          badge.className = 'badge status-' + state.status;
        }
        if (state.status !== 'running') {
          clearInterval(timer);
          onDone();
        }
      });
    }, 1500);
  }

  if (processBtn) {
    processBtn.addEventListener('click', () => {
      const ids = selectedIds();
      if (!ids.length) return;
      processBtn.disabled = true;
      batchStatusEl.textContent = `Starting ${ids.length} claim(s)...`;

      Promise.all(ids.map((id) => fetch(`/api/claims/${id}/process`, { method: 'POST' })
        .then((r) => ({ id, ok: r.ok, status: r.status }))))
        .then((results) => {
          const started = results.filter((r) => r.ok).length;
          const alreadyRunning = results.filter((r) => r.status === 409).length;
          batchStatusEl.textContent = `Started ${started} claim(s)`
            + (alreadyRunning ? `, ${alreadyRunning} already processing` : '')
            + '. Watching progress...';

          let remaining = ids.length;
          ids.forEach((id) => {
            pollClaim(id, () => {
              remaining -= 1;
              if (remaining === 0) {
                batchStatusEl.textContent = 'All done - refreshing...';
                window.location.reload();
              }
            });
          });
        });
    });
  }
})();
