/* =========================================================
   Diagnostic — local density of practitioners + priority basins
   ========================================================= */

let accDeck = null;
let accLayers = null;      // access_layers.json
let accBasins = null;      // priority_basins.json
let accOpp = null;         // opportunity.json
let accProf = 'Médecin';
let accMode = 'by_population';

// map pin (teardrop) for the studied location
const SITE_PIN_URL = 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(
    `<svg xmlns="http://www.w3.org/2000/svg" width="48" height="64" viewBox="0 0 24 32">
        <path d="M12 1C6.2 1 1.5 5.6 1.5 11.4 1.5 19.4 12 31 12 31s10.5-11.6 10.5-19.6C22.5 5.6 17.8 1 12 1z"
              fill="#1E4B3A" stroke="#FBFCFB" stroke-width="1.6"/>
        <circle cx="12" cy="11.5" r="4" fill="#FBFCFB"/>
    </svg>`);

// diverging ramp: ratio (local density / national) in [0,2]
const ACC_RAMP = [
    { r: 0.0, c: [192, 57, 43] },
    { r: 0.5, c: [224, 138, 107] },
    { r: 1.0, c: [222, 222, 205] },
    { r: 1.5, c: [111, 167, 144] },
    { r: 2.0, c: [30, 75, 58] },
];
function rampColor(ratio) {
    const x = Math.max(0, Math.min(2, ratio));
    for (let i = 1; i < ACC_RAMP.length; i++) {
        if (x <= ACC_RAMP[i].r) {
            const a = ACC_RAMP[i - 1], b = ACC_RAMP[i];
            const t = (x - a.r) / (b.r - a.r);
            return [0, 1, 2].map(k => Math.round(a.c[k] + t * (b.c[k] - a.c[k])));
        }
    }
    return ACC_RAMP[ACC_RAMP.length - 1].c;
}

async function initAccessMap() {
    showLoading();
    try {
        [accLayers, accBasins, accOpp] = await Promise.all([
            fetchJSON('data/access_layers.json'),
            fetchJSON('data/priority_basins.json'),
            fetchJSON('data/opportunity.json'),
        ]);
    } catch (e) {
        console.error('Échec chargement diagnostic', e);
        hideLoading();
        return;
    }

    buildProfessionButtons();

    accDeck = createDeckInstance('map-access', {
        onClick: (info) => { if (info && info.coordinate) studyPoint(info.coordinate[0], info.coordinate[1]); },
        onHover: ({ object, x, y, layer }) => {
            if (object && layer && layer.id === 'basin-badges') {
                showTooltip(`
                    <div class="tooltip-title">Zone prioritaire n°${object.rank}</div>
                    <div class="tooltip-row"><span class="tooltip-label">Commune</span><span class="tooltip-value">${object.name} · ${object.dn}</span></div>
                    <div class="tooltip-row"><span class="tooltip-label">Hab. en faible densité</span><span class="tooltip-value">${fmt(object.underserved_pop)}</span></div>
                    <div class="tooltip-row"><span class="tooltip-label">Praticiens manquants</span><span class="tooltip-value">${object.missing == null ? '—' : fmt(object.missing)}</span></div>
                `, x, y);
            } else if (object && layer && layer.id === 'density') {
                const dens = object[3];
                const nat = accLayers.national_density[accProf];
                const pct = nat > 0 ? Math.round((dens / nat) * 100) : 0;
                const cls = pct < 70 ? 'low' : 'ok';
                showTooltip(`
                    <div class="tooltip-title">Densité locale</div>
                    <div class="tooltip-row"><span class="tooltip-label">${accProf}</span><span class="tooltip-value">${fmt(dens)} /100k</span></div>
                    <div class="tooltip-row"><span class="tooltip-label">vs moyenne nat.</span><span class="tooltip-badge ${cls}">${pct} %</span></div>
                    <div class="tooltip-row"><span class="tooltip-label">Population (maille)</span><span class="tooltip-value">${fmt(object[2])}</span></div>
                `, x, y);
            } else { hideTooltip(); }
        }
    });

    document.querySelectorAll('#basins-toggle button').forEach(btn => {
        btn.addEventListener('click', () => {
            accMode = btn.dataset.mode;
            document.querySelectorAll('#basins-toggle button').forEach(b => b.classList.toggle('on', b === btn));
            renderBasins();
            renderLayers();
        });
    });

    document.getElementById('site-radius').addEventListener('input', (e) => {
        document.getElementById('site-radius-val').textContent = e.target.value;
        if (accSite) computeSite();
    });
    document.getElementById('site-close').addEventListener('click', () => {
        accSite = null;
        document.getElementById('site-panel').hidden = true;
        renderLayers();
    });

    setProfession('Médecin');
    hideLoading();
}

// ---- Site study (drop a point) ----
let accSite = null;        // {lon, lat}
let siteCommunes = null;   // [{la, lo, d, counts}]
let siteDeptStats = null;

async function loadSiteData() {
    if (siteCommunes) return;
    const [cp, ds] = await Promise.all([
        fetchJSON('data/communes_professions.json'),
        fetchJSON('data/dept_stats.json'),
    ]);
    siteDeptStats = ds;
    siteCommunes = Object.keys(cp.communes).map(code => {
        const c = cp.communes[code];
        return { n: c.n, la: c.la, lo: c.lo, d: c.d, dn: c.dn, counts: cp.counts[code] || {} };
    });
}

async function studyPoint(lon, lat) {
    accSite = { lon, lat };
    document.getElementById('site-panel').hidden = false;
    document.getElementById('site-readout').innerHTML = '<div class="muted">Calcul…</div>';
    renderLayers();
    await loadSiteData();
    computeSite();
}

function computeSite() {
    if (!accSite || !siteCommunes) return;
    const R = parseInt(document.getElementById('site-radius').value);
    const { lon, lat } = accSite;
    const prof = accProf;

    // population within radius
    let pop = 0;
    for (const p of accLayers.points) {
        if (haversineKm(lat, lon, p[1], p[0]) <= R) pop += p[2];
    }
    // practitioners within radius + nearest commune (for department)
    let pros = 0, nearest = null, nd = Infinity;
    for (const c of siteCommunes) {
        const dk = haversineKm(lat, lon, c.la, c.lo);
        if (dk <= R) pros += (c.counts[prof] || 0);
        if (dk < nd) { nd = dk; nearest = c; }
    }

    const natDens = accLayers.national_density[prof];          // /100k
    const natRatio = natDens > 0 ? Math.round(1e5 / natDens) : null;
    let deptRatio = null, deptName = nearest ? nearest.dn : '—';
    const ds = nearest && siteDeptStats[nearest.d];
    if (ds && ds.pop > 0) {
        const dDens = ((ds.pros[prof] || 0) / ds.pop) * 1e5;
        deptRatio = dDens > 0 ? Math.round(1e5 / dDens) : null;
    }

    document.getElementById('site-place').textContent =
        nearest ? `${nearest.n} · ${deptName}` : deptName;

    // practitioners to add to bring the radius back to the low-density threshold
    const thresh = accLayers.underserved_ratio || 0.7;
    const need = natDens > 0 ? Math.max(0, Math.ceil(thresh * natDens * pop / 1e5 - pros)) : 0;
    const needTxt = need > 0
        ? `Il manque ${fmt(need)} ${prof.toLowerCase()}${need > 1 ? 's' : ''} pour repasser le seuil de faible densité. `
        : '';

    let html;
    if (pros === 0) {
        html = `<div class="site-headline"><span class="big warn">Aucun ${prof.toLowerCase()}</span>
            dans un rayon de ${R} km — pour ${fmt(pop)} habitants.</div>
            <div class="site-note">${needTxt}</div>`;
    } else {
        const ratio = Math.round(pop / pros);
        const after = Math.round(pop / (pros + 1));
        const vsNat = natRatio ? (ratio / natRatio) : null;
        html = `<div class="site-headline">
                <span class="big">1 ${prof.toLowerCase()} pour ${fmt(ratio)}</span> habitants
                — ${fmt(pros)} ${prof.toLowerCase()}${pros > 1 ? 's' : ''} pour ${fmt(pop)} hab. dans ${R} km.
            </div>
            <div class="site-cmp">
                <div class="row"><span class="k">Moyenne nationale</span><span class="v">1 / ${fmt(natRatio)}</span></div>
                <div class="row"><span class="k">Moyenne ${deptName}</span><span class="v">${deptRatio ? '1 / ' + fmt(deptRatio) : '—'}</span></div>
            </div>
            <div class="site-note">${cmpSentence(vsNat)} ${needTxt}<b>En vous installant : 1 / ${fmt(after)}.</b></div>`;
    }
    document.getElementById('site-readout').innerHTML = html;
}

function cmpSentence(vsNat) {
    if (!vsNat) return '';
    const x = n => n.toFixed(1).replace('.', ',');
    if (vsNat >= 1.15) return `Soit ${x(vsNat)}× plus d'habitants par praticien qu'en moyenne nationale : offre tendue.`;
    if (vsNat <= 0.85) return `Soit ${x(1 / vsNat)}× moins d'habitants par praticien qu'en moyenne nationale : offre dense.`;
    return `Proche de la moyenne nationale.`;
}

function buildProfessionButtons() {
    const box = document.getElementById('pro-buttons');
    box.innerHTML = '';
    accLayers.professions.forEach(p => {
        const b = document.createElement('button');
        b.className = 'pro' + (p === accProf ? ' on' : '');
        b.textContent = p;
        b.addEventListener('click', () => setProfession(p));
        box.appendChild(b);
    });
}

function setProfession(prof) {
    accProf = prof;
    document.querySelectorAll('#pro-buttons .pro').forEach(b => b.classList.toggle('on', b.textContent === prof));

    // hero figures
    const o = accOpp.professions[prof];
    document.getElementById('fig-pop').innerHTML = fmtCompact(o.underserved_pop).replace(/ (M|k)$/, '<small>$1</small>');
    document.getElementById('fig-pop-lbl').textContent = `habitants exposés à une faible densité (${prof.toLowerCase()})`;
    document.getElementById('fig-pct').innerHTML = fmt(o.underserved_pct, 1) + '<small>%</small>';
    document.getElementById('fig-nat').textContent = fmt(Math.round(o.national_density));

    // titles
    document.getElementById('ov-title').textContent = `Densité locale · ${prof}`;
    document.getElementById('basins-sub').textContent =
        `Communes classées par déficit d'offre — ${prof.toLowerCase()}, rayon de 15 km.`;

    renderBasins();
    renderLayers();
    computeSite();
}

function renderLayers() {
    if (!accDeck) return;
    const dens = accLayers.density[accProf];
    const nat = accLayers.national_density[accProf];
    const pts = accLayers.points;

    const data = pts.map((p, i) => [p[0], p[1], p[2], dens[i]]);

    const density = new deck.ScatterplotLayer({
        id: 'density',
        data,
        getPosition: d => [d[0], d[1]],
        getFillColor: d => [...rampColor(nat > 0 ? d[3] / nat : 0), 165],
        getRadius: d => Math.sqrt(d[2]) * 26,
        radiusUnits: 'meters',
        radiusMinPixels: 1.5,
        radiusMaxPixels: 20,
        pickable: true,
        updateTriggers: { getFillColor: [accProf] }
    });

    // priority basins — numbered badges matching the ranking table
    const top = (accBasins.basins[accProf]?.[accMode] || [])
        .map((b, i) => ({ ...b, rank: i + 1 }));
    const badgeDots = new deck.ScatterplotLayer({
        id: 'basin-badges',
        data: top,
        getPosition: d => [d.lon, d.lat],
        getRadius: 11, radiusUnits: 'pixels',
        getFillColor: [178, 58, 46, 240],
        stroked: true, getLineColor: [251, 252, 251, 255], lineWidthMinPixels: 1.5,
        pickable: true,
        parameters: { depthTest: false }
    });
    const badgeNums = new deck.TextLayer({
        id: 'basin-badge-nums',
        data: top,
        getPosition: d => [d.lon, d.lat],
        getText: d => String(d.rank),
        getSize: 13, getColor: [251, 252, 251],
        fontWeight: 700, sizeUnits: 'pixels',
        pickable: false
    });

    const layers = [density, badgeDots, badgeNums];
    if (accSite) {
        const R = parseInt(document.getElementById('site-radius').value) * 1000;
        layers.push(new deck.ScatterplotLayer({
            id: 'site-catchment', data: [accSite],
            getPosition: d => [d.lon, d.lat], getRadius: R, radiusUnits: 'meters',
            getFillColor: [30, 75, 58, 28], stroked: true, getLineColor: [30, 75, 58, 200],
            getLineWidth: 1.5, lineWidthUnits: 'pixels',
        }));
        layers.push(new deck.IconLayer({
            id: 'site-pin', data: [accSite],
            getPosition: d => [d.lon, d.lat],
            getIcon: () => ({ url: SITE_PIN_URL, width: 48, height: 64, anchorY: 64 }),
            getSize: 38, sizeUnits: 'pixels',
        }));
    }
    accDeck.setProps({ layers });
}

function renderBasins() {
    const rows = accBasins.basins[accProf]?.[accMode] || [];
    const body = document.getElementById('basins-body');
    body.innerHTML = '';
    rows.forEach((b, i) => {
        const tr = document.createElement('tr');
        tr.innerHTML = `
            <td class="rank">${String(i + 1).padStart(2, '0')}</td>
            <td>${b.name} <span class="dept">· ${b.dn}</span></td>
            <td class="muted">${fmt(b.catchment_pop)}</td>
            <td class="big">${fmt(b.underserved_pop)}</td>
            <td>${b.missing == null ? '—' : fmt(b.missing)}</td>
            <td class="muted">${b.density == null ? '—' : fmt(b.density)}</td>`;
        tr.addEventListener('click', () => flyToBasin(b, tr));
        body.appendChild(tr);
    });
}

function flyToBasin(b, tr) {
    document.querySelectorAll('#basins-body tr').forEach(r => r.classList.toggle('sel', r === tr));
    accDeck.setProps({
        initialViewState: {
            longitude: b.lon, latitude: b.lat, zoom: 8.6, pitch: 0, bearing: 0,
            transitionDuration: 1100, transitionInterpolator: new deck.FlyToInterpolator()
        }
    });
    studyPoint(b.lon, b.lat);
    document.getElementById('map-access').scrollIntoView({ behavior: 'smooth', block: 'center' });
}
