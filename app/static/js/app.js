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
  let activeIndex = -1;

  function setActive(i) {
    const items = listEl.querySelectorAll('.search-result');
    if (!items.length) return;
    activeIndex = Math.max(0, Math.min(i, items.length - 1));
    items.forEach((el, idx) => el.classList.toggle('active', idx === activeIndex));
    items[activeIndex].scrollIntoView({ block: 'nearest' });
  }

  function pick(index) {
    const r = results[index];
    if (!r) return;
    onPick(r);
    results = [];
    activeIndex = -1;
    listEl.innerHTML = '';
    listEl.classList.add('d-none');
    inputEl.value = '';
  }

  inputEl.addEventListener('input', () => {
    clearTimeout(timer);
    timer = setTimeout(async () => {
      const q = inputEl.value.trim();
      if (q.length < 1) { listEl.innerHTML = ''; listEl.classList.add('d-none'); activeIndex = -1; return; }
      const campus = options.campus || '';
      const url = `/api/students?q=${encodeURIComponent(q)}${campus ? '&campus=' + campus : ''}`;
      results = await apiFetch(url);
      activeIndex = -1;
      listEl.innerHTML = '';
      if (!results.length) {
        listEl.innerHTML = '<div class="px-3 py-2 text-muted small">Aucun résultat</div>';
      }
      results.forEach((r, idx) => {
        const div = document.createElement('div');
        div.className = 'search-result px-3 py-2 border-bottom d-flex justify-content-between align-items-center';
        const badges = [];
        if (r.blacklist) badges.push('<span class="badge badge-blacklist">blacklist</span>');
        if (r.blacklist_alcohol) badges.push('<span class="badge badge-alcool">blacklist alcool</span>');
        if (r.is_team) badges.push('<span class="badge bg-secondary">équipe</span>');
        div.innerHTML = `<div><strong>${r.name}</strong>
          ${r.promotion ? `<span class="text-muted small">· promo ${r.promotion}</span>` : ''}
          <span class="balance-chip badge bg-light text-dark ms-1">${(r.balance / 100).toFixed(2)} €</span> ${badges.join(' ')}</div>`;
        div.addEventListener('click', () => pick(idx));
        div.addEventListener('mouseenter', () => setActive(idx));
        listEl.appendChild(div);
      });
      listEl.classList.remove('d-none');
      setActive(0);
    }, 200);
  });

  inputEl.addEventListener('keydown', (e) => {
    const open = !listEl.classList.contains('d-none') && results.length > 0;
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      if (open) setActive(activeIndex + 1);
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      if (open) setActive(activeIndex - 1);
    } else if (e.key === 'Enter') {
      if (open) {
        e.preventDefault();
        pick(activeIndex);
      }
    } else if (e.key === 'Escape' && !listEl.classList.contains('d-none')) {
      listEl.classList.add('d-none');
      activeIndex = -1;
    }
  });

  document.addEventListener('click', (e) => {
    if (!listEl.contains(e.target) && e.target !== inputEl) listEl.classList.add('d-none');
  });
}
