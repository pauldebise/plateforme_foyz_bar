function csrfToken() {
  return document.querySelector('meta[name="csrf-token"]').content;
}

async function apiFetch(url, options = {}) {
  const opts = Object.assign({ headers: { 'X-CSRFToken': csrfToken() } }, options);
  if (opts.json !== undefined) {
    opts.method = opts.method || 'POST';
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(opts.json);
    delete opts.json;
  }
  const res = await fetch(url, opts);
  let data = null;
  try { data = await res.json(); } catch (e) { data = null; }
  if (!res.ok) {
    const err = new Error((data && data.error) || `Erreur ${res.status}`);
    err.code = data && data.code;
    err.extra = (data && data.extra) || {};
    throw err;
  }
  return data;
}

function initStudentSearch(inputEl, listEl, onPick, options = {}) {
  let timer = null;
  let results = [];
  inputEl.addEventListener('input', () => {
    clearTimeout(timer);
    timer = setTimeout(async () => {
      const q = inputEl.value.trim();
      if (q.length < 1) { listEl.innerHTML = ''; listEl.classList.add('d-none'); return; }
      const campus = options.campus || '';
      const url = `/api/students?q=${encodeURIComponent(q)}${campus ? '&campus=' + campus : ''}`;
      results = await apiFetch(url);
      listEl.innerHTML = '';
      if (!results.length) {
        listEl.innerHTML = '<div class="px-3 py-2 text-muted small">Aucun résultat</div>';
      }
      results.forEach((r) => {
        const div = document.createElement('div');
        div.className = 'search-result px-3 py-2 border-bottom d-flex justify-content-between align-items-center';
        const badges = [];
        if (r.blacklist) badges.push('<span class="badge badge-blacklist">blacklist</span>');
        if (r.blacklist_alcohol) badges.push('<span class="badge badge-alcool">blacklist alcool</span>');
        if (r.is_team) badges.push('<span class="badge bg-secondary">équipe</span>');
        div.innerHTML = `<div><strong>${r.name}</strong>
          ${r.promotion ? `<span class="text-muted small">· promo ${r.promotion}</span>` : ''}
          <span class="balance-chip badge bg-light text-dark ms-1">${(r.balance / 100).toFixed(2)} €</span> ${badges.join(' ')}</div>`;
        div.addEventListener('click', () => {
          onPick(r);
          listEl.innerHTML = '';
          listEl.classList.add('d-none');
          inputEl.value = '';
        });
        listEl.appendChild(div);
      });
      listEl.classList.remove('d-none');
    }, 200);
  });
  document.addEventListener('click', (e) => {
    if (!listEl.contains(e.target) && e.target !== inputEl) listEl.classList.add('d-none');
  });
}
