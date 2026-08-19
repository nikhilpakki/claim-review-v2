(function () {
  // Popup listing every place a looked-up value (patient name, hospital) was
  // found in the claim's documents. The occurrences are rendered server-side
  // into a JSON script block per field, so opening this costs no request.
  const modal = document.getElementById('ocr-modal');
  if (!modal) return;

  const titleEl = document.getElementById('ocr-modal-title');
  const noteEl = document.getElementById('ocr-modal-note');
  const tbody = document.querySelector('#ocr-modal-table tbody');
  let lastFocused = null;

  function occurrencesFor(field) {
    const holder = document.querySelector('script.ocr-occurrences[data-field="' + CSS.escape(field) + '"]');
    if (!holder) return [];
    try {
      return JSON.parse(holder.textContent) || [];
    } catch (err) {
      return [];
    }
  }

  function cell(text) {
    const td = document.createElement('td');
    td.textContent = text === null || text === undefined || text === '' ? '—' : String(text);
    return td;
  }

  function open(link) {
    const field = link.dataset.field;
    const rows = occurrencesFor(field);
    titleEl.textContent = link.dataset.label + ': "' + link.textContent.trim() + '"';

    const summary = link.parentElement.querySelector('.settings-help');
    noteEl.textContent = (summary ? summary.textContent.replace(/\s+/g, ' ').trim() + '. ' : '')
      + 'Each row is a value in the documents that matched. Only the strictest stage that found '
      + 'anything is listed - an exact hit anywhere means fuzzy and phonetic near-misses are not '
      + 'mixed in, so this is not necessarily every mention of the name in the claim.';

    tbody.innerHTML = '';
    if (!rows.length) {
      const tr = document.createElement('tr');
      const td = document.createElement('td');
      td.colSpan = 5;
      td.className = 'hint';
      td.textContent = 'No occurrences recorded.';
      tr.appendChild(td);
      tbody.appendChild(tr);
    }
    rows.forEach((row) => {
      const tr = document.createElement('tr');
      tr.appendChild(cell(row.file));
      tr.appendChild(cell(row.page));
      tr.appendChild(cell(row.key));
      tr.appendChild(cell(row.value));
      tr.appendChild(cell(row.method + ' ' + row.score));
      tbody.appendChild(tr);
    });

    lastFocused = link;
    modal.hidden = false;
    modal.querySelector('[data-close]').focus();
  }

  function close() {
    modal.hidden = true;
    if (lastFocused) lastFocused.focus();
  }

  document.addEventListener('click', (e) => {
    const link = e.target.closest('.ocr-lookup-link');
    if (link) {
      e.preventDefault();
      open(link);
      return;
    }
    if (e.target.closest('[data-close]')) close();
  });

  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !modal.hidden) close();
  });
})();
