const DATA = JSON.parse(document.getElementById('stats-data').textContent);
const STATS = DATA.stats;
const CAT_LABELS = DATA.labels;
const STUDENTS = DATA.students;

new Chart(document.getElementById('chart-categories'), {
  type: 'bar',
  data: {
    labels: Object.keys(STATS.by_category).map((k) => CAT_LABELS[k] || k),
    datasets: [
      { label: 'Volume', data: Object.values(STATS.by_category).map((v) => v.qty), backgroundColor: 'rgba(0,82,156,.7)', yAxisID: 'y' },
      { label: 'Recettes (€)', data: Object.values(STATS.by_category).map((v) => v.revenue / 100), type: 'line', yAxisID: 'y1', tension: .3 }
    ]
  },
  options: { scales: { y: { beginAtZero: true }, y1: { position: 'right', beginAtZero: true, grid: { drawOnChartArea: false } } } }
});

new Chart(document.getElementById('chart-trend'), {
  type: 'line',
  data: {
    labels: Object.keys(STATS.series),
    datasets: [{ label: 'Recettes par jour (€)', data: Object.values(STATS.series).map((v) => v / 100), tension: .3, fill: true, backgroundColor: 'rgba(0,82,156,.12)', borderColor: 'rgba(0,82,156,.9)' }]
  },
  options: { scales: { y: { beginAtZero: true } } }
});

if (STUDENTS.length) {
  new Chart(document.getElementById('chart-students'), {
    type: 'bar',
    data: {
      labels: STUDENTS.map((s) => s.name),
      datasets: [{ label: 'Total dépensé (€)', data: STUDENTS.map((s) => s.spent / 100), backgroundColor: 'rgba(0,82,156,.75)', borderRadius: 4 }]
    },
    options: { indexAxis: 'y', plugins: { legend: { display: false } }, scales: { x: { beginAtZero: true } } }
  });
}
