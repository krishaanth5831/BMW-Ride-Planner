const COLORS = ['#2ea043', '#d29922', '#a371f7'];
const map = L.map('map').setView([47.92, 11.45], 10);
L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
  attribution: '&copy; OpenStreetMap contributors', maxZoom: 18,
}).addTo(map);

const scenicLayer = L.layerGroup().addTo(map);
const routeLayer = L.layerGroup().addTo(map);
let plan = null, selected = 0, mode = 'loop', scoreMode = 'scenic';
let ptA = null, ptB = null, arm = 'A';
let markA = null, markB = null;

const $ = (id) => document.getElementById(id);
const setStatus = (m, c = '') => { $('status').textContent = m; $('status').className = 'status ' + c; };
const scoreValue = (route) => route.kpis[scoreMode] ?? route.kpis.scenic ?? 0;

async function health() {
  try {
    const j = await (await fetch('/api/health')).json();
    if (!j.ok) { $('health').textContent = 'no graph — run the precompute'; return; }
    const m = j.meta || {};
    $('health').textContent =
      `${(m.n_segments || 0).toLocaleString()} OSM segments · ${m.crowd_coverage_pct ?? '?'}% with crowd data`;
    drawScenic();
  } catch { $('health').textContent = 'engine offline'; }
}

async function drawScenic() {
  try {
    const { segments } = await (await fetch('/api/segments?limit=3500')).json();
    segments.forEach((s) => {
      const t = Math.max(0, Math.min(1, s.scenic));
      L.polyline(s.geometry, {
        color: `hsl(${140 * t + 10}, 75%, ${38 + 14 * t}%)`,
        weight: 1.5, opacity: 0.45,
      }).bindTooltip(`${s.name || s.highway} · scenic ${s.scenic}`).addTo(scenicLayer);
    });
  } catch (e) { console.warn('scenic layer', e); }
}

function renderProfile(p, wx, ex) {
  const c = p.capability, x = p.context;
  const basis = c.style_basis_cells
    ? `${c.style_basis_cells} shared places`
    : 'no crowd overlap — conservative default';
  $('profileBody').innerHTML = `
    <p class="muted">Built from <b>${p.n_trips}</b> rides
      (${p.n_points.toLocaleString()} telemetry points).</p>
    <table>
      <tr><td>Experience</td><td><b>${p.experience_level || '—'}</b> (${(p.experience_score || 0).toFixed(2)})</td></tr>
      <tr><td>Style ratio <span class="muted">vs crowd, same roads</span></td>
          <td><b>${c.style_ratio.toFixed(2)}×</b></td></tr>
      <tr><td class="muted">basis</td><td class="muted">${basis}</td></tr>
      <tr><td>Lean p90 / p95</td><td>${c.lean_p90.toFixed(1)}° / ${c.lean_p95.toFixed(1)}°</td></tr>
      <tr><td>Lean ceiling</td><td>${(p.lean_ceiling || c.lean_ceiling || 0).toFixed(1)}°</td></tr>
      <tr><td>Their median ride</td><td>${Math.round(x.median_ride_minutes)} min</td></tr>
      <tr><td>Bike class <span class="muted">inferred</span></td><td>${x.bike_class}</td></tr>
      <tr><td>Total ridden</td><td>${x.total_km.toLocaleString()} km</td></tr>
      <tr><td>Weather</td><td>${wx.available
        ? `${wx.temp_c}°C, ${wx.precip_mm} mm <span class="muted">(${wx.source})</span>`
        : '<span class="muted">unavailable</span>'}</td></tr>
    </table>
    <p class="muted" style="margin-top:8px">${ex.value_term}. Capability is always a hard exclusion.</p>`;
  $('profileCard').classList.remove('hidden');
}

// One plain sentence per heatmap route: what the avoidance terms bought you.
function twistyLine(k) {
  const bits = [];
  if (k.twisty_pct >= 40) bits.push(`Twisty route — ${k.twisty_pct}% of it on roads that keep turning`);
  else if (k.twist_score != null) bits.push(`Twist score ${k.twist_score}`);
  if (k.long_straight_km != null) {
    bits.push(k.long_straight_km <= 1
      ? 'no long straight runs'
      : `${k.long_straight_km} km on long straight roads (longest road run ${k.max_road_run_km} km)`);
  }
  if (k.traffic_pressure != null) {
    bits.push(k.traffic_pressure <= 0.2
      ? 'traffic avoided'
      : `traffic pressure ${k.traffic_pressure}`);
  }
  return bits.join(' · ');
}

function renderRoutes(routes) {
  const ordered = [...routes].sort((a, b) => scoreValue(b) - scoreValue(a));
  const current = plan && plan.mode === 'heatmap' ? ordered : routes;
  $('routeList').innerHTML = current.map((r) => {
    const i = routes.indexOf(r);
    return `
    <div class="route ${i === selected ? 'sel' : ''}" data-i="${i}">
      <div class="top">
        <span><span class="swatch" style="background:${COLORS[i % 3]}"></span><b>${r.label}</b></span>
        <span>${r.kpis.km} km · ${Math.round(r.kpis.minutes)} min</span>
      </div>
      <div class="chips">
        <span class="chip">scenic ${r.kpis.scenic}</span>
        <span class="chip">fun ${r.kpis.fun ?? '—'}</span>
        <span class="chip">personal ${r.kpis.personal ?? '—'}</span>
        <span class="chip">${r.kpis.crowd_covered_pct}% crowd</span>
        <span class="chip">${r.kpis.curviness || 0}°/km</span>
        <span class="chip">${r.kpis.junctions} junctions</span>
        ${r.kpis.twisty_pct != null ? `<span class="chip">${r.kpis.twisty_pct}% twisty</span>` : ''}
      </div>
      ${r.via && r.via.length ? `<div class="via">via ${r.via.slice(0, 3).join(' · ')}</div>` : ''}
      <div class="via">On roads BMW riders use <b>${r.kpis.crowd_covered_pct}%</b></div>
      ${plan && plan.mode === 'heatmap' ? `<div class="via">${twistyLine(r.kpis)}</div>` : ''}
    </div>`;
  }).join('');
  $('next').classList.toggle('hidden', !(plan && plan.mode === 'ab') || routes.length < 2);
  $('routeList').querySelectorAll('.route').forEach((el) =>
    el.addEventListener('click', () => { selected = +el.dataset.i; draw(); }));

  const r = routes[selected] || routes[0];
  if (!r) return;
  const k = r.kpis;
  $('kpis').innerHTML = `
    <table style="margin-top:10px">
      <tr><td>Selected ${scoreMode}</td><td>${scoreValue(r).toFixed(3)}</td></tr>
      <tr><td>Scenic score</td><td>${k.scenic}</td></tr>
      <tr><td>Fun score</td><td>${k.fun ?? '—'}</td></tr>
      <tr><td>Personal score</td><td>${k.personal ?? '—'}</td></tr>
      <tr><td>Distance</td><td>${k.km} km</td></tr>
      <tr><td>Moving time</td><td>${Math.round(k.minutes)} min</td></tr>
      <tr><td>Curviness <span class="muted">measured lean</span></td><td>${k.curviness || '—'} °/km</td></tr>
      <tr><td>Elevation gain</td><td>${k.elev_gain_m} m</td></tr>
      <tr><td>Junctions</td><td>${k.junctions}</td></tr>
      <tr><td>Twisty share <span class="muted">roads that keep turning</span></td><td>${k.twisty_pct ?? '—'}%</td></tr>
      <tr><td>Longest uninterrupted road</td><td>${k.max_road_run_km ?? '—'} km</td></tr>
      <tr><td>On long straight roads</td><td>${k.long_straight_km ?? '—'} km</td></tr>
      <tr><td>Traffic pressure <span class="muted">crowd crawl + road class</span></td><td>${k.traffic_pressure ?? '—'}</td></tr>
      <tr><td>On roads BMW riders use</td><td>${k.crowd_covered_pct}%</td></tr>
    </table>`;
  $('routeCard').classList.remove('hidden');
}

function drawHeatmap() {
  if (!plan.heatmap) return;
  const chosen = scoreMode;
  plan.heatmap.forEach((h) => {
    const value = chosen === 'fun' ? h.fun : chosen === 'personal' ? h.personal : h.scenic;
    const t = Math.max(0, Math.min(1, value));
    const w = h.weight || 0;
    L.polyline(h.geometry, {
      color: `hsl(${140 * t}, 75%, 42%)`,
      weight: 1.5 + 2 * w,
      opacity: 0.45 + 0.5 * w,
    }).addTo(routeLayer);
  });
}

function draw() {
  routeLayer.clearLayers();
  if (!plan) return;
  if (plan.mode === 'heatmap') drawHeatmap();
  plan.routes.forEach((r, i) => {
    const on = i === selected;
    L.polyline(r.coords, {
      color: COLORS[i % 3], weight: on ? 5 : (plan.mode === 'heatmap' && i < 3 ? 3.5 : 2.5),
      opacity: on ? 0.95 : (plan.mode === 'heatmap' && i < 3 ? 0.75 : 0.3),
    }).addTo(routeLayer);
  });
  const r = plan.routes[selected] || plan.routes[0];
  L.circleMarker([plan.origin.lat, plan.origin.lon], {
    radius: 7, color: '#fff', weight: 2, fillColor: '#1c69d4', fillOpacity: 1,
  }).bindTooltip('Start — A').addTo(routeLayer);
  if (plan.destination) {
    L.circleMarker([plan.destination.lat, plan.destination.lon], {
      radius: 7, color: '#fff', weight: 2, fillColor: '#f85149', fillOpacity: 1,
    }).bindTooltip('Destination — B').addTo(routeLayer);
  }
  renderRoutes(plan.routes);
  if (plan.mode === 'heatmap') {
    $('heatmapLegend').classList.remove('hidden');
    $('heatmapLegend').innerHTML = '<i style="background:#2ea043"></i> greener = higher selected score · <i style="background:#2ea043;height:6px"></i> thicker = more routes use it';
  } else $('heatmapLegend').classList.add('hidden');
  const b = L.latLngBounds(r.coords.map((p) => L.latLng(p[0], p[1])));
  requestAnimationFrame(() => {
    map.invalidateSize({ animate: false });
    if (b.isValid()) map.fitBounds(b, { padding: [40, 40], animate: false, maxZoom: 15 });
  });
}

function params() {
  const p = new URLSearchParams({ mode, minutes: $('mins').value });
  if (ptA) { p.set('origin_lat', ptA[0]); p.set('origin_lon', ptA[1]); }
  if ((mode === 'ab' || mode === 'heatmap') && ptB) { p.set('dest_lat', ptB[0]); p.set('dest_lon', ptB[1]); }
  if (mode === 'heatmap') p.set('radius_km', $('radius').value);
  return p;
}

async function send(url, opts) {
  document.querySelectorAll('button').forEach((b) => (b.disabled = true));
  setStatus('Building the profile and searching the road network…');
  try {
    const res = await fetch(url, opts);
    const body = await res.json();
    if (!res.ok) throw new Error(typeof body.detail === 'string' ? body.detail : (body.detail?.message || res.statusText));
    plan = body; selected = 0;
    renderProfile(body.profile, body.weather, body.explain);
    draw();
    setStatus(`${body.routes.length} route(s) · ${body.profile.n_trips} rides analysed`, 'ok');
  } catch (e) {
    setStatus(String(e.message || e), 'err');
  } finally {
    document.querySelectorAll('button').forEach((b) => (b.disabled = false));
  }
}

function chosenFiles() {
  const a = [...$('folder').files], b = [...$('file').files];
  return [...a, ...b].filter((f) => /\.(csv|zip)$/i.test(f.name));
}
function updatePicked() {
  const f = chosenFiles();
  $('picked').textContent = f.length
    ? `${f.length} file(s) selected — ${(f.reduce((s, x) => s + x.size, 0) / 1e6).toFixed(0)} MB` : '';
}
$('folder').addEventListener('change', updatePicked);
$('file').addEventListener('change', updatePicked);

function setMode(m) {
  mode = m;
  $('minsHint').textContent = m === 'loop' ? 'minutes — target' : 'minutes — preference only';
  $('mLoop').classList.toggle('on', m === 'loop');
  $('mAb').classList.toggle('on', m === 'ab');
  $('mHeatmap').classList.toggle('on', m === 'heatmap');
  $('abPick').classList.toggle('hidden', m === 'loop');
  $('heatmapControls').classList.toggle('hidden', m !== 'heatmap');
  $('go').classList.toggle('hidden', m === 'heatmap');
  $('buildHeatmap').classList.toggle('hidden', m !== 'heatmap');
}
$('mLoop').addEventListener('click', () => setMode('loop'));
$('mAb').addEventListener('click', () => setMode('ab'));
$('mHeatmap').addEventListener('click', () => setMode('heatmap'));

function setArm(which) {
  arm = which;
  $('pickA').classList.toggle('on', which === 'A');
  $('pickB').classList.toggle('on', which === 'B');
}
map.on('click', (e) => {
  if (mode !== 'ab' && mode !== 'heatmap') return;
  const ll = [e.latlng.lat, e.latlng.lng];
  const txt = `<b>${ll[0].toFixed(4)}, ${ll[1].toFixed(4)}</b>`;
  if (arm === 'A') {
    ptA = ll;
    if (markA) map.removeLayer(markA);
    markA = L.circleMarker(e.latlng, { radius: 7, color: '#fff', weight: 2,
      fillColor: '#1c69d4', fillOpacity: 1 }).bindTooltip('A — start').addTo(map);
    $('txtA').innerHTML = txt;
    setArm('B');
  } else {
    ptB = ll;
    if (markB) map.removeLayer(markB);
    markB = L.circleMarker(e.latlng, { radius: 7, color: '#fff', weight: 2,
      fillColor: '#f85149', fillOpacity: 1 }).bindTooltip('B — end').addTo(map);
    $('txtB').innerHTML = txt;
  }
});
$('pickA').addEventListener('click', () => setArm('A'));
$('pickB').addEventListener('click', () => setArm('B'));

function submitPlan(heatmap) {
  const files = chosenFiles();
  if (!files.length) { setStatus('Pick a folder or some files first.', 'err'); return; }
  if ((mode === 'ab' || heatmap) && (!ptA || !ptB)) { setStatus('Click the map to set both A and B.', 'err'); return; }
  const fd = new FormData();
  for (const f of files) fd.append('files', f);
  fd.append('mode', heatmap ? 'heatmap' : mode);
  fd.append('minutes', $('mins').value);
  if (ptA) { fd.append('origin_lat', ptA[0]); fd.append('origin_lon', ptA[1]); }
  if (ptB) { fd.append('dest_lat', ptB[0]); fd.append('dest_lon', ptB[1]); }
  if (heatmap) fd.append('radius_km', $('radius').value);
  setStatus(`Uploading ${files.length} files…`);
  send(heatmap ? '/api/heatmap' : '/api/plan/upload', { method: 'POST', body: fd });
}
$('go').addEventListener('click', () => submitPlan(false));
$('buildHeatmap').addEventListener('click', () => submitPlan(true));

document.querySelectorAll('.ex').forEach((b) => b.addEventListener('click', () => {
  if (mode === 'ab' && (!ptA || !ptB)) { setStatus('Click the map to set both A and B.', 'err'); return; }
  send(`/api/plan/example/${b.dataset.rider}?${params()}`, { method: 'POST' });
}));

document.querySelectorAll('.score').forEach((b) => b.addEventListener('click', () => {
  scoreMode = b.id.replace('score', '').toLowerCase();
  document.querySelectorAll('.score').forEach((x) => x.classList.toggle('on', x === b));
  if (plan) { selected = 0; draw(); }
}));

setMode('loop');
health();
$('next').addEventListener('click', () => {
  if (!plan || !plan.routes.length) return;
  selected = (selected + 1) % plan.routes.length;
  draw();
});
