let contributors = [];
let cart = new Map();
const CATALOG = JSON.parse(document.getElementById('catalog-data').textContent);
const CONFIG = document.getElementById('payment-config');
const DEPOSIT_VALUE = parseInt(CONFIG.dataset.depositValue, 10);
const DEPOSIT_ENABLED = CONFIG.dataset.depositEnabled === '1';
const CAMPUS = CONFIG.dataset.campus || '';
const GATEWAY = CONFIG.dataset.gateway === '1';

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
  mobileBar: $('mobile-pay-bar'),
  depositSwitch: $('deposit-switch'),
  glasses: $('deposit-glasses'),
  directSwitch: $('direct-switch'),
  directMethods: $('direct-methods'),
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
    const info = document.createElement('div');
    const icon = makeEl('i', 'bi bi-person-circle');
    info.appendChild(icon);
    info.appendChild(document.createTextNode(' '));
    info.appendChild(makeEl('strong', '', c.name));
    info.appendChild(document.createTextNode(' '));
    info.appendChild(makeEl('span', 'badge bg-light text-dark', `${(c.balance / 100).toFixed(2)} €`));
    const badges = [];
    if (c.blacklist) badges.push(['badge-blacklist', 'blacklist']);
    if (c.blacklist_alcohol) badges.push(['badge-alcool', 'blacklist alcool']);
    if (badges.length) info.appendChild(document.createTextNode(' '));
    appendBadges(info, badges);
    const button = makeEl('button', 'btn btn-sm btn-outline-danger');
    button.title = 'Retirer';
    button.appendChild(makeEl('i', 'bi bi-x'));
    button.addEventListener('click', () => {
      contributors = contributors.filter((x) => x.id !== c.id);
      renderContributors();
      // Les tarifs (équipe ou standard) et donc le panier dépendent des
      // contributeurs : on recalcule catalogue et total (U2).
      renderCatalog();
      renderCart();
    });
    li.appendChild(info);
    li.appendChild(button);
    els.contributorList.appendChild(li);
  });
  if (!contributors.length) {
    els.contributorList.innerHTML = '<li class="list-group-item text-muted px-2">Aucun étudiant sélectionné.</li>';
  }
  updatePayButton();
}

function payButtons() {
  return Array.from(document.querySelectorAll('[data-pay]'));
}

function updatePayButton() {
  const disabled = cart.size === 0 || (!contributors.length && !isDirect());
  payButtons().forEach((b) => { b.disabled = disabled; });
}

function syncTotals() {
  document.querySelectorAll('[data-total]').forEach((el) => {
    el.textContent = (grandTotal() / 100).toFixed(2) + ' €';
  });
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
  const header = makeEl('div', 'card-header py-1 small fw-bold');
  if (icon) header.innerHTML = icon;
  header.appendChild(document.createTextNode(`${icon ? ' ' : ''}${label}`));
  card.appendChild(header);
  const body = makeEl('div', 'card-body p-0');
  card.appendChild(body);
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
  const label = makeEl('span', 'tap-label');
  label.appendChild(document.createTextNode(`${group.label} `));
  if (group.sizes.some((s) => s.article.alcohol)) {
    const warning = makeEl('i', 'bi bi-exclamation-diamond text-warning');
    warning.title = 'Alcoolisé';
    label.appendChild(warning);
  }
  const sizes = makeEl('div', 'tap-sizes');
  group.sizes.forEach((s) => {
    const btn = makeEl('button', 'btn btn-sm btn-primary tap-btn');
    btn.appendChild(makeEl('span', 'tap-size', s.size));
    const price = makeEl('span', 'tap-price', `${(unitPrice(s.article) / 100).toFixed(2)} € `);
    price.appendChild(makeEl('i', 'bi bi-plus-lg'));
    btn.appendChild(price);
    btn.addEventListener('click', () => addToCart(s.article));
    sizes.appendChild(btn);
  });
  row.appendChild(label);
  row.appendChild(sizes);
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
  if (GATEWAY) {
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
    els.catalog.appendChild(catalogCard(TYPE_LABELS[type] || type, '', '', std));
  });
  clearCatalogActive();
}

function catalogRow(a) {
  const row = document.createElement('div');
  row.className = 'cat-item';
  row.dataset.id = a.id;
  const info = makeEl('div');
  info.appendChild(makeEl('span', '', a.name));
  if (a.alcohol) {
    const warning = makeEl('i', 'bi bi-exclamation-diamond text-warning');
    warning.title = 'Alcoolisé';
    info.appendChild(document.createTextNode(' '));
    info.appendChild(warning);
  }
  if (a.volume) {
    info.appendChild(document.createTextNode(' '));
    info.appendChild(makeEl('span', 'text-muted small', `${a.volume} cl`));
  }
  const actions = makeEl('div', 'd-flex align-items-center gap-2');
  const price = makeEl('span', 'badge bg-light text-dark', `${(unitPrice(a) / 100).toFixed(2)} €`);
  price.dataset.price = '';
  const button = makeEl('button', 'btn btn-sm btn-primary qty-btn');
  button.appendChild(makeEl('i', 'bi bi-plus-lg'));
  button.addEventListener('click', () => addToCart(a));
  actions.appendChild(price);
  actions.appendChild(button);
  row.appendChild(info);
  row.appendChild(actions);
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
    tr.appendChild(makeEl('td', '', a.name));
    const unitCell = makeEl('td', 'text-end', `${(unit / 100).toFixed(2)} €`);
    unitCell.dataset.unit = '';
    tr.appendChild(unitCell);
    const qtyCell = makeEl('td', 'text-center');
    const group = makeEl('div', 'btn-group btn-group-sm');
    const dec = makeEl('button', 'btn btn-outline-secondary qty-btn');
    dec.dataset.dec = '';
    dec.appendChild(makeEl('i', 'bi bi-dash-lg'));
    const qtySpan = makeEl('span', 'btn btn-light disabled', `${qty}`);
    qtySpan.dataset.qty = '';
    qtySpan.style.minWidth = '44px';
    const inc = makeEl('button', 'btn btn-outline-secondary qty-btn');
    inc.dataset.inc = '';
    inc.appendChild(makeEl('i', 'bi bi-plus-lg'));
    group.appendChild(dec);
    group.appendChild(qtySpan);
    group.appendChild(inc);
    qtyCell.appendChild(group);
    tr.appendChild(qtyCell);
    const lineCell = makeEl('td', 'text-end fw-bold', `${((unit * qty) / 100).toFixed(2)} €`);
    lineCell.dataset.line = '';
    tr.appendChild(lineCell);
    const delCell = makeEl('td', 'text-end');
    const del = makeEl('button', 'btn btn-sm btn-outline-danger');
    del.dataset.del = '';
    del.appendChild(makeEl('i', 'bi bi-trash'));
    delCell.appendChild(del);
    tr.appendChild(delCell);
    inc.addEventListener('click', () => { cart.set(id, qty + 1); renderCart(); });
    dec.addEventListener('click', () => {
      qty > 1 ? cart.set(id, qty - 1) : cart.delete(id);
      renderCart();
    });
    del.addEventListener('click', () => { cart.delete(id); renderCart(); });
    els.cartBody.appendChild(tr);
  });
  const depositRow = document.getElementById('deposit-row');
  if (depositRow) depositRow.remove();
  if (glassesCount() > 0) {
    const drow = els.cartBody.insertRow();
    drow.id = 'deposit-row';
    drow.appendChild(makeEl('td', '', 'Consigne (verres empruntés)'));
    drow.appendChild(makeEl('td', 'text-end', `${(DEPOSIT_VALUE / 100).toFixed(2)} €`));
    drow.appendChild(makeEl('td', 'text-center', `${glassesCount()}`));
    drow.appendChild(makeEl('td', 'text-end fw-bold', `${((glassesCount() * DEPOSIT_VALUE) / 100).toFixed(2)} €`));
    drow.appendChild(makeEl('td'));
  }
  syncTotals();
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
let orderKey = null;
let orderSignature = null;

function fallbackOrderKey() {
  return 'k-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 12);
}

// Jeton d'idempotence stable tant que le panier ne change pas : un rejeu
// (double clic, retentative réseau) renvoie la transaction déjà créée sans
// débiter une seconde fois.
function orderKeyFor(payload) {
  const signature = JSON.stringify([
    payload.items,
    payload.deposit_glasses,
    payload.direct,
    payload.payment_method || null,
    payload.contributors || null,
  ]);
  if (orderKey === null || signature !== orderSignature) {
    orderKey = (window.crypto && window.crypto.randomUUID)
      ? window.crypto.randomUUID()
      : fallbackOrderKey();
    orderSignature = signature;
  }
  return orderKey;
}

async function pay(adminPassword) {
  if (paying) return;
  paying = true;
  const payload = buildPayload(adminPassword);
  try {
    payload.idempotency_key = orderKeyFor(payload);
    const res = await apiFetch(GATEWAY ? location.pathname + '/encaisser' : '/api/purchase', { json: payload });
    showSuccess(res.total);
  } catch (e) {
    // Échec réseau (fetch rejette) : la vente est mise en file locale et sera
    // rejouée avec la même clé d'idempotence — le serveur ne débitera qu'une fois.
    if (e instanceof TypeError && window.FoyzOffline && window.FoyzOffline.queueSale) {
      payload.idempotency_key = payload.idempotency_key || orderKeyFor(payload);
      window.FoyzOffline.queueSale(
        GATEWAY ? location.pathname + '/encaisser' : '/api/purchase',
        payload
      );
      resetCartState();
      toast('Hors ligne : vente enregistrée sur ce poste, transmission au retour du réseau.');
      return;
    }
    if (e.code === 'admin_password_required' && els.adminModal) {
      $('admin-negative-users').textContent = (e.extra.negative_users || []).join(', ');
      els.adminModal.show();
    } else {
      toast(e.message);
    }
  } finally {
    paying = false;
  }
}

function resetCartState() {
  cart = new Map();
  contributors = [];
  if (els.depositSwitch) els.depositSwitch.checked = false;
  if (els.glasses) els.glasses.value = 1;
  if (els.directSwitch) els.directSwitch.checked = false;
  if (els.contributorsCard) els.contributorsCard.classList.remove('d-none');
  const depositCard = $('deposit-card');
  const directCard = $('direct-card');
  if (depositCard) depositCard.classList.remove('d-none');
  if (directCard) directCard.classList.add('d-none');
  orderKey = null;
  orderSignature = null;
  renderContributors();
  renderCart();
  renderCatalog();
}

function showSuccess(total) {
  if (!els.successModal) return;
  $('success-total').textContent = (total / 100).toFixed(2) + ' €';
  // Pas de rechargement : l'état (panier, contributeurs, défilement) est déjà
  // remis à zéro, la page reste en place (U4).
  resetCartState();
  els.successModal.show();
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

document.querySelectorAll('[data-deposit-step]').forEach((button) => {
  button.addEventListener('click', () => {
    const step = parseInt(button.dataset.depositStep, 10) || 0;
    const next = Math.max(1, parseInt(els.glasses.value || '1', 10) + step);
    els.glasses.value = next;
    renderCart();
  });
});

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
      } else if (!els.catalogSearch.value.trim() && payButtons().some((b) => !b.disabled)) {
        pay();
      }
    } else if (e.key === 'Escape') {
      els.catalogSearch.value = '';
      renderCatalog();
    }
  });
}
payButtons().forEach((button) => button.addEventListener('click', () => pay()));
if (els.mobileBar) document.body.classList.add('has-mobile-pay-bar');

initStudentSearch(els.search, els.results, (r) => {
  if (contributors.find((c) => c.id === r.id)) return;
  contributors.push(r);
  renderContributors();
  // Les tarifs dépendent des contributeurs : catalogue et panier recalculés (U2).
  renderCatalog();
  renderCart();
  if (els.catalogSearch) els.catalogSearch.focus();
}, { campus: CAMPUS });

if (els.rgSearch) {
  let rgUser = null;
  initStudentSearch(els.rgSearch, els.rgResults, async (r) => {
    rgUser = r;
    const w = await apiFetch(`/api/wallet/${r.id}`);
    els.rgPanel.classList.remove('d-none');
    const info = $('glasses-info');
    info.replaceChildren();
    info.appendChild(makeEl('strong', '', w.name));
    info.appendChild(document.createTextNode(' — verres consignés en cours : '));
    info.appendChild(makeEl('span', 'badge bg-secondary', `${w.glasses}`));
  }, { campus: CAMPUS });

  $('glasses-return-1').addEventListener('click', () => returnGlasses(1));
  $('glasses-return-all').addEventListener('click', () => {
    if (rgUser) returnGlasses(rgUser.glasses || 1);
  });

  async function returnGlasses(count) {
    if (!rgUser) return;
    try {
      const res = await apiFetch('/api/glasses/return', { json: { user_id: rgUser.id, count } });
      toast(`Retour enregistré : ${(res.total / 100).toFixed(2)} € crédités.`, 'success');
      // Mise à jour sur place : pas de rechargement de page (U4).
      rgUser.glasses = Math.max(0, (rgUser.glasses || 0) - count);
      if (rgUser.glasses > 0) {
        const info = $('glasses-info');
        info.replaceChildren();
        info.appendChild(makeEl('strong', '', rgUser.name));
        info.appendChild(document.createTextNode(' — verres consignés en cours : '));
        info.appendChild(makeEl('span', 'badge bg-secondary', `${rgUser.glasses}`));
      } else {
        els.rgPanel.classList.add('d-none');
        els.rgSearch.value = '';
        rgUser = null;
      }
    } catch (e) {
      toast(e.message);
    }
  }
}

renderContributors();
renderCatalog();
renderCart();
