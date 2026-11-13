(function () {
  const list = document.getElementById('claim-list');
  const textInput = document.getElementById('claim-filter-text');
  const statusBar = document.getElementById('claim-filter-status');
  const tagBar = document.getElementById('claim-filter-tags');
  const emptyMsg = document.getElementById('claim-filter-empty');
  if (!list || !textInput || !statusBar) return;

  // Filtering and paging are the server's job now. A folder of a thousand
  // claims is no longer all in the DOM - fifteen of it are - so a filter the
  // browser applied to the rows it happens to hold would answer about the
  // page rather than about the folder.
  const state = {
    offset: Number(list.dataset.offset || 0),
    limit: Number(list.dataset.limit || 15),
    matching: Number(list.dataset.matching || 0),
    status: 'all',
    tags: new Set(),
    runFilter: '',
    runOnly: false,
  };

  // Ticked claims, kept here rather than read off the checkboxes: the rows
  // they belong to are replaced on every page turn, and a selection that
  // vanished when you looked at page two would make "process these forty"
  // impossible to express.
  const selected = new Set(window.PRESELECTED_CLAIMS || []);

  function params(extra) {
    const p = new URLSearchParams();
    if (state.offset) p.set('offset', state.offset);
    const q = textInput.value.trim();
    if (q) p.set('q', q);
    if (state.status !== 'all') p.set('status', state.status);
    if (state.tags.size) p.set('tags', Array.from(state.tags).join(','));
    if (state.runFilter) p.set('fetch_run', state.runFilter);
    if (state.runOnly) p.set('run_only', '1');
    Object.entries(extra || {}).forEach(([k, v]) => p.set(k, v));
    return p;
  }

  // ------------------------------------------------------------ the list

  let inFlight = null;
  const loadError = document.getElementById('claim-load-error');
  const loadingNote = document.getElementById('claim-loading');
  const batchBar = document.querySelector('.batch-bar');

  // The rows, the counts and the batch bar all describe the answer that is
  // on screen. While a new one is being fetched they describe the old
  // question, so they are dimmed and taken out of reach rather than left
  // looking live - which is what made a slow page read as a frozen one.
  function busy(on) {
    list.classList.toggle('loading', on);
    if (batchBar) batchBar.classList.toggle('loading', on);
    // Looked up here rather than closed over: the pager's own const is
    // declared further down, and a function hoisted above it must not read
    // the binding before it exists.
    const pagerEl = document.getElementById('claim-pager');
    if (pagerEl) pagerEl.classList.toggle('loading', on);
    if (loadingNote) loadingNote.hidden = !on;
  }

  function load(resetOffset) {
    if (resetOffset) state.offset = 0;
    const query = params().toString();
    const token = {};
    inFlight = token;
    busy(true);
    fetch('/api/claims/page?' + query)
      .then((r) => r.json().then((data) => ({ ok: r.ok, code: r.status, data })))
      .then(({ ok, code, data }) => {
        // A slower earlier request must not overwrite a newer answer: typing
        // quickly fires several, and they do not come back in order.
        if (inFlight !== token) return;
        if (!ok) { failed(code, data); return; }
        if (loadError) loadError.hidden = true;
        list.innerHTML = data.rows_html
          || '<li class="empty">No claims match this filter.</li>';
        state.matching = data.matching;
        state.offset = data.offset;
        restoreChecks();
        updateCounts(data);
        updatePager(data);
        updateBatchUI();
        syncUrl();
      })
      .catch(() => { if (inFlight === token) failed(0, {}); })
      .finally(() => {
        if (inFlight === token) {
          inFlight = null;
          busy(false);
        }
      });
  }

  // A request that failed says nothing about the folder. Reading its error
  // body as a claim list is what turned an expired session into "undefined of
  // 439 claim(s)", a pager reading "0-NaN of undefined", and "No claims match
  // this filter" over a folder with 439 claims in it - every field the page
  // wanted was simply absent. The rows already on screen are the last true
  // answer, so they stay, and the reason is said out loud above them.
  function failed(code, data) {
    if (!loadError) return;
    loadError.hidden = false;
    if (code === 401 || code === 403) {
      loadError.innerHTML = 'Signed out - the folder is still there. '
        + '<a href="/login?next=%2Fclaims">Sign in again</a> to load it.';
      return;
    }
    const detail = (data && data.error) ? data.error : '';
    loadError.textContent = code === 400
      ? `${detail || 'That folder cannot be read'} - choose a folder again.`
      : `Could not load the claim list${detail ? ' - ' + detail : ''}. `
        + 'The list below is what was last loaded.';
  }

  function syncUrl() {
    const url = new URL(window.location.href);
    url.search = params().toString();
    window.history.replaceState({}, '', url);
  }

  const countSummary = document.getElementById('claim-count-summary');
  const searchScope = document.getElementById('search-scope');
  const readySummary = document.getElementById('claim-ready-summary');
  const scanProgress = document.getElementById('scan-progress');

  function updateCounts(data) {
    // The dropdown still shows the fetch run, because the search box will be
    // emptied again and it should come back. While it is not applying, the
    // page has to say which of the two is in force.
    if (searchScope) searchScope.hidden = !data.searched_all;
    if (countSummary) {
      const folder = Number(countSummary.dataset.folderTotal || 0);
      countSummary.textContent = data.matching === folder
        ? `${folder} claim(s)`
        : `${data.matching} of ${folder} claim(s)`;
    }
    if (readySummary) readySummary.textContent = data.ready;
    if (emptyMsg) emptyMsg.style.display = data.matching === 0 ? '' : 'none';
    // A tag filter can only have searched the claims that have been read.
    // Without this the page would say "no blurry claims" when it means
    // "none among the two hundred looked at so far".
    if (scanProgress) {
      const partial = data.scanned < data.considered;
      scanProgress.hidden = !(partial && (data.warming || state.tags.size));
      scanProgress.textContent = state.tags.size
        ? `Quality filters have searched ${data.scanned} of ${data.considered} claims so far`
          + (data.warming ? ' - still reading the rest.' : '.')
        : `Reading the folder in the background: ${data.scanned} of ${data.considered} claims so far.`;
    }
  }

  const pager = document.getElementById('claim-pager');
  const prevBtn = document.getElementById('page-prev');
  const nextBtn = document.getElementById('page-next');
  const pageLabel = document.getElementById('page-label');

  function updatePager(data) {
    if (!pager) return;
    const last = Math.max(0, Math.ceil(data.matching / data.limit) - 1);
    const current = Math.floor(data.offset / data.limit);
    pager.hidden = data.matching <= data.limit;
    prevBtn.disabled = data.offset <= 0;
    nextBtn.disabled = data.offset + data.limit >= data.matching;
    const from = data.matching ? data.offset + 1 : 0;
    const to = Math.min(data.offset + data.limit, data.matching);
    pageLabel.textContent = `${from}-${to} of ${data.matching}`
      + (last ? ` (page ${current + 1} of ${last + 1})` : '');
  }

  if (prevBtn) prevBtn.addEventListener('click', () => {
    state.offset = Math.max(0, state.offset - state.limit);
    load(false);
  });
  if (nextBtn) nextBtn.addEventListener('click', () => {
    state.offset += state.limit;
    load(false);
  });

  // ------------------------------------------------------------ the filters

  statusBar.querySelectorAll('.filter-btn').forEach((btn) => {
    btn.addEventListener('click', () => {
      statusBar.querySelectorAll('.filter-btn').forEach((b) => b.classList.remove('active'));
      btn.classList.add('active');
      state.status = btn.dataset.status;
      load(true);
    });
  });

  if (tagBar) {
    tagBar.querySelectorAll('.tag-toggle').forEach((btn) => {
      btn.addEventListener('click', () => {
        const tag = btn.dataset.tag;
        if (state.tags.has(tag)) {
          state.tags.delete(tag);
          btn.classList.remove('active');
        } else {
          state.tags.add(tag);
          btn.classList.add('active');
        }
        load(true);
      });
    });
  }

  // Coalesce keystrokes: each one is now a request, not a DOM pass.
  let typingTimer = null;
  textInput.addEventListener('input', () => {
    clearTimeout(typingTimer);
    typingTimer = setTimeout(() => load(true), 250);
  });

  // ------------------------------------------------------------ batch select/process

  const selectAllCheckbox = document.getElementById('select-all-checkbox');
  const selectAllMatching = document.getElementById('select-all-matching');
  const clearSelection = document.getElementById('clear-selection');
  const countEl = document.getElementById('batch-selected-count');
  const processBtn = document.getElementById('batch-process-btn');
  const batchStatusEl = document.getElementById('batch-status');

  function checkboxes() {
    return Array.from(list.querySelectorAll('.claim-checkbox'));
  }

  // A fresh page of rows knows nothing about what was ticked before it; the
  // rows rendered pre-checked by a fetch run are folded in the other way.
  function restoreChecks() {
    checkboxes().forEach((cb) => {
      if (cb.checked) selected.add(cb.value);
      else cb.checked = selected.has(cb.value);
      cb.addEventListener('change', () => {
        if (cb.checked) selected.add(cb.value);
        else selected.delete(cb.value);
        updateBatchUI();
      });
    });
  }

  const exportBtn = document.getElementById('export-scoped');
  const exportNote = document.getElementById('export-scoped-note');
  const exportRunInput = document.getElementById('export-run-id');
  const exportIdsInput = document.getElementById('export-claim-ids');

  function updateExportScope() {
    if (!exportBtn) return;
    if (selected.size) {
      // A ticked selection is the most specific thing on screen, so it wins.
      exportIdsInput.value = Array.from(selected).join(',');
      exportRunInput.value = '';
      exportBtn.disabled = false;
      exportNote.textContent = `${selected.size} selected claim${selected.size === 1 ? '' : 's'}`;
      return;
    }
    exportIdsInput.value = '';
    exportRunInput.value = state.runFilter || '';
    exportBtn.disabled = !state.runFilter;
    exportNote.textContent = state.runFilter
      ? `fetch ${state.runFilter} only`
      : 'choose a fetch ID or tick some claims';
  }

  function updateBatchUI() {
    countEl.textContent = `${selected.size} selected`;
    processBtn.disabled = selected.size === 0;
    if (clearSelection) clearSelection.hidden = selected.size === 0;
    if (selectAllCheckbox) {
      const onPage = checkboxes();
      selectAllCheckbox.checked = onPage.length > 0 && onPage.every((cb) => cb.checked);
    }
    if (selectAllMatching) {
      selectAllMatching.hidden = state.matching <= state.limit;
      selectAllMatching.textContent = `Select all ${state.matching} matching`;
    }
    updateExportScope();
  }

  if (selectAllCheckbox) {
    selectAllCheckbox.addEventListener('change', () => {
      checkboxes().forEach((cb) => {
        cb.checked = selectAllCheckbox.checked;
        if (cb.checked) selected.add(cb.value);
        else selected.delete(cb.value);
      });
      updateBatchUI();
    });
  }

  if (selectAllMatching) {
    selectAllMatching.addEventListener('click', () => {
      selectAllMatching.disabled = true;
      fetch('/api/claims/matching-ids?' + params().toString())
        .then((r) => r.json())
        .then((data) => {
          (data.claim_ids || []).forEach((id) => selected.add(id));
          checkboxes().forEach((cb) => { cb.checked = selected.has(cb.value); });
          updateBatchUI();
        })
        .finally(() => { selectAllMatching.disabled = false; });
    });
  }

  if (clearSelection) {
    clearSelection.addEventListener('click', () => {
      selected.clear();
      checkboxes().forEach((cb) => { cb.checked = false; });
      updateBatchUI();
    });
  }

  restoreChecks();
  updateBatchUI();
  updatePager({ matching: state.matching, offset: state.offset, limit: state.limit });

  // Filtering by fetch run is a filter like any other now, so it goes to the
  // server with the rest rather than hiding rows the browser already has.
  const runSelect = document.getElementById('fetch-run-select');
  if (runSelect) {
    state.runFilter = runSelect.value || '';
    runSelect.addEventListener('change', () => {
      state.runFilter = runSelect.value || '';
      load(true);
    });
  }

  const fetchRunOnlyBtn = document.getElementById('fetch-run-only');
  if (fetchRunOnlyBtn) {
    fetchRunOnlyBtn.addEventListener('click', () => {
      state.runOnly = !state.runOnly;
      fetchRunOnlyBtn.classList.toggle('active', state.runOnly);
      fetchRunOnlyBtn.textContent = state.runOnly ? 'Show all claims' : 'Show only these';
      load(true);
    });
  }

  // ------------------------------------------------------------ background pass

  // Poll while the folder is being read, so the counts and the quality
  // filters stop being a snapshot of the moment the page loaded. Stops as
  // soon as the pass finishes - there is nothing further to report.
  let scanTimer = null;
  function watchScan() {
    scanTimer = setInterval(() => {
      fetch('/api/claims/scan-progress').then((r) => r.json()).then((s) => {
        if (!s.running) {
          clearInterval(scanTimer);
          scanTimer = null;
          if (scanProgress) scanProgress.hidden = true;
          // One last load, so a filter that was searching a partial folder
          // now shows the whole answer without the reviewer re-clicking it.
          if (state.tags.size) load(false);
          return;
        }
        if (scanProgress && !state.tags.size) {
          scanProgress.hidden = false;
          scanProgress.textContent =
            `Reading the folder in the background: ${s.scanned} of ${s.total} claims so far.`;
        }
      }).catch(() => {});
    }, 3000);
  }
  watchScan();

  // ------------------------------------------------------------ processing

  function statusLabel(st) {
    switch (st.status) {
      case 'not_started': return 'Not processed';
      case 'running': {
        const docs = `${st.processed + st.cached + st.failed}/${st.total}`;
        const pages = st.total_pages ? `, ${st.pages_done || 0}/${st.total_pages} pages` : '';
        return `Processing ${docs}${pages}`;
      }
      case 'completed': return `Processed (${st.processed + st.cached}/${st.total})`;
      case 'failed': return `Failed (${st.failed} error${st.failed !== 1 ? 's' : ''})`;
      case 'interrupted': return 'Interrupted - retry';
      default: return st.status;
    }
  }

  function pollClaim(claimId, onDone) {
    const timer = setInterval(() => {
      fetch(`/api/claims/${claimId}/process/status`).then((r) => r.json()).then((st) => {
        // The row may not be on the page any more - the reviewer can turn to
        // page four while forty claims process - so the badge is optional.
        const li = list.querySelector(`li[data-claim-id="${CSS.escape(claimId)}"]`);
        const badge = li ? li.querySelector('[data-badge]') : null;
        if (badge) {
          badge.textContent = statusLabel(st);
          badge.className = 'badge status-' + st.status;
        }
        if (st.status !== 'running') {
          clearInterval(timer);
          onDone();
        }
      });
    }, 1500);
  }

  if (processBtn) {
    processBtn.addEventListener('click', () => {
      const ids = Array.from(selected);
      if (!ids.length) return;
      // Selection now outlives the page it was made on, so the count can be
      // far larger than what is on screen - and each claim is real Textract
      // spend. Worth one question before starting a batch nobody can see.
      if (ids.length > state.limit
          && !window.confirm(`Process ${ids.length} claim(s)?\n\nThis runs Textract on every `
                             + 'document of each one, which costs money and cannot be undone.')) {
        return;
      }
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
