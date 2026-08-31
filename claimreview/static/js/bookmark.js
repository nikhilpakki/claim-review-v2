(function () {
  const bar = document.getElementById('bookmark-bar');
  if (!bar || !window.CLAIM_ID) return;

  const applied = document.getElementById('bookmark-applied');
  const picker = document.getElementById('bookmark-picker');
  const select = document.getElementById('bookmark-label');
  const newInput = document.getElementById('bookmark-new');
  const noteInput = document.getElementById('bookmark-note');
  const saveBtn = document.getElementById('bookmark-save');
  const errorEl = document.getElementById('bookmark-error');

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
  }

  // Each label this claim is already filed under, with a way to take it off
  // again - the same control both adds and removes, so there is one place to
  // look rather than a separate "manage bookmarks" step.
  function renderApplied(labels) {
    if (!labels.length) {
      applied.innerHTML = '';
      return;
    }
    applied.innerHTML = labels.map((label) => `
      <span class="tag tag-bookmark">${escapeHtml(label)}
        <a href="#" class="bookmark-remove" data-label="${escapeHtml(label)}"
           title="Remove this bookmark">&times;</a>
      </span>`).join(' ');
    applied.querySelectorAll('.bookmark-remove').forEach((link) => {
      link.addEventListener('click', (e) => {
        e.preventDefault();
        post('remove', { label: link.dataset.label });
      });
    });
  }

  function renderLabels(labels, appliedLabels) {
    const set = new Set(appliedLabels);
    select.innerHTML = labels.map((label) => {
      // A label already on this claim is still selectable - saving it again is
      // harmless and idempotent - but saying so avoids a puzzling no-op.
      const suffix = set.has(label) ? ' (already added)' : '';
      return `<option value="${escapeHtml(label)}">${escapeHtml(label)}${suffix}</option>`;
    }).join('');
  }

  function apply(data) {
    renderLabels(data.labels || [], data.applied || []);
    renderApplied(data.applied || []);
  }

  function post(action, body) {
    errorEl.textContent = '';
    saveBtn.disabled = true;
    const url = action === 'remove'
      ? `/api/claims/${encodeURIComponent(window.CLAIM_ID)}/bookmark/remove`
      : `/api/claims/${encodeURIComponent(window.CLAIM_ID)}/bookmark`;
    return fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
      body: JSON.stringify(body),
    })
      .then((r) => r.json().then((data) => ({ ok: r.ok, data })))
      .then(({ ok, data }) => {
        if (!ok) {
          errorEl.textContent = data.error || 'Could not save that bookmark.';
          return;
        }
        apply(data);
        if (action !== 'remove') {
          newInput.value = '';
          noteInput.value = '';
          picker.open = false;
        }
      })
      .catch(() => { errorEl.textContent = 'Could not reach the server.'; })
      .finally(() => { saveBtn.disabled = false; });
  }

  saveBtn.addEventListener('click', () => {
    post('add', {
      label: select.value,
      new_label: newInput.value,
      note: noteInput.value,
    });
  });

  // Enter in the new-name box should save, not submit the surrounding page.
  newInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); saveBtn.click(); }
  });

  fetch(`/api/claims/${encodeURIComponent(window.CLAIM_ID)}/bookmarks`)
    .then((r) => r.json())
    .then(apply)
    .catch(() => { /* the bar simply stays empty */ });
})();
