const TREASURY = JSON.parse(document.getElementById('treasury-data').textContent);
const euros = (cents) => cents / 100;

// Deux barres côte à côte par mois : rechargements (entrée d'argent) et ventes
// (sortie de monnaie virtuelle). Les ventes forment une seule barre empilée,
// ventes ordinaires puis évènements/soirées. L'option `stack` regroupe les
// séries : deux groupes = deux barres côte à côte.
new Chart(document.getElementById('chart-treasury'), {
  type: 'bar',
  data: {
    labels: TREASURY.map((e) => e.label),
    datasets: [
      {
        label: 'Rechargements',
        data: TREASURY.map((e) => euros(e.reloads_total)),
        backgroundColor: 'rgba(0,82,156,.85)',
        stack: 'rechargements',
      },
      {
        label: 'Ventes (hors évènements/soirées)',
        data: TREASURY.map((e) => euros(e.sales_other)),
        backgroundColor: 'rgba(25,135,84,.85)',
        stack: 'ventes',
      },
      {
        label: 'Ventes évènements/soirées',
        data: TREASURY.map((e) => euros(e.sales_events)),
        backgroundColor: 'rgba(255,193,7,.9)',
        stack: 'ventes',
      },
    ],
  },
  options: {
    scales: {
      x: { stacked: true },
      y: { stacked: true, beginAtZero: true, ticks: { callback: (v) => v + ' €' } },
    },
    plugins: {
      tooltip: {
        callbacks: {
          label: (ctx) => `${ctx.dataset.label} : ${ctx.parsed.y.toFixed(2)} €`,
          footer: (items) => {
            if (items[0].dataset.stack !== 'ventes') return '';
            const month = TREASURY[items[0].dataIndex];
            return `Total ventes : ${euros(month.sales_total).toFixed(2)} €`;
          },
        },
      },
    },
  },
});
