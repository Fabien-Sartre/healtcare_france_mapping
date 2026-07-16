/* =========================================================
   Accès aux soins · France — shared application logic
   ========================================================= */

// ---- Data cache ----
const dataCache = {};
async function fetchJSON(url) {
    if (dataCache[url]) return dataCache[url];
    const resp = await fetch(url);
    if (!resp.ok) throw new Error(`Fetch failed: ${url} (${resp.status})`);
    const data = await resp.json();
    dataCache[url] = data;
    return data;
}

// ---- French number formatting ----
function fmt(n, decimals = 0) {
    if (n == null || isNaN(n)) return '—';
    return Number(n).toLocaleString('fr-FR', {
        minimumFractionDigits: decimals,
        maximumFractionDigits: decimals
    });
}
// Compact millions (e.g. 22 107 947 -> "22,1 M")
function fmtCompact(n) {
    if (n == null || isNaN(n)) return '—';
    if (n >= 1e6) return (n / 1e6).toLocaleString('fr-FR', { minimumFractionDigits: 1, maximumFractionDigits: 1 }) + ' M';
    if (n >= 1e3) return Math.round(n / 1e3) + ' k';
    return fmt(n);
}

// ---- Tooltip ----
const tooltipEl = document.getElementById('tooltip');
function showTooltip(html, x, y) {
    tooltipEl.innerHTML = html;
    tooltipEl.classList.add('visible');
    const pad = 14;
    let left = x + pad, top = y + pad;
    const rect = tooltipEl.getBoundingClientRect();
    if (left + rect.width > window.innerWidth - pad) left = x - rect.width - pad;
    if (top + rect.height > window.innerHeight - pad) top = y - rect.height - pad;
    tooltipEl.style.left = left + 'px';
    tooltipEl.style.top = top + 'px';
}
function hideTooltip() { tooltipEl.classList.remove('visible'); }

// ---- Loading ----
const loadingEl = document.getElementById('loading-overlay');
function showLoading() { loadingEl.classList.add('visible'); }
function hideLoading() { loadingEl.classList.remove('visible'); }

// ---- Haversine distance (km) ----
function haversineKm(lat1, lon1, lat2, lon2) {
    const R = 6371, toRad = Math.PI / 180;
    const dLat = (lat2 - lat1) * toRad, dLon = (lon2 - lon1) * toRad;
    const a = Math.sin(dLat / 2) ** 2 +
        Math.cos(lat1 * toRad) * Math.cos(lat2 * toRad) * Math.sin(dLon / 2) ** 2;
    return 2 * R * Math.asin(Math.sqrt(a));
}

// ---- Map defaults ----
const FRANCE_CENTER = { latitude: 46.6, longitude: 2.4 };
const FRANCE_ZOOM = 5.1;
const BASEMAP_STYLE = 'https://basemaps.cartocdn.com/gl/positron-gl-style/style.json';

function createDeckInstance(containerId, options = {}) {
    const { layers = [], viewState = null, onHover = null, onClick = null, controller = true } = options;
    const initialViewState = viewState || {
        longitude: FRANCE_CENTER.longitude, latitude: FRANCE_CENTER.latitude,
        zoom: FRANCE_ZOOM, pitch: 0, bearing: 0
    };
    return new deck.DeckGL({
        container: containerId, mapLib: maplibregl, mapStyle: BASEMAP_STYLE,
        initialViewState, controller, layers,
        onHover: onHover || undefined, onClick: onClick || undefined
    });
}

// ---- Checkbox helpers (centers view) ----
function buildCheckboxList(container, items, onChange, defaultChecked = null) {
    container.innerHTML = '';
    items.forEach(item => {
        const label = document.createElement('label');
        label.className = 'checkbox-item';
        const cb = document.createElement('input');
        cb.type = 'checkbox'; cb.value = item;
        cb.checked = (defaultChecked === null || defaultChecked.includes(item));
        cb.addEventListener('change', onChange);
        label.appendChild(cb);
        label.appendChild(document.createTextNode(' ' + item));
        container.appendChild(label);
    });
}
function getCheckedValues(container) {
    return Array.from(container.querySelectorAll('input[type="checkbox"]:checked')).map(cb => cb.value);
}
function setAllCheckboxes(container, checked) {
    container.querySelectorAll('input[type="checkbox"]').forEach(cb => { cb.checked = checked; });
}

// ---- Tab switching ----
const tabBtns = document.querySelectorAll('.tab-btn');
const panels = document.querySelectorAll('.panel');
const tabInitialized = {};

function switchTab(tabId) {
    tabBtns.forEach(btn => {
        const on = btn.dataset.tab === tabId;
        btn.classList.toggle('active', on);
        btn.setAttribute('aria-selected', on);
    });
    panels.forEach(p => p.classList.toggle('active', p.id === `panel-${tabId}`));
    hideTooltip();

    if (!tabInitialized[tabId]) {
        tabInitialized[tabId] = true;
        if (tabId === 'diagnostic') initAccessMap();
        if (tabId === 'population') initPopulationMap();
        if (tabId === 'offre') initCentersMap();
    } else {
        window.dispatchEvent(new Event('resize'));
    }
}
tabBtns.forEach(btn => btn.addEventListener('click', () => {
    switchTab(btn.dataset.tab);
    history.replaceState(null, '', '#' + btn.dataset.tab);
}));

const VALID_TABS = ['diagnostic', 'population', 'offre', 'methode'];
document.addEventListener('DOMContentLoaded', () => {
    const hash = (location.hash || '').replace('#', '');
    switchTab(VALID_TABS.includes(hash) ? hash : 'diagnostic');
});
