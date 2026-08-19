(function () {
  if (!document.getElementById('review-panel')) return;
  const claimId = window.CLAIM_ID;

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
  }

  function render(data) {
    const panel = document.getElementById('review-panel');
    const current = data.current;
    const currentLine = current
      ? `<p class="current-status">Current status: <span class="badge ${current.status}">${current.status}</span> (by ${escapeHtml(current.reviewer || 'unknown')} at ${current.created_at})</p>`
      : '<p class="hint">No review recorded yet for this claim.</p>';

    const history = data.reviews.map((r) => `
      <div class="entry">
        <span class="badge ${r.status}">${r.status}</span>
        <strong>${escapeHtml(r.reviewer || 'unknown')}</strong> - ${r.created_at}
        ${r.notes ? '<div>' + escapeHtml(r.notes) + '</div>' : ''}
      </div>
    `).join('');

    panel.innerHTML = `
      ${currentLine}
      <form class="review-form" id="review-form">
        <div class="status-buttons">
          <button type="button" class="approved" data-status="approved">Approve</button>
          <button type="button" class="flagged" data-status="flagged">Flag</button>
          <button type="button" class="rejected" data-status="rejected">Reject</button>
        </div>
        <input type="text" id="reviewer-input" placeholder="Your name" value="">
        <textarea id="notes-input" placeholder="Notes (optional)"></textarea>
        <button type="submit" style="margin-top:0.5rem;">Submit decision</button>
      </form>
      <div class="review-history">
        <h3>History</h3>
        ${history || '<p class="hint">No entries yet.</p>'}
      </div>
    `;

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
          reviewer: document.getElementById('reviewer-input').value,
          notes: document.getElementById('notes-input').value,
        }),
      }).then((r) => r.json()).then(() => load());
    });
  }

  function load() {
    fetch(`/api/claims/${claimId}/reviews`).then((r) => r.json()).then(render);
  }

  window.loadReviewPanel = load;
})();
