document.querySelectorAll('.op-search').forEach((input) => {
  const results = input.parentElement.querySelector('.op-results');
  const hidden = document.getElementById(input.dataset.target);
  const info = input.parentElement.querySelector('.op-info');
  // Une nouvelle saisie invalide la sélection précédente : le champ caché et
  // le récapitulatif sont vidés tant qu'un résultat n'a pas été choisi.
  input.addEventListener('input', () => {
    hidden.value = '';
    info.replaceChildren();
  });
  initStudentSearch(input, results, async (r) => {
    hidden.value = r.id;
    const w = await apiFetch(`/api/wallet/${r.id}`);
    const badges = [];
    if (w.blacklist) badges.push(['badge-blacklist', 'blacklist']);
    if (w.blacklist_alcohol) badges.push(['badge-alcool', 'blacklist alcool']);
    info.replaceChildren();
    info.appendChild(makeEl('strong', '', w.name));
    info.appendChild(document.createTextNode(' — solde : '));
    info.appendChild(makeEl('strong', '', `${(w.balance / 100).toFixed(2)} €`));
    info.appendChild(document.createTextNode(` · verres consignés : ${w.glasses} `));
    appendBadges(info, badges);
  }, { keepValue: true });
});
