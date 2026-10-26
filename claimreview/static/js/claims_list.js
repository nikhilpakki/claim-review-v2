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

  // The rows, read once. querySelectorAll on every keystroke across a folder of
  // a few thousand claims was a measurable part of the lag, and the set does
  // not change while the page is open.
  const rows = Array.from(list.querySelectorAll('li[data-claim-id]'));
  // Each row's run ids, split once rather than on every pass.
  rows.forEach((li) => {
    li._runIds = (li.dataset.runIds || '').split(' ').filter(Boolean);
    li._claimIdLower = li.dataset.claimId.toLowerCase();
  });

  let visibleRows = rows.slice();

  function applyFilter() {
    const text = textInput.value.trim().toLowerCase();
    const tags = Array.from(activeTags);
    const visible = [];
    let readyCount = 0;
    for (const li of rows) {
      const show = (activeStatus === 'all' || li.dataset.status === activeStatus)
        && (!text || li._claimIdLower.includes(text))
        && tags.every((tag) => li.dataset[tag] === '1')
        && (!fetchRunOnly || li.dataset.fetchRun === '1')
        // A claim can belong to several runs; selecting any of them shows it.
        && (!runFilter || li._runIds.includes(runFilter));
      // Only touch the DOM when the answer actually changed: assigning the
      // same display value to thousands of nodes still costs a style
      // recalculation, and most rows do not change between passes.
      const hidden = li.style.display === 'none';
      if (show === hidden) li.style.display = show ? '' : 'none';
      if (show) {
        visible.push(li);
        if (li.dataset.status === 'completed') readyCount += 1;
      }
    }
    visibleRows = visible;
    emptyMsg.style.display = visible.length === 0 ? '' : 'none';
    updateCounts(visible.length, readyCount);
    updateBatchUI();
  }

  // The header counts are rendered for the whole folder; once a filter hides
  // most of it they described a page nobody was looking at, which is what made
  // filtering feel like it had not taken effect.
  const countSummary = document.getElementById('claim-count-summary');
  const readySummary = document.getElementById('claim-ready-summary');

  function updateCounts(visibleCount, readyCount) {
    if (!countSummary) return;
    const total = Number(countSummary.dataset.total || 0);
    countSummary.textContent = visibleCount === total
      ? `${total} claim(s)`
      : `${visibleCount} of ${total} claim(s)`;
    if (readySummary) readySummary.textContent = readyCount;
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

  // Coalesce keystrokes: a filter pass over a few thousand rows per character
  // is what the typing lag was.
  let typingTimer = null;
  textInput.addEventListener('input', () => {
    clearTimeout(typingTimer);
    typingTimer = setTimeout(applyFilter, 90);
  });

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

  // The Export menu's first item follows whatever is narrowing the page. It
  // used to read a server-rendered ?fetch_run=, which the dropdown never sets -
  // the filtering is client-side - so picking a fetch left it disabled.
  const exportForm = document.getElementById('export-form');
  const exportBtn = document.getElementById('export-scoped');
  const exportNote = document.getElementById('export-scoped-note');
  const exportRunInput = document.getElementById('export-run-id');
  const exportIdsInput = document.getElementById('export-claim-ids');

  function updateExportScope(selected) {
    if (!exportBtn) return;
    if (selected.length) {
      // A ticked selection is the most specific thing on screen, so it wins.
      exportIdsInput.value = selected.join(',');
      exportRunInput.value = '';
      exportBtn.disabled = false;
      exportNote.textContent = `${selected.length} selected claim${selected.length === 1 ? '' : 's'}`;
      return;
    }
    exportIdsInput.value = '';
    exportRunInput.value = runFilter || '';
    exportBtn.disabled = !runFilter;
    exportNote.textContent = runFilter
      ? `fetch ${runFilter} only`
      : 'choose a fetch ID or tick some claims';
  }

  function updateBatchUI() {
    const selected = selectedIds();
    countEl.textContent = `${selected.length} selected`;
    processBtn.disabled = selected.length === 0;
    updateExportScope(selected);
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
      // Keep the address bar in step, so a reload, a bookmark or a shared link
      // lands on the same batch rather than silently on the whole folder.
      const url = new URL(window.location.href);
      if (runFilter) url.searchParams.set('fetch_run', runFilter);
      else url.searchParams.delete('fetch_run');
      window.history.replaceState({}, '', url);
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
      visibleRows.forEach((li) => {
        const cb = li.querySelector('.claim-checkbox');
        if (cb) cb.checked = selectAllCheckbox.checked;
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
