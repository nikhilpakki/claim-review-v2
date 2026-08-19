(function () {
  const container = document.getElementById('preview-col');
  if (!container) return;

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
  }

  function encodeDocId(docId) {
    return docId.split('/').map(encodeURIComponent).join('/');
  }

  function splitPath(path) {
    const slash = path.lastIndexOf('/');
    return slash === -1
      ? { name: path, dir: '' }
      : { name: path.slice(slash + 1), dir: path.slice(0, slash) };
  }

  function drawBoxes(frame, img, boxes) {
    frame.querySelectorAll('.bbox-highlight').forEach((el) => el.remove());
    const w = img.clientWidth;
    const h = img.clientHeight;
    boxes.forEach(({ bbox, className }) => {
      if (!bbox || bbox.Width === undefined) return;
      const div = document.createElement('div');
      div.className = 'bbox-highlight flash' + (className ? ' ' + className : '');
      div.style.left = (bbox.Left * w) + 'px';
      div.style.top = (bbox.Top * h) + 'px';
      div.style.width = (bbox.Width * w) + 'px';
      div.style.height = (bbox.Height * h) + 'px';
      frame.appendChild(div);
    });
  }

  function highlightBoxes(highlight) {
    if (!highlight) return [];
    const boxes = [];
    if (highlight.key_bbox) boxes.push({ bbox: highlight.key_bbox });
    if (highlight.value_bbox) boxes.push({ bbox: highlight.value_bbox });
    if (highlight.bbox) boxes.push({ bbox: highlight.bbox, className: 'signature' });
    return boxes;
  }

  function render(claimId, docId, data, highlight) {
    const formsHtml = data.forms.map((f, idx) => `
      <li class="form-field-item" data-idx="${idx}">
        <span class="doc-name"><strong>${escapeHtml(f.key)}</strong>: ${escapeHtml(f.value)}</span>
      </li>
    `).join('') || '<li class="empty">No form fields detected on this page.</li>';

    const queriesHtml = Object.entries(data.queries || {}).map(([alias, ans]) => `
      <li><span class="doc-name">${escapeHtml(alias)}: ${escapeHtml(ans.answer)}</span>
        <span class="doc-group">${ans.confidence.toFixed(1)}%</span></li>
    `).join('');

    const sigsHtml = data.signatures.map((s, idx) => {
      const tags = [];
      if (s.paste_suspicious) {
        const reason = (s.notes || []).join('; ') || 'artifacts consistent with a pasted patch';
        tags.push(`<span class="tag tag-paste" title="${escapeHtml(reason)} - worth a closer look, not proof of tampering">&#9888; possible paste: ${escapeHtml(reason)}</span>`);
      }
      const dupes = s.possible_duplicate_of || [];
      if (dupes.length) {
        const dupLinks = dupes.map((d) => {
          const dPath = d.document_path.split('/').map(encodeURIComponent).join('/');
          const dUrl = `/claims/${d.claim_id}/doc/${dPath}/page/${d.page_number}`;
          return `<a href="${dUrl}" target="_blank" rel="noopener">${escapeHtml(d.claim_id)}/${escapeHtml(d.document_path)} (p.${d.page_number})</a>`;
        }).join(', ');
        tags.push(`<span class="tag tag-duplicate-signature" title="Matches a signature elsewhere">&#9888; matches: ${dupLinks}</span>`);
      }
      return `
        <li class="signature-item" data-idx="${idx}">
          <span class="doc-name">Signature ${idx + 1}</span>
          <span class="doc-group">${s.confidence.toFixed(1)}%</span>
          ${tags.length ? `<div class="sig-flags">${tags.join(' ')}</div>` : ''}
        </li>
      `;
    }).join('') || '<li class="empty">No signatures detected on this page.</li>';

    const tablesHtml = (data.tables || []).map((table) => `
      <table class="doc-table">
        ${table.map((row) => `<tr>${row.map((cell) => `<td>${escapeHtml(cell)}</td>`).join('')}</tr>`).join('')}
      </table>
    `).join('');

    const { name: previewName, dir: previewDir } = splitPath(data.file_name);
    container.innerHTML = `
      <div class="preview-header">
        <span class="doc-text" title="${escapeHtml(data.file_name)}">
          <span class="doc-file-name">${escapeHtml(previewName)}</span>
          ${previewDir ? `<span class="doc-dir-path">${escapeHtml(previewDir)}</span>` : ''}
        </span>
        <a href="${data.standalone_url}" target="_blank" rel="noopener" class="open-new-tab" title="Open in new tab">&#8599; open</a>
      </div>
      <div class="page-nav">
        ${data.page_number > 1 ? `<a href="#" id="prev-page-link">&laquo; prev</a>` : ''}
        <span>Page ${data.page_number} / ${data.num_pages}</span>
        ${data.page_number < data.num_pages ? `<a href="#" id="next-page-link">next &raquo;</a>` : ''}
        ${data.is_blurry ? `<span class="tag tag-blurry" title="Sharpness score ${data.blur_score}">&#9888; blurry</span>` : ''}
        ${data.content_type ? `<span class="tag tag-${data.content_type.replace(/ /g, '-')}">${data.content_type}</span>` : ''}
        ${data.too_clean_suspected ? `<span class="tag tag-too-clean" title="Suspiciously low scan/sensor noise - possibly a native digital PDF rather than a physical scan">&#9888; too clean</span>` : ''}
        ${data.cropped_suspected ? `<span class="tag tag-cropped" title="Content runs right up to the image edge (${(data.edges || []).join(', ')}), suggesting a cropped/cut-off capture">&#9888; cropped</span>` : ''}
        ${data.correction_suspected ? `<span class="tag tag-correction" title="Localized ink-density anomaly - possible correction/strikethrough. Low confidence, can misfire on stamps/tables/signatures.">&#9888; possible correction</span>` : ''}
        ${data.has_face ? `<span class="tag tag-has-face" title="A face was detected on this page">&#9888; has face</span>` : ''}
      </div>
      <div class="doc-viewer">
        <div class="page-pane">
          <div class="page-frame" id="preview-page-frame">
            <img id="preview-page-img" src="${data.image_url}">
          </div>
        </div>
        <div class="side-pane">
          <h3>Form fields on this page (${data.forms.length})</h3>
          <ul class="doc-list">${formsHtml}</ul>
          ${queriesHtml ? `<h3>Query answers</h3><ul class="doc-list">${queriesHtml}</ul>` : ''}
          <h3>Signatures on this page (${data.signatures.length})</h3>
          <ul class="doc-list">${sigsHtml}</ul>
          ${tablesHtml ? `<h3>Tables on this page (${data.tables.length})</h3>${tablesHtml}` : ''}
        </div>
      </div>
    `;

    const frame = document.getElementById('preview-page-frame');
    const img = document.getElementById('preview-page-img');

    const applyInitial = () => drawBoxes(frame, img, highlightBoxes(highlight));
    if (img.complete) applyInitial();
    img.addEventListener('load', applyInitial);
    window.addEventListener('resize', () => drawBoxes(frame, img, highlightBoxes(highlight)), { once: true });

    container.querySelectorAll('.form-field-item').forEach((li) => {
      li.addEventListener('click', () => {
        const f = data.forms[Number(li.dataset.idx)];
        drawBoxes(frame, img, [{ bbox: f.key_bbox }, { bbox: f.value_bbox }]);
      });
    });
    container.querySelectorAll('.signature-item').forEach((li) => {
      li.addEventListener('click', () => {
        const s = data.signatures[Number(li.dataset.idx)];
        drawBoxes(frame, img, [{ bbox: s.bbox, className: 'signature' }]);
      });
    });

    const prevLink = document.getElementById('prev-page-link');
    if (prevLink) prevLink.addEventListener('click', (e) => {
      e.preventDefault();
      window.openPreview(claimId, docId, data.page_number - 1, null);
    });
    const nextLink = document.getElementById('next-page-link');
    if (nextLink) nextLink.addEventListener('click', (e) => {
      e.preventDefault();
      window.openPreview(claimId, docId, data.page_number + 1, null);
    });
  }

  window.openPreview = function (claimId, docId, page, highlight) {
    container.innerHTML = '<p class="hint">Loading preview...</p>';
    fetch(`/api/claims/${claimId}/doc/${encodeDocId(docId)}/page/${page}/blocks`)
      .then((r) => r.json().then((data) => ({ ok: r.ok, data })))
      .then(({ ok, data }) => {
        if (!ok) {
          container.innerHTML = `<p class="hint">${escapeHtml(data.error || 'Could not load preview.')}</p>`;
          return;
        }
        render(claimId, docId, data, highlight);
      });
  };
})();
