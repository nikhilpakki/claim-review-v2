// Marking the page you are looking at as evidence, from wherever you are
// looking at it. The preview pane re-renders itself on every page turn and
// the standalone page view is server-rendered, so neither owns this: both
// put an empty host in their header and this fills it in.
//
// The marks for a claim are fetched once and kept, because turning pages is
// the common action and a request per turn to answer "is this one marked"
// would be a request per turn for an answer that rarely changes.
(function () {
  const state = { claimId: null, marks: null, loading: null };
  const hosts = new Set();

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
  }

  function find(docId, page) {
    return (state.marks || []).find(
      (m) => m.document_path === docId && Number(m.page_number) === Number(page));
  }

  function load(claimId) {
    if (state.claimId === claimId && state.marks) return Promise.resolve(state.marks);
    if (state.claimId === claimId && state.loading) return state.loading;
    state.claimId = claimId;
    state.marks = null;
    state.loading = fetch(`/api/claims/${encodeURIComponent(claimId)}/evidence`)
      .then((r) => r.json())
      .then((data) => {
        state.marks = data.marks || [];
        return state.marks;
      })
      // A failed fetch must not leave every page stuck on "loading": an empty
      // list renders an honest unmarked button, and pressing it will surface
      // the real error.
      .catch(() => { state.marks = []; return state.marks; })
      .finally(() => { state.loading = null; });
    return state.loading;
  }

  function post(claimId, body) {
    return fetch(`/api/claims/${encodeURIComponent(claimId)}/evidence`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }).then((r) => r.json().then((data) => {
      if (!r.ok) throw new Error(data.error || 'Could not save that mark.');
      state.marks = data.marks || [];
      return state.marks;
    }));
  }

  function changed() {
    hosts.forEach(paint);
    // The Review tab lists what will be attached, so it is wrong the moment
    // a page is marked anywhere else.
    if (window.refreshPendingEvidence) window.refreshPendingEvidence(state.marks);
  }

  function paint(host) {
    if (!host.isConnected) { hosts.delete(host); return; }
    const { claimId, docId } = host.dataset;
    const page = Number(host.dataset.page);
    if (state.marks === null) {
      host.innerHTML = '<span class="evidence-loading">&hellip;</span>';
      return;
    }
    const mark = find(docId, page);
    host.innerHTML = `
      <button type="button" class="evidence-toggle${mark ? ' marked' : ''}"
              title="${mark ? 'Attached to your next decision on this claim - click to remove'
                            : 'Mark this page as evidence for your decision on this claim'}">
        ${mark ? '&#9733; Evidence' : '&#9734; Mark as evidence'}
      </button>
      ${mark ? `<input type="text" class="evidence-note" maxlength="300"
                       placeholder="Why? (optional)"
                       value="${escapeHtml(mark.note || '')}">` : ''}
    `;

    host.querySelector('.evidence-toggle').addEventListener('click', () => {
      const note = (host.querySelector('.evidence-note') || {}).value;
      post(claimId, mark
        ? { action: 'unmark', document_path: docId, page_number: page }
        : { action: 'mark', document_path: docId, page_number: page, note })
        .then(changed)
        .catch((err) => window.alert(err.message));
    });

    const noteInput = host.querySelector('.evidence-note');
    if (noteInput) {
      // Saved on blur and on Enter, not per keystroke: the note is a sentence,
      // and a request per character would be a request per character.
      const save = () => {
        if ((noteInput.value || '') === (mark.note || '')) return;
        post(claimId, { action: 'mark', document_path: docId, page_number: page,
                        note: noteInput.value })
          .then(changed)
          .catch((err) => window.alert(err.message));
      };
      noteInput.addEventListener('change', save);
      noteInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') { e.preventDefault(); noteInput.blur(); }
      });
    }
  }

  // Called by whoever rendered the host. Safe to call again for the same
  // element - the preview pane rebuilds its header on every page turn.
  window.mountEvidenceMark = function (host) {
    if (!host || !host.dataset.claimId || !host.dataset.docId) return;
    hosts.add(host);
    if (state.marks && state.claimId === host.dataset.claimId) {
      paint(host);
    } else {
      paint(host);
      load(host.dataset.claimId).then(() => paint(host));
    }
  };

  // The Review tab owns the list of what will be attached; this lets it drop
  // a mark without duplicating the request and the cache.
  window.unmarkEvidence = function (claimId, docId, page) {
    return post(claimId, { action: 'unmark', document_path: docId, page_number: page })
      .then((marks) => { changed(); return marks; });
  };

  window.evidenceMarks = function (claimId) { return load(claimId); };

  // After a decision is submitted the server has cleared the marks, so the
  // cache has to go with them or the buttons keep claiming pages are marked.
  window.evidenceSubmitted = function () {
    state.marks = [];
    changed();
  };
})();
