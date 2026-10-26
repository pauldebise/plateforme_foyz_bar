/*!
 * Santa Deco — décoration de décembre
 * --------------------------------------------------------------------------
 * Père Noël 24×24 (3 frames) + flocons, en overlay canvas fixe.
 * Aucune dépendance. Les canvas sont en pointer-events:none : le site reste
 * entièrement cliquable. Le sprite est embarqué dans ce fichier.
 *
 * Intégration :
 *   <script>
 *     window.SantaDecoConfig = { ... };
 *   </script>
 *   <script src="santa-deco.js" defer></script>
 *
 * API :
 *   SantaDeco.init(options) / destroy() / setOptions(partial)
 *   SantaDeco.pause() / resume() / getOptions() / getMetrics()
 *
 * Le décor est automatiquement lancé au chargement, sauf si
 * window.SantaDecoConfig.autoStart === false.
 * --------------------------------------------------------------------------
 */
(function (global) {
  'use strict';

  if (!global || !global.document) return;
  if (global.SantaDeco && global.SantaDeco.version) return;

  const PALETTE = {
    '.': null,
    'O': '#1e1e2a',
    'R': '#e03131',
    'D': '#a81f24',
    'd': '#961e26',
    'W': '#ffffff',
    'w': '#c9d2e0',
    'v': '#e8eef8',
    'S': '#f6c89e',
    's': '#cd9164',
    'K': '#4a4a5c',
    'F': '#2c2c3a',
    'k': '#7a7a94',
    'N': '#17171d',
    'G': '#f7c948',
    'g': '#b8860b',
    'B': '#c99a5f',
    'H': '#e2ba84',
    'b': '#8a6134',
    'T': '#6b4a26',
  };

  const FRAMES = [
  [
    '.................OOOO...',
    '..............OOOOWWOO..',
    '............OOODRWWWwO..',
    '..........OOODRRRDWwOO..',
    '.........OODDDRRRRDRO...',
    '........OODDDRRRRRRROO..',
    '...OOOOOOwWWWWWWWWWWwO..',
    '...OTTTOOWWSSSSSSKSSOOO.',
    '..OObbbOOWWSSSSSSSSSSsO.',
    '.OOHBBbOOwWSSSSSSSWWWwO.',
    '.ObbbBbOOOWWWWWWWWvvvvO.',
    '.OHbbBbOwWWWWWWWWWWWWwO.',
    '.OBBBbbDDwWWWWWWWWWwDOO.',
    '.OObBBbDDRwWWWWWwRRRDO..',
    '..OObbONNNNGgGNNNRRRNOOO',
    '...OOOONNNNGNGNNNNNWWWwO',
    '......ODDRRRRRRRRRRRSsSO',
    '......OWWWWWWWWWWWWWWOOO',
    '......OORRRROOddddOOOO..',
    '.......ORRRROOddddO.....',
    '......OODRRROOddddO.....',
    '......OkKKKOOOFFFFOO....',
    '......OKKKKkOOFFFFFO....',
    '......OOOOOOOOOOOOOO....',
  ],
  [
    '.................OOOO...',
    '..............OOOOWWOO..',
    '............OOODRWWWwO..',
    '..........OOODRRRDWwOO..',
    '.........OODDDRRRRDRO...',
    '........OODDDRRRRRRROO..',
    '...OOOOOOwWWWWWWWWWWwO..',
    '...OTTTOOWWSSSSSSKSSOOO.',
    '..OObbbOOWWSSSSSSSSSSsO.',
    '.OOHBBbOOwWSSSSSSSWWWwO.',
    '.ObbbBbOOOWWWWWWWWvvvvO.',
    '.OHbbBbOwWWWWWWWWWWWWwO.',
    '.OBBBbbDDwWWWWWWWWWwDOO.',
    '.OObBBbDDRwWWWWWwRRRDO..',
    '..OObbONNNNGgGNNNRRRNO..',
    '...OOOONNNNGNGNNNWWWwO..',
    '......ODDRRRRRRRRRSsSO..',
    '......OWWWWWWWWWWWWWWO..',
    '......OOOdddRRRROOOOOO..',
    '........OdddRRRRO.......',
    '.......OOdddDRRRO.......',
    '.......OFFFFkKKKOO......',
    '.......OFFFFKKKKkO......',
    '.......OOOOOOOOOOO......',
  ],
  [
    '.................OOOO...',
    '..............OOOOWWOO..',
    '............OOODRWWWwO..',
    '..........OOODRRRDWwOO..',
    '.........OODDDRRRRDRO...',
    '........OODDDRRRRRRROO..',
    '...OOOOOOwWWWWWWWWWWwO..',
    '...OTTTOOWWSSSSSSKSSOOO.',
    '..OObbbOOWWSSSSSSSSSSsO.',
    '.OOHBBbOOwWSSSSSSSWWWwO.',
    '.ObbbBbOOOWWWWWWWWvvvvO.',
    '.OHbbBbOwWWWWWWWWWWWWwO.',
    '.OBBBbbDDwWWWWWWWWWwDOO.',
    '.OObBBbDDRwWWWWWwRRRDO..',
    '..OObbONNNNGgGNNNRRRNO..',
    '...OOOONNNNGNGNWWWwNNO..',
    '......ODDRRRRRRRSsSRDO..',
    '......OWWWWWWWWWWWWWWO..',
    '......OOddddOORRRROOOO..',
    '.......OddddOORRRRO.....',
    '......OOddddOODRRRO.....',
    '......OFFFFOOOkKKKOO....',
    '......OFFFFFOOKKKKkO....',
    '......OOOOOOOOOOOOOO....',
  ],
  ];

  const SPRITE_W = 24;
  const SPRITE_H = 24;
  const CYCLE = [0, 1, 2, 1];
  const TAU = Math.PI * 2;
  const VERSION = '1.0.0';

  // Flocons pixel art : motifs 7×7, dessinés en carrés de pixelSize px.
  const SNOW_PATTERNS = [
    [
      '...#...',
      '...#...',
      '.#.#.#.',
      '#######',
      '.#.#.#.',
      '...#...',
      '...#...',
    ],
    [
      '#..#..#',
      '.#.#.#.',
      '..###..',
      '#######',
      '..###..',
      '.#.#.#.',
      '#..#..#',
    ],
  ].map(function (rows) {
    const cells = [];
    const white = new Set();
    rows.forEach(function (row, y) {
      for (let x = 0; x < row.length; x++) {
        if (row.charAt(x) === '#') {
          cells.push([x, y]);
          white.add(x + ',' + y);
        }
      }
    });
    const shadow = [];
    cells.forEach(function (cell) {
      const x = cell[0] + 1;
      const y = cell[1] + 1;
      if (!white.has(x + ',' + y)) shadow.push([x, y]);
    });
    return { cells: cells, shadow: shadow };
  });

  const DEFAULTS = {
    enabled: true,
    zIndex: 9000,
    respectReducedMotion: true,
    pauseWhenHidden: true,
    santa: {
      enabled: true,
      scale: 3,          // 24 px × 3 = 72 px
      speed: 32,         // px/s
      bottom: 0,         // px entre les pieds et le bas du viewport
      margin: 12,        // marge de balade à gauche/droite
      edgePause: 0.65,   // pause aux extrémités, en secondes
      start: 0.08,       // position de départ (0 → 1)
      opacity: 1,
    },
    snow: {
      enabled: true,
      count: 28,         // flocons sur desktop
      mobileCount: 17,   // flocons sous mobileBreakpoint
      mobileBreakpoint: 640,
      pixelSize: 3,      // taille d'un pixel du flocon (7 × pixelSize px)
      speed: 16,         // px/s
      opacity: 0.8,
      wind: 4,           // amplitude de dérive horizontale
      contrast: true,    // petite ombre pixel pour rester visible sur fond clair
    },
  };

  let currentOptions = null;
  let state = null;

  const isNum = (v) => typeof v === 'number' && Number.isFinite(v);
  const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
  const clone = (v) => JSON.parse(JSON.stringify(v));
  const raf = (cb) => global.requestAnimationFrame(cb);
  const caf = (id) => global.cancelAnimationFrame(id);

  function merge(base, override) {
    const out = {};
    const keys = new Set(Object.keys(base || {}).concat(Object.keys(override || {})));
    keys.forEach((key) => {
      const b = base ? base[key] : undefined;
      const o = override ? override[key] : undefined;
      if (b && typeof b === 'object' && !Array.isArray(b)) {
        out[key] = merge(b, o && typeof o === 'object' && !Array.isArray(o) ? o : {});
      } else {
        out[key] = o === undefined ? b : o;
      }
    });
    return out;
  }

  function normalize(raw) {
    const o = merge(DEFAULTS, raw || {});
    const n = (v, fallback) => (isNum(v) ? v : fallback);

    o.enabled = o.enabled !== false;
    o.respectReducedMotion = o.respectReducedMotion !== false;
    o.pauseWhenHidden = o.pauseWhenHidden !== false;
    o.zIndex = Math.round(clamp(n(o.zIndex, 9000), 0, 2147483000));

    o.santa.enabled = o.santa.enabled !== false;
    o.santa.scale = clamp(n(o.santa.scale, 3), 1, 8);
    o.santa.speed = clamp(n(o.santa.speed, 32), 4, 160);
    o.santa.bottom = clamp(n(o.santa.bottom, 0), -48, 240);
    o.santa.margin = clamp(n(o.santa.margin, 12), 0, 240);
    o.santa.edgePause = clamp(n(o.santa.edgePause, 0.65), 0, 5);
    o.santa.start = clamp(n(o.santa.start, 0.08), 0, 1);
    o.santa.opacity = clamp(n(o.santa.opacity, 1), 0.1, 1);

    o.snow.enabled = o.snow.enabled !== false;
    o.snow.count = Math.round(clamp(n(o.snow.count, 28), 0, 60));
    o.snow.mobileCount = Math.round(clamp(n(o.snow.mobileCount, 17), 0, 60));
    o.snow.mobileBreakpoint = clamp(n(o.snow.mobileBreakpoint, 640), 0, 2400);
    o.snow.pixelSize = Math.round(clamp(n(o.snow.pixelSize, 3), 1, 4));
    o.snow.speed = clamp(n(o.snow.speed, 16), 3, 80);
    o.snow.opacity = clamp(n(o.snow.opacity, 0.8), 0.05, 1);
    o.snow.wind = clamp(n(o.snow.wind, 4), 0, 30);
    o.snow.contrast = o.snow.contrast !== false;

    return o;
  }

  function makeCanvas(className, z) {
    const canvas = global.document.createElement('canvas');
    canvas.className = className;
    canvas.setAttribute('aria-hidden', 'true');
    canvas.setAttribute('role', 'presentation');
    canvas.setAttribute('data-santa-deco', '1');

    const s = canvas.style;
    s.setProperty('position', 'fixed', 'important');
    s.setProperty('left', '0', 'important');
    s.setProperty('top', '0', 'important');
    s.setProperty('display', 'block', 'important');
    s.setProperty('border', '0', 'important');
    s.setProperty('margin', '0', 'important');
    s.setProperty('padding', '0', 'important');
    s.setProperty('background', 'transparent', 'important');
    s.setProperty('pointer-events', 'none', 'important');
    s.setProperty('user-select', 'none', 'important');
    s.setProperty('-webkit-user-select', 'none', 'important');
    s.setProperty('z-index', String(z), 'important');
    s.setProperty('max-width', 'none', 'important');
    s.setProperty('max-height', 'none', 'important');

    global.document.documentElement.appendChild(canvas);
    return canvas;
  }

  function drawSprite(ctx, frame) {
    for (let y = 0; y < SPRITE_H; y++) {
      const row = frame[y];
      for (let x = 0; x < SPRITE_W; x++) {
        const color = PALETTE[row[x]];
        if (!color) continue;
        ctx.fillStyle = color;
        ctx.fillRect(x, y, 1, 1);
      }
    }
  }

  function isReduced() {
    return !!(state && state.options.respectReducedMotion && state.media && state.media.matches);
  }

  function canAnimate() {
    if (!state) return false;
    const o = state.options;
    if (!o.enabled || state.userPaused || state.hidden) return false;
    if (o.respectReducedMotion && state.media && state.media.matches) return false;
    return !!(o.santa.enabled || o.snow.enabled);
  }

  function stopRaf(st) {
    if (!st) return;
    if (st.raf) caf(st.raf);
    st.raf = 0;
  }

  function stop() {
    if (state) stopRaf(state);
  }

  function start() {
    if (!state || state.raf || !canAnimate()) return;
    state.lastTs = 0;
    state.raf = raf(tick);
  }

  function setSantaFrame(index) {
    if (!state || !state.santaCanvas || state.frame === index) return;
    const ctx = state.santaCtx;
    ctx.clearRect(0, 0, SPRITE_W, SPRITE_H);
    drawSprite(ctx, FRAMES[index] || FRAMES[0]);
    state.frame = index;
  }

  function placeSanta() {
    if (!state || !state.santaCanvas) return;
    const x = Math.round(state.x);
    const y = Math.round(state.y);
    state.santaCanvas.style.setProperty(
      'transform',
      'translate3d(' + x + 'px, ' + y + 'px, 0) scaleX(' + state.dir + ')',
      'important'
    );
  }

  function buildFlakes(rebuild) {
    if (!state) return;
    if (!state.snowCanvas) {
      state.flakes = [];
      return;
    }

    const snow = state.options.snow;
    const count = state.width <= snow.mobileBreakpoint ? snow.mobileCount : snow.count;

    if (!rebuild && state.flakes.length === count) {
      for (const f of state.flakes) {
        f.vy = snow.speed * f.speedFactor;
        f.alpha = snow.opacity * f.alphaFactor;
      }
      return;
    }

    state.flakes = [];
    for (let i = 0; i < count; i++) {
      const speedFactor = 0.65 + Math.random() * 0.7;
      const alphaFactor = 0.6 + Math.random() * 0.4;
      state.flakes.push({
        x: Math.random() * state.width,
        y: Math.random() * state.height,
        vy: snow.speed * speedFactor,
        alpha: snow.opacity * alphaFactor,
        speedFactor: speedFactor,
        alphaFactor: alphaFactor,
        phase: Math.random() * TAU,
        drift: 0.6 + Math.random() * 0.8,
        pattern: Math.floor(Math.random() * SNOW_PATTERNS.length),
      });
    }
  }

  function layout(rebuildSnow) {
    if (!state) return;
    const o = state.options;
    const width = Math.max(1, global.innerWidth || global.document.documentElement.clientWidth || 1);
    const height = Math.max(1, global.innerHeight || global.document.documentElement.clientHeight || 1);
    const dpr = Math.min(2, Math.max(1, global.devicePixelRatio || 1));

    state.width = width;
    state.height = height;
    state.dpr = dpr;

    if (state.snowCanvas) {
      state.snowCanvas.width = Math.round(width * dpr);
      state.snowCanvas.height = Math.round(height * dpr);
      state.snowCanvas.style.setProperty('width', width + 'px', 'important');
      state.snowCanvas.style.setProperty('height', height + 'px', 'important');
      state.snowCtx.setTransform(dpr, 0, 0, dpr, 0, 0);
    }

    state.size = Math.max(24, Math.round(SPRITE_W * o.santa.scale));

    if (state.santaCanvas) {
      state.santaCanvas.style.setProperty('width', state.size + 'px', 'important');
      state.santaCanvas.style.setProperty('height', state.size + 'px', 'important');
      state.santaCanvas.style.setProperty('opacity', String(o.santa.opacity), 'important');
    }

    const minX = Math.min(o.santa.margin, Math.max(0, width - state.size));
    const maxX = Math.max(minX, width - state.size - o.santa.margin);

    if (state.x === null) {
      state.x = minX + (maxX - minX) * o.santa.start;
    } else {
      state.x = clamp(state.x, minX, maxX);
    }

    state.y = height - state.size - o.santa.bottom;
    buildFlakes(!!rebuildSnow);
    placeSanta();
  }

  function updateFrame() {
    const o = state.options.santa;
    const duration = clamp(5.44 / o.speed, 0.10, 0.26);
    const index = CYCLE[Math.floor(state.walkTime / duration) % CYCLE.length];
    setSantaFrame(index);
  }

  function edgeStop() {
    const o = state.options.santa;
    state.edgeTimer = o.edgePause;

    if (state.edgeTimer > 0) {
      state.walkTime = 0;
      setSantaFrame(1);
    } else {
      state.dir *= -1;
      state.walkTime = 0;
      setSantaFrame(0);
    }
  }

  function updateSanta(dt) {
    if (!state || !state.santaCanvas) return;
    const o = state.options.santa;

    if (state.edgeTimer > 0) {
      state.edgeTimer -= dt;
      if (state.edgeTimer <= 0) {
        state.edgeTimer = 0;
        state.dir *= -1;
        state.walkTime = 0;
        setSantaFrame(0);
      }
      return;
    }

    const minX = Math.min(o.margin, Math.max(0, state.width - state.size));
    const maxX = Math.max(minX, state.width - state.size - o.margin);

    state.x += state.dir * o.speed * dt;

    if (state.x <= minX) {
      state.x = minX;
      edgeStop();
    } else if (state.x >= maxX) {
      state.x = maxX;
      edgeStop();
    } else {
      state.walkTime += dt;
      updateFrame();
    }
  }

  function updateSnow(dt) {
    const snow = state.options.snow;
    const patternSize = 7 * snow.pixelSize;

    for (const f of state.flakes) {
      f.y += f.vy * dt;
      f.x += Math.sin(state.time * 0.55 + f.phase) * snow.wind * f.drift * dt;

      if (f.y > state.height + 8) {
        f.y = -patternSize - Math.random() * 40;
        f.x = Math.random() * state.width;
      }
      if (f.x < -patternSize - 4) {
        f.x = state.width + 4;
      } else if (f.x > state.width + 4) {
        f.x = -patternSize - 4;
      }
    }
  }

  function update(dt) {
    state.time += dt;
    if (state.options.santa.enabled) updateSanta(dt);
    if (state.options.snow.enabled) updateSnow(dt);
  }

  function drawSnow() {
    if (!state || !state.snowCanvas) return;
    const ctx = state.snowCtx;
    const snow = state.options.snow;
    const unit = snow.pixelSize;

    ctx.clearRect(0, 0, state.width, state.height);

    for (const f of state.flakes) {
      const alpha = clamp(
        f.alpha * (0.9 + 0.1 * Math.sin(state.time * 0.9 + f.phase)),
        0, 1
      );
      const pattern = SNOW_PATTERNS[f.pattern] || SNOW_PATTERNS[0];
      const sx = Math.round(f.x);
      const sy = Math.round(f.y);

      if (snow.contrast) {
        ctx.fillStyle = 'rgba(70, 100, 132, ' + (alpha * 0.45) + ')';
        for (const cell of pattern.shadow) {
          ctx.fillRect(sx + cell[0] * unit, sy + cell[1] * unit, unit, unit);
        }
      }

      ctx.fillStyle = 'rgba(255, 255, 255, ' + alpha + ')';
      for (const cell of pattern.cells) {
        ctx.fillRect(sx + cell[0] * unit, sy + cell[1] * unit, unit, unit);
      }
    }
  }

  function draw() {
    if (!state) return;
    if (state.snowCanvas) drawSnow();
    if (state.santaCanvas) placeSanta();
  }

  function tick(now) {
    if (!state) return;
    const st = state;
    st.raf = raf(tick);

    if (!st.lastTs) {
      st.lastTs = now;
      return;
    }

    const elapsed = now - st.lastTs;
    if (elapsed < 1000 / 30) return;

    st.lastTs = now;
    const raw = elapsed / 1000;
    const dt = Number.isFinite(raw) ? clamp(raw, 0, 0.05) : 0;

    update(dt);
    draw();
  }

  function recenterStatic() {
    if (!state || !state.santaCanvas) return;
    const o = state.options.santa;
    const minX = Math.min(o.margin, Math.max(0, state.width - state.size));
    const maxX = Math.max(minX, state.width - state.size - o.margin);

    state.x = clamp(state.width / 2 - state.size / 2, minX, maxX);
    state.y = state.height - state.size - o.bottom;
    state.dir = 1;
    state.edgeTimer = 0;
    state.walkTime = 0;
    setSantaFrame(1);
    placeSanta();
  }

  function createState() {
    const o = currentOptions;
    const st = {
      options: o,
      width: 0,
      height: 0,
      dpr: 1,
      size: Math.max(24, Math.round(SPRITE_W * o.santa.scale)),
      x: null,
      y: 0,
      dir: 1,
      edgeTimer: 0,
      walkTime: 0,
      frame: -1,
      time: 0,
      flakes: [],
      raf: 0,
      lastTs: 0,
      resizeRaf: 0,
      userPaused: false,
      hidden: !!global.document.hidden,
      media: global.matchMedia ? global.matchMedia('(prefers-reduced-motion: reduce)') : null,
      mediaListenerType: null,
      snowCanvas: null,
      snowCtx: null,
      santaCanvas: null,
      santaCtx: null,
    };

    state = st;

    if (o.snow.enabled) {
      const c = makeCanvas('santa-deco-snow', o.zIndex);
      const ctx = c.getContext('2d');
      if (ctx) {
        st.snowCanvas = c;
        st.snowCtx = ctx;
        c.style.setProperty('width', '100%', 'important');
        c.style.setProperty('height', '100%', 'important');
      } else {
        c.remove();
      }
    }

    if (o.santa.enabled) {
      const c = makeCanvas('santa-deco-santa', o.zIndex + 1);
      const ctx = c.getContext('2d');
      if (ctx) {
        c.width = SPRITE_W;
        c.height = SPRITE_H;
        ctx.imageSmoothingEnabled = false;
        c.style.setProperty('image-rendering', 'pixelated', 'important');
        c.style.setProperty('transform-origin', '50% 50%', 'important');
        c.style.setProperty('will-change', 'transform', 'important');
        c.style.setProperty('opacity', String(o.santa.opacity), 'important');
        st.santaCanvas = c;
        st.santaCtx = ctx;
      } else {
        c.remove();
      }
    }

    st.onResize = function () {
      if (!state || state !== st) return;
      if (st.resizeRaf) return;
      st.resizeRaf = raf(function () {
        if (!state || state !== st) return;
        st.resizeRaf = 0;
        layout(true);
        if (isReduced()) recenterStatic();
        draw();
      });
    };

    st.onVisibility = function () {
      if (!state || state !== st) return;
      st.hidden = !!global.document.hidden;
      if (st.hidden) {
        if (st.options.pauseWhenHidden) stop();
      } else if (canAnimate()) {
        start();
      }
    };

    st.onReduced = function () {
      if (!state || state !== st) return;
      if (isReduced()) {
        stop();
        recenterStatic();
        draw();
      } else if (canAnimate()) {
        start();
      }
    };

    global.addEventListener('resize', st.onResize, { passive: true });
    global.document.addEventListener('visibilitychange', st.onVisibility);

    if (st.media) {
      if (st.media.addEventListener) {
        st.media.addEventListener('change', st.onReduced);
        st.mediaListenerType = 'addEventListener';
      } else if (st.media.addListener) {
        st.media.addListener(st.onReduced);
        st.mediaListenerType = 'addListener';
      }
    }

    return st;
  }

  function init(options) {
    if (state) destroy();

    const base = global.SantaDecoConfig || {};
    currentOptions = normalize(merge(merge(DEFAULTS, base), options || {}));

    if (!currentOptions.enabled) return;
    if (!currentOptions.santa.enabled && !currentOptions.snow.enabled) return;

    createState();
    layout(true);

    if (isReduced()) {
      recenterStatic();
      draw();
    } else {
      setSantaFrame(0);
      draw();
      start();
    }
  }

  function destroy() {
    if (!state) return;
    const st = state;
    state = null;

    stopRaf(st);
    if (st.resizeRaf) caf(st.resizeRaf);

    global.removeEventListener('resize', st.onResize);
    global.document.removeEventListener('visibilitychange', st.onVisibility);

    if (st.media && st.mediaListenerType) {
      if (st.mediaListenerType === 'addEventListener') {
        st.media.removeEventListener('change', st.onReduced);
      } else if (st.media.removeListener) {
        st.media.removeListener(st.onReduced);
      }
    }

    if (st.snowCanvas && st.snowCanvas.parentNode) {
      st.snowCanvas.parentNode.removeChild(st.snowCanvas);
    }
    if (st.santaCanvas && st.santaCanvas.parentNode) {
      st.santaCanvas.parentNode.removeChild(st.santaCanvas);
    }
  }

  function setOptions(partial) {
    const next = normalize(merge(currentOptions || DEFAULTS, partial || {}));
    const structural =
      next.enabled !== (currentOptions && currentOptions.enabled) ||
      next.santa.enabled !== (currentOptions && currentOptions.santa.enabled) ||
      next.snow.enabled !== (currentOptions && currentOptions.snow.enabled);
    const reducedChanged = next.respectReducedMotion !== (currentOptions && currentOptions.respectReducedMotion);

    currentOptions = next;

    if (!next.enabled || (!next.santa.enabled && !next.snow.enabled)) {
      destroy();
      return;
    }

    if (structural || !state) {
      init(next);
      return;
    }

    state.options = next;

    if (state.snowCanvas) {
      state.snowCanvas.style.setProperty('z-index', String(next.zIndex), 'important');
    }
    if (state.santaCanvas) {
      state.santaCanvas.style.setProperty('z-index', String(next.zIndex + 1), 'important');
      state.santaCanvas.style.setProperty('opacity', String(next.santa.opacity), 'important');
    }

    layout(false);

    if (reducedChanged) {
      if (isReduced()) {
        stop();
        recenterStatic();
      } else if (canAnimate()) {
        start();
      }
    }

    if (isReduced()) recenterStatic();
    draw();
    if (canAnimate() && !state.raf) start();
  }

  function pause() {
    if (!state) return;
    state.userPaused = true;
    stop();
  }

  function resume() {
    if (!state) return;
    state.userPaused = false;
    if (canAnimate()) start();
    else draw();
  }

  function getOptions() {
    return clone(currentOptions || normalize(DEFAULTS));
  }

  function getMetrics() {
    if (!state) {
      return {
        width: global.innerWidth || 0,
        height: global.innerHeight || 0,
        santa: null,
        snow: null,
        paused: false,
        reduced: false,
        running: false,
      };
    }

    const o = state.options;
    return {
      width: state.width,
      height: state.height,
      santa: {
        enabled: o.santa.enabled,
        size: state.size,
        bottom: o.santa.bottom,
        top: state.height - state.size - o.santa.bottom,
        band: state.size + Math.max(0, o.santa.bottom),
        x: state.x,
        y: state.y,
        direction: state.dir,
        scale: o.santa.scale,
      },
      snow: {
        enabled: o.snow.enabled,
        count: state.flakes.length,
        mobile: state.width <= o.snow.mobileBreakpoint,
      },
      paused: state.userPaused,
      reduced: isReduced(),
      running: !!state.raf,
    };
  }

  global.SantaDeco = {
    version: VERSION,
    defaults: clone(DEFAULTS),
    init: init,
    destroy: destroy,
    setOptions: setOptions,
    pause: pause,
    resume: resume,
    getOptions: getOptions,
    getMetrics: getMetrics,
  };

  const boot = function () {
    const cfg = global.SantaDecoConfig || {};
    if (cfg.autoStart === false) return;
    init(cfg);
  };

  if (global.document.readyState === 'loading') {
    global.document.addEventListener('DOMContentLoaded', boot, { once: true });
  } else {
    boot();
  }
})(typeof window !== 'undefined' ? window : undefined);
