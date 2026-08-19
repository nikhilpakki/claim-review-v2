(function () {
  const dirList = document.getElementById('dir-list');
  const pathInput = document.getElementById('path-input');
  const pathForm = document.getElementById('path-form');
  const breadcrumb = document.getElementById('breadcrumb');
  const selectBtn = document.getElementById('select-root-btn');

  function renderBreadcrumb(path) {
    breadcrumb.innerHTML = '';
    if (!path) return;
    const sep = path.includes('\\') ? '\\' : '/';
    const parts = path.split(sep).filter(Boolean);
    let acc = path.startsWith(sep) ? sep : '';
    parts.forEach((part, idx) => {
      acc = idx === 0 && /^[A-Za-z]:$/.test(part) ? part + sep : acc + part + sep;
      const a = document.createElement('a');
      a.href = '#';
      a.textContent = part;
      a.dataset.path = acc.endsWith(sep) ? acc.slice(0, -1) : acc;
      a.className = 'crumb';
      breadcrumb.appendChild(a);
      breadcrumb.appendChild(document.createTextNode(' / '));
    });
  }

  function loadPath(path) {
    const url = new URL(window.location.origin + '/browse');
    url.searchParams.set('format', 'json');
    if (path) url.searchParams.set('path', path);
    fetch(url).then((r) => r.json()).then((data) => {
      if (data.error) {
        alert(data.error);
        return;
      }
      pathInput.value = data.path || '';
      renderBreadcrumb(data.path);
      dirList.innerHTML = '';
      if (!data.entries.length) {
        const li = document.createElement('li');
        li.className = 'empty';
        li.textContent = 'No subfolders here.';
        dirList.appendChild(li);
      }
      data.entries.forEach((entry) => {
        const li = document.createElement('li');
        const a = document.createElement('a');
        a.href = '#';
        a.className = 'dir-entry';
        a.dataset.path = entry.path;
        a.textContent = entry.name;
        li.appendChild(a);
        dirList.appendChild(li);
      });
      selectBtn.disabled = !data.path;
      selectBtn.dataset.path = data.path || '';
      history.replaceState(null, '', '/browse');
    });
  }

  document.body.addEventListener('click', (e) => {
    const target = e.target.closest('.dir-entry, .crumb');
    if (!target) return;
    e.preventDefault();
    loadPath(target.dataset.path);
  });

  pathForm.addEventListener('submit', (e) => {
    e.preventDefault();
    loadPath(pathInput.value.trim());
  });

  selectBtn.addEventListener('click', () => {
    const path = selectBtn.dataset.path || pathInput.value.trim();
    if (!path) return;
    const form = new FormData();
    form.set('path', path);
    fetch('/browse/select-root', { method: 'POST', body: form })
      .then((r) => {
        if (r.redirected) {
          window.location.href = r.url;
        } else {
          return r.json().then((data) => alert(data.error || 'Could not select folder'));
        }
      });
  });

  renderBreadcrumb(pathInput.value);
})();
