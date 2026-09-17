const TREASURY = JSON.parse(document.getElementById('treasury-data').textContent);
const reloadsById = TREASURY.map((e) => e.reloads_total / 100);
const sales = TREASURY.map((e) => e.sales_total / 100);
const events = TREASURY.map((e) => e.events_total / 100);

new Chart(document.getElementById('chart-treasury'), {
  data: {
    labels: TREASURY.map((e) => e.label),
    datasets: [
      { type: 'bar', label: 'Rechargements (entrée réelle)', data: reloadsById, backgroundColor: 'rgba(0,82,156,.85)', stack: 'entrees' },
      { type: 'bar', label: 'Recettes événements (entrée réelle)', data: events, backgroundColor: 'rgba(255,193,7,.9)', stack: 'entrees' },
      { type: 'line', label: 'Ventes bar (consommation virtuelle)', data: sales, borderColor: 'rgba(25,135,84,.9)', backgroundColor: 'rgba(25,135,84,.12)', tension: .3, yAxisID: 'y2' }
    ]
  },
  options: {
    scales: {
      x: { stacked: true },
      y: { stacked: true, beginAtZero: true, ticks: { callback: (v) => v + ' €' } },
      y2: { position: 'right', beginAtZero: true, grid: { drawOnChartArea: false }, ticks: { callback: (v) => v + ' €' } }
    }
  }
});
