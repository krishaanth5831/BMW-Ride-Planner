const COLORS = ['#2ea043', '#d29922', '#a371f7'];
const map = L.map('map').setView([47.92, 11.45], 10);
// Plain OSM tiles: no API key. For the demo these should be pre-cached locally
// so the map survives venue wifi.
L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
  attribution: '&copy; OpenStreetMap contributors', maxZoom: 18,
}).addTo(map);

const scenicLayer = L.layerGroup().addTo(map);
const routeLayer = L.layerGroup().addTo(map);
let plan = null, selected = 0, mode = 'loop', dest = null, destMarker = null;

const $ = (id) => document.getElementById(id);
const setStatus = (m, c = '') => { $('status').textContent = m; $('status').className = 'status ' + c; };

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

// Underlay: the most scenic roads in the covered region, so you can see what
// the score actually thinks before asking for a route.
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

function bar(v) { return `<div class="bar"><i style="width:${Math.round(v * 100)}%"></i></div>`; }

function renderProfile(p, wx, ex) {
  const c = p.capability, x = p.context;
  const basis = c.style_basis_cells
    ? `${c.style_basis_cells} shared places`
    : 'no crowd overlap — conservative default';
  $('profileBody').innerHTML = `
    <p class="muted">Built from <b>${p.n_trips}</b> rides
      (${p.n_points.toLocaleString()} telemetry points).</p>
    <table>
      <tr><td>Style ratio <span class="muted">vs crowd, same roads</span></td>
          <td><b>${c.style_ratio.toFixed(2)}×</b></td></tr>
      <tr><td class="muted">basis</td><td class="muted">${basis}</td></tr>
      <tr><td>Lean p90 / p95</td><td>${c.lean_p90.toFixed(1)}° / ${c.lean_p95.toFixed(1)}°</td></tr>
      <tr><td>Their median ride</td><td>${Math.round(x.median_ride_minutes)} min</td></tr>
      <tr><td>Usual start hour</td><td>${x.usual_start_hour}:00</td></tr>
      <tr><td>Bike class <span class="muted">inferred</span></td><td>${x.bike_class}</td></tr>
      <tr><td>Total ridden</td><td>${x.total_km.toLocaleString()} km</td></tr>
      <tr><td>Weather</td><td>${wx.available
        ? `${wx.temp_c}°C, ${wx.precip_mm} mm <span class="muted">(${wx.source})</span>`
        : '<span class="muted">unavailable</span>'}</td></tr>
    </table>
    <p class="muted" style="margin-top:8px">
      Routing on <b>${ex.value_term}</b>.<br>
      The profile is used only for: ${ex.profile_used_for.join(', ')}.</p>`;
  $('profileCard').classList.remove('hidden');
}

function renderRoutes(routes) {
  $('routeList').innerHTML = routes.map((r, i) => `
    <div class="route ${i === selected ? 'sel' : ''}" data-i="${i}">
      <div class="top">
        <span><span class="swatch" style="background:${COLORS[i % 3]}"></span><b>${r.label}</b></span>
        <span>${r.kpis.km} km · ${Math.round(r.kpis.minutes)} min</span>
      </div>
      <div class="chips">
        <span class="chip">scenic ${r.kpis.scenic}</span>
        <span class="chip">${r.kpis.crowd_covered_pct}% crowd</span>
        <span class="chip">${r.kpis.curviness || 0}°/km</span>
        <span class="chip">${r.kpis.junctions} junctions</span>
        ${r.bearing !== undefined ? `<span class="chip">brg ${r.bearing}°</span>` : ''}
      </div>
      ${r.via && r.via.length ? `<div class="via">via ${r.via.slice(0, 3).join(' · ')}</div>` : ''}
    </div>`).join('');
  $('routeList').querySelectorAll('.route').forEach((el) =>
    el.addEventListener('click', () => { selected = +el.dataset.i; draw(); }));

  const k = routes[selected].kpis;
  $('kpis').innerHTML = `
    <table style="margin-top:10px">
      <tr><td>Scenic score</td><td>${k.scenic}</td></tr>
      <tr><td>Distance</td><td>${k.km} km</td></tr>
      <tr><td>Moving time</td><td>${Math.round(k.minutes)} min</td></tr>
      <tr><td>Curviness <span class="muted">measured lean</span></td><td>${k.curviness || '—'} °/km</td></tr>
      <tr><td>Time leaned over</td><td>${k.leaned_share ? (k.leaned_share * 100).toFixed(0) + '%' : '—'}</td></tr>
      <tr><td>Elevation gain</td><td>${k.elev_gain_m} m</td></tr>
      <tr><td>Junctions</td><td>${k.junctions}</td></tr>
      <tr><td>Risk <span class="muted">lower is better</span></td><td>${k.risk}</td></tr>
      <tr><td>On roads BMW riders use</td><td>${k.crowd_covered_pct}%</td></tr>
    </table>
    <div class="legend">
      <i style="background:#2ea043"></i>selected route<br>
      <i style="background:#1f6f3f"></i>scenic underlay — greener is more scenic<br>
      Where <b>On roads BMW riders use</b> is low, the score comes from road
      geometry and class rather than measured lean.
    </div>`;
  $('routeCard').classList.remove('hidden');
}

function draw() {
  routeLayer.clearLayers();
  if (!plan) return;
  plan.routes.forEach((r, i) => {
    const on = i === selected;
    L.polyline(r.coords, {
      color: COLORS[i % 3], weight: on ? 5 : 2.5, opacity: on ? 0.95 : 0.3,
    }).addTo(routeLayer);
  });
  const r = plan.routes[selected];
  L.circleMarker([plan.origin.lat, plan.origin.lon], {
    radius: 7, color: '#fff', weight: 2, fillColor: '#1c69d4', fillOpacity: 1,
  }).bindTooltip('Start — their own most frequent trip origin').addTo(routeLayer);
  if (plan.destination) {
    L.circleMarker([plan.destination.lat, plan.destination.lon], {
      radius: 7, color: '#fff', weight: 2, fillColor: '#f85149', fillOpacity: 1,
    }).bindTooltip('Destination').addTo(routeLayer);
  } else if (r.turnaround) {
    L.circleMarker([r.turnaround.lat, r.turnaround.lon], {
      radius: 5, color: COLORS[selected % 3], weight: 2, fillOpacity: 0.6,
    }).bindTooltip('Turnaround').addTo(routeLayer);
  }
  renderRoutes(plan.routes);
  // Fit after the panel has re-rendered, so the map has its final size, and
  // build bounds explicitly from the coordinates rather than from a throwaway
  // polyline.
  const b = L.latLngBounds(r.coords.map((p) => L.latLng(p[0], p[1])));
  requestAnimationFrame(() => {
    map.invalidateSize({ animate: false });
    if (b.isValid()) map.fitBounds(b, { padding: [40, 40], animate: false, maxZoom: 15 });
  });
}

function params() {
  const p = new URLSearchParams({ mode, minutes: $('mins').value });
  if (mode === 'ab' && dest) { p.set('dest_lat', dest[0]); p.set('dest_lon', dest[1]); }
  return p;
}

async function send(url, opts) {
  document.querySelectorAll('button').forEach((b) => (b.disabled = true));
  setStatus('Building the profile and searching the road network…');
  try {
    const res = await fetch(url, opts);
    const body = await res.json();
    if (!res.ok) throw new Error(body.detail || res.statusText);
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
    ? `${f.length} file(s) selected — ${(f.reduce((s, x) => s + x.size, 0) / 1e6).toFixed(0)} MB`
    : '';
}
$('folder').addEventListener('change', updatePicked);
$('file').addEventListener('change', updatePicked);

function setMode(m) {
  mode = m;
  // Duration is a target for a round trip, but only a preference for A->B:
  // the router will not detour just to consume time.
  $('minsHint').textContent = m === 'loop' ? 'minutes — target' : 'minutes — preference only';
  $('mLoop').classList.toggle('on', m === 'loop');
  $('mAb').classList.toggle('on', m === 'ab');
  $('abHint').classList.toggle('hidden', m !== 'ab');
}
$('mLoop').addEventListener('click', () => setMode('loop'));
$('mAb').addEventListener('click', () => setMode('ab'));

map.on('click', (e) => {
  if (mode !== 'ab') return;
  dest = [e.latlng.lat, e.latlng.lng];
  if (destMarker) map.removeLayer(destMarker);
  destMarker = L.circleMarker(e.latlng, {
    radius: 7, color: '#fff', weight: 2, fillColor: '#f85149', fillOpacity: 1,
  }).addTo(map);
  $('destTxt').innerHTML = `<b>${dest[0].toFixed(4)}, ${dest[1].toFixed(4)}</b>`;
});

$('go').addEventListener('click', () => {
  const files = chosenFiles();
  if (!files.length) { setStatus('Pick a folder or some files first.', 'err'); return; }
  if (mode === 'ab' && !dest) { setStatus('Click the map to set a destination.', 'err'); return; }
  const fd = new FormData();
  for (const f of files) fd.append('files', f);
  fd.append('mode', mode);
  fd.append('minutes', $('mins').value);
  if (mode === 'ab' && dest) { fd.append('dest_lat', dest[0]); fd.append('dest_lon', dest[1]); }
  setStatus(`Uploading ${files.length} files…`);
  send('/api/plan/upload', { method: 'POST', body: fd });
});

document.querySelectorAll('.ex').forEach((b) => b.addEventListener('click', () => {
  if (mode === 'ab' && !dest) { setStatus('Click the map to set a destination.', 'err'); return; }
  send(`/api/plan/example/${b.dataset.rider}?${params()}`, { method: 'POST' });
}));

setMode('loop');
health();
