(function () {
  const form = document.getElementById('fetch-form');
  if (!form) return;

  const formStatus = document.getElementById('fetch-form-status');
  const previewBtn = document.getElementById('preview-btn');
  const fetchBtn = document.getElementById('fetch-btn');
  const previewPanel = document.getElementById('preview-panel');
  const previewSummary = document.getElementById('preview-summary');
  const previewIds = document.getElementById('preview-ids');
  const previewWarning = document.getElementById('preview-warning');

  const runPanel = document.getElementById('run-panel');
  const runIdEl = document.getElementById('run-id');
  const runBadge = document.getElementById('run-badge');
  const runBar = document.getElementById('run-bar');
  const runStatus = document.getElementById('run-status');
  const runClaims = document.getElementById('run-claims');
  const runLog = document.getElementById('run-log');
  const runActions = document.getElementById('run-actions');
  const reviewBtn = document.getElementById('review-btn');
  const cancelBtn = document.getElementById('cancel-btn');

  let pollTimer = null;
  let currentRunId = null;

  // ------------------------------------------------------------- source toggle

  function applySourceToggle() {
    const source = form.querySelector('input[name="source"]:checked').value;
    form.querySelectorAll('.fetch-subfield').forEach((el) => {
      el.hidden = el.dataset.for !== source;
    });
  }
  form.querySelectorAll('input[name="source"]').forEach((radio) => {
    radio.addEventListener('change', applySourceToggle);
  });
  applySourceToggle();

  // -------------------------------------------------- destination folder picker
  // Reuses /browse's JSON listing, but deliberately does NOT go through
  // /browse/select-root: picking where to download to must not change which
  // folder the review side is currently pointed at.

  const destInput = document.getElementById('field-destination');
  const browser = document.getElementById('fetch-browser');
  const browseToggle = document.getElementById('browse-toggle');
  const dirList = document.getElementById('fetch-dir-list');
  const breadcrumb = document.getElementById('fetch-breadcrumb');
  const useFolderBtn = document.getElementById('fetch-use-folder');
  let browsePath = '';

  function renderBreadcrumb(path) {
    breadcrumb.innerHTML = '';
    if (!path) return;
    const sep = path.includes('\\') ? '\\' : '/';
    const parts = path.split(sep).filter(Boolean);
    let acc = path.startsWith(sep) ? sep : '';
    parts.forEach((part, idx) => {
      acc = idx === 0 && /^[A-Za-z]:$/.test(part) ? part + sep : acc + part + sep;
      const a = document.createElement('a');
      a.href = '#';
      a.textContent = part;
      a.dataset.path = acc.endsWith(sep) ? acc.slice(0, -1) : acc;
      a.className = 'fetch-crumb';
      breadcrumb.appendChild(a);
      breadcrumb.appendChild(document.createTextNode(' / '));
    });
  }

  function loadBrowse(path) {
    const url = new URL(window.location.origin + '/browse');
    url.searchParams.set('format', 'json');
    if (path) url.searchParams.set('path', path);
    fetch(url).then((r) => r.json()).then((data) => {
      if (data.error) { alert(data.error); return; }
      browsePath = data.path || '';
      renderBreadcrumb(browsePath);
      dirList.innerHTML = '';
      if (data.parent) {
        dirList.appendChild(dirEntry('.. (up one level)', data.parent));
      }
      (data.entries || []).forEach((entry) => {
        dirList.appendChild(dirEntry(entry.name, entry.path));
      });
      if (!data.entries.length && !data.parent) {
        const li = document.createElement('li');
        li.className = 'empty';
        li.textContent = 'No subfolders here.';
        dirList.appendChild(li);
      }
      useFolderBtn.disabled = !browsePath;
    });
  }

  function dirEntry(name, path) {
    const li = document.createElement('li');
    const a = document.createElement('a');
    a.href = '#';
    a.className = 'fetch-dir-entry';
    a.dataset.path = path;
    a.textContent = name;
    li.appendChild(a);
    return li;
  }

  browseToggle.addEventListener('click', () => {
    browser.hidden = !browser.hidden;
    if (!browser.hidden) loadBrowse(destInput.value.trim());
  });

  browser.addEventListener('click', (e) => {
    const target = e.target.closest('.fetch-dir-entry, .fetch-crumb');
    if (!target) return;
    e.preventDefault();
    loadBrowse(target.dataset.path);
  });

  useFolderBtn.addEventListener('click', () => {
    if (browsePath) destInput.value = browsePath;
    browser.hidden = true;
  });

  // ------------------------------------------------------------------ requests

  // --- hypothesis mode -----------------------------------------------------
  // Selecting a hypothesis fills the fields it owns and locks them. The lock
  // is presentation only: build_options() reads these values from the stored
  // hypothesis, never from the submission, because a disabled input is simply
  // absent from a POST and an edited page could send anything.
  const hypothesisSelect = document.getElementById('field-hypothesis');
  const hypothesisHelp = document.getElementById('hypothesis-help');
  const lockedFields = [
    document.querySelector('[name="procedure_codes"]'),
    document.querySelector('[name="exclude_procedure_codes"]'),
    document.querySelector('[name="convergence"]'),
  ].filter(Boolean);
  const limitField = document.querySelector('[name="limit"]');
  let hypothesisNote = null;
  const savedValues = new Map();

  function hypothesesById() {
    if (!hypothesisSelect) return {};
    try {
      const list = JSON.parse(hypothesisSelect.dataset.hypotheses || '[]');
      return list.reduce((acc, h) => { acc[String(h.id)] = h; return acc; }, {});
    } catch (err) {
      return {};
    }
  }
  const HYPOTHESES = hypothesesById();

  function setLocked(locked) {
    lockedFields.forEach((field) => {
      field.disabled = locked;
      // A disabled field renders greyed by the browser; the title says why so
      // it does not just look broken.
      field.title = locked ? 'Fixed by the selected hypothesis' : '';
    });
  }

  function applyHypothesis() {
    if (!hypothesisSelect) return;
    const chosen = HYPOTHESES[hypothesisSelect.value];
    if (hypothesisNote) { hypothesisNote.remove(); hypothesisNote = null; }

    // The batch size is prefilled but never locked, so it is remembered and
    // restored alongside the locked fields - deselecting should put the form
    // back exactly as the reviewer left it, not leave a hypothesis's number
    // behind on an ordinary fetch.
    const remembered = limitField ? lockedFields.concat([limitField]) : lockedFields;

    if (!chosen) {
      // Restore whatever the reviewer had typed before selecting one.
      remembered.forEach((field) => {
        if (!savedValues.has(field)) return;
        if (field.type === 'checkbox') field.checked = savedValues.get(field);
        else field.value = savedValues.get(field);
      });
      savedValues.clear();
      setLocked(false);
      return;
    }

    remembered.forEach((field) => {
      if (!savedValues.has(field)) {
        savedValues.set(field, field.type === 'checkbox' ? field.checked : field.value);
      }
    });
    const includeField = document.querySelector('[name="procedure_codes"]');
    const excludeField = document.querySelector('[name="exclude_procedure_codes"]');
    const convergenceField = document.querySelector('[name="convergence"]');
    if (includeField) includeField.value = chosen.procedure_codes || '';
    if (excludeField) excludeField.value = chosen.exclude_procedure_codes || '';
    if (convergenceField) convergenceField.checked = !!chosen.include_non_pmjay;
    setLocked(true);

    // Default the batch size to what is still outstanding, but leave it
    // editable - fetching the sample in smaller batches is normal.
    if (limitField && chosen.reviews_remaining) limitField.value = chosen.reviews_remaining;

    hypothesisNote = document.createElement('p');
    hypothesisNote.className = 'settings-help';
    const bits = [
      chosen.period_description,
      chosen.claim_count != null ? chosen.claim_count.toLocaleString() + ' claims matched' : null,
      'sample ' + chosen.sample_size,
      chosen.claims_reviewed + ' reviewed',
      chosen.reviews_remaining != null ? chosen.reviews_remaining + ' still needed' : null,
    ].filter(Boolean);
    hypothesisNote.textContent = bits.join(' · ');
    if (chosen.is_stale) {
      hypothesisNote.textContent +=
        ' · these counts are stale: the criteria changed since it was processed.';
    }
    if (hypothesisHelp && hypothesisHelp.parentNode) {
      hypothesisHelp.parentNode.appendChild(hypothesisNote);
    }
  }

  if (hypothesisSelect) {
    hypothesisSelect.addEventListener('change', applyHypothesis);
    applyHypothesis();
  }

  function post(url) {
    return fetch(url, { method: 'POST', body: new FormData(form) })
      .then((r) => r.json().then((data) => ({ ok: r.ok, status: r.status, data })));
  }

  function busy(isBusy, message) {
    previewBtn.disabled = isBusy;
    fetchBtn.disabled = isBusy;
    formStatus.textContent = message || '';
  }

  previewBtn.addEventListener('click', () => {
    busy(true, 'Running the selection query...');
    previewPanel.hidden = true;
    post('/api/fetch/preview').then(({ ok, data }) => {
      busy(false, '');
      if (!ok) {
        previewSummary.innerHTML = '<strong>Preview failed.</strong> ' + escapeHtml(data.error || '');
        previewIds.textContent = '';
        previewWarning.textContent = '';
        document.getElementById('preview-filters').innerHTML = '';
        previewPanel.hidden = false;
        return;
      }
      previewSummary.innerHTML = '<strong>' + data.count + ' claim(s)</strong> match: '
        + escapeHtml(data.source);
      // Echo how each filter expression was actually read - comma means "all of
      // these", pipe means "any of these", and mixing them up silently returns
      // the wrong claims.
      const f = data.filters || {};
      document.getElementById('preview-filters').innerHTML = [
        ['Policy', f.policy], ['Hospital type', f.hospital_type],
        ['Include procedure codes', f.include], ['Exclude procedure codes', f.exclude],
      ].map((pair) => '<li>' + pair[0] + ': <strong>' + escapeHtml(pair[1] || '-') + '</strong></li>').join('');
      previewIds.textContent = data.claim_ids.join(', ') + (data.truncated ? ' ...' : '');
      previewWarning.textContent = data.without_preauth_count
        ? data.without_preauth_count + ' of these have no preauthorization path in the source table; '
          + 'they will still be attempted, using the downloader’s own lookup.'
        : '';
      previewPanel.hidden = false;
    }).catch((err) => busy(false, String(err)));
  });

  fetchBtn.addEventListener('click', () => {
    busy(true, 'Starting...');
    post('/api/fetch/start').then(({ ok, data }) => {
      if (!ok) { busy(false, data.error || 'Could not start the fetch'); return; }
      busy(true, '');
      attachToRun(data.run_id);
    }).catch((err) => busy(false, String(err)));
  });

  cancelBtn.addEventListener('click', () => {
    if (!currentRunId) return;
    cancelBtn.disabled = true;
    fetch('/api/fetch/' + currentRunId + '/cancel', { method: 'POST' });
  });

  reviewBtn.addEventListener('click', () => {
    if (!currentRunId) return;
    reviewBtn.disabled = true;
    fetch('/api/fetch/' + currentRunId + '/adopt', { method: 'POST' })
      .then((r) => r.json())
      .then((data) => {
        if (data.next) window.location.href = data.next;
        else { reviewBtn.disabled = false; alert(data.error || 'Could not open these claims'); }
      });
  });

  // ------------------------------------------------------- delete a run's files

  document.querySelectorAll('.delete-files-btn').forEach((btn) => {
    btn.addEventListener('click', () => {
      const runId = btn.dataset.runId;
      const ok = window.confirm(
        'Delete the downloaded files for run ' + runId + '?\n\n'
        + btn.dataset.claims + ' claim folder(s) under:\n' + btn.dataset.destination
        + '\n\nOCR results, review notes and rule history are kept - they are stored by '
        + 'document content, not by file path. Document previews for these claims will '
        + 'stop working until they are fetched again.');
      if (!ok) return;

      const statusEl = document.querySelector('.delete-status[data-run-id="' + CSS.escape(runId) + '"]');
      btn.disabled = true;
      statusEl.textContent = 'Deleting...';
      fetch('/api/fetch/' + runId + '/delete-files', { method: 'POST' })
        .then((r) => r.json())
        .then((data) => {
          if (data.error) {
            btn.disabled = false;
            statusEl.textContent = data.error;
            return;
          }
          btn.remove();
          const mb = (data.bytes_freed / (1024 * 1024)).toFixed(1);
          statusEl.textContent = 'deleted ' + data.deleted + ' folder(s), freed ' + mb + ' MB'
            + (data.missing ? ' (' + data.missing + ' already gone)' : '')
            + (data.errors && data.errors.length ? ' - ' + data.errors.length + ' error(s)' : '');
        });
    });
  });

  // ------------------------------------------------------------------ progress

  function attachToRun(runId) {
    currentRunId = runId;
    runPanel.hidden = false;
    runIdEl.textContent = runId;
    runActions.hidden = false;
    reviewBtn.disabled = true;
    cancelBtn.disabled = false;
    poll();
    pollTimer = setInterval(poll, 1500);
  }

  const PHASE_LABELS = {
    selecting: 'Selecting claims',
    downloading: 'Downloading and extracting',
    loading: 'Loading into Redshift',
    skipping_load: 'Skipping the Redshift load',
    reporting: 'Writing reports',
    cleanup: 'Cleaning up',
    cancelling: 'Stopping after the claims in flight',
    done: 'Done',
  };

  function poll() {
    fetch('/api/fetch/' + currentRunId + '/status').then((r) => r.json()).then(render);
  }

  function render(state) {
    if (state.status === 'not_found') return;

    runBadge.textContent = state.status;
    runBadge.className = 'badge status-' + (
      state.status === 'running' ? 'running'
        : state.status === 'completed' ? 'completed'
        : state.status === 'cancelled' ? 'interrupted' : 'failed');

    const total = state.total || 0;
    const done = state.done || 0;
    runBar.style.width = total ? Math.round((done / total) * 100) + '%' : '0%';

    const phase = PHASE_LABELS[state.phase] || state.phase || '';
    const counts = total ? done + '/' + total + ' claims' : 'no claims yet';
    runStatus.textContent = phase + ' — ' + counts
      + (state.ok || state.failed ? ' (' + state.ok + ' ok, ' + state.failed + ' failed)' : '')
      + (state.error ? ' — ' + state.error : '');

    if (state.recent && state.recent.length) {
      runClaims.innerHTML = '';
      state.recent.forEach((claim) => {
        const li = document.createElement('li');
        li.className = 'fetch-claim-row';
        li.innerHTML = '<span class="mono">' + escapeHtml(claim.registration_id) + '</span>'
          + '<span class="tag ' + (claim.ok ? 'tag-pass' : 'tag-blurry') + '">'
          + escapeHtml(claim.extraction_status) + '</span>'
          + '<span class="hint">' + escapeHtml(claim.download_status) + '</span>';
        runClaims.appendChild(li);
      });
    }

    if (state.log && state.log.length) {
      runLog.textContent = state.log.map((entry) => entry.message).join('\n');
      runLog.scrollTop = runLog.scrollHeight;
    }

    if (state.status !== 'running') {
      clearInterval(pollTimer);
      pollTimer = null;
      busy(false, '');
      cancelBtn.disabled = true;
      const result = state.result || {};
      const landed = result.claims_ok || state.ok || 0;
      reviewBtn.disabled = landed === 0;
      reviewBtn.textContent = landed
        ? 'Review these ' + landed + ' claim(s)'
        : 'Nothing to review';
      if (result.xlsx_report || result.json_report) {
        runStatus.textContent += ' — reports: '
          + [result.xlsx_report, result.json_report].filter(Boolean).join(', ');
      }
    }
  }

  function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text == null ? '' : String(text);
    return div.innerHTML;
  }

  // A fetch started before this page was loaded (or reloaded) keeps running in
  // the background - reattach to it rather than showing an idle form.
  if (window.ACTIVE_FETCH_RUN) attachToRun(window.ACTIVE_FETCH_RUN);
})();
