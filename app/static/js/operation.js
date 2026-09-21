// Rechargement/retrait : un seul compte à la fois, présenté comme les
// contributeurs du paiement et les comptes du transfert (même carte de liste).
document.querySelectorAll('.op-search').forEach((input) => {
  const results = input.parentElement.querySelector('.op-results');
  const hidden = document.getElementById(input.dataset.target);
  const list = document.getElementById(input.dataset.list);
  let selected = null;

  function render() {
    list.replaceChildren();
    if (!selected) {
      list.appendChild(
        makeEl('li', 'list-group-item text-muted px-2', 'Aucun étudiant sélectionné.'),
      );
      return;
    }
    const li = makeEl('li', 'list-group-item d-flex justify-content-between align-items-center px-2');
    const info = document.createElement('div');
    info.appendChild(makeEl('i', 'bi bi-person-circle'));
    info.appendChild(document.createTextNode(' '));
    info.appendChild(makeEl('strong', '', selected.name));
    info.appendChild(document.createTextNode(' '));
    info.appendChild(makeEl('span', 'badge bg-light text-dark', `${(selected.balance / 100).toFixed(2)} €`));
    const badges = [];
    if (selected.blacklist) badges.push(['badge-blacklist', 'blacklist']);
    if (selected.blacklist_alcohol) badges.push(['badge-alcool', 'blacklist alcool']);
    if (badges.length) info.appendChild(document.createTextNode(' '));
    appendBadges(info, badges);
    const remove = makeEl('button', 'btn btn-sm btn-outline-danger');
    remove.type = 'button';
    remove.title = 'Retirer';
    remove.appendChild(makeEl('i', 'bi bi-x'));
    remove.addEventListener('click', () => {
      selected = null;
      hidden.value = '';
      input.value = '';
      render();
    });
    li.appendChild(info);
    li.appendChild(remove);
    list.appendChild(li);
  }

  // Une nouvelle saisie invalide la sélection précédente : le champ caché et
  // la carte sont vidés tant qu'un résultat n'a pas été choisi.
  input.addEventListener('input', () => {
    selected = null;
    hidden.value = '';
    render();
  });
  initStudentSearch(input, results, async (r) => {
    selected = await apiFetch(`/api/wallet/${r.id}`);
    hidden.value = r.id;
    render();
  }, { keepValue: true });
  render();
});

// Transfert multi-comptes : on peut choisir plusieurs donneurs et plusieurs
// receveurs, matérialisés par un champ caché par personne sélectionnée.
function initTransferMultiSelect({ searchId, resultsId, inputsId, listId }) {
  const input = document.getElementById(searchId);
  const results = document.getElementById(resultsId);
  const inputs = document.getElementById(inputsId);
  const list = document.getElementById(listId);
  const fieldName = inputs.dataset.name;
  const selected = [];

  function render() {
    inputs.replaceChildren();
    list.replaceChildren();
    selected.forEach((u) => {
      const li = makeEl('li', 'list-group-item d-flex justify-content-between align-items-center px-2');
      const info = document.createElement('div');
      info.appendChild(makeEl('i', 'bi bi-person-circle'));
      info.appendChild(document.createTextNode(' '));
      info.appendChild(makeEl('strong', '', u.name));
      info.appendChild(document.createTextNode(' '));
      info.appendChild(makeEl('span', 'badge bg-light text-dark', `${(u.balance / 100).toFixed(2)} €`));
      const badges = [];
      if (u.blacklist) badges.push(['badge-blacklist', 'blacklist']);
      if (u.blacklist_alcohol) badges.push(['badge-alcool', 'blacklist alcool']);
      if (badges.length) info.appendChild(document.createTextNode(' '));
      appendBadges(info, badges);
      const remove = makeEl('button', 'btn btn-sm btn-outline-danger');
      remove.type = 'button';
      remove.title = 'Retirer';
      remove.appendChild(makeEl('i', 'bi bi-x'));
      remove.addEventListener('click', () => {
        const index = selected.findIndex((x) => x.id === u.id);
        if (index >= 0) selected.splice(index, 1);
        render();
      });
      li.appendChild(info);
      li.appendChild(remove);
      list.appendChild(li);

      const hidden = document.createElement('input');
      hidden.type = 'hidden';
      hidden.name = fieldName;
      hidden.value = u.id;
      inputs.appendChild(hidden);
    });
    if (!selected.length) {
      list.appendChild(makeEl('li', 'list-group-item text-muted px-2', 'Aucun compte sélectionné.'));
    }
  }

  initStudentSearch(input, results, async (r) => {
    if (selected.some((u) => u.id === r.id)) return;
    const w = await apiFetch(`/api/wallet/${r.id}`);
    selected.push(w);
    render();
    input.focus();
  });

  render();
}

if (document.getElementById('from-inputs')) {
  initTransferMultiSelect({
    searchId: 'from-search',
    resultsId: 'from-results',
    inputsId: 'from-inputs',
    listId: 'from-list',
  });
  initTransferMultiSelect({
    searchId: 'to-search',
    resultsId: 'to-results',
    inputsId: 'to-inputs',
    listId: 'to-list',
  });
}
