(function () {
  const img = document.getElementById('page-img');
  const frame = document.getElementById('page-frame');
  if (!img || !frame) return;

  function clearBoxes() {
    frame.querySelectorAll('.bbox-highlight').forEach((el) => el.remove());
  }

  function drawBox(bbox, className, flash) {
    if (!bbox || bbox.Width === undefined) return;
    const w = img.clientWidth;
    const h = img.clientHeight;
    const div = document.createElement('div');
    div.className = 'bbox-highlight' + (className ? ' ' + className : '') + (flash ? ' flash' : '');
    div.style.left = (bbox.Left * w) + 'px';
    div.style.top = (bbox.Top * h) + 'px';
    div.style.width = (bbox.Width * w) + 'px';
    div.style.height = (bbox.Height * h) + 'px';
    frame.appendChild(div);
    return div;
  }

  let pendingHighlight = null;

  function applyHighlight(highlight) {
    clearBoxes();
    if (!highlight) return;
    if (highlight.key_bbox) drawBox(highlight.key_bbox, '', true);
    if (highlight.value_bbox) drawBox(highlight.value_bbox, '', true);
    if (highlight.bbox) drawBox(highlight.bbox, 'signature', true);
  }

  function findInitialHighlight() {
    const ctx = window.PAGE_CONTEXT && window.PAGE_CONTEXT.highlight;
    if (!ctx || !ctx.key) return null;
    const item = Array.from(document.querySelectorAll('.form-field-item')).find((li) => {
      const nameEl = li.querySelector('.doc-name');
      return nameEl && nameEl.textContent.trim().startsWith(ctx.key + ':');
    });
    if (!item) return null;
    return {
      key_bbox: JSON.parse(item.dataset.keyBbox || 'null'),
      value_bbox: JSON.parse(item.dataset.valueBbox || 'null'),
    };
  }

  img.addEventListener('load', () => {
    applyHighlight(pendingHighlight || findInitialHighlight());
  });
  if (img.complete) {
    applyHighlight(pendingHighlight || findInitialHighlight());
  }

  window.addEventListener('resize', () => applyHighlight(pendingHighlight));

  document.querySelectorAll('.form-field-item').forEach((li) => {
    li.addEventListener('click', () => {
      pendingHighlight = {
        key_bbox: JSON.parse(li.dataset.keyBbox || 'null'),
        value_bbox: JSON.parse(li.dataset.valueBbox || 'null'),
      };
      applyHighlight(pendingHighlight);
    });
  });

  document.querySelectorAll('.signature-item').forEach((li) => {
    li.addEventListener('click', () => {
      pendingHighlight = { bbox: JSON.parse(li.dataset.bbox || 'null') };
      applyHighlight(pendingHighlight);
    });
  });

  // Signatures tab (on the claim page) links here with ?signature_id=...&bbox not
  // passed directly - instead it navigates with the page number and we just show
  // all signature boxes so the reviewer can see them without an exact id match.
  const params = new URLSearchParams(window.location.search);
  if (params.get('field') === 'signature') {
    document.querySelectorAll('.signature-item').forEach((li) => {
      const bbox = JSON.parse(li.dataset.bbox || 'null');
      if (bbox) drawBox(bbox, 'signature', false);
    });
  }
})();
