const COLORS = ['#2ea043', '#d29922', '#a371f7'];
const map = L.map('map', { zoomControl: true }).setView([47.95, 11.4], 9);
// Plain OSM tiles: no API key, no signup. For the demo these should be
// pre-cached locally so the map survives venue wifi.
L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
  attribution: '&copy; OpenStreetMap contributors', maxZoom: 18,
}).addTo(map);

const crowdLayer = L.layerGroup().addTo(map);
const routeLayer = L.layerGroup().addTo(map);
let lastPlan = null, selected = 0;

const $ = (id) => document.getElementById(id);
const setStatus = (msg, cls = '') => { const s = $('status'); s.textContent = msg; s.className = 'status ' + cls; };

async function health() {
  try {
    const r = await fetch('/api/health');
    const j = await r.json();
    if (!j.ok) { $('health').textContent = 'no graph — run the precompute'; return; }
    $('health').textContent = `${j.segments.toLocaleString()} segments · ${j.meta.cell_m} m nodes`;
    drawCrowd();
  } catch { $('health').textContent = 'engine offline'; }
}

// Crowd layer: every segment the fleet has ridden, shaded by curviness. This is
// the "what does the data actually cover" view.
async function drawCrowd() {
  try {
    const { segments } = await (await fetch('/api/segments?limit=4000')).json();
    const max = Math.max(...segments.map((s) => s.curviness), 1);
    segments.forEach((s) => {
      const t = Math.min(1, s.curviness / max);
      L.polyline(s.geometry, {
        color: `hsl(${210 - 190 * t}, 70%, ${35 + 20 * t}%)`,
        weight: 1.6, opacity: 0.5,
      }).addTo(crowdLayer);
    });
  } catch (e) { console.warn('crowd layer failed', e); }
}

function bar(v) { return `<div class="bar"><i style="width:${Math.round(v * 100)}%"></i></div>`; }

function renderProfile(p, wx, explain) {
  const t = p.taste, c = p.capability, x = p.context;
  const names = {
    curviness: 'Curviness', leaned_share: 'Time leaned over', speed_mean: 'Speed',
    band_share: '50–120 km/h band', elev_mean: 'Altitude',
  };
  const weights = Object.entries(t.weights).sort((a, b) => b[1] - a[1]).map(([k, v]) => `
    <div class="wrow"><div class="lbl"><span>${names[k] || k}</span>
      <span>${(v * 100).toFixed(0)}%</span></div>${bar(v)}</div>`).join('');

  const basis = c.style_basis_cells
    ? `${c.style_basis_cells} shared places`
    : 'no crowd overlap — conservative default used';

  $('profileBody').innerHTML = `
    <p class="muted">Fitted from <b>${p.n_trips}</b> rides
      (${p.n_points.toLocaleString()} telemetry points).</p>
    <h3 class="muted" style="font-size:11px;margin:10px 0 4px">TASTE — what they seek out</h3>
    ${weights}
    <h3 class="muted" style="font-size:11px;margin:12px 0 4px">CAPABILITY — never overridden</h3>
    <table>
      <tr><td>Style ratio <span class="muted">vs crowd, same roads</span></td>
          <td><b>${c.style_ratio.toFixed(2)}×</b></td></tr>
      <tr><td class="muted">basis</td><td class="muted">${basis}</td></tr>
      <tr><td>Lean p90 / p95</td><td>${c.lean_p90.toFixed(1)}° / ${c.lean_p95.toFixed(1)}°</td></tr>
    </table>
    <h3 class="muted" style="font-size:11px;margin:12px 0 4px">CONTEXT — sets the defaults</h3>
    <table>
      <tr><td>Ride length used</td><td>${Math.round(x.median_ride_minutes)} min</td></tr>
      <tr><td>Usual start hour</td><td>${x.usual_start_hour}:00</td></tr>
      <tr><td>Bike class <span class="muted">inferred</span></td><td>${x.bike_class}</td></tr>
      <tr><td>Total ridden</td><td>${x.total_km.toLocaleString()} km</td></tr>
      <tr><td>Weather</td><td>${wx.available
        ? `${wx.temp_c}°C, ${wx.precip_mm} mm <span class="muted">(${wx.source})</span>`
        : '<span class="muted">unavailable</span>'}</td></tr>
    </table>
    <p class="muted" style="margin-top:10px">
      Start point: ${explain.why_origin}.<br>Duration: ${explain.why_duration}.</p>`;
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
        <span class="chip">fun ${r.kpis.fun}</span>
        <span class="chip">scenic ${r.kpis.scenic}</span>
        <span class="chip">${r.kpis.curviness}°/km</span>
        <span class="chip">${r.kpis.junctions} junctions</span>
        <span class="chip">bearing ${r.bearing}°</span>
      </div>
    </div>`).join('');
  $('routeList').querySelectorAll('.route').forEach((el) =>
    el.addEventListener('click', () => { selected = +el.dataset.i; draw(); }));

  const r = routes[selected], k = r.kpis;
  $('kpis').innerHTML = `
    <table style="margin-top:10px">
      <tr><td>Distance</td><td>${k.km} km</td></tr>
      <tr><td>Moving time <span class="muted">observed speeds</span></td><td>${Math.round(k.minutes)} min</td></tr>
      <tr><td>Curviness</td><td>${k.curviness} °/km</td></tr>
      <tr><td>Time leaned over</td><td>${(k.leaned_share * 100).toFixed(0)}%</td></tr>
      <tr><td>Elevation gain</td><td>${k.elev_gain_m} m</td></tr>
      <tr><td>Avg speed</td><td>${k.avg_speed_kmh} km/h</td></tr>
      <tr><td>Junctions</td><td>${k.junctions}</td></tr>
      <tr><td>Risk <span class="muted">lower is better</span></td><td>${k.risk}</td></tr>
      <tr><td>Growth</td><td>${k.growth}</td></tr>
      <tr><td>Crowd confidence</td><td>${k.confidence}</td></tr>
      <tr><td>Return-leg overlap</td><td>${(r.overlap * 100).toFixed(0)}%</td></tr>
    </table>
    <p class="muted" style="margin-top:8px">Crowd confidence is how much of this score
      comes from measured lean rather than fallback. Low means few riders have been here.</p>`;
  $('routeCard').classList.remove('hidden');
}

function draw() {
  routeLayer.clearLayers();
  if (!lastPlan) return;
  const { routes, origin } = lastPlan;
  routes.forEach((r, i) => {
    const on = i === selected;
    L.polyline(r.coords, {
      color: COLORS[i % 3], weight: on ? 5 : 2.5, opacity: on ? 0.95 : 0.35,
    }).addTo(routeLayer);
  });
  const r = routes[selected];
  L.circleMarker([origin.lat, origin.lon], {
    radius: 7, color: '#fff', weight: 2, fillColor: '#1c69d4', fillOpacity: 1,
  }).bindTooltip('Usual start point (from their own rides)').addTo(routeLayer);
  if (r.turnaround) {
    L.circleMarker([r.turnaround.lat, r.turnaround.lon], {
      radius: 5, color: COLORS[selected % 3], weight: 2, fillOpacity: 0.6,
    }).bindTooltip('Turnaround').addTo(routeLayer);
  }
  map.fitBounds(L.polyline(r.coords).getBounds(), { padding: [30, 30] });
  renderRoutes(routes);
}

async function plan(url, opts) {
  document.querySelectorAll('button').forEach((b) => (b.disabled = true));
  setStatus('Reading telemetry, fitting the profile, searching…');
  try {
    const res = await fetch(url, opts);
    const body = await res.json();
    if (!res.ok) throw new Error(body.detail || res.statusText);
    lastPlan = body; selected = 0;
    renderProfile(body.profile, body.weather, body.explain);
    draw();
    setStatus(`${body.routes.length} route${body.routes.length > 1 ? 's' : ''} from ${body.profile.n_trips} rides.`, 'ok');
  } catch (e) {
    setStatus(String(e.message || e), 'err');
  } finally {
    document.querySelectorAll('button').forEach((b) => (b.disabled = false));
  }
}

$('go').addEventListener('click', () => {
  const files = $('file').files;
  if (!files.length) { setStatus('Pick at least one .csv or .zip first.', 'err'); return; }
  const fd = new FormData();
  for (const f of files) fd.append('files', f);
  plan('/api/plan/upload', { method: 'POST', body: fd });
});

document.querySelectorAll('.ex').forEach((b) =>
  b.addEventListener('click', () => plan(`/api/plan/example/${b.dataset.rider}`, { method: 'POST' })));

health();
