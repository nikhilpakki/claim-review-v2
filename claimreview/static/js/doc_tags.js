/* Why a page carries an admin-defined tag.
 *
 * The badge says a tag matched; clicking it says which field matched, on
 * which page, and jumps there with the field boxed. Without that a tag is
 * something a reviewer has to take on faith - and the tag patterns are
 * written by hand, so taking them on faith is exactly the wrong habit.
 *
 * The badges live in three places (the documents list, the preview header and
 * the standalone page view), so this is one delegated handler rather than
 * three bindings: the preview pane rebuilds its badges on every page turn,
 * and a handler bound to them would be re-bound or lost each time.
 */
(function () {
  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
  }

  function encodeDocId(docId) {
    return docId.split('/').map(encodeURIComponent).join('/');
  }

  // One panel open at a time within a host, and the badge it belongs to is
  // marked - two open panels in a document list would be unreadable, and an
  // unmarked one leaves the reviewer guessing which badge they asked about.
  function closePanels(host) {
    host.querySelectorAll(':scope > .tag-matches').forEach((el) => el.remove());
    host.querySelectorAll('.tag-custom.is-open').forEach((el) => el.classList.remove('is-open'));
  }

  function renderMatches(panel, tag, entry) {
    if (!entry || !entry.matches.length) {
      // Reachable: the tag was computed when the row was rendered, and a tag
      // edited since then may no longer match. Saying so beats an empty box.
      panel.innerHTML = `<p class="hint">Nothing on this document matches
        '${escapeHtml(tag)}' any more - the tag's pattern may have changed
        since this page was loaded. Reload to see the current tags.</p>`;
      return;
    }
    const shown = entry.matches.length;
    const more = entry.total > shown
      ? ` (showing the first ${shown})`
      : '';
    const items = entry.matches.map((m, idx) => `
      <a class="tag-match" href="${escapeHtml(m.url)}" data-idx="${idx}">
        <span class="score">page ${m.page_number} &middot; ${escapeHtml(m.method)}
          &middot; matched on ${escapeHtml(m.matched_on)}</span>
        <span class="kv"><span class="key">${escapeHtml(m.key)}</span>${escapeHtml(m.value)}</span>
      </a>
    `).join('');
    panel.innerHTML = `
      <p class="hint">'${escapeHtml(tag)}' matched ${entry.total}
        field${entry.total === 1 ? '' : 's'}${more}:</p>
      ${items}
    `;

    // Left-click opens the preview pane where there is one; the href is still
    // a real link, so a middle-click or ctrl-click opens the page in a tab.
    panel.querySelectorAll('.tag-match').forEach((el) => {
      el.addEventListener('click', (e) => {
        if (e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;
        if (!window.openPreview) return;
        const m = entry.matches[Number(el.dataset.idx)];
        e.preventDefault();
        window.openPreview(panel.dataset.claimId, panel.dataset.docId, m.page_number, {
          key_bbox: m.key_bbox, value_bbox: m.value_bbox,
        });
      });
    });
  }

  document.addEventListener('click', (e) => {
    const badge = e.target.closest('.tag-custom[data-tag]');
    if (!badge) return;
    const host = badge.closest('[data-tag-host]');
    if (!host) return;
    e.preventDefault();

    const reopening = badge.classList.contains('is-open');
    closePanels(host);
    if (reopening) return;           // a second click closes it

    const claimId = badge.dataset.claimId || window.CLAIM_ID;
    const docId = badge.dataset.docId;
    const tag = badge.dataset.tag;
    if (!claimId || !docId) return;

    badge.classList.add('is-open');
    const panel = document.createElement('div');
    panel.className = 'tag-matches';
    panel.dataset.claimId = claimId;
    panel.dataset.docId = docId;
    panel.innerHTML = '<p class="hint">Looking for what matched...</p>';
    host.appendChild(panel);

    const params = new URLSearchParams({ tag });
    // Present on a per-page badge (the preview header, the standalone view),
    // absent on the documents list, where the badge covers the whole document.
    if (badge.dataset.page) params.set('page', badge.dataset.page);

    fetch(`/api/claims/${claimId}/doc/${encodeDocId(docId)}/tag-matches?${params}`)
      .then((r) => r.json().then((data) => ({ ok: r.ok, data })))
      .then(({ ok, data }) => {
        if (!ok) {
          panel.innerHTML = `<p class="hint">${escapeHtml(data.error || 'Could not load matches.')}</p>`;
          return;
        }
        renderMatches(panel, tag, (data.tags || [])[0]);
      })
      .catch(() => {
        panel.innerHTML = '<p class="hint">Could not load matches - try again.</p>';
      });
  });
})();
