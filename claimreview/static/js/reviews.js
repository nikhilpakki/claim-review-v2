(function () {
  const bar = document.getElementById('review-batch');
  if (!bar) return;

  const picks = Array.from(document.querySelectorAll('.review-pick'));
  const selectAll = document.getElementById('review-select-all');
  const countLabel = document.getElementById('review-selected');
  const exportBtn = document.getElementById('review-export');
  const max = Number(bar.dataset.max || 100);

  function selected() {
    return picks.filter((box) => box.checked);
  }

  // The export button is the only thing on this page that can be pressed with
  // nothing chosen and no way to tell, so the count says what will go into the
  // document and the button is disabled until there is something to put there.
  function sync() {
    const chosen = selected().length;
    countLabel.textContent = chosen === 1 ? '1 selected' : chosen + ' selected';
    exportBtn.disabled = chosen === 0;
    if (chosen > max) {
      countLabel.textContent += ' — more than the ' + max + ' a single export carries';
      countLabel.classList.add('over-limit');
    } else {
      countLabel.classList.remove('over-limit');
    }
    if (selectAll) {
      selectAll.checked = chosen > 0 && chosen === picks.length;
      selectAll.indeterminate = chosen > 0 && chosen < picks.length;
    }
  }

  picks.forEach((box) => box.addEventListener('change', sync));
  if (selectAll) {
    selectAll.addEventListener('change', () => {
      picks.forEach((box) => { box.checked = selectAll.checked; });
      sync();
    });
  }

  // Building the document re-reads every selected claim's documents to render
  // its evidence pages, so a large batch is a slow request rather than a quick
  // one. Saying so beats a button that looks broken for half a minute.
  document.getElementById('export-form').addEventListener('submit', () => {
    exportBtn.disabled = true;
    exportBtn.textContent = 'Building the document…';
    // The response is a download, so nothing on this page changes when it
    // arrives - the button has to put itself back.
    window.setTimeout(() => {
      exportBtn.textContent = 'Export case validation summary';
      sync();
    }, 4000);
  });

  // Deleting somebody else's decision takes its evidence with it and cannot be
  // undone, so it is confirmed by naming what is about to go.
  document.querySelectorAll('.review-delete').forEach((btn) => {
    btn.addEventListener('click', (event) => {
      const message = 'Delete the decision on claim ' + btn.dataset.claim
        + ' by ' + btn.dataset.reviewer + '?\n\n'
        + 'This removes the decision and the evidence pages attached to it. '
        + 'It cannot be undone - a record of the deletion is kept.';
      if (!window.confirm(message)) event.preventDefault();
    });
  });

  sync();
})();
