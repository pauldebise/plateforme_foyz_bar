let contributors = [];
let cart = new Map();
const CATALOG = JSON.parse(document.getElementById('catalog-data').textContent);
const CONFIG = document.getElementById('payment-config');
const DEPOSIT_VALUE = parseInt(CONFIG.dataset.depositValue, 10);
const DEPOSIT_ENABLED = CONFIG.dataset.depositEnabled === '1';
const CAMPUS = CONFIG.dataset.campus || '';

const $ = (id) => document.getElementById(id);
const els = {
  search: $('student-search'),
  results: $('student-results'),
  contributorList: $('contributor-list'),
  contributorsCard: $('contributors-card'),
  catalogSearch: $('catalog-search'),
  catalog: $('catalog'),
  cartBody: $('cart-body'),
  cartEmpty: $('cart-empty'),
  total: $('cart-total'),
  depositSwitch: $('deposit-switch'),
  glasses: $('deposit-glasses'),
  directSwitch: $('direct-switch'),
  directMethods: $('direct-methods'),
  payBtn: $('pay-button'),
  adminModal: $('admin-modal') ? new bootstrap.Modal($('admin-modal')) : null,
  successModal: $('success-modal') ? new bootstrap.Modal($('success-modal')) : null,
  rgSearch: $('glasses-search'),
  rgResults: $('glasses-results'),
  rgPanel: $('glasses-panel'),
};

function allTeam() {
  return contributors.length > 0 && contributors.every((c) => c.is_team);
}

function unitPrice(article) {
  return allTeam() ? article.team : article.std;
}

function isDirect() {
  return els.directSwitch ? els.directSwitch.checked : false;
}

function depositActive() {
  return DEPOSIT_ENABLED && !isDirect() && els.depositSwitch && els.depositSwitch.checked;
}

function glassesCount() {
  return depositActive() ? Math.max(0, parseInt(els.glasses.value || '0', 10)) : 0;
}

function articleTotal() {
  let sum = 0;
  cart.forEach((qty, id) => {
    const a = CATALOG.find((x) => x.id === id);
    if (a) sum += unitPrice(a) * qty;
  });
  return sum;
}

function grandTotal() {
  return articleTotal() + glassesCount() * DEPOSIT_VALUE;
}

function renderContributors() {
  if (!els.contributorList) return;
  els.contributorList.innerHTML = '';
  contributors.forEach((c) => {
    const li = document.createElement('li');
    li.className = 'list-group-item d-flex justify-content-between align-items-center px-2';
    const badges = [];
    if (c.blacklist) badges.push('<span class="badge badge-blacklist">blacklist</span>');
    if (c.blacklist_alcohol) badges.push('<span class="badge badge-alcool">blacklist alcool</span>');
    li.innerHTML = `<div><i class="bi bi-person-circle"></i> <strong>${c.name}</strong>
      <span class="badge bg-light text-dark">${(c.balance / 100).toFixed(2)} €</span> ${badges.join(' ')}</div>
      <button class="btn btn-sm btn-outline-danger" title="Retirer"><i class="bi bi-x"></i></button>`;
    li.querySelector('button').addEventListener('click', () => {
      contributors = contributors.filter((x) => x.id !== c.id);
      renderContributors();
    });
    els.contributorList.appendChild(li);
  });
  if (!contributors.length) {
    els.contributorList.innerHTML = '<li class="list-group-item text-muted px-2">Aucun étudiant sélectionné.</li>';
  }
  updatePayButton();
}

function updatePayButton() {
  if (els.payBtn) els.payBtn.disabled = cart.size === 0 || (!contributors.length && !isDirect());
}

const TYPE_LABELS = {
  biere: 'Bières', vin: 'Vins', cidre: 'Cidres',
  snack: 'Snacks', saucisson: 'Saucissons', evenement: 'Événements',
};

let catalogActive = -1;

function catalogItems() {
  return els.catalog ? Array.from(els.catalog.querySelectorAll('.cat-item')) : [];
}

function setCatalogActive(i) {
  const items = catalogItems();
  if (!items.length) { catalogActive = -1; return; }
  catalogActive = Math.max(0, Math.min(i, items.length - 1));
  items.forEach((el, idx) => el.classList.toggle('active', idx === catalogActive));
  items[catalogActive].scrollIntoView({ block: 'nearest' });
}

function clearCatalogActive() {
  catalogActive = -1;
  catalogItems().forEach((el) => el.classList.remove('active'));
}

function catalogCard(label, icon, cls, items) {
  const card = document.createElement('div');
  card.className = 'card mb-2' + (cls ? ` ${cls}` : '');
  card.innerHTML = `<div class="card-header py-1 small fw-bold">${icon} ${label}</div><div class="card-body p-0"></div>`;
  const body = card.querySelector('.card-body');
  items.forEach((a) => body.appendChild(catalogRow(a)));
  return card;
}

// Tri "popularité récente", comme la recherche d'étudiants : les articles les
// plus vendus et les plus récemment vendus arrivent en tête, puis alphabétique.
function popularitySort(a, b) {
  const ra = a.rank ?? Number.MAX_SAFE_INTEGER;
  const rb = b.rank ?? Number.MAX_SAFE_INTEGER;
  if (ra !== rb) return ra - rb;
  return a.name.localeCompare(b.name, 'fr', { numeric: true, sensitivity: 'base' });
}

const TAP_SIZE_ORDER = ['Demi', 'Pinte', 'Pot'];

function tapGroups() {
  const map = new Map();
  CATALOG.filter((a) => a.tap).forEach((a) => {
    const m = a.name.match(/^(Demi|Pinte|Pot) de (.*)$/);
    const key = a.tap_number != null ? `t${a.tap_number}` : a.id;
    const g = map.get(key) || { label: m ? m[2] : a.name, sizes: [] };
    g.sizes.push({ size: m ? m[1] : 'Pinte', article: a });
    map.set(key, g);
  });
  const groups = Array.from(map.values());
  groups.forEach((g) => g.sizes.sort(
    (x, y) => TAP_SIZE_ORDER.indexOf(x.size) - TAP_SIZE_ORDER.indexOf(y.size),
  ));
  return groups;
}

function tapRow(group) {
  const row = document.createElement('div');
  row.className = 'cat-item tap-item';
  row.dataset.id = (group.sizes.find((s) => s.size === 'Pinte') || group.sizes[0]).article.id;
  const alcohol = group.sizes.some((s) => s.article.alcohol)
    ? '<i class="bi bi-exclamation-diamond text-warning" title="Alcoolisé"></i>' : '';
  const buttons = group.sizes.map((s) =>
    `<button class="btn btn-sm btn-primary tap-btn"><span class="tap-size">${s.size}</span><span class="tap-price">${(unitPrice(s.article) / 100).toFixed(2)} € <i class="bi bi-plus-lg"></i></span></button>`
  ).join('');
  row.innerHTML = `<span class="tap-label">${group.label} ${alcohol}</span><div class="tap-sizes">${buttons}</div>`;
  row.querySelectorAll('.tap-btn').forEach((btn, i) => {
    btn.addEventListener('click', () => addToCart(group.sizes[i].article));
  });
  row.addEventListener('mouseenter', () => {
    const idx = catalogItems().indexOf(row);
    if (idx >= 0) setCatalogActive(idx);
  });
  return row;
}

function tapCard(taps) {
  const card = document.createElement('div');
  card.className = 'card mb-2 border-warning';
  card.innerHTML = `<div class="card-header py-1 small fw-bold"><i class="bi bi-cup-straw"></i> Tireuses</div><div class="card-body p-0"></div>`;
  const body = card.querySelector('.card-body');
  taps.forEach((g) => body.appendChild(tapRow(g)));
  return card;
}

function eventCard(eventItems) {
  const card = document.createElement('div');
  card.className = 'card mb-2 border-warning';
  card.innerHTML = `<div class="card-header py-1 small fw-bold text-warning-emphasis"><i class="bi bi-calendar-event"></i> Articles de l'événement</div><div class="card-body p-0"></div>`;
  const body = card.querySelector('.card-body');
  eventItems.forEach((a) => body.appendChild(catalogRow(a)));
  return card;
}

function standardCards() {
  const groups = {};
  CATALOG.filter((a) => !a.event && !a.tap).forEach((a) => {
    (groups[a.type] = groups[a.type] || []).push(a);
  });
  return Object.entries(groups).map(([type, items]) => (
    catalogCard(TYPE_LABELS[type] || type, '', '', items)
  ));
}

function renderCatalog() {
  if (!els.catalog) return;
  const q = els.catalogSearch.value.trim().toLowerCase();
  els.catalog.innerHTML = '';
  if (q) {
    const matches = CATALOG
      .filter((a) => a.name.toLowerCase().includes(q))
      .sort(popularitySort);
    clearCatalogActive();
    if (!matches.length) {
      const none = document.createElement('div');
      none.className = 'text-muted text-center py-4';
      none.textContent = 'Aucun article ne correspond à cette recherche.';
      els.catalog.appendChild(none);
      return;
    }
    const card = document.createElement('div');
    card.className = 'card mb-2';
    card.innerHTML = '<div class="card-body p-0"></div>';
    const body = card.querySelector('.card-body');
    matches.forEach((a) => body.appendChild(catalogRow(a)));
    els.catalog.appendChild(card);
    return;
  }
  // Passerelle : événement, tireuses puis catalogue standard — sans réduction
  // aux « tendances » (les articles de l'événement doivent rester visibles
  // même sans historique de vente).
  if (window.GATEWAY_MODE) {
    const eventItems = CATALOG.filter((a) => a.event);
    if (eventItems.length) els.catalog.appendChild(eventCard(eventItems));
    const taps = tapGroups();
    if (taps.length) els.catalog.appendChild(tapCard(taps));
    standardCards().forEach((card) => els.catalog.appendChild(card));
    clearCatalogActive();
    return;
  }

  const trending = CATALOG
    .filter((a) => a.rank != null && !a.tap)
    .sort(popularitySort)
    .slice(0, 9);
  const taps = tapGroups();
  if (trending.length || taps.length) {
    if (taps.length) els.catalog.appendChild(tapCard(taps));
    if (trending.length) {
      els.catalog.appendChild(catalogCard(
        'Articles tendances', '<i class="bi bi-fire"></i>', 'border-success', trending,
      ));
    }
    clearCatalogActive();
    return;
  }
  const groups = {};
  CATALOG.forEach((a) => {
    (groups[a.type] = groups[a.type] || []).push(a);
  });
  const eventItems = CATALOG.filter((a) => a.event);
  if (eventItems.length) {
    const card = document.createElement('div');
    card.className = 'card mb-2 border-warning';
    card.innerHTML = `<div class="card-header py-1 small fw-bold text-warning-emphasis">Articles de l'événement</div><div class="card-body p-0"></div>`;
    const body = card.querySelector('.card-body');
    eventItems.forEach((a) => body.appendChild(catalogRow(a)));
    els.catalog.appendChild(card);
  }
  Object.entries(groups).forEach(([type, items]) => {
    const std = items.filter((a) => !a.event);
    if (!std.length) return;
    const card = document.createElement('div');
    card.className = 'card mb-2';
    card.innerHTML = `<div class="card-header py-1 small">${TYPE_LABELS[type] || type}</div><div class="card-body p-0"></div>`;
    const body = card.querySelector('.card-body');
    std.forEach((a) => body.appendChild(catalogRow(a)));
    els.catalog.appendChild(card);
  });
  clearCatalogActive();
}

function catalogRow(a) {
  const row = document.createElement('div');
  row.className = 'cat-item';
  row.dataset.id = a.id;
  row.innerHTML = `<div><span>${a.name}</span>
    ${a.alcohol ? '<i class="bi bi-exclamation-diamond text-warning" title="Alcoolisé"></i>' : ''}
    ${a.volume ? `<span class="text-muted small">${a.volume} cl</span>` : ''}</div>
    <div class="d-flex align-items-center gap-2">
      <span class="badge bg-light text-dark" data-price>${(unitPrice(a) / 100).toFixed(2)} €</span>
      <button class="btn btn-sm btn-primary qty-btn"><i class="bi bi-plus-lg"></i></button>
    </div>`;
  row.querySelector('button').addEventListener('click', () => addToCart(a));
  row.addEventListener('mouseenter', () => {
    const idx = catalogItems().indexOf(row);
    if (idx >= 0) setCatalogActive(idx);
  });
  return row;
}

function addToCart(article) {
  cart.set(article.id, (cart.get(article.id) || 0) + 1);
  renderCart();
  if (els.catalogSearch) els.catalogSearch.focus({ preventScroll: true });
}

function renderCart() {
  if (!els.cartBody) return;
  els.cartBody.innerHTML = '';
  if (!cart.size) els.cartEmpty.classList.remove('d-none');
  else els.cartEmpty.classList.add('d-none');
  cart.forEach((qty, id) => {
    const a = CATALOG.find((x) => x.id === id);
    if (!a) return;
    const unit = unitPrice(a);
    const tr = document.createElement('tr');
    tr.innerHTML = `<td>${a.name}</td>
      <td class="text-end" data-unit>${(unit / 100).toFixed(2)} €</td>
      <td class="text-center">
        <div class="btn-group btn-group-sm">
          <button class="btn btn-outline-secondary qty-btn" data-dec><i class="bi bi-dash-lg"></i></button>
          <span class="btn btn-light disabled" data-qty style="min-width:44px">${qty}</span>
          <button class="btn btn-outline-secondary qty-btn" data-inc><i class="bi bi-plus-lg"></i></button>
        </div>
      </td>
      <td class="text-end fw-bold" data-line>${((unit * qty) / 100).toFixed(2)} €</td>
      <td class="text-end"><button class="btn btn-sm btn-outline-danger" data-del><i class="bi bi-trash"></i></button></td>`;
    tr.querySelector('[data-inc]').addEventListener('click', () => { cart.set(id, qty + 1); renderCart(); });
    tr.querySelector('[data-dec]').addEventListener('click', () => {
      qty > 1 ? cart.set(id, qty - 1) : cart.delete(id);
      renderCart();
    });
    tr.querySelector('[data-del]').addEventListener('click', () => { cart.delete(id); renderCart(); });
    els.cartBody.appendChild(tr);
  });
  const depositRow = document.getElementById('deposit-row');
  if (depositRow) depositRow.remove();
  if (glassesCount() > 0) {
    const drow = els.cartBody.insertRow();
    drow.id = 'deposit-row';
    drow.innerHTML = `<td>Consigne (verres empruntés)</td><td class="text-end">${(DEPOSIT_VALUE / 100).toFixed(2)} €</td>
      <td class="text-center">${glassesCount()}</td>
      <td class="text-end fw-bold">${((glassesCount() * DEPOSIT_VALUE) / 100).toFixed(2)} €</td><td></td>`;
  }
  els.total.textContent = (grandTotal() / 100).toFixed(2) + ' €';
  updatePayButton();
}

function buildPayload(adminPassword) {
  const items = [];
  cart.forEach((qty, id) => items.push({ article_id: id, quantity: qty }));
  const payload = {
    items,
    deposit_glasses: glassesCount(),
    direct: isDirect(),
    admin_password: adminPassword || undefined,
  };
  if (isDirect()) payload.payment_method = els.directMethods.value;
  else payload.contributors = contributors.map((c) => c.id);
  return payload;
}

let paying = false;

async function pay(adminPassword) {
  if (paying) return;
  paying = true;
  try {
    const res = await apiFetch(window.GATEWAY_MODE ? location.pathname + '/encaisser' : '/api/purchase', { json: buildPayload(adminPassword) });
    showSuccess(res.total);
  } catch (e) {
    if (e.code === 'admin_password_required' && els.adminModal) {
      $('admin-negative-users').textContent = (e.extra.negative_users || []).join(', ');
      els.adminModal.show();
    } else {
      alert(e.message);
    }
  } finally {
    paying = false;
  }
}

function showSuccess(total) {
  if (!els.successModal) return;
  $('success-total').textContent = (total / 100).toFixed(2) + ' €';
  cart = new Map();
  contributors = [];
  if (els.depositSwitch) els.depositSwitch.checked = false;
  if (els.glasses) els.glasses.value = 1;
  if (els.directSwitch) els.directSwitch.checked = false;
  renderContributors();
  renderCart();
  renderCatalog();
  els.successModal.show();
  setTimeout(() => window.location.reload(), 2600);
}

if ($('admin-validate')) {
  $('admin-validate').addEventListener('click', () => {
    const pwd = $('admin-password-input').value;
    els.adminModal.hide();
    pay(pwd);
  });
}

if (els.directSwitch) {
  els.directSwitch.addEventListener('change', () => {
    if (els.contributorsCard) els.contributorsCard.classList.toggle('d-none', isDirect());
    if (els.depositSwitch) $('deposit-card').classList.toggle('d-none', isDirect());
    $('direct-card').classList.toggle('d-none', !isDirect());
    renderContributors();
    renderCart();
  });
}

if (els.depositSwitch) {
  els.depositSwitch.addEventListener('change', renderCart);
  els.glasses.addEventListener('input', renderCart);
}

if (els.catalogSearch) {
  els.catalogSearch.addEventListener('input', () => {
    renderCatalog();
    if (els.catalogSearch.value.trim()) setCatalogActive(0);
  });

  els.catalogSearch.addEventListener('keydown', (e) => {
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      setCatalogActive(catalogActive + 1);
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      setCatalogActive(catalogActive - 1);
    } else if (e.key === 'Enter') {
      e.preventDefault();
      const row = catalogItems()[catalogActive];
      const a = row ? CATALOG.find((x) => String(x.id) === String(row.dataset.id)) : null;
      if (a) {
        addToCart(a);
        els.catalogSearch.value = '';
        renderCatalog();
      } else if (!els.catalogSearch.value.trim() && els.payBtn && !els.payBtn.disabled) {
        pay();
      }
    } else if (e.key === 'Escape') {
      els.catalogSearch.value = '';
      renderCatalog();
    }
  });
}
if (els.payBtn) els.payBtn.addEventListener('click', () => pay());

initStudentSearch(els.search, els.results, (r) => {
  if (contributors.find((c) => c.id === r.id)) return;
  contributors.push(r);
  renderContributors();
  if (els.catalogSearch) els.catalogSearch.focus();
}, { campus: CAMPUS });

if (els.rgSearch) {
  let rgUser = null;
  initStudentSearch(els.rgSearch, els.rgResults, async (r) => {
    rgUser = r;
    const w = await apiFetch(`/api/wallet/${r.id}`);
    els.rgPanel.classList.remove('d-none');
    $('glasses-info').innerHTML =
      `<strong>${w.name}</strong> — verres consignés en cours : <span class="badge bg-secondary">${w.glasses}</span>`;
  }, { campus: CAMPUS });

  $('glasses-return-1').addEventListener('click', () => returnGlasses(1));
  $('glasses-return-all').addEventListener('click', () => {
    if (rgUser) returnGlasses(rgUser.glasses || 1);
  });

  async function returnGlasses(count) {
    if (!rgUser) return;
    try {
      const res = await apiFetch('/api/glasses/return', { json: { user_id: rgUser.id, count } });
      alert(`Retour enregistré : ${(res.total / 100).toFixed(2)} € crédités.`);
      window.location.reload();
    } catch (e) {
      alert(e.message);
    }
  }
}

renderContributors();
renderCatalog();
renderCart();
