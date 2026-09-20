function csrfToken() {
  return document.querySelector('meta[name="csrf-token"]').content;
}

// Construction d'éléments sans jamais interpréter de HTML : les noms
// d'articles/étudiants viennent de la base (voire de la migration) et ne
// doivent jamais être injectés via innerHTML (XSS stocké).
function makeEl(tag, className, text) {
  const el = document.createElement(tag);
  if (className) el.className = className;
  if (text !== undefined && text !== null) el.textContent = text;
  return el;
}

function appendBadges(parent, badges) {
  badges.forEach(([cls, label]) => {
    parent.appendChild(makeEl('span', `badge ${cls}`, label));
  });
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

let searchListSeq = 0;

// Liste de résultats accessible : combobox + listbox ARIA, options focusables
// au clavier (flèches, Entrée, Échap) et restituées au lecteur d'écran (U5).
function initStudentSearch(inputEl, listEl, onPick, options = {}) {
  let timer = null;
  let results = [];
  let activeIndex = -1;

  const listId = listEl.id || `search-list-${++searchListSeq}`;
  listEl.id = listId;
  listEl.setAttribute('role', 'listbox');
  listEl.setAttribute('aria-label', 'Résultats de recherche');
  inputEl.setAttribute('role', 'combobox');
  inputEl.setAttribute('aria-autocomplete', 'list');
  inputEl.setAttribute('aria-controls', listId);
  inputEl.setAttribute('aria-expanded', 'false');

  function setOpen(open) {
    listEl.classList.toggle('d-none', !open);
    inputEl.setAttribute('aria-expanded', open ? 'true' : 'false');
    if (!open) inputEl.removeAttribute('aria-activedescendant');
  }

  function setActive(i) {
    const items = listEl.querySelectorAll('.search-result');
    if (!items.length) return;
    activeIndex = Math.max(0, Math.min(i, items.length - 1));
    items.forEach((el, idx) => {
      const isActive = idx === activeIndex;
      el.classList.toggle('active', isActive);
      el.setAttribute('aria-selected', isActive ? 'true' : 'false');
    });
    items[activeIndex].scrollIntoView({ block: 'nearest' });
    inputEl.setAttribute('aria-activedescendant', items[activeIndex].id);
  }

  function pick(index) {
    const r = results[index];
    if (!r) return;
    onPick(r);
    results = [];
    activeIndex = -1;
    listEl.innerHTML = '';
    setOpen(false);
    inputEl.value = options.keepValue ? r.name : '';
  }

  inputEl.addEventListener('input', () => {
    clearTimeout(timer);
    timer = setTimeout(async () => {
      const q = inputEl.value.trim();
      if (q.length < 1) {
        listEl.innerHTML = '';
        setOpen(false);
        activeIndex = -1;
        return;
      }
      const campus = options.campus || '';
      const url = `/api/students?q=${encodeURIComponent(q)}${campus ? '&campus=' + campus : ''}`;
      results = await apiFetch(url);
      activeIndex = -1;
      listEl.innerHTML = '';
      if (!results.length) {
        const empty = document.createElement('div');
        empty.className = 'px-3 py-2 text-muted small';
        empty.setAttribute('role', 'status');
        empty.textContent = 'Aucun résultat';
        listEl.appendChild(empty);
      }
      results.forEach((r, idx) => {
        const div = document.createElement('div');
        div.className = 'search-result px-3 py-2 border-bottom d-flex justify-content-between align-items-center';
        div.id = `${listId}-opt-${idx}`;
        div.setAttribute('role', 'option');
        div.setAttribute('aria-selected', 'false');
        div.tabIndex = -1;
        const info = document.createElement('div');
        info.appendChild(makeEl('strong', '', r.name));
        if (r.promotion) info.appendChild(makeEl('span', 'text-muted small', ` · promo ${r.promotion}`));
        info.appendChild(makeEl(
          'span', 'balance-chip badge bg-light text-dark ms-1', `${(r.balance / 100).toFixed(2)} €`,
        ));
        const badges = [];
        if (r.blacklist) badges.push(['badge-blacklist', 'blacklist']);
        if (r.blacklist_alcohol) badges.push(['badge-alcool', 'blacklist alcool']);
        if (r.is_team) badges.push(['bg-secondary', 'équipe']);
        appendBadges(info, badges);
        div.appendChild(info);
        div.addEventListener('click', () => pick(idx));
        div.addEventListener('mouseenter', () => setActive(idx));
        listEl.appendChild(div);
      });
      setOpen(true);
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
      setOpen(false);
      activeIndex = -1;
    }
  });

  document.addEventListener('click', (e) => {
    if (!listEl.contains(e.target) && e.target !== inputEl) setOpen(false);
  });
}
