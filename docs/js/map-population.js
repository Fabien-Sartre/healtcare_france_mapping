/* =========================================================
   Population — 3D columns, height & colour = inhabitants
   ========================================================= */

let popDeck = null;

// sequential population ramp (low -> high): single warm family, light -> dark,
// monotonic lightness (CVD-safe), anchored on the site's coral alert colour
const POP_RAMP = [
    { r: 0.0,  c: [242, 237, 224] },   // sand
    { r: 0.25, c: [233, 185, 140] },   // apricot
    { r: 0.5,  c: [219, 90, 75] },     // brand coral
    { r: 0.75, c: [166, 38, 44] },     // dark red
    { r: 1.0,  c: [84, 12, 35] },      // deep maroon
];
function popColor(t) {
    const x = Math.max(0, Math.min(1, t));
    for (let i = 1; i < POP_RAMP.length; i++) {
        if (x <= POP_RAMP[i].r) {
            const a = POP_RAMP[i - 1], b = POP_RAMP[i];
            const k = (x - a.r) / (b.r - a.r);
            return [0, 1, 2].map(j => Math.round(a.c[j] + k * (b.c[j] - a.c[j])));
        }
    }
    return POP_RAMP[POP_RAMP.length - 1].c;
}

async function initPopulationMap() {
    showLoading();
    let layers;
    try {
        layers = await fetchJSON('data/access_layers.json');
    } catch (e) {
        console.error('Échec chargement population', e);
        hideLoading();
        return;
    }
    const pts = layers.points; // [lon, lat, pop]
    const maxPop = pts.reduce((m, p) => Math.max(m, p[2]), 1);

    // colour: population quantile (rank) so the ramp spreads evenly across the
    // skewed distribution — rural teal, towns amber, cities red
    const sorted = pts.map(p => p[2]).sort((a, b) => a - b);
    const quantile = v => {
        let lo = 0, hi = sorted.length;
        while (lo < hi) { const m = (lo + hi) >> 1; if (sorted[m] < v) lo = m + 1; else hi = m; }
        return lo / sorted.length;
    };
    // height: linear, strictly proportional to inhabitants (as the page copy
    // states) — Paris renders its true 2.6× ratio over Lyon; rural texture is
    // carried by colour. Heavily exaggerated scale: at national zoom
    // (~3 km/px) the tallest column is ~200 km.
    const elevationScale = 200000 / maxPop;

    const column = new deck.ColumnLayer({
        id: 'pop-columns',
        data: pts,
        diskResolution: 6,
        radius: 3400,
        extruded: true,
        getPosition: p => [p[0], p[1]],
        getElevation: p => p[2],
        elevationScale,
        // bend the quantile (^1.8) so dark reds stay reserved for the top
        // ~10% of mailles instead of a quarter of the country
        getFillColor: p => [...popColor(Math.pow(quantile(p[2]), 1.8)), 235],
        material: { ambient: 0.75, diffuse: 0.5, shininess: 28 },
        pickable: true,
    });

    popDeck = new deck.DeckGL({
        container: 'map-population',
        mapLib: maplibregl,
        mapStyle: BASEMAP_STYLE,
        initialViewState: {
            longitude: 2.6, latitude: 46.2, zoom: 5.2, pitch: 52, bearing: -12
        },
        controller: true,
        layers: [column],
        onHover: ({ object, x, y }) => {
            if (object) {
                showTooltip(`
                    <div class="tooltip-title">Maille de population</div>
                    <div class="tooltip-row"><span class="tooltip-label">Habitants</span><span class="tooltip-value">${fmt(object[2])}</span></div>
                `, x, y);
            } else { hideTooltip(); }
        }
    });

    hideLoading();
}
