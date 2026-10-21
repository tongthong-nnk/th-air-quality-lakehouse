"use strict";

const THAI_STANDARD = 37.5;
const WHO_GUIDELINE = 15;
// Thailand's PM2.5 index bands for a 24-hour average in µg/m³, in force since 2023.
const BANDS = [
  { upTo: 15, range: "0–15", label: "Very good" },
  { upTo: 25, range: "15–25", label: "Good" },
  { upTo: 37.5, range: "25–37.5", label: "Moderate" },
  { upTo: 75, range: "37.5–75", label: "Starting to affect health" },
  { upTo: Infinity, range: "over 75", label: "Affects health" },
];
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
// Heatmap rows run July to June, so December to April sits in the middle of one row.
const ROW_MONTHS = [7, 8, 9, 10, 11, 12, 1, 2, 3, 4, 5, 6];
const DAY_MS = 86_400_000;
const THAILAND = { west: 97.2, east: 105.8, south: 5.4, north: 20.7 };
// More than half of the stations are in and around Bangkok, so they get their own map.
const BANGKOK = { west: 100.3, east: 100.9, south: 13.53, north: 14.07 };
const SVG_NS = "http://www.w3.org/2000/svg";

const longDate = new Intl.DateTimeFormat("en-GB", {
  weekday: "short", day: "numeric", month: "short", year: "numeric", timeZone: "UTC",
});
const shortDate = new Intl.DateTimeFormat("en-GB", {
  day: "numeric", month: "short", year: "numeric", timeZone: "UTC",
});
const integer = new Intl.NumberFormat("en");

const state = { period: null, stationId: null, range: "year", dayIndex: null };
let model;

// ---------- data ----------

async function getJson(path) {
  const response = await fetch(path);
  if (!response.ok) throw new Error(`${path}: HTTP ${response.status}`);
  return response.json();
}

function utcDay(iso) {
  return Date.parse(`${iso}T00:00:00Z`);
}

function cleanName(name) {
  // OpenAQ appends the provider's station number to the name.
  return (name || "Unnamed station").replace(/-\d+$/, "").replace(/\s+/g, " ").trim();
}

function band(value) {
  return BANDS.findIndex((b) => value <= b.upTo);
}

function buildModel(data, region) {
  const first = utcDay(data.first_day);
  const size = Math.round((utcDay(data.last_day) - first) / DAY_MS) + 1;
  const dates = Array.from({ length: size }, (_, i) => new Date(first + i * DAY_MS));
  const stations = data.stations.map((s) => ({ ...s, label: cleanName(s.name) }));
  return {
    data,
    region,
    dates,
    stations,
    byId: new Map(stations.map((s) => [s.id, s])),
    periods: buildPeriods(dates),
  };
}

// Every haze season (December to April) that lies fully inside the data, newest first.
function buildPeriods(dates) {
  const index = (iso) => Math.round((utcDay(iso) - dates[0].getTime()) / DAY_MS);
  const periods = [];
  const lastYear = dates.at(-1).getUTCFullYear();
  for (let year = dates[0].getUTCFullYear(); year < lastYear; year++) {
    const start = index(`${year}-12-01`);
    const end = index(`${year + 1}-04-30`);
    if (start >= 0 && end < dates.length) {
      periods.push({ id: String(year + 1), label: `${year}–${String(year + 1).slice(2)}`, start, end });
    }
  }
  periods.reverse();
  periods.push({ id: "all", label: "All days", start: 0, end: dates.length - 1 });
  return periods;
}

function summarize(values, start, end) {
  let count = 0;
  let over = 0;
  let sum = 0;
  for (let i = start; i <= end; i++) {
    const value = values[i];
    if (value === null || value === undefined) continue;
    count += 1;
    sum += value;
    if (value > THAI_STANDARD) over += 1;
  }
  return { count, over, avg: count ? sum / count : null, share: count ? over / count : null };
}

function summarizeAll(start, end) {
  const covered = new Uint8Array(end - start + 1);
  let count = 0;
  let over = 0;
  let sum = 0;
  let stations = 0;
  for (const station of model.stations) {
    const s = summarize(station.pm25, start, end);
    if (!s.count) continue;
    stations += 1;
    count += s.count;
    over += s.over;
    sum += s.avg * s.count;
    for (let i = start; i <= end; i++) if (station.pm25[i] !== null) covered[i - start] = 1;
  }
  return {
    stations,
    count,
    over,
    avg: count ? sum / count : null,
    share: count ? over / count : null,
    days: covered.reduce((a, b) => a + b, 0),
    length: covered.length,
  };
}

// ---------- small helpers ----------

function svg(tag, attrs = {}, parent = null) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
  if (parent) parent.append(node);
  return node;
}

function label(parent, x, y, text, attrs = {}) {
  const node = svg("text", { x, y, ...attrs }, parent);
  node.textContent = text;
  return node;
}

function chartSvg(figure, width, height, title) {
  figure.replaceChildren();
  const root = svg("svg", { viewBox: `0 0 ${width} ${height}`, role: "img" });
  root.setAttribute("aria-label", title);
  figure.append(root);
  return root;
}

function percent(share, digits = 0) {
  return share === null ? "–" : `${(share * 100).toFixed(digits)}%`;
}

function micrograms(value) {
  return value === null ? "–" : `${value.toFixed(1)} µg/m³`;
}

function niceCeil(value, step) {
  return Math.max(step, Math.ceil(value / step) * step);
}

// A column that grows from the baseline with a 4px rounded top.
function columnPath(x, y, width, height) {
  const r = Math.min(4, width / 2, height);
  const bottom = y + height;
  return `M${x},${bottom}V${y + r}Q${x},${y} ${x + r},${y}H${x + width - r}` +
    `Q${x + width},${y} ${x + width},${y + r}V${bottom}Z`;
}

function fillTable(table, head, rows) {
  table.replaceChildren();
  const thead = table.createTHead().insertRow();
  for (const text of head) {
    const th = document.createElement("th");
    th.textContent = text;
    thead.append(th);
  }
  const body = table.createTBody();
  for (const row of rows) {
    const tr = body.insertRow();
    for (const text of row) tr.insertCell().textContent = text;
  }
}

// ---------- tooltip ----------

const tooltip = () => document.getElementById("tooltip");

function showTooltip(lines, x, y) {
  const tip = tooltip();
  tip.replaceChildren();
  for (const line of lines) {
    const node = document.createElement(line.strong ? "strong" : "div");
    if (line.key) {
      const key = document.createElement("span");
      key.className = "key";
      key.style.background = line.key;
      node.append(key);
    }
    node.append(document.createTextNode(line.text));
    if (line.muted) node.className = "muted";
    tip.append(node);
  }
  tip.hidden = false;
  const box = tip.getBoundingClientRect();
  const left = Math.min(x + 14, window.innerWidth - box.width - 8);
  const top = y + 14 + box.height > window.innerHeight ? y - box.height - 14 : y + 14;
  tip.style.left = `${Math.max(8, left)}px`;
  tip.style.top = `${Math.max(8, top)}px`;
}

function hideTooltip() {
  tooltip().hidden = true;
}

function attachTooltip(target, lines) {
  target.addEventListener("pointermove", (e) => showTooltip(lines(), e.clientX, e.clientY));
  target.addEventListener("pointerleave", hideTooltip);
}

// ---------- summary ----------

function renderSummary() {
  const { data, dates, stations, periods } = model;
  const all = summarizeAll(0, dates.length - 1);
  const seasons = periods.filter((p) => p.id !== "all").map((p) => ({ ...p, ...summarizeAll(p.start, p.end) }));
  const worst = seasons.reduce((a, b) => (b.share > a.share ? b : a), seasons[0]);

  document.getElementById("coverage").textContent =
    `Daily PM2.5 averages from ${stations.length} government monitoring stations, ` +
    `${shortDate.format(dates[0])} to ${shortDate.format(dates.at(-1))}. ` +
    "Days with fewer than 18 hourly readings are left out.";
  document.getElementById("kpi-stations").textContent = integer.format(all.stations);
  document.getElementById("kpi-days").textContent = integer.format(all.count);
  document.getElementById("kpi-over").textContent = percent(all.share, 1);
  if (worst) {
    document.getElementById("kpi-season").textContent = worst.label;
    document.getElementById("kpi-season-note").textContent =
      `${percent(worst.share)} of station-days above 37.5 µg/m³, December to April`;
  }

  const exported = new Date(data.generated_at);
  document.getElementById("generated").textContent =
    `Exported ${shortDate.format(exported)}, ${exported.toISOString().slice(11, 16)} UTC.` +
    (data.has_fires ? "" : " This export has no fire detections.");
  document.getElementById("fires-credit").hidden = !data.has_fires;
  return seasons;
}

// ---------- haze seasons ----------

function renderSeasons(seasons) {
  const figure = document.getElementById("seasons-chart");
  const ordered = [...seasons].reverse();
  const width = figure.clientWidth;
  const height = 240;
  const m = { top: 24, right: 8, bottom: 28, left: 40 };
  const plotW = width - m.left - m.right;
  const plotH = height - m.top - m.bottom;
  const max = niceCeil(Math.max(...ordered.map((s) => s.share)) * 100, 10);
  const y = (share) => m.top + plotH - (share * 100 * plotH) / max;
  const root = chartSvg(figure, width, height,
    `Share of station-days above 37.5 µg/m³ in each haze season: ` +
    ordered.map((s) => `${s.label} ${percent(s.share)}`).join(", "));

  for (let tick = 0; tick <= max; tick += 10) {
    svg("line", {
      x1: m.left, x2: width - m.right, y1: y(tick / 100), y2: y(tick / 100),
      stroke: tick === 0 ? "var(--axis)" : "var(--grid)",
    }, root);
    label(root, m.left - 8, y(tick / 100) + 4, `${tick}%`, { "text-anchor": "end" });
  }

  const slot = plotW / ordered.length;
  const barW = Math.min(24, slot * 0.6);
  ordered.forEach((season, i) => {
    const cx = m.left + slot * (i + 0.5);
    const top = y(season.share);
    svg("path", { d: columnPath(cx - barW / 2, top, barW, m.top + plotH - top), fill: "var(--series-1)" }, root);
    label(root, cx, top - 8, percent(season.share), { "text-anchor": "middle", class: "value-label" });
    label(root, cx, height - 8, season.label, { "text-anchor": "middle" });
    const hit = svg("rect", { x: cx - slot / 2, y: m.top, width: slot, height: plotH, fill: "transparent" }, root);
    attachTooltip(hit, () => [
      { text: percent(season.share, 1), strong: true },
      { text: `of station-days above 37.5 µg/m³, ${season.label}` },
      { text: `Average ${micrograms(season.avg)}`, muted: true },
      { text: `${season.days} of ${season.length} days have readings`, muted: true },
    ]);
  });

  fillTable(
    document.getElementById("seasons-table"),
    ["Season", "Days with readings", "Station-days", "Above 37.5", "Average µg/m³"],
    ordered.map((s) => [
      `Dec ${s.label.slice(0, 4)} – Apr 20${s.label.slice(5)}`,
      `${s.days} of ${s.length}`,
      integer.format(s.count),
      percent(s.share, 1),
      s.avg.toFixed(1),
    ]),
  );
}

// ---------- monthly heatmap ----------

function renderBandLegend(container, emptyLabel, round = false) {
  container.replaceChildren();
  BANDS.forEach((b, i) => {
    const item = document.createElement("span");
    const swatch = document.createElement("i");
    swatch.style.background = `var(--band-${i + 1})`;
    if (round) swatch.classList.add("round");
    item.append(swatch, document.createTextNode(`${b.range} ${b.label.toLowerCase()}`));
    container.append(item);
  });
  const none = document.createElement("span");
  const swatch = document.createElement("i");
  swatch.className = round ? "no-data round" : "no-data";
  none.append(swatch, document.createTextNode(emptyLabel));
  container.append(none);
}

function heatmapRows() {
  const rows = new Map();
  for (const month of model.data.months) {
    const [year, mon] = month.month.split("-").map(Number);
    const startYear = mon >= 7 ? year : year - 1;
    const key = `${startYear}–${String(startYear + 1).slice(2)}`;
    if (!rows.has(key)) rows.set(key, new Map());
    rows.get(key).set(mon, { ...month, year, mon });
  }
  return [...rows.entries()].sort(([a], [b]) => a.localeCompare(b));
}

function renderHeatmap() {
  const figure = document.getElementById("heatmap");
  const rows = heatmapRows();
  const width = figure.clientWidth;
  const labelW = 64;
  const gap = 2;
  const cellW = (width - labelW) / 12;
  const cellH = Math.min(30, Math.max(22, cellW * 0.6));
  const top = 40;
  const height = top + rows.length * cellH;
  const root = chartSvg(figure, width, height,
    "Monthly average PM2.5 across all stations, one row per July-to-June year. Values are in the table below.");

  // Bracket over December to April.
  const x0 = labelW + 5 * cellW;
  const x1 = labelW + 10 * cellW - gap;
  svg("path", { d: `M${x0},18V12H${x1}V18`, fill: "none", stroke: "var(--axis)" }, root);
  label(root, (x0 + x1) / 2, 8, "haze season", { "text-anchor": "middle" });

  ROW_MONTHS.forEach((mon, col) => {
    const name = cellW < 36 ? MONTHS[mon - 1][0] : MONTHS[mon - 1];
    label(root, labelW + col * cellW + (cellW - gap) / 2, top - 8, name, { "text-anchor": "middle" });
  });

  rows.forEach(([key, months], row) => {
    const y = top + row * cellH;
    label(root, 0, y + cellH / 2 + 4, key);
    ROW_MONTHS.forEach((mon, col) => {
      const x = labelW + col * cellW;
      const cell = months.get(mon);
      if (!cell) {
        svg("rect", {
          x: x + 0.5, y: y + 0.5, width: cellW - gap - 1, height: cellH - gap - 1, rx: 3,
          fill: "none", stroke: "var(--grid)",
        }, root);
        return;
      }
      const rect = svg("rect", {
        x, y, width: cellW - gap, height: cellH - gap, rx: 3, fill: `var(--band-${band(cell.pm25_avg) + 1})`,
      }, root);
      attachTooltip(rect, () => [
        { text: micrograms(cell.pm25_avg), strong: true },
        { text: `average, ${MONTHS[mon - 1]} ${cell.year}` },
        { text: `${percent(cell.over_thai_pct / 100, 1)} of station-days above 37.5`, muted: true },
        { text: `${cell.stations} stations reporting`, muted: true },
      ]);
    });
  });

  fillTable(
    document.getElementById("heatmap-table"),
    ["July to June", ...ROW_MONTHS.map((mon) => MONTHS[mon - 1])],
    rows.map(([key, months]) => [
      key,
      ...ROW_MONTHS.map((mon) => (months.has(mon) ? months.get(mon).pm25_avg.toFixed(1) : "")),
    ]),
  );
}

// ---------- stations map and ranking ----------

function currentPeriod() {
  return model.periods.find((p) => p.id === state.period);
}

function stationStats(period) {
  return model.stations.map((station) => ({ station, ...summarize(station.pm25, period.start, period.end) }));
}

function minimumDays(period) {
  return period.id === "all" ? 365 : 30;
}

function renderStations() {
  const period = currentPeriod();
  const stats = stationStats(period);
  const minDays = minimumDays(period);
  document.getElementById("stations-sub").textContent =
    `Average PM2.5 at each station, ${period.id === "all" ? "over all days" : `December ${period.label.slice(0, 4)} to April 20${period.label.slice(5)}`}. ` +
    "Select a station to see its daily readings.";

  renderMap(document.getElementById("map"), THAILAND, stats, BANGKOK);
  const inBangkok = renderMap(document.getElementById("inset"), BANGKOK, stats);
  document.getElementById("inset-title").textContent = `Greater Bangkok, ${inBangkok} stations`;

  const ranked = stats
    .filter((s) => s.count >= minDays)
    .sort((a, b) => b.share - a.share || b.avg - a.avg)
    .slice(0, 10);
  document.getElementById("ranking-title").textContent =
    `Most days above the standard (stations with ${minDays}+ days of readings)`;
  const list = document.getElementById("ranking");
  list.replaceChildren();
  for (const s of ranked) {
    const item = document.createElement("li");
    const button = document.createElement("button");
    button.type = "button";
    button.setAttribute("aria-pressed", String(s.station.id === state.stationId));
    const name = document.createElement("span");
    name.className = "name";
    name.textContent = s.station.label;
    const value = document.createElement("span");
    value.className = "value";
    value.textContent = percent(s.share);
    const bar = document.createElement("span");
    bar.className = "bar";
    bar.style.width = `${Math.max(1, s.share * 100)}%`;
    button.append(name, value, bar);
    button.addEventListener("click", () => selectStation(s.station.id, true));
    item.append(button);
    list.append(item);
  }

  fillTable(
    document.getElementById("stations-table"),
    ["Station", "Days with readings", "Average µg/m³", "Days above 37.5", "Share above"],
    stats
      .filter((s) => s.count > 0)
      .sort((a, b) => b.share - a.share || a.station.label.localeCompare(b.station.label))
      .map((s) => [s.station.label, integer.format(s.count), s.avg.toFixed(1), integer.format(s.over), percent(s.share, 1)]),
  );
}

function inside(station, bounds) {
  return station.lon >= bounds.west && station.lon <= bounds.east &&
    station.lat >= bounds.south && station.lat <= bounds.north;
}

function renderMap(figure, bounds, stats, inset = null) {
  const width = figure.clientWidth;
  const kx = Math.cos((((bounds.south + bounds.north) / 2) * Math.PI) / 180);
  const scale = width / ((bounds.east - bounds.west) * kx);
  const height = (bounds.north - bounds.south) * scale;
  const project = ([lon, lat]) => [(lon - bounds.west) * kx * scale, (bounds.north - lat) * scale];
  const shown = stats.filter((s) => inside(s.station, bounds));
  const root = chartSvg(figure, width, height,
    `Map of ${shown.length} stations, coloured by their average PM2.5 in the selected period. ` +
    "The same values are in the table below.");

  const clipId = `${figure.id}-clip`;
  const clip = svg("clipPath", { id: clipId }, svg("defs", {}, root));
  svg("rect", { width, height }, clip);
  const land = svg("g", { "clip-path": `url(#${clipId})` }, root);
  for (const shape of model.region.shapes) {
    const d = shape.polygons
      .flatMap((polygon) => polygon.map((ring) => `M${ring.map((p) => project(p).map((v) => v.toFixed(1)).join(",")).join("L")}Z`))
      .join("");
    const thailand = shape.name === "Thailand";
    svg("path", {
      d,
      fill: thailand ? "var(--land)" : "none",
      stroke: thailand ? "var(--axis)" : "var(--grid)",
      "stroke-linejoin": "round",
    }, land);
  }
  if (inset) {
    const [x0, y0] = project([inset.west, inset.north]);
    const [x1, y1] = project([inset.east, inset.south]);
    svg("rect", { x: x0, y: y0, width: x1 - x0, height: y1 - y0, fill: "none", stroke: "var(--ink-2)" }, root);
  }

  // Highest values last, so they stay on top where stations crowd together.
  const ordered = [...shown].sort((a, b) => (a.avg ?? -1) - (b.avg ?? -1));
  const points = ordered.map((s) => ({ s, xy: project([s.station.lon, s.station.lat]) }));
  for (const { s, xy: [x, y] } of points) {
    if (s.count) {
      svg("circle", {
        cx: x, cy: y, r: 4.5, fill: `var(--band-${band(s.avg) + 1})`, stroke: "var(--surface)", "stroke-width": 2,
      }, root);
    } else {
      svg("circle", { cx: x, cy: y, r: 3, fill: "none", stroke: "var(--ink-2)" }, root);
    }
  }
  const selected = points.find((p) => p.s.station.id === state.stationId);
  if (selected) {
    svg("circle", { cx: selected.xy[0], cy: selected.xy[1], r: 8, fill: "none", stroke: "var(--ink)", "stroke-width": 2 }, root);
  }

  // Dots overlap, so the pointer picks the nearest station instead of the dot under it.
  const nearest = (e) => {
    const box = root.getBoundingClientRect();
    const px = ((e.clientX - box.left) / box.width) * width;
    const py = ((e.clientY - box.top) / box.height) * height;
    let best = null;
    let bestDistance = 16 ** 2;
    for (const p of points) {
      const distance = (p.xy[0] - px) ** 2 + (p.xy[1] - py) ** 2;
      if (distance < bestDistance) [best, bestDistance] = [p.s, distance];
    }
    return best;
  };
  root.addEventListener("pointermove", (e) => {
    const s = nearest(e);
    root.style.cursor = s ? "pointer" : "default";
    if (!s) return hideTooltip();
    showTooltip(s.count
      ? [
        { text: micrograms(s.avg), strong: true },
        { text: s.station.label },
        { text: `${percent(s.share)} of ${integer.format(s.count)} days above 37.5`, muted: true },
      ]
      : [{ text: s.station.label, strong: true }, { text: "No complete days in this period", muted: true }],
    e.clientX, e.clientY);
  });
  root.addEventListener("pointerleave", hideTooltip);
  root.addEventListener("click", (e) => {
    const s = nearest(e);
    if (s) selectStation(s.station.id, true);
  });
  return shown.length;
}

// ---------- daily readings ----------

function dailyWindow() {
  const last = model.dates.length - 1;
  return state.range === "all" ? [0, last] : [Math.max(0, last - 364), last];
}

function renderDaily() {
  const station = model.byId.get(state.stationId);
  const [start, end] = dailyWindow();
  const values = station.pm25;
  const figure = document.getElementById("daily");
  const width = figure.clientWidth;
  const height = 300;
  const m = { top: 12, right: 64, bottom: 28, left: 36 };
  const plotW = width - m.left - m.right;
  const plotH = height - m.top - m.bottom;
  let max = 0;
  for (let i = start; i <= end; i++) if (values[i] !== null) max = Math.max(max, values[i]);
  const yMax = niceCeil(Math.max(max, 50), 25);
  const x = (i) => m.left + ((i - start) / Math.max(1, end - start)) * plotW;
  const y = (v) => m.top + plotH - (v / yMax) * plotH;
  const s = summarize(values, start, end);

  document.getElementById("station-title").textContent = station.label;
  document.getElementById("station-sub").textContent = s.count
    ? `${integer.format(s.count)} days with readings from ${shortDate.format(model.dates[start])} to ` +
      `${shortDate.format(model.dates[end])}: average ${micrograms(s.avg)}, ${integer.format(s.over)} days ` +
      `(${percent(s.share)}) above Thailand's 37.5 µg/m³ standard.`
    : "No complete days in this range.";

  const root = chartSvg(figure, width, height,
    `Daily average PM2.5 at ${station.label}. ${document.getElementById("station-sub").textContent}`);

  for (let tick = 0; tick <= yMax; tick += 25) {
    svg("line", { x1: m.left, x2: width - m.right, y1: y(tick), y2: y(tick), stroke: tick ? "var(--grid)" : "var(--axis)" }, root);
    label(root, m.left - 6, y(tick) + 4, String(tick), { "text-anchor": "end" });
  }
  drawTimeAxis(root, start, end, x, height - 8);

  let d = "";
  for (let i = start; i <= end; i++) {
    if (values[i] === null) continue;
    const command = i === start || values[i - 1] === null ? "M" : "L";
    d += `${command}${x(i).toFixed(1)},${y(values[i]).toFixed(1)}`;
    if (command === "M" && (i === end || values[i + 1] === null)) {
      svg("circle", { cx: x(i), cy: y(values[i]), r: 1.5, fill: "var(--series-1)" }, root);
    }
  }
  svg("path", {
    d, fill: "none", stroke: "var(--series-1)", "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round",
  }, root);

  // Labels sit in the right margin; the upper one moves up if a tall axis squeezes them together.
  const thaiY = Math.min(y(THAI_STANDARD), y(WHO_GUIDELINE) - 14);
  for (const [value, text, labelY] of [[THAI_STANDARD, "Thai 37.5", thaiY], [WHO_GUIDELINE, "WHO 15", y(WHO_GUIDELINE)]]) {
    svg("line", {
      x1: m.left, x2: width - m.right, y1: y(value), y2: y(value), stroke: "var(--ink-2)", "stroke-dasharray": "4 3",
    }, root);
    label(root, width - m.right + 6, labelY + 4, text);
  }

  const cursor = svg("g", { visibility: "hidden" }, root);
  const cursorLine = svg("line", { y1: m.top, y2: m.top + plotH, stroke: "var(--ink-2)" }, cursor);
  const cursorDot = svg("circle", { r: 4, fill: "var(--series-1)", stroke: "var(--surface)", "stroke-width": 2 }, cursor);

  const fires = model.data.has_fires ? renderFires(station, start, end, x, width, m) : null;

  const show = (i, clientX, clientY) => {
    state.dayIndex = i;
    const value = values[i];
    cursor.setAttribute("visibility", "visible");
    cursorLine.setAttribute("x1", x(i));
    cursorLine.setAttribute("x2", x(i));
    cursorDot.setAttribute("visibility", value === null ? "hidden" : "visible");
    if (value !== null) {
      cursorDot.setAttribute("cx", x(i));
      cursorDot.setAttribute("cy", y(value));
    }
    fires?.move(i);
    const lines = [
      { text: value === null ? "No complete day" : micrograms(value), strong: true },
      { text: longDate.format(model.dates[i]) },
    ];
    if (value !== null) lines.push({ text: BANDS[band(value)].label, muted: true });
    if (model.data.has_fires && station.fires[i] !== null) {
      lines.push({ text: `${station.fires[i]} fire detections within 100 km`, muted: true, key: "var(--series-2)" });
    }
    showTooltip(lines, clientX, clientY);
  };
  const hide = () => {
    cursor.setAttribute("visibility", "hidden");
    fires?.move(null);
    hideTooltip();
  };
  const indexAt = (clientX, element) => {
    const box = element.getBoundingClientRect();
    const px = ((clientX - box.left) / box.width) * width;
    return Math.min(end, Math.max(start, Math.round(start + ((px - m.left) / plotW) * (end - start))));
  };

  for (const target of [root, fires?.root].filter(Boolean)) {
    target.addEventListener("pointermove", (e) => show(indexAt(e.clientX, target), e.clientX, e.clientY));
    target.addEventListener("pointerleave", hide);
  }
  figure.onkeydown = (e) => {
    const steps = { ArrowLeft: -1, ArrowRight: 1, PageUp: -30, PageDown: 30 };
    let i = state.dayIndex ?? end;
    if (e.key in steps) i += steps[e.key];
    else if (e.key === "Home") i = start;
    else if (e.key === "End") i = end;
    else return;
    e.preventDefault();
    i = Math.min(end, Math.max(start, i));
    const box = root.getBoundingClientRect();
    show(i, box.left + (x(i) / width) * box.width, box.top + box.height / 3);
  };
  figure.onblur = hide;
  state.dayIndex = null;

  renderDailyTable(station, start, end);
}

function drawTimeAxis(root, start, end, x, baseline) {
  const all = end - start > 400;
  let lastX = -Infinity;
  for (let i = start; i <= end; i++) {
    const date = model.dates[i];
    if (date.getUTCDate() !== 1) continue;
    const month = date.getUTCMonth();
    if (all && month !== 0) continue;
    const text = all ? String(date.getUTCFullYear()) : month === 0 ? `Jan ${date.getUTCFullYear()}` : MONTHS[month];
    const px = x(i);
    if (px - lastX < (all ? 40 : month === 0 ? 56 : 34)) continue;
    label(root, px, baseline, text, { "text-anchor": "middle" });
    lastX = px;
  }
}

function renderFires(station, start, end, x, width, m) {
  const figure = document.getElementById("fires");
  figure.hidden = false;
  const height = 120;
  const top = 28;
  const plotH = height - top - 8;
  let max = 0;
  for (let i = start; i <= end; i++) max = Math.max(max, station.fires[i] ?? 0);
  const yMax = niceCeil(max, max > 50 ? 50 : 10);
  const root = chartSvg(figure, width, height, `Fire detections within 100 km of ${station.label}, per day.`);
  label(root, m.left, 14, "Fire detections within 100 km", { class: "value-label" });
  svg("line", { x1: m.left, x2: width - m.right, y1: top + plotH, y2: top + plotH, stroke: "var(--axis)" }, root);
  label(root, m.left - 6, top + 4, String(yMax), { "text-anchor": "end" });
  label(root, m.left - 6, top + plotH + 4, "0", { "text-anchor": "end" });
  const barW = Math.max(1, ((width - m.left - m.right) / (end - start + 1)) * 0.7);
  let d = "";
  for (let i = start; i <= end; i++) {
    const count = station.fires[i];
    if (!count) continue;
    const h = (count / yMax) * plotH;
    d += `M${(x(i) - barW / 2).toFixed(1)},${(top + plotH).toFixed(1)}h${barW.toFixed(1)}v${(-h).toFixed(1)}h${(-barW).toFixed(1)}Z`;
  }
  svg("path", { d, fill: "var(--series-2)" }, root);
  const line = svg("line", { y1: top, y2: top + plotH, stroke: "var(--ink-2)", visibility: "hidden" }, root);
  return {
    root,
    move(i) {
      line.setAttribute("visibility", i === null ? "hidden" : "visible");
      if (i !== null) {
        line.setAttribute("x1", x(i));
        line.setAttribute("x2", x(i));
      }
    },
  };
}

function renderDailyTable(station, start, end) {
  const months = new Map();
  for (let i = start; i <= end; i++) {
    const date = model.dates[i];
    const key = `${MONTHS[date.getUTCMonth()]} ${date.getUTCFullYear()}`;
    if (!months.has(key)) months.set(key, { days: 0, over: 0, sum: 0, fires: 0 });
    const month = months.get(key);
    const value = station.pm25[i];
    if (value !== null) {
      month.days += 1;
      month.sum += value;
      if (value > THAI_STANDARD) month.over += 1;
    }
    if (model.data.has_fires) month.fires += station.fires[i] ?? 0;
  }
  const head = ["Month", "Days with readings", "Average µg/m³", "Days above 37.5"];
  if (model.data.has_fires) head.push("Fire detections");
  fillTable(
    document.getElementById("daily-table"),
    head,
    [...months.entries()].map(([key, mo]) => {
      const row = [key, String(mo.days), mo.days ? (mo.sum / mo.days).toFixed(1) : "", String(mo.over)];
      if (model.data.has_fires) row.push(integer.format(mo.fires));
      return row;
    }),
  );
}

// ---------- controls ----------

function selectStation(id, scroll = false) {
  state.stationId = id;
  document.getElementById("station").value = String(id);
  renderStations();
  renderDaily();
  if (scroll) document.getElementById("station-title").scrollIntoView({ behavior: "smooth", block: "start" });
}

function setupControls() {
  const period = document.getElementById("period");
  for (const p of model.periods) {
    const option = new Option(p.id === "all" ? p.label : `Haze season ${p.label}`, p.id);
    period.append(option);
  }
  state.period = model.periods[0].id;
  period.value = state.period;
  period.addEventListener("change", () => {
    state.period = period.value;
    renderStations();
  });

  const select = document.getElementById("station");
  for (const station of [...model.stations].sort((a, b) => a.label.localeCompare(b.label))) {
    select.append(new Option(station.label, String(station.id)));
  }
  select.addEventListener("change", () => selectStation(Number(select.value)));

  const range = document.getElementById("range");
  range.addEventListener("change", () => {
    state.range = range.value;
    renderDaily();
  });

  // Start on the station with the most days above the standard in the latest season.
  const latest = model.periods[0];
  const top = stationStats(latest)
    .filter((s) => s.count >= minimumDays(latest))
    .sort((a, b) => b.share - a.share)[0];
  state.stationId = (top ?? { station: model.stations[0] }).station.id;
  select.value = String(state.stationId);
}

function renderCharts(seasons) {
  renderSeasons(seasons);
  renderHeatmap();
  renderStations();
  renderDaily();
}

async function main() {
  try {
    const [data, region] = await Promise.all([getJson("data/dashboard.json"), getJson("data/region.json")]);
    model = buildModel(data, region);
  } catch (error) {
    document.getElementById("coverage").textContent = `Could not load the data: ${error.message}`;
    return;
  }
  const seasons = renderSummary();
  renderBandLegend(document.getElementById("band-legend"), "no readings");
  renderBandLegend(document.getElementById("map-legend"), "no complete days in the period", true);
  setupControls();
  renderCharts(seasons);

  let width = window.innerWidth;
  let timer;
  window.addEventListener("resize", () => {
    clearTimeout(timer);
    timer = setTimeout(() => {
      if (window.innerWidth === width) return;
      width = window.innerWidth;
      renderCharts(seasons);
    }, 150);
  });
}

main();
