/* Redact IACR — a daily redaction puzzle over CRYPTO / EUROCRYPT / TCC papers.
 *
 * The real paper is rendered with pdf.js and blacked out by laying rectangles
 * over it, so what you read is the published typesetting — equations, figures
 * and all — rather than a reconstruction of it. The build ships the PDF plus
 * the position of every redactable word and formula in PDF points; the client
 * only has to scale those by its render scale.
 *
 * No text layer is rendered, deliberately: one would put the answer in the DOM
 * for Ctrl+F to find.
 */

import * as pdfjsLib from './vendor/pdf.min.mjs';

pdfjsLib.GlobalWorkerOptions.workerSrc = 'vendor/pdf.worker.min.mjs';

const STORE_PREFIX = 'redact-iacr:v1:';
const ZOOM_STEPS = [0.75, 1, 1.25, 1.5, 2, 3];

/* Words handed to the player for free. Function words carry almost no signal
 * and blacking them out only makes the page unreadable. */
const FREE_WORDS = new Set(`
a an the this that these those it its it's is are was were be been being am
of in on at to for with by from into onto over under above below between
and or but nor so yet if then than as because while when where which who whom
whose what how why not no nor all any both each few more most other some such
we our us they them their he she his her you your i my me one two do does did
can could may might must shall should will would have has had there here also
however thus hence moreover furthermore therefore i.e e.g et al ie eg
`.trim().split(/\s+/));

const WORD_RE = /[0-9a-zà-ɏ]+/giu;

const state = {
  date: null,
  puzzle: null,
  pdf: null,
  views: [],             // one per page: { wrap, canvas, overlay, rendered, boxes }
  keyLookup: new Map(),  // guess word -> key id
  free: [],              // key id -> is it given away for free
  revealed: new Set(),   // key ids uncovered so far
  guesses: [],
  guessed: new Set(),
  won: false,
  gaveUp: false,
  startedAt: null,
  endedAt: null,
  sort: 'recent',
  focus: { word: null, index: 0 },
  zoom: 1,
  scale: 1,
  totalBoxes: 0,
  revealedBoxes: 0,
  // How much the player had uncovered when the game ended. Finishing reveals
  // the rest of the paper, which must not be credited to their score.
  finalRevealed: null,
  renderToken: 0,
};

const $ = (id) => document.getElementById(id);
const todayUTC = () => new Date().toISOString().slice(0, 10);
const isFree = (word) => word.length < 2 || FREE_WORDS.has(word.toLowerCase());

function normalise(text) {
  const tokens = String(text).toLowerCase().match(WORD_RE);
  return tokens && tokens.length ? tokens[0] : null;
}

/* ------------------------------------------------------------- persistence */

function loadDay(date) {
  try { return JSON.parse(localStorage.getItem(STORE_PREFIX + date)) || null; }
  catch { return null; }
}

function saveDay() {
  try {
    localStorage.setItem(STORE_PREFIX + state.date, JSON.stringify({
      guesses: state.guesses, won: state.won, gaveUp: state.gaveUp,
      startedAt: state.startedAt, endedAt: state.endedAt,
    }));
  } catch { /* private browsing: play on without persistence */ }
}

function loadStats() {
  const empty = { played: 0, won: 0, streak: 0, maxStreak: 0, history: [] };
  try { return JSON.parse(localStorage.getItem(STORE_PREFIX + 'stats')) || empty; }
  catch { return empty; }
}

function recordResult() {
  const stats = loadStats();
  if (stats.history.some((entry) => entry.date === state.date)) return;

  const previous = stats.history[stats.history.length - 1];
  const consecutive = previous && dayGap(previous.date, state.date) === 1;
  stats.played += 1;
  if (state.won) {
    stats.won += 1;
    stats.streak = consecutive ? stats.streak + 1 : 1;
    stats.maxStreak = Math.max(stats.maxStreak, stats.streak);
  } else {
    stats.streak = 0;
  }
  const total = state.guesses.length;
  const hits = state.guesses.filter((g) => g.hits > 0).length;
  const uncovered = state.guesses.reduce((sum, g) => sum + g.hits, 0);
  stats.history.push({
    date: state.date,
    won: state.won,
    guesses: total,
    hits,
    accuracy: total ? Math.round((hits / total) * 100) : 0,
    // Share of the paper the player uncovered before the game ended.
    revealed: state.totalBoxes
      ? Math.round(((state.finalRevealed ?? 0) / state.totalBoxes) * 1000) / 10
      : 0,
    uncovered,
    seconds: state.startedAt && state.endedAt
      ? Math.round((state.endedAt - state.startedAt) / 1000)
      : null,
    bestGuess: state.guesses.reduce(
      (best, g) => (g.hits > (best?.hits ?? 0) ? g : best), null,
    )?.word ?? null,
    venue: state.puzzle.venue,
    year: state.puzzle.year,
    id: state.puzzle.id,
    title: state.puzzle.titleText,
    pages: state.puzzle.stats.pages,
    words: state.puzzle.stats.words,
  });
  stats.history = stats.history.slice(-200);
  try { localStorage.setItem(STORE_PREFIX + 'stats', JSON.stringify(stats)); } catch { /* ignore */ }
}

function dayGap(from, to) {
  return Math.round((Date.parse(to + 'T00:00:00Z') - Date.parse(from + 'T00:00:00Z')) / 86400000);
}

/* ------------------------------------------------------------- the viewer */

function boxHidden(box, isMath) {
  if (isMath) return !box[4].some((key) => state.revealed.has(key));
  return !state.free[box[4]] && !state.revealed.has(box[4]);
}

function viewerWidth() {
  const viewer = $('viewer');
  // clientWidth already excludes the padding we lay the pages out inside.
  return Math.max(240, viewer.clientWidth - 2);
}

function computeScale() {
  const widest = Math.max(...state.puzzle.pages.map((page) => page.w));
  return (viewerWidth() / widest) * state.zoom;
}

function buildViewer() {
  const viewer = $('viewer');
  viewer.textContent = '';
  state.views = [];
  state.scale = computeScale();

  state.puzzle.pages.forEach((page, index) => {
    const wrap = document.createElement('div');
    wrap.className = 'page';
    wrap.style.width = `${Math.round(page.w * state.scale)}px`;
    wrap.style.height = `${Math.round(page.h * state.scale)}px`;

    const canvas = document.createElement('canvas');
    const overlay = document.createElement('div');
    overlay.className = 'redactions';
    wrap.append(canvas, overlay);
    viewer.appendChild(wrap);

    state.views.push({ wrap, canvas, overlay, index, rendered: false, boxes: [] });
  });

  // Render pages as they come into view; a 50-page paper is far too much to
  // rasterise up front.
  const observer = new IntersectionObserver((entries) => {
    for (const entry of entries) {
      if (entry.isIntersecting) renderPage(state.views[Number(entry.target.dataset.page)]);
    }
  }, { rootMargin: '400px 0px' });

  state.views.forEach((view, index) => {
    view.wrap.dataset.page = String(index);
    observer.observe(view.wrap);
  });
  state.observer = observer;
}

async function renderPage(view) {
  if (view.rendered || view.busy) return;
  // Never rasterise a page whose redactions we do not have.
  if (!state.puzzle.pages[view.index]) return;
  view.busy = true;
  const token = state.renderToken;

  try {
    const page = await state.pdf.getPage(view.index + 1);
    if (token !== state.renderToken) { view.busy = false; return; }

    const viewport = page.getViewport({ scale: state.scale });
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    view.canvas.width = Math.floor(viewport.width * dpr);
    view.canvas.height = Math.floor(viewport.height * dpr);
    view.canvas.style.width = `${Math.floor(viewport.width)}px`;
    view.canvas.style.height = `${Math.floor(viewport.height)}px`;

    // Cover the page before a single pixel of it is drawn. pdf.js paints
    // progressively into a canvas that is already in the document, so boxes
    // added after the render let the paper be read while it draws.
    paintBoxes(view);

    await page.render({
      canvasContext: view.canvas.getContext('2d', { alpha: false }),
      viewport,
      transform: dpr === 1 ? null : [dpr, 0, 0, dpr, 0, 0],
    }).promise;

    if (token !== state.renderToken) { view.busy = false; return; }
    view.rendered = true;
    // Only now is it safe to show: redactions are up and the bitmap matches
    // the current scale.
    view.wrap.classList.add('ready');
  } catch (error) {
    if (error?.name !== 'RenderingCancelledException') console.error(error);
  }
  view.busy = false;
}

function paintBoxes(view) {
  const page = state.puzzle.pages[view.index];
  const scale = state.scale;
  view.overlay.textContent = '';
  view.boxes = [];

  const place = (box, isMath) => {
    if (!boxHidden(box, isMath)) return;
    const el = document.createElement('i');
    if (isMath) el.className = 'math';
    el.style.left = `${box[0] * scale}px`;
    el.style.top = `${box[1] * scale}px`;
    el.style.width = `${box[2] * scale}px`;
    el.style.height = `${box[3] * scale}px`;
    view.overlay.appendChild(el);
    view.boxes.push({ el, box, isMath });
  };

  for (const box of page.words) place(box, false);
  for (const box of page.math) place(box, true);
}

function clearRevealedBoxes({ flash = false } = {}) {
  for (const view of state.views) {
    if (!view.rendered) continue;
    const keep = [];
    for (const entry of view.boxes) {
      if (boxHidden(entry.box, entry.isMath)) { keep.push(entry); continue; }
      if (flash) flashAt(view, entry);
      entry.el.remove();
    }
    view.boxes = keep;
  }
}

function flashAt(view, entry) {
  const mark = document.createElement('i');
  mark.className = 'flash';
  mark.style.cssText = entry.el.style.cssText;
  view.overlay.appendChild(mark);
  setTimeout(() => mark.remove(), 1200);
}

function rescale() {
  state.renderToken += 1;
  const scale = computeScale();
  if (Math.abs(scale - state.scale) < 0.001) return;
  state.scale = scale;

  for (const view of state.views) {
    const page = state.puzzle.pages[view.index];
    view.wrap.style.width = `${Math.round(page.w * scale)}px`;
    view.wrap.style.height = `${Math.round(page.h * scale)}px`;
    view.rendered = false;
    // Hide again: the bitmap still holds the old scale, so leaving it up
    // would show text drifting out from under its rectangles.
    view.wrap.classList.remove('ready');
    view.overlay.textContent = '';
    view.boxes = [];
  }
  for (const view of state.views) {
    const rect = view.wrap.getBoundingClientRect();
    if (rect.bottom > -400 && rect.top < innerHeight + 400) renderPage(view);
  }
}

/* ------------------------------------------------------------------ guess */

/* How many still-hidden boxes this key would uncover. */
function countHits(key) {
  let hits = 0;
  for (const page of state.puzzle.pages) {
    // Words handed over for free were never covered, so uncovering them is
    // not a hit — counting them would inflate both accuracy and progress.
    if (!state.free[key]) {
      for (const box of page.words) if (box[4] === key) hits += 1;
    }
    for (const box of page.math) {
      // A formula already uncovered by one of its other identifiers must not
      // be counted a second time.
      if (box[4].includes(key) && !box[4].some((k) => k !== key && state.revealed.has(k))) hits += 1;
    }
  }
  return hits;
}

function applyGuess(word, { flash = true } = {}) {
  const key = state.keyLookup.get(word);
  if (key === undefined || state.revealed.has(key)) return 0;

  const hits = countHits(key);
  state.revealed.add(key);
  state.revealedBoxes += hits;
  clearRevealedBoxes({ flash });
  return hits;
}

function submitGuess(raw) {
  const word = normalise(raw);
  if (!word || state.won || state.gaveUp) return;

  if (state.guessed.has(word)) {
    focusOn(word);
    flashInput();
    return;
  }

  // A word shown for free is already on the page, so it is not a guess at
  // all: recording it would either flatter accuracy or punish it unfairly.
  const known = state.keyLookup.get(word);
  if (isFree(word) && (known === undefined || countHits(known) === 0)) {
    flashInput();
    return;
  }

  // The clock starts at the first guess, so leaving a tab open does not count.
  state.startedAt ??= Date.now();
  state.guessed.add(word);
  const hits = applyGuess(word);
  state.guesses.push({ word, hits });
  state.focus = { word: hits ? word : null, index: 0 };

  checkWin();
  saveDay();
  renderSidebar();
  renderCounters();
}

function checkWin() {
  const remaining = state.puzzle.titleWords.filter(
    (word) => !isFree(word) && !state.guessed.has(word),
  );
  if (remaining.length) return;
  state.won = true;
  finish();
}

async function giveUp() {
  if (state.won || state.gaveUp) return;
  const confirmed = await askConfirm({
    kicker: 'Give up?',
    text: 'This reveals the whole paper and ends today’s game. It cannot be undone.',
    confirmLabel: 'Reveal the paper',
  });
  if (!confirmed) return;
  state.gaveUp = true;
  finish();
}

function finish({ announce = true } = {}) {
  if (state.finalRevealed === null) state.finalRevealed = state.revealedBoxes;

  state.endedAt ??= Date.now();

  for (let key = 0; key < state.puzzle.keys.length; key += 1) state.revealed.add(key);
  state.revealedBoxes = state.totalBoxes;
  // No flash here: uncovering the whole paper at once would wash the page.
  clearRevealedBoxes({ flash: false });

  $('guess-input').disabled = true;
  $('guess-submit').disabled = true;
  const giveUpButton = $('btn-giveup');
  giveUpButton.textContent = 'Show result';
  giveUpButton.classList.add('done');

  recordResult();
  saveDay();
  renderCounters();
  if (announce) showResult();
}

/* ------------------------------------------------------- focus navigation */

function focusOn(word) {
  const key = state.keyLookup.get(word);
  if (key === undefined) return;

  const spots = [];
  state.puzzle.pages.forEach((page, index) => {
    for (const box of page.words) if (box[4] === key) spots.push({ index, box });
    for (const box of page.math) if (box[4].includes(key)) spots.push({ index, box });
  });
  if (!spots.length) return;

  if (state.focus.word !== word) state.focus = { word, index: 0 };
  else state.focus.index = (state.focus.index + 1) % spots.length;

  // The sheet covers the page it is about to jump to.
  if (sheetLayout()) setSheet(false);

  const spot = spots[state.focus.index % spots.length];
  const view = state.views[spot.index];
  const top = view.wrap.offsetTop + spot.box[1] * state.scale;
  scrollTo({ top: top - innerHeight / 2, behavior: 'smooth' });

  document.querySelectorAll('.spotlight').forEach((el) => el.remove());
  const mark = document.createElement('i');
  mark.className = 'spotlight';
  mark.style.left = `${spot.box[0] * state.scale}px`;
  mark.style.top = `${spot.box[1] * state.scale}px`;
  mark.style.width = `${spot.box[2] * state.scale}px`;
  mark.style.height = `${spot.box[3] * state.scale}px`;
  view.overlay.appendChild(mark);
  setTimeout(() => mark.remove(), 2000);
  renderSidebar();
}

/* ------------------------------------------------------------ guess sheet */

const sheetLayout = () => matchMedia('(max-width: 860px)').matches;

function setSheet(open) {
  $('sidebar').classList.toggle('open', open);
  $('sheet-backdrop').hidden = !open;
  $('btn-guesses').setAttribute('aria-expanded', String(open));
}

function toggleSheet() {
  setSheet(!$('sidebar').classList.contains('open'));
}

function flashInput() {
  const input = $('guess-input');
  input.style.borderColor = 'var(--hit)';
  setTimeout(() => { input.style.borderColor = ''; }, 400);
}

/* ---------------------------------------------------------------- chrome  */

function renderCounters() {
  const total = state.guesses.length;
  const hits = state.guesses.filter((g) => g.hits > 0).length;
  $('stat-guesses').textContent = total;
  $('guesses-count').textContent = total;
  $('stat-hits').textContent = hits;
  $('stat-accuracy').textContent = total ? `${Math.round((hits / total) * 100)}%` : '—';
  // "Revealed" sits beside guesses, hits and accuracy: it is a score, so it
  // stops at whatever the player had uncovered themselves.
  const revealed = state.finalRevealed ?? state.revealedBoxes;
  const pct = state.totalBoxes ? (revealed / state.totalBoxes) * 100 : 0;
  $('stat-revealed').textContent = `${pct > 0 && pct < 10 ? pct.toFixed(1) : Math.round(pct)}%`;
}

function renderSidebar() {
  const list = $('guess-list');
  list.textContent = '';

  const items = [...state.guesses];
  if (state.sort === 'hits') items.sort((a, b) => b.hits - a.hits || a.word.localeCompare(b.word));
  else if (state.sort === 'alpha') items.sort((a, b) => a.word.localeCompare(b.word));
  else items.reverse();

  for (const item of items) {
    const li = document.createElement('li');
    if (!item.hits) li.classList.add('zero');
    if (state.focus.word === item.word) li.classList.add('active');

    const word = document.createElement('span');
    word.className = 'word';
    word.textContent = item.word;
    const count = document.createElement('span');
    count.className = 'n';
    count.textContent = item.hits;

    li.append(word, count);
    if (item.hits) li.addEventListener('click', () => focusOn(item.word));
    list.appendChild(li);
  }
}

function shareText() {
  const total = state.guesses.length;
  const hits = state.guesses.filter((g) => g.hits > 0).length;
  const accuracy = total ? Math.round((hits / total) * 100) : 0;
  const outcome = state.won ? `solved in ${total} guesses` : `gave up after ${total} guesses`;
  return [
    `Redact IACR — ${state.date}`,
    `${state.puzzle.venue} ${state.puzzle.year} · ${outcome} · ${accuracy}% accuracy`,
    location.origin + location.pathname,
  ].join('\n');
}

async function copyToClipboard(text) {
  try {
    if (navigator.clipboard?.writeText) { await navigator.clipboard.writeText(text); return true; }
  } catch { /* no permission, or an insecure origin: fall through */ }
  try {
    const area = document.createElement('textarea');
    area.value = text;
    area.setAttribute('readonly', '');
    area.style.cssText = 'position:fixed;opacity:0;pointer-events:none';
    $('dlg-result').appendChild(area);
    area.select();
    const copied = document.execCommand('copy');
    area.remove();
    return copied;
  } catch { return false; }
}

function showResult() {
  const puzzle = state.puzzle;
  const dialog = $('dlg-result');
  const total = state.guesses.length;
  const hits = state.guesses.filter((g) => g.hits > 0).length;

  dialog.classList.toggle('lost', !state.won);
  $('result-kicker').textContent = state.won
    ? `Solved in ${total} ${total === 1 ? 'guess' : 'guesses'}`
    : 'Paper revealed';
  $('result-title').textContent = puzzle.titleText;
  $('result-authors').textContent = puzzle.authorsText.join(', ');

  const tags = $('result-tags');
  tags.textContent = '';
  for (const [text, className] of [
    [puzzle.venue, 'venue'],
    [String(puzzle.year), ''],
    [`${puzzle.stats.pages} pages`, ''],
    [`${puzzle.stats.words.toLocaleString()} words`, ''],
  ]) {
    const tag = document.createElement('span');
    tag.className = className;
    tag.textContent = text;
    tags.appendChild(tag);
  }

  const stats = $('result-stats');
  stats.textContent = '';
  for (const [label, value] of [
    ['guesses', total],
    ['hits', hits],
    ['accuracy', total ? `${Math.round((hits / total) * 100)}%` : '—'],
    ['revealed', $('stat-revealed').textContent],
  ]) {
    const tile = document.createElement('div');
    const number = document.createElement('span');
    number.textContent = value;
    const caption = document.createElement('label');
    caption.textContent = label;
    tile.append(number, caption);
    stats.appendChild(tile);
  }

  const streak = loadStats().streak;
  $('result-note').textContent = state.won && streak > 1
    ? `${streak}-day streak — next paper at 00:00 UTC.`
    : 'Next paper at 00:00 UTC.';
  $('result-link').href = puzzle.url;
  $('result-copy').textContent = 'Copy result';
  if (!dialog.open) dialog.showModal();
}

function formatDuration(seconds) {
  if (seconds === null || seconds === undefined) return '—';
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ${String(seconds % 60).padStart(2, '0')}s`;
  return `${Math.floor(minutes / 60)}h ${String(minutes % 60).padStart(2, '0')}m`;
}

function average(values) {
  const usable = values.filter((v) => typeof v === 'number' && !Number.isNaN(v));
  if (!usable.length) return null;
  return usable.reduce((a, b) => a + b, 0) / usable.length;
}

function showStats() {
  const stats = loadStats();
  const history = stats.history || [];
  const wins = history.filter((entry) => entry.won);

  const fastest = wins.reduce(
    (best, entry) => (entry.guesses < (best?.guesses ?? Infinity) ? entry : best), null,
  );
  const avgAccuracy = average(history.map((entry) => entry.accuracy));
  const avgGuesses = average(history.map((entry) => entry.guesses));
  const avgTime = average(history.map((entry) => entry.seconds));

  const body = $('stats-body');
  body.textContent = '';
  for (const [label, value] of [
    ['played', stats.played],
    ['solved', stats.won],
    ['win rate', stats.played ? `${Math.round((stats.won / stats.played) * 100)}%` : '—'],
    ['streak', stats.streak],
    ['best streak', stats.maxStreak],
    ['avg guesses', avgGuesses === null ? '—' : Math.round(avgGuesses)],
    ['avg accuracy', avgAccuracy === null ? '—' : `${Math.round(avgAccuracy)}%`],
    ['fewest guesses', fastest ? fastest.guesses : '—'],
    ['avg time', formatDuration(avgTime === null ? null : Math.round(avgTime))],
    ['total guesses', history.reduce((sum, entry) => sum + (entry.guesses || 0), 0)],
  ]) {
    const cell = document.createElement('div');
    const number = document.createElement('span');
    number.textContent = value;
    const caption = document.createElement('label');
    caption.textContent = label;
    cell.append(number, caption);
    body.appendChild(cell);
  }

  renderHistoryTable(history);
  $('dlg-stats').showModal();
}

function renderHistoryTable(history) {
  const container = $('stats-history');
  container.textContent = '';

  if (!history.length) {
    const empty = document.createElement('p');
    empty.className = 'history-empty';
    empty.textContent = 'No games played yet.';
    container.appendChild(empty);
    return;
  }

  const table = document.createElement('table');
  table.className = 'history-table';

  const head = document.createElement('thead');
  const headRow = document.createElement('tr');
  for (const [label, className] of [
    ['Date', ''], ['Paper', ''], ['Result', ''], ['Guesses', 'num'],
    ['Hits', 'num'], ['Acc', 'num'], ['Revealed', 'num'], ['Time', 'num'],
  ]) {
    const th = document.createElement('th');
    th.textContent = label;
    if (className) th.className = className;
    headRow.appendChild(th);
  }
  head.appendChild(headRow);

  const bodyEl = document.createElement('tbody');
  for (const entry of [...history].reverse()) {
    const row = document.createElement('tr');
    if (!entry.won) row.className = 'lost';

    const cells = [
      [entry.date, ''],
      [`${entry.venue || '?'} ${entry.year || ''}`.trim(), 'paper'],
      [entry.won ? 'solved' : 'gave up', 'result'],
      [entry.guesses ?? '—', 'num'],
      [entry.hits ?? '—', 'num'],
      [entry.accuracy === undefined ? '—' : `${entry.accuracy}%`, 'num'],
      [entry.revealed === undefined ? '—' : `${entry.revealed}%`, 'num'],
      [formatDuration(entry.seconds), 'num'],
    ];
    for (const [text, className] of cells) {
      const td = document.createElement('td');
      td.textContent = String(text);
      if (className) td.className = className;
      // The full title is the reward for finishing; keep it on hover so the
      // table stays narrow.
      if (className === 'paper' && entry.title) td.title = entry.title;
      row.appendChild(td);
    }
    bodyEl.appendChild(row);
  }

  table.append(head, bodyEl);
  container.appendChild(table);
}

/* An in-page replacement for confirm(), which browsers render in their own
 * chrome and stylesheets cannot reach. */
function askConfirm({ kicker, text, confirmLabel }) {
  return new Promise((resolve) => {
    const dialog = $('dlg-confirm');
    const yes = $('confirm-yes');
    const no = $('confirm-no');

    $('confirm-kicker').textContent = kicker;
    $('confirm-text').textContent = text;
    yes.textContent = confirmLabel;

    let answer = false;
    const accept = () => { answer = true; dialog.close(); };
    const decline = () => dialog.close();
    yes.addEventListener('click', accept);
    no.addEventListener('click', decline);
    dialog.addEventListener('close', () => {
      yes.removeEventListener('click', accept);
      no.removeEventListener('click', decline);
      resolve(answer);
    }, { once: true });

    dialog.showModal();
    no.focus();
  });
}

function trackHeadHeight() {
  const head = $('sticky-head');
  const publish = () => {
    document.documentElement.style.setProperty('--head-h', `${head.offsetHeight}px`);
  };
  publish();
  if (typeof ResizeObserver === 'function') new ResizeObserver(publish).observe(head);
  else addEventListener('resize', publish);
}

function fail(message) {
  const viewer = $('viewer');
  viewer.textContent = '';
  const el = document.createElement('div');
  el.className = 'error';
  el.textContent = message;
  viewer.appendChild(el);
  $('puzzle-label').textContent = 'unavailable';
}

function updateZoomButtons() {
  const at = ZOOM_STEPS.indexOf(state.zoom);
  $('zoom-out').disabled = at <= 0;
  $('zoom-in').disabled = at >= ZOOM_STEPS.length - 1;
  $('zoom-label').textContent = `${Math.round(state.zoom * 100)}%`;
}

function setZoom(direction) {
  const current = ZOOM_STEPS.indexOf(state.zoom);
  const next = Math.min(ZOOM_STEPS.length - 1, Math.max(0, (current < 0 ? 1 : current) + direction));
  if (ZOOM_STEPS[next] === state.zoom) return;
  state.zoom = ZOOM_STEPS[next];
  updateZoomButtons();
  rescale();
}

/* ------------------------------------------------------------------- boot */

async function boot() {
  trackHeadHeight();
  $('btn-help').addEventListener('click', () => $('dlg-help').showModal());
  $('btn-stats').addEventListener('click', showStats);
  $('btn-giveup').addEventListener('click', () => {
    if (state.won || state.gaveUp) showResult(); else giveUp();
  });
  $('zoom-in').addEventListener('click', () => setZoom(1));
  $('zoom-out').addEventListener('click', () => setZoom(-1));
  updateZoomButtons();
  $('btn-guesses').addEventListener('click', toggleSheet);
  $('sheet-backdrop').addEventListener('click', () => setSheet(false));
  addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && $('sidebar').classList.contains('open')) setSheet(false);
  });
  $('result-close').addEventListener('click', () => $('dlg-result').close());
  $('result-copy').addEventListener('click', async (event) => {
    const copied = await copyToClipboard(shareText());
    event.target.textContent = copied ? 'Copied to clipboard' : 'Copy failed';
  });
  $('guess-form').addEventListener('submit', (event) => {
    event.preventDefault();
    submitGuess($('guess-input').value);
    $('guess-input').value = '';
  });
  document.querySelectorAll('.sort-toggle button').forEach((button) => {
    button.addEventListener('click', () => {
      document.querySelectorAll('.sort-toggle button').forEach((b) => b.classList.remove('active'));
      button.classList.add('active');
      state.sort = button.dataset.sort;
      renderSidebar();
    });
  });

  let debounce;
  addEventListener('resize', () => {
    if (!sheetLayout()) setSheet(false);   // no stale sheet state on desktop
    clearTimeout(debounce);
    debounce = setTimeout(rescale, 200);
  });

  let index;
  try {
    index = await (await fetch('puzzles/index.json', { cache: 'no-cache' })).json();
  } catch {
    return fail('Could not load the puzzle index. Run the build script first.');
  }

  const today = todayUTC();
  state.date = today;
  if (!index.dates.includes(today)) {
    return fail(index.end < today
      ? `The schedule ran out on ${index.end}. Rebuild with: python -m build.main --days 100`
      : `No puzzle for ${today}. The schedule starts on ${index.start}.`);
  }

  let puzzle;
  try {
    puzzle = await (await fetch(`puzzles/${today}.json`, { cache: 'no-cache' })).json();
  } catch (error) {
    return fail(`Could not load today's puzzle: ${error.message || error.name}`);
  }
  state.puzzle = puzzle;

  state.free = puzzle.keys.map(isFree);
  puzzle.keys.forEach((key, id) => state.keyLookup.set(key, id));
  state.totalBoxes = puzzle.pages.reduce(
    (sum, page) => sum + page.words.filter((b) => !state.free[b[4]]).length + page.math.length,
    0,
  );

  try {
    state.pdf = await pdfjsLib.getDocument({ url: `puzzles/${puzzle.pdf}` }).promise;
  } catch (error) {
    return fail(`Could not load the paper: ${error.message || error.name}`);
  }

  buildViewer();

  // Counted from the schedule's epoch, since only a window of days is served.
  const dayNumber = index.epoch
    ? dayGap(index.epoch, today) + 1
    : index.dates.indexOf(today) + 1;
  $('puzzle-label').textContent = `#${dayNumber} · ${today}`;
  document.title = `Redact IACR #${dayNumber}`;

  const saved = loadDay(today);
  if (saved) {
    state.guesses = saved.guesses || [];
    for (const guess of state.guesses) {
      state.guessed.add(guess.word);
      applyGuess(guess.word, { flash: false });
    }
    state.won = !!saved.won;
    state.gaveUp = !!saved.gaveUp;
    state.startedAt = saved.startedAt ?? null;
    state.endedAt = saved.endedAt ?? null;
  }

  $('guess-input').disabled = false;
  $('guess-submit').disabled = false;
  renderSidebar();
  renderCounters();

  if (state.won || state.gaveUp) finish({ announce: false });
  else {
    $('guess-input').focus();
    if (!localStorage.getItem(STORE_PREFIX + 'seen-help')) {
      $('dlg-help').showModal();
      try { localStorage.setItem(STORE_PREFIX + 'seen-help', '1'); } catch { /* ignore */ }
    }
  }
}

boot();
