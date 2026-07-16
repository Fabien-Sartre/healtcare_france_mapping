/* =========================================================
   Offre de soins — établissements de santé (FINESS)
   ========================================================= */

let ctrDeck = null;
let ctrData = null;
let ctrCategories = [];

const CTR_PUBLIC  = [30, 75, 58];
const CTR_PRIVATE = [219, 90, 75];

async function initCentersMap() {
    showLoading();
    try {
        ctrData = await fetchJSON('data/etablissements.json');
    } catch (e) {
        console.error('Échec chargement etablissements.json', e);
        hideLoading();
        return;
    }

    ctrCategories = [...new Set(ctrData.map(e => e.ta).filter(Boolean))].sort();
    buildCheckboxList(document.getElementById('filter-ctr-categories'), ctrCategories,
                      () => updateCentersLayer());

    document.querySelectorAll('#filter-ctr-class input[type="checkbox"]')
        .forEach(cb => cb.addEventListener('change', () => updateCentersLayer()));

    ctrDeck = createDeckInstance('map-centers', {
        onHover: ({ object, x, y }) => {
            if (object && object.n) {
                showTooltip(`
                    <div class="tooltip-title">${object.n}</div>
                    <div class="tooltip-row"><span class="tooltip-label">Type</span><span class="tooltip-value">${object.t || '—'}</span></div>
                    <div class="tooltip-row"><span class="tooltip-label">Statut</span><span class="tooltip-value">${object.c}</span></div>
                    <div class="tooltip-row"><span class="tooltip-label">Département</span><span class="tooltip-value">${object.d || '—'}</span></div>
                `, x, y);
            } else { hideTooltip(); }
        }
    });

    updateCentersLayer();
    hideLoading();
}

function updateCentersLayer() {
    if (!ctrDeck || !ctrData) return;

    const selectedClass = [];
    document.querySelectorAll('#filter-ctr-class input[type="checkbox"]:checked')
        .forEach(cb => selectedClass.push(cb.value));
    const selectedCats = getCheckedValues(document.getElementById('filter-ctr-categories'));

    const filtered = ctrData.filter(e =>
        selectedClass.includes(e.c) && selectedCats.includes(e.ta) && e.la && e.lo);

    let publicCount = 0;
    filtered.forEach(e => { if (e.c === 'Public') publicCount++; });
    document.getElementById('kpi-ctr-total').textContent = fmt(filtered.length);
    document.getElementById('kpi-ctr-public').textContent = fmt(publicCount);

    const dots = new deck.ScatterplotLayer({
        id: 'centers-dots',
        data: filtered,
        getPosition: d => [d.lo, d.la],
        getRadius: 2200,
        getFillColor: d => [...(d.c === 'Public' ? CTR_PUBLIC : CTR_PRIVATE), 200],
        getLineColor: [251, 252, 251, 220],
        lineWidthMinPixels: 0.5, stroked: true,
        radiusMinPixels: 2.5, radiusMaxPixels: 9,
        pickable: true, antialiasing: true,
        parameters: { depthTest: false }
    });

    ctrDeck.setProps({ layers: [dots] });
}

window.ctrMap = {
    selectAllCats() {
        setAllCheckboxes(document.getElementById('filter-ctr-categories'), true);
        updateCentersLayer();
    },
    selectNoneCats() {
        setAllCheckboxes(document.getElementById('filter-ctr-categories'), false);
        updateCentersLayer();
    }
};
