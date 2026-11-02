(function () {
  if (!document.getElementById('review-panel')) return;
  const claimId = window.CLAIM_ID;

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
  }

  // A page's path as a reviewer reads it: the file name, with the folders it
  // sits in kept as the title rather than spelled out in a narrow panel.
  function pageLink(docId, page, label) {
    const encoded = String(docId).split('/').map(encodeURIComponent).join('/');
    return `<a href="/claims/${claimId}/doc/${encoded}/page/${page}"
               target="_blank" rel="noopener" title="${escapeHtml(docId)}"
               >${escapeHtml(label)}</a>`;
  }

  function evidenceRow(item, removable) {
    const name = String(item.document_path).split('/').pop();
    return `<li>
      ${pageLink(item.document_path, item.page_number, `${name} p${item.page_number}`)}
      ${item.note ? `<span class="evidence-note-text">${escapeHtml(item.note)}</span>` : ''}
      ${removable ? `<button type="button" class="evidence-drop"
                             data-doc-id="${escapeHtml(item.document_path)}"
                             data-page="${item.page_number}"
                             title="Remove this page from the evidence">&times;</button>` : ''}
    </li>`;
  }

  // What pressing submit will attach. Rendered above the decision buttons
  // rather than below them, because it is part of the decision being made.
  function renderPending(marks) {
    const box = document.getElementById('pending-evidence');
    if (!box) return;
    if (!marks || !marks.length) {
      box.innerHTML = '<p class="hint">No pages marked as evidence. Open a document '
        + 'and press &#9734; Mark as evidence on any page to attach it to your decision.</p>';
      return;
    }
    box.innerHTML = `
      <p class="hint">${marks.length} page${marks.length === 1 ? '' : 's'} will be
        attached to your decision.</p>
      <ul class="evidence-list">${marks.map((m) => evidenceRow(m, true)).join('')}</ul>`;
    box.querySelectorAll('.evidence-drop').forEach((btn) => {
      btn.addEventListener('click', () => {
        window.unmarkEvidence(claimId, btn.dataset.docId, Number(btn.dataset.page));
      });
    });
  }

  // evidence.js calls this whenever a page is marked anywhere on the claim,
  // so the list cannot be stale while the panel is open behind the preview.
  window.refreshPendingEvidence = renderPending;

  function render(data) {
    const panel = document.getElementById('review-panel');
    const current = data.current;
    const currentLine = current
      ? `<p class="current-status">Current status: <span class="badge ${current.status}">${current.status}</span> (by ${escapeHtml(current.reviewer || 'unknown')} at ${current.created_at})</p>`
      : '<p class="hint">No review recorded yet for this claim.</p>';

    const history = data.reviews.map((r) => {
      const attached = r.evidence || [];
      return `
      <div class="entry">
        <span class="badge ${r.status}">${r.status}</span>
        <strong>${escapeHtml(r.reviewer || 'unknown')}</strong> - ${r.created_at}
        ${r.notes ? '<div>' + escapeHtml(r.notes) + '</div>' : ''}
        ${attached.length ? `<div class="evidence-attached">
          <span class="hint">Evidence (${attached.length})</span>
          <ul class="evidence-list">${attached.map((e) => evidenceRow(e, false)).join('')}</ul>
        </div>` : ''}
      </div>`;
    }).join('');

    panel.innerHTML = `
      ${currentLine}
      <div class="evidence-pending" id="pending-evidence"></div>
      <form class="review-form" id="review-form">
        <div class="status-buttons">
          <button type="button" class="approved" data-status="approved">Approve</button>
          <button type="button" class="flagged" data-status="flagged">Flag</button>
          <button type="button" class="rejected" data-status="rejected">Reject</button>
        </div>
        <p class="hint" id="reviewer-identity">Recorded as the signed-in user.</p>
        <textarea id="notes-input" placeholder="Notes (optional)"></textarea>
        <button type="submit" style="margin-top:0.5rem;">Submit decision</button>
      </form>
      <div class="review-history">
        <h3>History</h3>
        ${history || '<p class="hint">No entries yet.</p>'}
      </div>
    `;

    renderPending(data.pending_evidence);

    let selectedStatus = null;
    panel.querySelectorAll('.status-buttons button').forEach((btn) => {
      btn.addEventListener('click', () => {
        panel.querySelectorAll('.status-buttons button').forEach((b) => b.classList.remove('selected'));
        btn.classList.add('selected');
        selectedStatus = btn.dataset.status;
      });
    });

    panel.querySelector('#review-form').addEventListener('submit', (e) => {
      e.preventDefault();
      if (!selectedStatus) {
        alert('Choose approve, flag, or reject first.');
        return;
      }
      fetch(`/api/claims/${claimId}/review`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          status: selectedStatus,
          notes: document.getElementById('notes-input').value,
        }),
      }).then((r) => r.json()).then(() => {
        // The server copied the marks onto the decision and cleared them, so
        // every evidence button on the claim has to stop claiming otherwise.
        if (window.evidenceSubmitted) window.evidenceSubmitted();
        load();
      });
    });
  }

  function load() {
    fetch(`/api/claims/${claimId}/reviews`).then((r) => r.json()).then(render);
  }

  window.loadReviewPanel = load;
})();
