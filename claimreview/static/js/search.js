(function () {
  const input = document.getElementById('search-input');
  const btn = document.getElementById('search-btn');
  const resultsEl = document.getElementById('search-results');
  if (!input || !window.CLAIM_ID) return;
  const claimId = window.CLAIM_ID;

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
  }

  function splitPath(path) {
    const slash = path.lastIndexOf('/');
    return slash === -1
      ? { name: path, dir: '' }
      : { name: path.slice(slash + 1), dir: path.slice(0, slash) };
  }

  function matchUrl(match) {
    if (!match.page_number) return null;
    const docPath = match.file.split('/').map(encodeURIComponent).join('/');
    const params = new URLSearchParams({
      field: match.matched_on,
      key: match.key,
      value: match.value,
    });
    return `/claims/${claimId}/doc/${docPath}/page/${match.page_number}?${params.toString()}`;
  }

  function renderResults(data) {
    if (!data.best) {
      resultsEl.innerHTML = '<p class="hint">No match found (exact, fuzzy, and soundex lookups all failed).</p>';
      return;
    }
    const items = data.matches.map((m, idx) => {
      const url = matchUrl(m);
      const openLink = url ? `<a href="${url}" target="_blank" rel="noopener" class="open-new-tab" title="Open in new tab">&#8599;</a>` : '';
      const { name, dir } = splitPath(m.file);
      return `
        <div class="match-item" data-idx="${idx}">
          <span class="score">${m.score.toFixed(1)} &middot; ${m.method}</span>
          <div class="kv"><span class="key">${escapeHtml(m.key)}</span> ${escapeHtml(m.value)}</div>
          <div class="loc">
            <span class="doc-text">
              <span class="doc-file-name">${escapeHtml(name)}</span>
              ${dir ? `<span class="doc-dir-path">${escapeHtml(dir)}</span>` : ''}
              <span class="doc-dir-path">${m.page_number ? 'page ' + m.page_number + ' &middot; ' : ''}matched on ${m.matched_on}</span>
            </span>
            ${openLink}
          </div>
        </div>
      `;
    }).join('');
    resultsEl.innerHTML = `<p class="hint">${data.matches.length} match(es), best first:</p>` + items;

    resultsEl.querySelectorAll('.match-item').forEach((el) => {
      el.addEventListener('click', (e) => {
        if (e.target.classList.contains('open-new-tab')) return;
        const m = data.matches[Number(el.dataset.idx)];
        if (!m.page_number || !window.openPreview) return;
        window.openPreview(claimId, m.file, m.page_number, {
          key_bbox: m.key_bbox, value_bbox: m.value_bbox,
        });
      });
    });
  }

  function runSearch() {
    const q = input.value.trim();
    if (!q) {
      resultsEl.innerHTML = '';
      return;
    }
    resultsEl.innerHTML = '<p class="hint">Searching...</p>';
    fetch(`/api/claims/${claimId}/search?q=${encodeURIComponent(q)}`)
      .then((r) => r.json())
      .then(renderResults);
  }

  btn.addEventListener('click', runSearch);
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') runSearch();
  });
})();
