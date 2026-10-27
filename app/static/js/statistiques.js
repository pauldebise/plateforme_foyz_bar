(() => {
  'use strict';

  const eur0 = new Intl.NumberFormat('fr-FR', {
    style: 'currency',
    currency: 'EUR',
    maximumFractionDigits: 0,
  });
  const eur2 = new Intl.NumberFormat('fr-FR', {
    style: 'currency',
    currency: 'EUR',
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
  const int = new Intl.NumberFormat('fr-FR');
  const dayLong = new Intl.DateTimeFormat('fr-FR', {
    weekday: 'short',
    day: 'numeric',
    month: 'long',
    year: 'numeric',
  });
  const monthShort = new Intl.DateTimeFormat('fr-FR', { month: 'short', year: 'numeric' });
  const monthLong = new Intl.DateTimeFormat('fr-FR', { month: 'long', year: 'numeric' });

  function parseDay(iso) {
    const [year, month, day] = iso.split('-').map(Number);
    return new Date(year, month - 1, day);
  }

  function parseMonth(iso) {
    const [year, month] = iso.split('-').map(Number);
    return new Date(year, month - 1, 1);
  }

  function rgba(color, alpha) {
    const value = String(color || '').trim().replace('#', '');
    if (value.length === 3) {
      return `rgba(${parseInt(value[0] + value[0], 16)}, ${parseInt(value[1] + value[1], 16)}, ${parseInt(value[2] + value[2], 16)}, ${alpha})`;
    }
    if (value.length === 6) {
      return `rgba(${parseInt(value.slice(0, 2), 16)}, ${parseInt(value.slice(2, 4), 16)}, ${parseInt(value.slice(4, 6), 16)}, ${alpha})`;
    }
    return `rgba(0, 82, 156, ${alpha})`;
  }

  function movingAverage(values, size = 7) {
    return values.map((_value, index) => {
      const start = Math.max(0, index - size + 1);
      const window = values.slice(start, index + 1);
      return window.reduce((total, value) => total + value, 0) / window.length;
    });
  }

  // Filtres : application immédiate, avec le bouton « Appliquer » en secours
  // lorsque JavaScript est indisponible.
  document.querySelectorAll('[data-stats-filter]').forEach((form) => {
    const preset = form.querySelector('[name="preset"]');
    const customDates = document.getElementById('stats-custom-dates');
    const dateInputs = customDates ? [...customDates.querySelectorAll('input')] : [];

    function setCustomDates(active) {
      if (!customDates) return;
      customDates.classList.toggle('d-none', !active);
      dateInputs.forEach((input) => {
        input.disabled = !active;
      });
    }

    setCustomDates(preset && preset.value === 'custom');
    form.addEventListener('submit', () => {
      form.querySelectorAll('select').forEach((select) => {
        if (select.name && !select.value) select.disabled = true;
      });
      document.body.classList.add('stats-loading');
    });
    form.querySelectorAll('[data-auto-submit]').forEach((control) => {
      control.addEventListener('change', () => {
        if (control.name === 'preset' && control.value === 'custom') {
          setCustomDates(true);
          document.getElementById('stats-more')?.classList.add('show');
          document
            .querySelector('[data-bs-target="#stats-more"]')
            ?.setAttribute('aria-expanded', 'true');
          customDates?.querySelector('input')?.focus();
          return;
        }
        if (control.name === 'preset') setCustomDates(false);
        if (typeof form.requestSubmit === 'function') form.requestSubmit();
        else form.submit();
      });
    });
  });

  document.querySelectorAll('[data-stats-search]').forEach((form) => {
    form.addEventListener('submit', () => {
      const search = form.querySelector('input[type="search"]');
      if (search && !search.value.trim()) search.disabled = true;
      document.body.classList.add('stats-loading');
    });
    form.querySelectorAll('[data-auto-submit]').forEach((control) => {
      control.addEventListener('change', () => {
        if (typeof form.requestSubmit === 'function') form.requestSubmit();
        else form.submit();
      });
    });
  });

  const dataElement = document.getElementById('stats-data');
  if (!dataElement || typeof window.Chart === 'undefined') return;

  const DATA = JSON.parse(dataElement.textContent);
  const dates = DATA.dates || [];
  if (!dates.length) return;
  const labels = DATA.labels || dates;
  const tooltips = DATA.tooltips || null;
  const daily = (DATA.granularity || 'jour') === 'jour';

  const css = getComputedStyle(document.documentElement);
  const primary = css.getPropertyValue('--primary').trim() || '#00529c';
  const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  Chart.defaults.font.family = getComputedStyle(document.body).fontFamily;
  Chart.defaults.color = '#6c757d';
  Chart.defaults.borderColor = '#eef0f2';
  if (reducedMotion) Chart.defaults.animation = false;

  const METRICS = {
    revenue: {
      label: 'Recettes',
      data: (DATA.revenue || []).map((value) => value / 100),
      currency: true,
    },
    qty: { label: 'Articles', data: DATA.qty || [], currency: false },
    transactions: { label: 'Transactions', data: DATA.transactions || [], currency: false },
  };
  const PREVIOUS = DATA.previous
    ? {
        dates: DATA.previous.dates || [],
        revenue: (DATA.previous.revenue || []).map((value) => value / 100),
        qty: DATA.previous.qty || [],
        transactions: DATA.previous.transactions || [],
      }
    : null;

  let currentMetric = 'revenue';
  let chart = null;
  let currentDataset = null;
  let averageDataset = null;
  let previousDataset = null;

  function formatValue(value) {
    return METRICS[currentMetric].currency ? eur2.format(value) : int.format(value);
  }

  function yTick(value) {
    return METRICS[currentMetric].currency ? eur0.format(value) : int.format(value);
  }

  function eventMarkersPlugin() {
    return {
      id: 'statsEventMarkers',
      afterDatasetsDraw(canvasChart) {
        if (!DATA.events || !DATA.events.length) return;
        const area = canvasChart.chartArea;
        const scale = canvasChart.scales.x;
        if (!area || !scale) return;
        const ctx = canvasChart.ctx;
        ctx.save();
        ctx.strokeStyle = 'rgba(180, 83, 9, .30)';
        ctx.setLineDash([3, 3]);
        DATA.events.forEach((event) => {
          const index = dates.indexOf(event.date);
          if (index < 0) return;
          const x = scale.getPixelForValue(index);
          if (x < area.left || x > area.right) return;
          ctx.beginPath();
          ctx.moveTo(x, area.top);
          ctx.lineTo(x, area.bottom);
          ctx.stroke();
        });
        ctx.restore();
      },
    };
  }

  const trendCanvas = document.getElementById('chart-trend');
  if (trendCanvas) {
    const datasets = [
      {
        label: METRICS.revenue.label,
        data: METRICS.revenue.data,
        borderColor: primary,
        backgroundColor: rgba(primary, 0.12),
        fill: true,
        tension: 0.25,
        borderWidth: 2,
        pointRadius: daily && dates.length <= 45 ? 2 : 0,
        pointHoverRadius: 4,
      },
    ];
    currentDataset = datasets[0];
    if (dates.length >= 7 && daily) {
      averageDataset = {
        label: 'Moyenne 7 jours',
        data: movingAverage(METRICS.revenue.data),
        borderColor: rgba(primary, 0.55),
        borderDash: [5, 4],
        borderWidth: 1.5,
        pointRadius: 0,
        fill: false,
      };
      datasets.push(averageDataset);
    }
    if (PREVIOUS) {
      previousDataset = {
        label: 'Période précédente',
        data: PREVIOUS.revenue,
        borderColor: '#b45309',
        borderDash: [4, 4],
        borderWidth: 1.5,
        pointRadius: 0,
        fill: false,
      };
      datasets.push(previousDataset);
    }

    chart = new Chart(trendCanvas, {
      type: 'line',
      data: { labels, datasets },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: 'index', intersect: false },
        plugins: {
          legend: {
            position: 'bottom',
            labels: { usePointStyle: true, boxWidth: 8, boxHeight: 8, padding: 16 },
          },
          tooltip: {
            callbacks: {
              title: (items) => {
                const item = items[0];
                if (item.dataset === previousDataset && PREVIOUS) {
                  return `Période précédente · ${dayLong.format(parseDay(PREVIOUS.dates[item.dataIndex]))}`;
                }
                if (tooltips && tooltips[item.dataIndex]) return tooltips[item.dataIndex];
                return dayLong.format(parseDay(dates[item.dataIndex]));
              },
              label: (item) => {
                let text = `${item.dataset.label} : ${formatValue(item.parsed.y)}`;
                if (item.dataset === currentDataset) {
                  if (currentMetric === 'revenue') {
                    const qty = DATA.qty[item.dataIndex];
                    const count = DATA.transactions[item.dataIndex];
                    text += ` · ${int.format(qty)} article${qty > 1 ? 's' : ''} · ${int.format(count)} transaction${count > 1 ? 's' : ''}`;
                  } else {
                    text += ` · ${eur2.format(DATA.revenue[item.dataIndex] / 100)} de recettes`;
                  }
                }
                return text;
              },
            },
          },
        },
        scales: {
          x: {
            grid: { display: false },
            ticks: {
              maxTicksLimit: window.innerWidth < 576 ? 5 : 10,
              maxRotation: 0,
              autoSkip: true,
              autoSkipPadding: 8,
              callback: (_value, index) =>
                index !== undefined && labels[index] ? labels[index] : '',
            },
          },
          y: {
            beginAtZero: true,
            ticks: { callback: yTick },
          },
        },
      },
      plugins: [eventMarkersPlugin()],
    });

    document.querySelectorAll('[data-metric]').forEach((button) => {
      button.addEventListener('click', () => {
        currentMetric = button.dataset.metric;
        const metric = METRICS[currentMetric];
        currentDataset.data = metric.data;
        currentDataset.label = metric.label;
        if (averageDataset) averageDataset.data = movingAverage(metric.data);
        if (previousDataset) previousDataset.data = PREVIOUS[currentMetric];
        chart.options.scales.y.ticks.callback = yTick;
        chart.update();
        document.querySelectorAll('[data-metric]').forEach((other) => {
          const active = other === button;
          other.classList.toggle('active', active);
          other.setAttribute('aria-pressed', active ? 'true' : 'false');
        });
      });
    });
  }

  const monthlyCanvas = document.getElementById('chart-monthly');
  if (
    monthlyCanvas &&
    DATA.monthly &&
    DATA.monthly.labels &&
    DATA.monthly.labels.length >= 2 &&
    DATA.monthly.series
  ) {
    const palette = [
      primary,
      '#e07a5f',
      '#3d9970',
      '#f2cc8f',
      '#6a4c93',
      '#17a2b8',
      '#8d6e63',
      '#adb5bd',
      '#d63384',
      '#fd7e14',
    ];
    new Chart(monthlyCanvas, {
      type: 'bar',
      data: {
        labels: DATA.monthly.labels,
        datasets: DATA.monthly.series.map((series, index) => ({
          label: series.label,
          data: series.qty,
          backgroundColor: palette[index % palette.length],
          stack: 'qty',
          borderRadius: 2,
        })),
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: 'index', intersect: false },
        plugins: {
          legend: {
            position: 'bottom',
            labels: { usePointStyle: true, boxWidth: 8, boxHeight: 8, padding: 12 },
          },
          tooltip: {
            callbacks: {
              title: (items) =>
                monthLong.format(parseMonth(DATA.monthly.labels[items[0].dataIndex])),
              label: (item) => {
                const series = DATA.monthly.series[item.datasetIndex];
                const revenue = series.revenue[item.dataIndex] / 100;
                const qty = item.parsed.y;
                return `${item.dataset.label} : ${int.format(qty)} article${qty > 1 ? 's' : ''} · ${eur2.format(revenue)}`;
              },
            },
          },
        },
        scales: {
          x: {
            stacked: true,
            grid: { display: false },
            ticks: {
              maxTicksLimit: window.innerWidth < 576 ? 4 : 12,
              maxRotation: 0,
              minRotation: 0,
              autoSkipPadding: 8,
              callback: (_value, index) =>
                index !== undefined && DATA.monthly.labels[index]
                  ? monthShort.format(parseMonth(DATA.monthly.labels[index]))
                  : '',
            },
          },
          y: {
            stacked: true,
            beginAtZero: true,
            ticks: { callback: (value) => int.format(value) },
          },
        },
      },
    });
  }
})();
