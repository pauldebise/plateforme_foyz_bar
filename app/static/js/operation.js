document.querySelectorAll('.op-search').forEach((input) => {
  const results = input.parentElement.querySelector('.op-results');
  const hidden = document.getElementById(input.dataset.target);
  const info = input.parentElement.querySelector('.op-info');
  initStudentSearch(input, results, async (r) => {
    hidden.value = r.id;
    const w = await apiFetch(`/api/wallet/${r.id}`);
    const flags = [];
    if (w.blacklist) flags.push('<span class="badge badge-blacklist">blacklist</span>');
    if (w.blacklist_alcohol) flags.push('<span class="badge badge-alcool">blacklist alcool</span>');
    info.innerHTML = `<strong>${w.name}</strong> — solde : <strong>${(w.balance / 100).toFixed(2)} €</strong> · verres consignés : ${w.glasses} ${flags.join(' ')}`;
  });
});
