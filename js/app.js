const RADIUS_KM = 80.4672;
const KM_PER_MI = 1.609344;
const COLORS = { strand: "#0A8F99", jacket: "#F0B400", ink: "#17212B", contract: "#A23B72" };
const STATUS = {
  open: "Open for bids",
  proposed: "Proposed",
  provisional: "Provisional award",
  awarded: "Awarded",
  construction: "Under construction",
  complete: "Built",
  defaulted: "Defaulted"
};

const S = {
  manifest: null,
  map: null,
  layers: {},
  fine: new Map(),
  coarse: new Map(),
  contracts: new Map(),
  search: 0
};

const $ = id => document.getElementById(id);

function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
}

function esc(s) {
  return String(s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#39;" })[c]);
}

async function getJSON(url) {
  try {
    const r = await fetch(url, { cache: "no-cache" });
    return r.ok ? await r.json() : null;
  } catch {
    return null;
  }
}

function fmtSpeed(mbps) {
  if (!mbps) return "speed not listed";
  if (mbps >= 1000) {
    const g = mbps / 1000;
    return `${Number.isInteger(g) ? g : g.toFixed(1)} Gig`;
  }
  return `${mbps} Mbps`;
}

const fmtInt = n => Math.round(n).toLocaleString("en-US");
const fmtMoney = n => "$" + Math.round(n).toLocaleString("en-US");

function haversineKm(a, b) {
  const R = 6371.0088;
  const rad = Math.PI / 180;
  const dLat = (b[0] - a[0]) * rad;
  const dLng = (b[1] - a[1]) * rad;
  const s = Math.sin(dLat / 2) ** 2 + Math.cos(a[0] * rad) * Math.cos(b[0] * rad) * Math.sin(dLng / 2) ** 2;
  return 2 * R * Math.asin(Math.sqrt(s));
}

function inRing(pt, ring) {
  let inside = false;
  for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
    const [xi, yi] = ring[i];
    const [xj, yj] = ring[j];
    if ((yi > pt[1]) !== (yj > pt[1]) && pt[0] < ((xj - xi) * (pt[1] - yi)) / (yj - yi) + xi) inside = !inside;
  }
  return inside;
}

function geomDistanceKm(geom, latlng) {
  const pt = [latlng[1], latlng[0]];
  const polys = geom.type === "Polygon" ? [geom.coordinates] : geom.type === "MultiPolygon" ? geom.coordinates : [];
  if (polys.some(p => inRing(pt, p[0]) && !p.slice(1).some(h => inRing(pt, h)))) return 0;
  let best = Infinity;
  const visit = c => {
    if (typeof c[0] === "number") best = Math.min(best, haversineKm(latlng, [c[1], c[0]]));
    else c.forEach(visit);
  };
  visit(geom.coordinates);
  return best;
}

function initMap() {
  const map = L.map("map", { preferCanvas: true }).setView([34.3, -111.7], 6);
  const esri = "https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/";
  L.tileLayer(esri + "World_Light_Gray_Base/MapServer/tile/{z}/{y}/{x}", {
    maxZoom: 16,
    attribution: "Tiles &copy; Esri, HERE, Garmin, &copy; OpenStreetMap contributors"
  }).addTo(map);
  const labels = map.createPane("labels");
  labels.style.zIndex = 450;
  labels.style.pointerEvents = "none";
  L.tileLayer(esri + "World_Light_Gray_Reference/MapServer/tile/{z}/{y}/{x}", { maxZoom: 16, pane: "labels" }).addTo(map);
  S.layers.fiber = L.layerGroup().addTo(map);
  S.layers.contracts = L.layerGroup().addTo(map);
  S.layers.focus = L.layerGroup().addTo(map);
  const legend = L.control({ position: "bottomright" });
  legend.onAdd = () => {
    const d = L.DomUtil.create("div", "legend");
    d.innerHTML =
      `<div><i style="background:${COLORS.strand};opacity:.55"></i>Fiber reported</div>` +
      `<div><i style="background:${COLORS.contract};opacity:.55"></i>Contract or bid area</div>` +
      `<div><i style="border:2px dashed ${COLORS.ink}"></i>50 miles</div>`;
    return d;
  };
  legend.addTo(map);
  S.map = map;
}

async function geocodeEsri(q) {
  const url = "https://geocode.arcgis.com/arcgis/rest/services/World/GeocodeServer/findAddressCandidates?" + new URLSearchParams({
    SingleLine: q,
    f: "json",
    outFields: "Match_addr,RegionAbbr,Subregion",
    sourceCountry: "USA",
    maxLocations: "1",
    outSR: "4326"
  });
  try {
    const r = await fetch(url);
    if (!r.ok) return null;
    const d = await r.json();
    const c = (d.candidates || [])[0];
    if (!c || c.score < 80) return null;
    const a = c.attributes || {};
    return {
      lat: c.location.y,
      lng: c.location.x,
      label: a.Match_addr || c.address,
      state: a.RegionAbbr || null,
      county: (a.Subregion || "").replace(/\s+County$/i, "").trim()
    };
  } catch {
    return null;
  }
}

async function geocodeOsm(q) {
  const url = "https://nominatim.openstreetmap.org/search?" + new URLSearchParams({
    q,
    format: "jsonv2",
    addressdetails: "1",
    limit: "1",
    countrycodes: "us"
  });
  try {
    const r = await fetch(url, { headers: { "Accept-Language": "en" } });
    if (!r.ok) return null;
    const list = await r.json();
    if (!list.length) return null;
    const hit = list[0];
    const a = hit.address || {};
    const iso = a["ISO3166-2-lvl4"] || "";
    return {
      lat: Number(hit.lat),
      lng: Number(hit.lon),
      label: hit.display_name.split(",").slice(0, 4).join(",").trim(),
      state: iso.startsWith("US-") ? iso.slice(3) : null,
      county: (a.county || "").replace(/\s+County$/i, "").trim()
    };
  } catch {
    return null;
  }
}

async function geocode(q) {
  return (await geocodeEsri(q)) || (await geocodeOsm(q));
}

function shard(cache, dir, id) {
  if (!cache.has(id)) cache.set(id, getJSON(`data/fiber/${dir}/${id}.json`));
  return cache.get(id);
}

async function fiberAt(lat, lng) {
  const m = S.manifest;
  const cell = h3.latLngToCell(lat, lng, m.fineRes);
  const ring = h3.gridDisk(cell, 1);
  const ids = [...new Set(ring.map(c => h3.cellToParent(c, m.fineShardRes)))];
  const docs = new Map(await Promise.all(ids.map(async id => [id, await shard(S.fine, "r8", id)])));
  const read = c => {
    const d = docs.get(h3.cellToParent(c, m.fineShardRes));
    const v = d && d.c[c];
    if (!v) return null;
    return { cell: c, count: v[0], providers: v[1].map(([i, down, up]) => ({ name: d.p[i], down, up })) };
  };
  return { cell, own: read(cell), near: ring.filter(c => c !== cell).map(read).filter(Boolean) };
}

async function fiberRegion(lat, lng) {
  const m = S.manifest;
  const ids = h3.gridDisk(h3.latLngToCell(lat, lng, m.coarseShardRes), 2);
  const docs = await Promise.all(ids.map(id => shard(S.coarse, "r6", id)));
  const cells = [];
  const totals = new Map();
  for (const d of docs) {
    if (!d) continue;
    for (const [c, v] of Object.entries(d.c)) {
      if (haversineKm([lat, lng], h3.cellToLatLng(c)) > RADIUS_KM) continue;
      const names = v[1].map(i => d.p[i]);
      cells.push({ cell: c, count: v[0], names });
      for (const n of names) totals.set(n, (totals.get(n) || 0) + 1);
    }
  }
  return { cells, providers: [...totals.entries()].sort((a, b) => b[1] - a[1]) };
}

async function contractsNear(place) {
  const have = new Set(S.manifest.contractStates || []);
  const states = [...new Set([place.state, ...(S.manifest.contractGeoStates || [])])].filter(st => have.has(st));
  const docs = await Promise.all(states.map(async st => {
    if (!S.contracts.has(st)) S.contracts.set(st, getJSON(`data/contracts/${st}.json`));
    return [st, await S.contracts.get(st)];
  }));
  const items = [];
  const programs = [];
  const county = place.county.toLowerCase();
  for (const [st, d] of docs) {
    if (!d) continue;
    const home = st === place.state;
    if (home) programs.push(...(d.programs || []));
    for (const it of d.items || []) {
      let where = null;
      let distance = null;
      if (it.geometry) {
        const km = geomDistanceKm(it.geometry, [place.lat, place.lng]);
        if (km <= RADIUS_KM) {
          distance = km;
          where = km === 0 ? "Covers this address" : `${Math.max(1, Math.round(km / KM_PER_MI))} mi away`;
        }
      }
      if (!where && home && county && (it.counties || []).some(c => c.toLowerCase() === county)) where = `${place.county} County`;
      if (!where && home && it.scope === "statewide") where = "Statewide";
      if (where) items.push({ ...it, state: st, where, distance });
    }
  }
  const rank = it => (it.distance != null ? it.distance : it.where === "Statewide" ? 1e6 : 1e5);
  items.sort((a, b) => (a.status === "open" ? 0 : 1) - (b.status === "open" ? 0 : 1) || rank(a) - rank(b));
  return { items, programs };
}

function drawFocus(place, at) {
  const g = S.layers.focus;
  g.clearLayers();
  L.circle([place.lat, place.lng], {
    radius: RADIUS_KM * 1000,
    color: COLORS.ink,
    weight: 1.5,
    dashArray: "6 6",
    fill: false,
    interactive: false
  }).addTo(g);
  if (at) {
    L.polygon(h3.cellToBoundary(at.cell), { color: COLORS.jacket, weight: 2, fill: false, interactive: false }).addTo(g);
  }
  L.marker([place.lat, place.lng], {
    icon: L.divIcon({ className: "", html: "<div class=\"pin\"></div>", iconSize: [18, 18], iconAnchor: [9, 9] }),
    title: place.label,
    keyboard: false
  }).addTo(g);
}

function drawRegion(region) {
  const g = S.layers.fiber;
  g.clearLayers();
  if (!region) return;
  const max = region.cells.reduce((m, c) => Math.max(m, c.count), 1);
  for (const c of region.cells) {
    const t = Math.log1p(c.count) / Math.log1p(max);
    const names = c.names.slice(0, 4).map(esc).join(", ") + (c.names.length > 4 ? `, +${c.names.length - 4} more` : "");
    L.polygon(h3.cellToBoundary(c.cell), {
      color: COLORS.strand,
      weight: 0.5,
      opacity: 0.4,
      fillColor: COLORS.strand,
      fillOpacity: 0.12 + 0.5 * t
    }).bindTooltip(`<strong>${fmtInt(c.count)} fiber locations</strong><br>${names}`, { sticky: true }).addTo(g);
  }
}

function drawContracts(items) {
  const g = S.layers.contracts;
  g.clearLayers();
  for (const it of items) {
    if (!it.geometry) continue;
    const color = it.status === "open" ? COLORS.jacket : COLORS.contract;
    L.geoJSON(it.geometry, {
      style: { color, weight: 2, fillColor: color, fillOpacity: 0.12 },
      pointToLayer: (f, ll) => L.circleMarker(ll, { radius: 7, color, weight: 2, fillColor: color, fillOpacity: 0.5 })
    }).bindTooltip(`<strong>${esc(it.awardee || "Open bid")}</strong><br>${esc(it.program || "")}`, { sticky: true }).addTo(g);
  }
}

async function countyShape(lat, lng) {
  const url = "https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/State_County/MapServer/1/query?" + new URLSearchParams({
    geometry: `${lng},${lat}`,
    geometryType: "esriGeometryPoint",
    inSR: "4326",
    spatialRel: "esriSpatialRelIntersects",
    outFields: "NAME",
    returnGeometry: "true",
    outSR: "4326",
    maxAllowableOffset: "0.002",
    f: "geojson"
  });
  try {
    const r = await fetch(url);
    if (!r.ok) return null;
    const d = await r.json();
    const f = (d.features || [])[0];
    return f && f.geometry ? f : null;
  } catch {
    return null;
  }
}

function drawCounty(feature, items) {
  const color = items.some(it => it.status === "open") ? COLORS.jacket : COLORS.contract;
  L.geoJSON(feature, {
    interactive: false,
    style: { color, weight: 2, dashArray: "5 5", fillColor: color, fillOpacity: 0.08 }
  }).addTo(S.layers.contracts);
}

function verdictFor(place, covered, at, region) {
  if (!covered) {
    const list = S.manifest.states.length ? S.manifest.states.join(", ") : "no states yet";
    return {
      kind: "unknown",
      title: "No fiber data for this state yet",
      text: `Fiber data currently covers ${list}. Add ${place.state || "this state"} to the data build to check this address.`
    };
  }
  if (at.own) {
    return {
      kind: "yes",
      title: "Fiber is available here",
      text: `Reported at ${fmtInt(at.own.count)} ${at.own.count === 1 ? "location" : "locations"} in the quarter square mile around this address.`
    };
  }
  if (at.near.length) {
    const n = at.near.reduce((s, c) => s + c.count, 0);
    return {
      kind: "near",
      title: "Fiber is close by",
      text: `Not reported on this block, but reported at ${fmtInt(n)} ${n === 1 ? "location" : "locations"} within about half a mile.`
    };
  }
  return {
    kind: "no",
    title: "No fiber reported here",
    text: region && region.cells.length ? "Fiber is reported elsewhere within 50 miles. See the map." : "No fiber is reported within 50 miles of this address."
  };
}

function mergeProviders(cells) {
  const m = new Map();
  for (const c of cells) {
    for (const p of c.providers) {
      const e = m.get(p.name);
      if (!e) m.set(p.name, { ...p });
      else {
        e.down = Math.max(e.down, p.down);
        e.up = Math.max(e.up, p.up);
      }
    }
  }
  return [...m.values()].sort((a, b) => b.down - a.down || a.name.localeCompare(b.name));
}

function providerList(providers) {
  const ul = el("ul", "list");
  for (const p of providers) {
    const li = el("li");
    li.append(el("span", null, p.name), el("span", "sub", `${fmtSpeed(p.down)} down, ${fmtSpeed(p.up)} up`));
    ul.append(li);
  }
  return ul;
}

function contractCard(it) {
  const card = el("article", "contract" + (it.status === "open" ? " open" : ""));
  card.append(el("h4", null, it.awardee || it.title || "Open bid"));
  const facts = el("div", "facts");
  const badge = el("span", "badge" + (it.status === "open" ? " open" : ""), STATUS[it.status] || it.status || "Listed");
  facts.append(badge, el("span", null, it.where));
  if (it.program) facts.append(el("span", null, it.program));
  if (it.technology) facts.append(el("span", null, it.technology));
  if (it.amount != null) facts.append(el("span", null, fmtMoney(it.amount)));
  if (it.locations != null) facts.append(el("span", null, `${fmtInt(it.locations)} locations`));
  if (it.deadline) facts.append(el("span", null, `Bids due ${it.deadline}`));
  card.append(facts);
  if (it.notes) card.append(el("p", null, it.notes));
  if (it.source) {
    const p = el("p");
    const a = el("a", null, "Source");
    a.href = it.source;
    a.target = "_blank";
    a.rel = "noopener";
    p.append(a);
    if (it.updated) p.append(` (as of ${it.updated})`);
    card.append(p);
  }
  return card;
}

function statewideList(items) {
  const shown = 6;
  const wrap = el("div", "statewide");
  wrap.append(el("h4", null, `Statewide awards (${items.length})`));
  const provisional = items.every(it => it.status === "provisional");
  wrap.append(el("p", "note", `${provisional ? "Provisional awards" : "Awards"} across the state. Project areas are not mapped here yet, so these may not cover this address.`));
  const ul = el("ul", "list");
  items.forEach((it, i) => {
    const li = el("li");
    if (i >= shown) li.hidden = true;
    const parts = [];
    if (it.amount) parts.push(fmtMoney(it.amount));
    if (it.technology) parts.push(it.technology);
    if (it.locations) parts.push(`${fmtInt(it.locations)} locations`);
    li.append(el("span", null, it.awardee || it.title), el("span", "sub", parts.join(", ")));
    ul.append(li);
  });
  wrap.append(ul);
  if (items.length > shown) {
    const b = el("button", "more", `Show all ${items.length}`);
    b.type = "button";
    b.addEventListener("click", () => {
      ul.querySelectorAll("li[hidden]").forEach(li => { li.hidden = false; });
      b.remove();
    });
    wrap.append(b);
  }
  const src = items.find(it => it.source);
  if (src) {
    const p = el("p", "note");
    const a = el("a", null, "Source");
    a.href = src.source;
    a.target = "_blank";
    a.rel = "noopener";
    p.append(a);
    if (src.updated) p.append(` (as of ${src.updated})`);
    wrap.append(p);
  }
  return wrap;
}

function render(place, covered, at, region, deals) {
  const root = $("result");
  root.replaceChildren();

  const v = verdictFor(place, covered, at, region);
  const vb = el("section", `verdict ${v.kind}`);
  vb.append(el("p", "address", place.label), el("h2", null, v.title), el("p", null, v.text), el("div", "strand"));
  root.append(vb);

  if (covered && (at.own || at.near.length)) {
    const b = el("section", "block");
    b.append(el("h3", null, at.own ? "Providers at this address" : "Providers close by"));
    b.append(providerList(mergeProviders(at.own ? [at.own] : at.near)));
    root.append(b);
  }

  if (covered) {
    const b = el("section", "block");
    b.append(el("h3", null, "Fiber within 50 miles"));
    if (region.cells.length) {
      b.append(el("p", "note", `Fiber is reported in ${fmtInt(region.cells.length)} of the map areas within 50 miles.`));
      const ul = el("ul", "list");
      for (const [name, n] of region.providers.slice(0, 8)) {
        const li = el("li");
        li.append(el("span", null, name), el("span", "sub", `${fmtInt(n)} ${n === 1 ? "area" : "areas"}`));
        ul.append(li);
      }
      b.append(ul);
      if (region.providers.length > 8) b.append(el("p", "note", `Plus ${region.providers.length - 8} more providers. Hover the map for details.`));
    } else {
      b.append(el("p", "note", "No fiber reported within 50 miles."));
    }
    root.append(b);
  }

  const cb = el("section", "block");
  cb.append(el("h3", null, "Contracts and bids"));
  const local = deals.items.filter(it => it.where !== "Statewide");
  const wide = deals.items.filter(it => it.where === "Statewide").sort((a, b) => (b.amount || 0) - (a.amount || 0));
  local.forEach(it => cb.append(contractCard(it)));
  if (wide.length) cb.append(statewideList(wide));
  if (!local.length && !wide.length) cb.append(el("p", "note", "No broadband contracts or open bids are listed near this address."));
  for (const p of deals.programs) {
    const n = el("p", "note");
    n.append(`${p.name}: ${p.summary} `);
    if (p.url) {
      const a = el("a", null, p.office || "Program page");
      a.href = p.url;
      a.target = "_blank";
      a.rel = "noopener";
      n.append(a);
    }
    cb.append(n);
  }
  root.append(cb);
}

function renderError(msg) {
  const root = $("result");
  root.replaceChildren(el("p", "error", msg));
}

function renderMeta() {
  const m = S.manifest;
  const f = $("meta");
  f.replaceChildren();
  const p1 = el("p");
  if (m.asOf) {
    p1.append(`Fiber data: FCC National Broadband Map, as of ${m.asOf}. Providers report their own coverage, so confirm with the provider before you sign up. `);
  } else {
    p1.append("Fiber data has not been built yet. Run the Update fiber data workflow. ");
  }
  const a = el("a", null, "Check the FCC map");
  a.href = "https://broadbandmap.fcc.gov/";
  a.target = "_blank";
  a.rel = "noopener";
  p1.append(a);
  f.append(p1);
  f.append(el("p", null, "Address search by Esri, with OpenStreetMap Nominatim as backup."));
}

async function check(q) {
  const token = ++S.search;
  const btn = $("go");
  btn.disabled = true;
  btn.textContent = "Checking...";
  try {
    const place = await geocode(q);
    if (!place) {
      renderError("No match for that address. Add the city and state, then try again.");
      return;
    }
    const covered = S.manifest.states.includes(place.state);
    const [at, region, deals] = await Promise.all([
      covered ? fiberAt(place.lat, place.lng) : null,
      covered ? fiberRegion(place.lat, place.lng) : null,
      contractsNear(place)
    ]);
    drawRegion(region);
    drawContracts(deals.items);
    const countyItems = deals.items.filter(it => !it.geometry && place.county && it.where === `${place.county} County`);
    if (countyItems.length) {
      countyShape(place.lat, place.lng).then(f => {
        if (f && token === S.search) drawCounty(f, countyItems);
      });
    }
    drawFocus(place, at);
    S.map.fitBounds(L.latLng(place.lat, place.lng).toBounds(RADIUS_KM * 2000), { padding: [10, 10] });
    render(place, covered, at, region, deals);
    const url = new URL(location.href);
    url.searchParams.set("q", q);
    history.replaceState(null, "", url);
  } catch (e) {
    renderError(e.message || "Something went wrong loading the data. Reload the page and try again.");
  } finally {
    btn.disabled = false;
    btn.textContent = "Check address";
  }
}

async function main() {
  initMap();
  S.manifest = (await getJSON("data/manifest.json")) || { states: [], contractStates: [], fineRes: 8, fineShardRes: 5, coarseRes: 6, coarseShardRes: 3 };
  renderMeta();
  $("search").addEventListener("submit", e => {
    e.preventDefault();
    const q = $("address").value.trim();
    if (q) check(q);
  });
  const q = new URLSearchParams(location.search).get("q");
  if (q) {
    $("address").value = q;
    check(q);
  }
}

main();
