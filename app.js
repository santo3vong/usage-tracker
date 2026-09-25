/* ==========================================================================
   Usage Tracker — Frontend Application & Realtime Sync Engine
   ========================================================================== */

// Global State
const UI_PREFERENCES_STORAGE_KEY = 'usage-tracker:ui-preferences:v1';

function readUiPreferences() {
    try {
        const parsed = JSON.parse(localStorage.getItem(UI_PREFERENCES_STORAGE_KEY) || '{}');
        return parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? parsed : {};
    } catch (e) {
        return {};
    }
}

let uiPreferences = readUiPreferences();

function saveUiPreferences(patch) {
    uiPreferences = { ...uiPreferences, ...(patch || {}) };
    try {
        localStorage.setItem(UI_PREFERENCES_STORAGE_KEY, JSON.stringify(uiPreferences));
    } catch (e) {
        // The tracker still works when storage is unavailable or full.
    }
}

function savedChoice(key, allowed, fallback) {
    const value = uiPreferences[key];
    return Array.isArray(allowed) && allowed.includes(value) ? value : fallback;
}

function savedModelSelection(key) {
    const value = uiPreferences[key];
    if (value === null) return null;
    if (!Array.isArray(value)) return null;
    return new Set(value.filter(item => typeof item === 'string' && item));
}

function serializeModelSelection(selection) {
    return selection === null ? null : [...selection].sort();
}

function savedDateValue(key) {
    const value = uiPreferences[key];
    return typeof value === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(value) ? value : '';
}

function restoreSelectPreference(id, key) {
    const select = document.getElementById(id);
    const value = uiPreferences[key];
    if (select && typeof value === 'string' && [...select.options].some(option => option.value === value)) {
        select.value = value;
    }
}

let currentData = null;
let filteredConversations = [];
let autoSyncInterval = Number(savedChoice('autoSyncInterval', ['0', '5', '10', '30', '60'], '10'));
let countdownSeconds = 10;
let syncTimer = null;
let countdownTimer = null;
let inFlightFetch = null;
let conversationDiagnostics = new Map();

// Color Palette Constants
const THEME = {
    indigo: '#6366f1',
    purple: '#a855f7',
    cyan: '#06b6d4',
    emerald: '#10b981',
    amber: '#f59e0b',
    rose: '#f43f5e',
    sky: '#0ea5e9',
    pink: '#ec4899',
    palette: ['#6366f1', '#a855f7', '#06b6d4', '#10b981', '#f59e0b', '#f43f5e', '#0ea5e9', '#ec4899']
};

// Utilities
function colorForModelKey(modelKey) {
    const key = String(modelKey || '');
    const mapped = currentData?.summary?.model_colors?.[key];
    if (mapped) return mapped;
    const timelineMatch = currentData?.summary?.models_daily_timeline?.models?.find(model => model.name === key);
    if (timelineMatch?.color) return timelineMatch.color;
    // Compatibility with older static exports that have no model color map.
    let hash = 2166136261;
    for (const character of key.toLowerCase()) {
        hash ^= character.charCodeAt(0);
        hash = Math.imul(hash, 16777619);
    }
    return THEME.palette[(hash >>> 0) % THEME.palette.length];
}

function formatNumber(num) {
    if (num === null || num === undefined) return '0';
    if (num >= 1_000_000) return (num / 1_000_000).toFixed(2) + 'M';
    if (num >= 1_000) return (num / 1_000).toFixed(1) + 'K';
    return num.toLocaleString();
}

function formatBytes(bytes) {
    if (!bytes) return '0 B';
    if (bytes >= 1048576) return (bytes / 1048576).toFixed(2) + ' MB';
    if (bytes >= 1024) return (bytes / 1024).toFixed(1) + ' KB';
    return bytes + ' B';
}

function formatDuration(minutes) {
    if (!minutes || minutes <= 0) return '< 1p';
    const roundedMinutes = Math.round(minutes);
    if (roundedMinutes < 60) return roundedMinutes + ' phút';
    const h = Math.floor(roundedMinutes / 60);
    const m = roundedMinutes % 60;
    return `${h}h ${m}p`;
}

function formatDate(isoStr) {
    if (!isoStr) return '—';
    try {
        const d = new Date(isoStr);
        return d.toLocaleDateString('vi-VN', { day: '2-digit', month: '2-digit', year: 'numeric' })
            + ' ' + d.toLocaleTimeString('vi-VN', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
    } catch { return isoStr.slice(0, 19); }
}

function escapeHtml(str) {
    if (!str) return '';
    return str.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function showToast(msg, isError = false) {
    const toast = document.getElementById('toast');
    if (!toast) return;
    toast.innerHTML = isError ? `⚠️ ${msg}` : `⚡ ${msg}`;
    toast.style.borderColor = isError ? 'var(--rose-500)' : 'var(--border-accent)';
    toast.classList.add('show');
    setTimeout(() => {
        toast.classList.remove('show');
    }, 3200);
}

// Keep chart data safe when an older data.js payload predates a series. A
// series with any missing/malformed point is omitted as incomplete; this
// prevents null values from being coerced into fabricated zero-capacity points.
function normalizeAlignedSeries(labels, values) {
    if (!Array.isArray(labels) || labels.length === 0 || !Array.isArray(values) || values.length === 0) return null;
    const aligned = labels.map((_, index) => {
        const value = values[index];
        if (value === null || value === undefined || value === '') return null;
        const number = Number(value);
        return Number.isFinite(number) ? number : null;
    });
    return aligned.length === labels.length && aligned.every(value => value !== null) ? aligned : null;
}

// ---- Canvas Chart Engine ----
class CanvasCharts {
    static initCanvas(canvas) {
        if (!canvas) return null;
        const ctx = canvas.getContext('2d');
        const dpr = window.devicePixelRatio || 1;
        const rect = canvas.getBoundingClientRect();
        const width = rect.width || 380;
        const height = rect.height || 260;
        canvas.width = width * dpr;
        canvas.height = height * dpr;
        ctx.scale(dpr, dpr);
        return { ctx, width, height };
    }

    static drawDonut(canvas, slices, centerLabel = 'TOKENS') {
        const c = this.initCanvas(canvas);
        if (!c) return;
        const { ctx, width, height } = c;
        ctx.clearRect(0, 0, width, height);

        const cx = width / 2;
        const cy = height / 2;
        const radius = Math.min(cx, cy) - 22;
        const total = slices.reduce((sum, s) => sum + s.value, 0);
        if (total === 0) return;

        let startAngle = -Math.PI / 2;

        slices.forEach(slice => {
            if (slice.value <= 0) return;
            const sliceAngle = (slice.value / total) * Math.PI * 2;
            const midAngle = startAngle + sliceAngle / 2;

            ctx.beginPath();
            ctx.moveTo(cx, cy);
            ctx.arc(cx, cy, radius, startAngle, startAngle + sliceAngle);
            ctx.closePath();
            ctx.fillStyle = slice.color;
            ctx.fill();

            // Gap outline
            ctx.beginPath();
            ctx.moveTo(cx, cy);
            ctx.arc(cx, cy, radius, startAngle, startAngle + sliceAngle);
            ctx.closePath();
            ctx.strokeStyle = '#090a10';
            ctx.lineWidth = 2.5;
            ctx.stroke();

            // Percentage
            const pct = ((slice.value / total) * 100).toFixed(1);
            if (parseFloat(pct) > 6) {
                const labelRadius = radius * 0.68;
                const lx = cx + Math.cos(midAngle) * labelRadius;
                const ly = cy + Math.sin(midAngle) * labelRadius;
                ctx.fillStyle = '#ffffff';
                ctx.font = '700 11px "JetBrains Mono", monospace';
                ctx.textAlign = 'center';
                ctx.textBaseline = 'middle';
                ctx.fillText(pct + '%', lx, ly);
            }

            startAngle += sliceAngle;
        });

        // Donut Hole
        ctx.beginPath();
        ctx.arc(cx, cy, radius * 0.48, 0, Math.PI * 2);
        ctx.fillStyle = '#12131c';
        ctx.fill();

        // Inner Text
        ctx.fillStyle = '#f1f5f9';
        ctx.font = '800 16px "JetBrains Mono", monospace';
        ctx.textAlign = 'center';
        ctx.textBaseline = 'middle';
        ctx.fillText(formatNumber(total), cx, cy - 6);

        ctx.fillStyle = '#64748b';
        ctx.font = '600 9px "Plus Jakarta Sans", sans-serif';
        ctx.fillText(centerLabel, cx, cy + 12);
    }

    static drawMultiBar(canvas, labels, datasets) {
        const c = this.initCanvas(canvas);
        if (!c) return;
        const { ctx, width, height } = c;
        ctx.clearRect(0, 0, width, height);

        const pad = { top: 20, right: 16, bottom: 44, left: 56 };
        const chartW = width - pad.left - pad.right;
        const chartH = height - pad.top - pad.bottom;

        const maxVal = Math.max(1, ...datasets.flatMap(d => d.data));
        const numGroups = Math.max(1, labels.length);
        const groupWidth = chartW / numGroups;
        const barWidth = Math.min(groupWidth * 0.36, 26);
        const barGap = 4;

        // Grid lines
        const lines = 4;
        ctx.strokeStyle = 'rgba(255, 255, 255, 0.05)';
        ctx.fillStyle = '#64748b';
        ctx.font = '500 10px "JetBrains Mono", monospace';
        ctx.textAlign = 'right';
        ctx.textBaseline = 'middle';

        for (let i = 0; i <= lines; i++) {
            const y = pad.top + chartH - (chartH / lines) * i;
            const val = (maxVal / lines) * i;
            ctx.beginPath();
            ctx.moveTo(pad.left, y);
            ctx.lineTo(pad.left + chartW, y);
            ctx.stroke();
            ctx.fillText(formatNumber(Math.round(val)), pad.left - 8, y);
        }

        // Render bars
        labels.forEach((label, li) => {
            const gx = pad.left + li * groupWidth + groupWidth / 2;

            datasets.forEach((ds, di) => {
                const val = ds.data[li] || 0;
                const barH = (val / maxVal) * chartH;
                const bx = gx + (di - datasets.length / 2) * (barWidth + barGap);
                const by = pad.top + chartH - barH;

                // Rounded bar
                const r = Math.min(4, barWidth / 2);
                ctx.beginPath();
                ctx.moveTo(bx, pad.top + chartH);
                ctx.lineTo(bx, by + r);
                ctx.quadraticCurveTo(bx, by, bx + r, by);
                ctx.lineTo(bx + barWidth - r, by);
                ctx.quadraticCurveTo(bx + barWidth, by, bx + barWidth, by + r);
                ctx.lineTo(bx + barWidth, pad.top + chartH);
                ctx.closePath();

                const grad = ctx.createLinearGradient(bx, by, bx, pad.top + chartH);
                grad.addColorStop(0, ds.color);
                grad.addColorStop(1, ds.color + '22');
                ctx.fillStyle = grad;
                ctx.fill();
            });

            // X-Axis Label
            ctx.fillStyle = '#64748b';
            ctx.font = '600 10px "JetBrains Mono", monospace';
            ctx.textAlign = 'center';
            ctx.textBaseline = 'top';
            ctx.fillText(label, gx, pad.top + chartH + 8);
        });
    }

    static drawHorizontalBars(canvas, items) {
        const c = this.initCanvas(canvas);
        if (!c) return;
        const { ctx, width, height } = c;
        ctx.clearRect(0, 0, width, height);

        const pad = { top: 10, right: 60, bottom: 10, left: 130 };
        const chartW = width - pad.left - pad.right;
        const chartH = height - pad.top - pad.bottom;
        const topItems = items.slice(0, 8);
        if (topItems.length === 0) return;

        const barH = Math.min(chartH / topItems.length - 6, 22);
        const maxVal = Math.max(1, ...topItems.map(d => d.value));

        topItems.forEach((item, i) => {
            const y = pad.top + i * (chartH / topItems.length) + (chartH / topItems.length - barH) / 2;
            const w = (item.value / maxVal) * chartW;
            const col = item.color || THEME.palette[i % THEME.palette.length];

            // Bar shape
            const r = Math.min(4, barH / 2);
            ctx.beginPath();
            ctx.moveTo(pad.left, y);
            ctx.lineTo(pad.left + w - r, y);
            ctx.quadraticCurveTo(pad.left + w, y, pad.left + w, y + r);
            ctx.lineTo(pad.left + w, y + barH - r);
            ctx.quadraticCurveTo(pad.left + w, y + barH, pad.left + w - r, y + barH);
            ctx.lineTo(pad.left, y + barH);
            ctx.closePath();

            const grad = ctx.createLinearGradient(pad.left, 0, pad.left + w, 0);
            grad.addColorStop(0, col + '44');
            grad.addColorStop(1, col);
            ctx.fillStyle = grad;
            ctx.fill();

            // Label
            ctx.fillStyle = '#94a3b8';
            ctx.font = '600 11px "JetBrains Mono", monospace';
            ctx.textAlign = 'right';
            ctx.textBaseline = 'middle';
            const name = item.label.length > 17 ? item.label.slice(0, 17) + '…' : item.label;
            ctx.fillText(name, pad.left - 10, y + barH / 2);

            // Value text
            ctx.fillStyle = '#f1f5f9';
            ctx.font = '700 11px "JetBrains Mono", monospace';
            ctx.textAlign = 'left';
            ctx.fillText(formatNumber(item.value), pad.left + w + 8, y + barH / 2);
        });
    }

    static drawAreaSpline(canvas, labels, datasets, options = {}) {
        const c = this.initCanvas(canvas);
        if (!c) return;
        const { ctx, width, height } = c;
        ctx.clearRect(0, 0, width, height);

        const safeLabels = Array.isArray(labels) ? labels : [];
        const safeDatasets = (Array.isArray(datasets) ? datasets : [])
            .map(dataset => ({
                ...dataset,
                data: normalizeAlignedSeries(safeLabels, dataset?.data)
            }))
            .filter(dataset => dataset.data);
        if (safeLabels.length === 0 || safeDatasets.length === 0) return;

        const pad = { top: 25, right: 24, bottom: 44, left: 60 };
        const chartW = width - pad.left - pad.right;
        const chartH = height - pad.top - pad.bottom;

        const allValues = safeDatasets.flatMap(d => d.data.filter(value => value !== null));
        if (options.limit && options.limit > 0 && options.limit < 5000000) allValues.push(options.limit);
        const maxVal = Math.max(1000, ...allValues) * 1.12;

        // Grid lines (Horizontal)
        const lines = 4;
        ctx.strokeStyle = 'rgba(255, 255, 255, 0.05)';
        ctx.fillStyle = '#64748b';
        ctx.font = '500 10px "JetBrains Mono", monospace';
        ctx.textAlign = 'right';
        ctx.textBaseline = 'middle';

        for (let i = 0; i <= lines; i++) {
            const y = pad.top + chartH - (chartH / lines) * i;
            const val = (maxVal / lines) * i;
            ctx.beginPath();
            ctx.moveTo(pad.left, y);
            ctx.lineTo(pad.left + chartW, y);
            ctx.stroke();
            ctx.fillText(formatNumber(Math.round(val)), pad.left - 8, y);
        }

        // Draw Limit Line if provided
        if (options.limit && options.limit <= maxVal) {
            const limitY = pad.top + chartH - (options.limit / maxVal) * chartH;
            ctx.save();
            ctx.setLineDash([5, 5]);
            ctx.strokeStyle = '#f43f5e';
            ctx.lineWidth = 1.5;
            ctx.beginPath();
            ctx.moveTo(pad.left, limitY);
            ctx.lineTo(pad.left + chartW, limitY);
            ctx.stroke();
            ctx.fillStyle = '#f43f5e';
            ctx.font = '600 10px "JetBrains Mono", monospace';
            ctx.textAlign = 'left';
            ctx.fillText(`Hạn Mức (${formatNumber(options.limit)})`, pad.left + 8, limitY - 8);
            ctx.restore();
        }

        const numPoints = safeLabels.length;
        if (numPoints < 2) return;

        const stepX = chartW / (numPoints - 1);

        safeDatasets.forEach(ds => {
            const points = ds.data.map((val, idx) => ({
                x: pad.left + idx * stepX,
                y: pad.top + chartH - (val / maxVal) * chartH,
                val: val
            }));

            // Area fill under curve
            ctx.beginPath();
            ctx.moveTo(points[0].x, pad.top + chartH);
            ctx.lineTo(points[0].x, points[0].y);

            for (let i = 0; i < points.length - 1; i++) {
                const xc = (points[i].x + points[i + 1].x) / 2;
                const yc = (points[i].y + points[i + 1].y) / 2;
                ctx.quadraticCurveTo(points[i].x, points[i].y, xc, yc);
            }
            ctx.lineTo(points[points.length - 1].x, points[points.length - 1].y);
            ctx.lineTo(points[points.length - 1].x, pad.top + chartH);
            ctx.closePath();

            const areaGrad = ctx.createLinearGradient(0, pad.top, 0, pad.top + chartH);
            areaGrad.addColorStop(0, ds.color + '44');
            areaGrad.addColorStop(1, ds.color + '02');
            ctx.fillStyle = areaGrad;
            ctx.fill();

            // Line curve
            ctx.beginPath();
            ctx.moveTo(points[0].x, points[0].y);
            for (let i = 0; i < points.length - 1; i++) {
                const xc = (points[i].x + points[i + 1].x) / 2;
                const yc = (points[i].y + points[i + 1].y) / 2;
                ctx.quadraticCurveTo(points[i].x, points[i].y, xc, yc);
            }
            ctx.lineTo(points[points.length - 1].x, points[points.length - 1].y);
            ctx.strokeStyle = ds.color;
            ctx.lineWidth = 2.5;
            ctx.stroke();

            // Glow dots
            points.forEach((pt, pIdx) => {
                if (pt.val > 0 || pIdx === points.length - 1 || pIdx === 0) {
                    ctx.beginPath();
                    ctx.arc(pt.x, pt.y, 4, 0, Math.PI * 2);
                    ctx.fillStyle = ds.color;
                    ctx.fill();
                    ctx.strokeStyle = '#090a10';
                    ctx.lineWidth = 1.8;
                    ctx.stroke();
                }
            });
        });

        // X-Axis Labels (Display every Nth label to prevent clutter)
        const labelInterval = numPoints > 15 ? 4 : (numPoints > 8 ? 2 : 1);
        labels.forEach((label, idx) => {
            if (idx % labelInterval === 0 || idx === numPoints - 1) {
                const x = pad.left + idx * stepX;
                ctx.fillStyle = '#64748b';
                ctx.font = '600 10px "JetBrains Mono", monospace';
                ctx.textAlign = 'center';
                ctx.textBaseline = 'top';
                ctx.fillText(label, x, pad.top + chartH + 8);
            }
        });
    }

    static drawQuotaRatioTimeline(canvas, labels, datasets) {
        const c = this.initCanvas(canvas);
        if (!c) return;
        const { ctx, width, height } = c;
        ctx.clearRect(0, 0, width, height);
        const safeLabels = Array.isArray(labels) ? labels : [];
        const safeDatasets = Array.isArray(datasets) ? datasets : [];
        if (!safeLabels.length || !safeDatasets.length) return;

        const pad = { top: 24, right: 24, bottom: 42, left: 62 };
        const chartW = Math.max(1, width - pad.left - pad.right);
        const chartH = Math.max(1, height - pad.top - pad.bottom);
        const values = safeDatasets.flatMap(ds => (ds.data || []).filter(value => Number.isFinite(value) && value > 0));
        if (!values.length) return;
        const maxVal = Math.max(1.25, ...values) * 1.12;
        const lines = 5;

        ctx.font = '500 10px "JetBrains Mono", monospace';
        ctx.textAlign = 'right';
        ctx.textBaseline = 'middle';
        for (let i = 0; i <= lines; i++) {
            const value = (maxVal / lines) * i;
            const y = pad.top + chartH - (value / maxVal) * chartH;
            ctx.strokeStyle = 'rgba(255,255,255,0.055)';
            ctx.setLineDash([]);
            ctx.beginPath();
            ctx.moveTo(pad.left, y);
            ctx.lineTo(pad.left + chartW, y);
            ctx.stroke();
            ctx.fillStyle = '#64748b';
            ctx.fillText(`${value.toFixed(2)}×`, pad.left - 8, y);
        }

        if (maxVal >= 1) {
            const baselineY = pad.top + chartH - (1 / maxVal) * chartH;
            ctx.save();
            ctx.setLineDash([6, 5]);
            ctx.strokeStyle = 'rgba(226,232,240,0.65)';
            ctx.lineWidth = 1.25;
            ctx.beginPath();
            ctx.moveTo(pad.left, baselineY);
            ctx.lineTo(pad.left + chartW, baselineY);
            ctx.stroke();
            ctx.fillStyle = '#cbd5e1';
            ctx.font = '600 10px "JetBrains Mono", monospace';
            ctx.textAlign = 'left';
            ctx.fillText('Sol High = 1×', pad.left + 8, baselineY - 8);
            ctx.restore();
        }

        const stepX = safeLabels.length > 1 ? chartW / (safeLabels.length - 1) : 0;
        safeDatasets.forEach(ds => {
            const points = (ds.data || []).map((rawValue, index) => {
                const value = Number(rawValue);
                if (!Number.isFinite(value) || value <= 0) return null;
                return {
                    x: pad.left + index * stepX,
                    y: pad.top + chartH - (value / maxVal) * chartH,
                };
            });
            let segment = [];
            const strokeSegment = () => {
                if (!segment.length) return;
                ctx.beginPath();
                ctx.moveTo(segment[0].x, segment[0].y);
                segment.slice(1).forEach(point => ctx.lineTo(point.x, point.y));
                ctx.strokeStyle = ds.color;
                ctx.lineWidth = 2.3;
                ctx.stroke();
                segment = [];
            };
            points.forEach(point => {
                if (point) segment.push(point);
                else strokeSegment();
            });
            strokeSegment();
            points.filter(Boolean).forEach(point => {
                ctx.beginPath();
                ctx.arc(point.x, point.y, 3.1, 0, Math.PI * 2);
                ctx.fillStyle = ds.color;
                ctx.fill();
                ctx.strokeStyle = '#090a10';
                ctx.lineWidth = 1.2;
                ctx.stroke();
            });
        });

        const labelInterval = safeLabels.length > 60 ? 10 : (safeLabels.length > 25 ? 5 : (safeLabels.length > 12 ? 2 : 1));
        safeLabels.forEach((label, index) => {
            if (index % labelInterval !== 0 && index !== safeLabels.length - 1) return;
            const x = pad.left + index * stepX;
            ctx.fillStyle = '#64748b';
            ctx.font = '600 10px "JetBrains Mono", monospace';
            ctx.textAlign = 'center';
            ctx.textBaseline = 'top';
            ctx.fillText(label, x, pad.top + chartH + 8);
        });
    }

    static drawWeeklyTimeline(canvas, labels, datasets, options = {}) {
        const c = this.initCanvas(canvas);
        if (!c) return;
        const { ctx, width, height } = c;
        ctx.clearRect(0, 0, width, height);

        const pad = { top: 25, right: 24, bottom: 44, left: 60 };
        const chartW = width - pad.left - pad.right;
        const chartH = height - pad.top - pad.bottom;

        const maxVal = Math.max(1000, ...datasets.flatMap(d => d.data)) * 1.15;
        const numGroups = Math.max(1, labels.length);
        const groupWidth = chartW / numGroups;
        const barWidth = Math.min(groupWidth * 0.32, 28);
        const barGap = 4;

        // Grid lines
        const lines = 4;
        ctx.strokeStyle = 'rgba(255, 255, 255, 0.05)';
        ctx.fillStyle = '#64748b';
        ctx.font = '500 10px "JetBrains Mono", monospace';
        ctx.textAlign = 'right';
        ctx.textBaseline = 'middle';

        for (let i = 0; i <= lines; i++) {
            const y = pad.top + chartH - (chartH / lines) * i;
            const val = (maxVal / lines) * i;
            ctx.beginPath();
            ctx.moveTo(pad.left, y);
            ctx.lineTo(pad.left + chartW, y);
            ctx.stroke();
            ctx.fillText(formatNumber(Math.round(val)), pad.left - 8, y);
        }

        // Draw Limit Line if provided
        if (options.limit && options.limit <= maxVal) {
            const limitY = pad.top + chartH - (options.limit / maxVal) * chartH;
            ctx.save();
            ctx.setLineDash([5, 5]);
            ctx.strokeStyle = '#f59e0b';
            ctx.lineWidth = 1.5;
            ctx.beginPath();
            ctx.moveTo(pad.left, limitY);
            ctx.lineTo(pad.left + chartW, limitY);
            ctx.stroke();
            ctx.fillStyle = '#f59e0b';
            ctx.font = '600 10px "JetBrains Mono", monospace';
            ctx.textAlign = 'left';
            ctx.fillText(`Hạn Mức Tuần (${formatNumber(options.limit)})`, pad.left + 8, limitY - 8);
            ctx.restore();
        }

        // Bars
        labels.forEach((label, li) => {
            const gx = pad.left + li * groupWidth + groupWidth / 2;

            datasets.forEach((ds, di) => {
                const val = ds.data[li] || 0;
                const barH = (val / maxVal) * chartH;
                const bx = gx + (di - datasets.length / 2) * (barWidth + barGap);
                const by = pad.top + chartH - barH;

                const r = Math.min(4, barWidth / 2);
                ctx.beginPath();
                ctx.moveTo(bx, pad.top + chartH);
                ctx.lineTo(bx, by + r);
                ctx.quadraticCurveTo(bx, by, bx + r, by);
                ctx.lineTo(bx + barWidth - r, by);
                ctx.quadraticCurveTo(bx + barWidth, by, bx + barWidth, by + r);
                ctx.lineTo(bx + barWidth, pad.top + chartH);
                ctx.closePath();

                const grad = ctx.createLinearGradient(bx, by, bx, pad.top + chartH);
                grad.addColorStop(0, ds.color);
                grad.addColorStop(1, ds.color + '22');
                ctx.fillStyle = grad;
                ctx.fill();
            });

            // X-Axis Label
            ctx.fillStyle = '#64748b';
            ctx.font = '600 10px "JetBrains Mono", monospace';
            ctx.textAlign = 'center';
            ctx.textBaseline = 'top';
            ctx.fillText(label, gx, pad.top + chartH + 8);
        });
    }

    static drawFixedTimelineYAxis(canvas, maxVal, axisFormatter, options = {}) {
        const c = this.initCanvas(canvas);
        if (!c) return;
        const { ctx, width, height } = c;
        ctx.clearRect(0, 0, width, height);

        const top = Number(options.top ?? 30);
        const bottom = Number(options.bottom ?? 42);
        const gridSteps = Math.max(1, Number(options.gridSteps) || 4);
        const chartH = height - top - bottom;
        if (chartH <= 0 || !Number.isFinite(maxVal) || maxVal <= 0) return;

        for (let i = 0; i <= gridSteps; i++) {
            const yVal = (maxVal / gridSteps) * i;
            const yPos = top + chartH - (yVal / maxVal) * chartH;

            ctx.fillStyle = '#94a3b8';
            ctx.font = '600 10px "JetBrains Mono", monospace';
            ctx.textAlign = 'right';
            ctx.textBaseline = 'middle';
            ctx.fillText(axisFormatter(yVal), width - 10, yPos);

            ctx.strokeStyle = 'rgba(148, 163, 184, 0.32)';
            ctx.lineWidth = 1;
            ctx.beginPath();
            ctx.moveTo(width - 6, yPos);
            ctx.lineTo(width, yPos);
            ctx.stroke();
        }
    }

    static drawMultiModelDailyStackedBar(canvas, timelineData, activeFilter = 'all', options = {}) {
        const c = this.initCanvas(canvas);
        if (!c || !timelineData || !timelineData.dates || !timelineData.models) return;
        const { ctx, width, height } = c;
        ctx.clearRect(0, 0, width, height);

        const dataKey = options.dataKey || 'daily_tokens';
        const valueMultiplier = Number(options.valueMultiplier || 1);
        const fallbackMax = Number(options.fallbackMax ?? 1000);
        const axisFormatter = options.axisFormatter || (value => formatNumber(Math.round(value)));
        const totalFormatter = options.totalFormatter || (value => formatNumber(Math.round(value)));
        const fixedAxisCanvas = options.axisCanvas || null;

        const pad = { top: 30, right: 24, bottom: 42, left: fixedAxisCanvas ? 12 : 68 };
        const chartW = width - pad.left - pad.right;
        const chartH = height - pad.top - pad.bottom;
        if (chartW <= 0 || chartH <= 0) return;

        const dates = timelineData.dates;
        const numPoints = dates.length;
        if (numPoints < 1) return;

        let models = timelineData.models;
        if (activeFilter && activeFilter !== 'all') {
            const selectedNames = activeFilter instanceof Set
                ? activeFilter
                : new Set(Array.isArray(activeFilter) ? activeFilter : [activeFilter]);
            models = models.filter(m => selectedNames.has(m.name));
        }

        const dailyTotals = Array.from({ length: numPoints }, (_, dayIndex) =>
            models.reduce((sum, model) => sum + (Number(model[dataKey]?.[dayIndex]) || 0) * valueMultiplier, 0)
        );
        const rawMax = Math.max(fallbackMax, ...dailyTotals, Number.EPSILON);
        const magnitude = Math.pow(10, Math.floor(Math.log10(rawMax)));
        const niceStep = Math.max(Number.EPSILON, magnitude / 2);
        const maxVal = Math.ceil((rawMax * 1.12) / niceStep) * niceStep;

        const gridSteps = 4;
        if (fixedAxisCanvas) {
            this.drawFixedTimelineYAxis(fixedAxisCanvas, maxVal, axisFormatter, {
                top: pad.top,
                bottom: pad.bottom,
                gridSteps,
            });
        }
        ctx.strokeStyle = 'rgba(255, 255, 255, 0.08)';
        ctx.lineWidth = 1;
        ctx.setLineDash([4, 4]);

        for (let i = 0; i <= gridSteps; i++) {
            const yVal = (maxVal / gridSteps) * i;
            const yPos = pad.top + chartH - (yVal / maxVal) * chartH;

            ctx.beginPath();
            ctx.moveTo(pad.left, yPos);
            ctx.lineTo(pad.left + chartW, yPos);
            ctx.stroke();

            if (!fixedAxisCanvas) {
                ctx.fillStyle = '#94a3b8';
                ctx.font = '600 10px "JetBrains Mono", monospace';
                ctx.textAlign = 'right';
                ctx.textBaseline = 'middle';
                ctx.fillText(axisFormatter(yVal), pad.left - 8, yPos);
            }
        }
        ctx.setLineDash([]);

        const groupWidth = chartW / numPoints;
        const barWidth = Math.min(32, Math.max(2, groupWidth * 0.68));
        const dateKeys = Array.isArray(timelineData.date_keys) ? timelineData.date_keys : [];
        const labelStride = numPoints <= 10 ? 1 : (numPoints <= 35 ? 5 : (numPoints <= 100 ? 10 : Math.ceil(numPoints / 10)));
        const segmentsByDate = Array.from({ length: numPoints }, () => []);

        dailyTotals.forEach((total, dayIndex) => {
            const xCenter = pad.left + groupWidth * (dayIndex + 0.5);
            let stackBottomY = pad.top + chartH;

            models.forEach(model => {
                const val = (Number(model[dataKey]?.[dayIndex]) || 0) * valueMultiplier;
                if (val <= 0) return;
                const segmentHeight = (val / maxVal) * chartH;
                const y = stackBottomY - segmentHeight;
                ctx.fillStyle = model.color || '#06b6d4';
                const drawnHeight = Math.max(1, segmentHeight);
                ctx.fillRect(xCenter - barWidth / 2, y, barWidth, drawnHeight);
                segmentsByDate[dayIndex].push({ model, value: val, top: y, bottom: y + drawnHeight });
                stackBottomY = y;
            });

            if (total > 0 && numPoints <= 10) {
                ctx.fillStyle = '#e2e8f0';
                ctx.font = '700 9.5px "JetBrains Mono", monospace';
                ctx.textAlign = 'center';
                ctx.textBaseline = 'bottom';
                ctx.fillText(totalFormatter(total), xCenter, Math.max(10, stackBottomY - 5));
            }

            const shouldLabel = dayIndex % labelStride === 0 || dayIndex === numPoints - 1;
            if (shouldLabel) {
                let label = dates[dayIndex];
                if (numPoints > 14 && dateKeys[dayIndex]) {
                    const parts = dateKeys[dayIndex].split('-');
                    if (parts.length === 3) label = `${parts[2]}/${parts[1]}`;
                }
                ctx.fillStyle = '#94a3b8';
                ctx.font = '500 10px "Plus Jakarta Sans", sans-serif';
                ctx.textAlign = 'center';
                ctx.textBaseline = 'top';
                ctx.fillText(label, xCenter, pad.top + chartH + 10);
            }
        });
        this.bindStackedBarTooltip(canvas, {
            dates, segmentsByDate, pad, groupWidth, barWidth,
            valueFormatter: options.tooltipFormatter || totalFormatter,
            valueLabel: options.tooltipValueLabel || (() => window.UsageI18n?.language === 'en' ? 'Value' : 'Giá trị'),
        });
    }

    static bindStackedBarTooltip(canvas, chart) {
        const frame = canvas.closest('.model-timeline-chart-frame');
        if (!frame) return;
        let tooltip = frame.querySelector('.model-timeline-tooltip');
        if (!tooltip) {
            tooltip = document.createElement('div');
            tooltip.className = 'model-timeline-tooltip';
            tooltip.setAttribute('role', 'tooltip');
            tooltip.hidden = true;
            frame.appendChild(tooltip);
        }
        tooltip.hidden = true;
        tooltip.dataset.hitKey = '';
        const hide = () => {
            tooltip.hidden = true;
            canvas.style.cursor = '';
        };
        const showForPointer = event => {
            const canvasRect = canvas.getBoundingClientRect();
            const x = event.clientX - canvasRect.left;
            const y = event.clientY - canvasRect.top;
            const dayIndex = Math.floor((x - chart.pad.left) / chart.groupWidth);
            const barCenter = chart.pad.left + chart.groupWidth * (dayIndex + 0.5);
            if (dayIndex < 0 || dayIndex >= chart.dates.length ||
                Math.abs(x - barCenter) > chart.barWidth / 2) {
                hide();
                return;
            }
            const segments = chart.segmentsByDate[dayIndex];
            if (!segments.length || y < segments[segments.length - 1].top || y > segments[0].bottom) {
                hide();
                return;
            }
            const hitKey = `${dayIndex}:${window.UsageI18n?.language || 'vi'}`;
            if (tooltip.dataset.hitKey !== hitKey) {
                tooltip.replaceChildren();
                const date = document.createElement('div');
                date.className = 'model-timeline-tooltip-date';
                date.textContent = `${chart.dates[dayIndex]} · ${chart.valueLabel()}`;
                const total = document.createElement('div');
                total.className = 'model-timeline-tooltip-total';
                total.textContent = `${window.UsageI18n?.language === 'en' ? 'Total' : 'Tổng'}: ${chart.valueFormatter(segments.reduce((sum, item) => sum + item.value, 0))}`;
                const rows = document.createElement('div');
                rows.className = 'model-timeline-tooltip-rows';
                [...segments].sort((a, b) => b.value - a.value).forEach(item => {
                    const row = document.createElement('div');
                    row.className = 'model-timeline-tooltip-row';
                    const model = document.createElement('span');
                    model.className = 'model-timeline-tooltip-model';
                    const swatch = document.createElement('span');
                    swatch.className = 'model-timeline-tooltip-swatch';
                    swatch.style.backgroundColor = item.model.color || '#06b6d4';
                    model.append(swatch, document.createTextNode(item.model.name));
                    const value = document.createElement('span');
                    value.className = 'model-timeline-tooltip-value';
                    value.textContent = chart.valueFormatter(item.value);
                    row.append(model, value);
                    rows.appendChild(row);
                });
                tooltip.append(date, total, rows);
                tooltip.dataset.hitKey = hitKey;
            }
            tooltip.hidden = false;
            canvas.style.cursor = 'crosshair';
            const frameRect = frame.getBoundingClientRect();
            const left = Math.max(4, Math.min(
                event.clientX - frameRect.left + 12,
                frame.clientWidth - tooltip.offsetWidth - 4,
            ));
            const viewportTop = Math.max(4, 8 - frameRect.top);
            const viewportBottom = Math.max(viewportTop, window.innerHeight - frameRect.top - tooltip.offsetHeight - 8);
            const above = event.clientY - frameRect.top - tooltip.offsetHeight - 12;
            const below = event.clientY - frameRect.top + 12;
            tooltip.style.left = `${left}px`;
            tooltip.style.top = `${Math.max(viewportTop, Math.min(above >= viewportTop ? above : below, viewportBottom))}px`;
        };
        canvas.onpointermove = showForPointer;
        canvas.onmousemove = showForPointer;
        const hideUnlessEnteringTooltip = event => {
            if (!tooltip.contains(event.relatedTarget)) hide();
        };
        canvas.onpointerleave = hideUnlessEnteringTooltip;
        canvas.onmouseleave = hideUnlessEnteringTooltip;
        canvas.onpointercancel = hide;
        tooltip.onpointerleave = hide;
        tooltip.onmouseleave = hide;
        const scroll = canvas.closest('.model-timeline-scroll');
        if (scroll && !scroll.dataset.tooltipScrollBound) {
            scroll.addEventListener('scroll', hide);
            scroll.dataset.tooltipScrollBound = 'true';
        }
    }
}

// ---- Data Fetching & Syncing ----
let offlineDataScriptPromise = null;

function loadOfflineDataScript() {
    if (typeof USAGE_DATA !== 'undefined') return Promise.resolve(USAGE_DATA);
    if (!offlineDataScriptPromise) {
        offlineDataScriptPromise = new Promise((resolve, reject) => {
            const script = document.createElement('script');
            script.src = 'data.js';
            script.onload = () => {
                if (typeof USAGE_DATA !== 'undefined') resolve(USAGE_DATA);
                else reject(new Error('Offline data.js did not define USAGE_DATA.'));
            };
            script.onerror = () => reject(new Error('Offline data.js is unavailable.'));
            document.head.appendChild(script);
        }).catch(error => {
            offlineDataScriptPromise = null;
            throw error;
        });
    }
    return offlineDataScriptPromise;
}

function buildDataApiUrl() {
    const params = new URLSearchParams();
    if (currentModelTimelineRange === 'custom' && currentModelTimelineStart && currentModelTimelineEnd) {
        params.set('model_timeline_start', currentModelTimelineStart);
        params.set('model_timeline_end', currentModelTimelineEnd);
    } else {
        params.set('model_timeline_days', currentModelTimelineRange || '7');
    }
    return `/api/data?${params.toString()}`;
}

async function doFetchData(isManual = false) {
    const refreshBtn = document.getElementById('btn-refresh');
    if (refreshBtn) refreshBtn.classList.add('loading');

    try {
        let data = null;

        // 1. Try fetching from live local Python server API
        try {
            const resp = await fetch(buildDataApiUrl(), { cache: 'no-store' });
            if (resp.ok) {
                data = await resp.json();
                const badge = document.getElementById('data-source-badge');
                if (badge) {
                    badge.textContent = 'API Trực Tiếp (Live Server)';
                    badge.className = 'status-value highlight-cyan';
                }
            }
        } catch (e) {
            console.log('Direct API not reachable, attempting fallback data...');
        }

        // 2. Load the large offline snapshot only when live data is unavailable.
        if (!data) {
            try {
                data = await loadOfflineDataScript();
                const badge = document.getElementById('data-source-badge');
                if (badge) badge.textContent = 'Static File (data.js)';
            } catch (error) {
                console.warn('Offline data is unavailable:', error);
            }
        }

        if (!data || !data.summary) {
            throw new Error('Không thể tải dữ liệu phân tích usage.');
        }

        currentData = data;
        renderAll(currentData);

        const updateTime = new Date().toLocaleTimeString('vi-VN');
        const lastUpdated = document.getElementById('last-updated-time');
        if (lastUpdated) lastUpdated.textContent = updateTime;

        if (isManual) {
            showToast(`Đã đồng bộ thành công lúc ${updateTime}! (${data.summary.total_conversations} phiên chat)`);
        }
        return currentData;
    } catch (err) {
        console.error('Fetch error:', err);
        showToast(err.message, true);
        throw err;
    } finally {
        if (refreshBtn) refreshBtn.classList.remove('loading');
    }
}

function loadUsageData(isManual = false) {
    if (inFlightFetch) {
        return inFlightFetch;
    }
    const fetchPromise = doFetchData(isManual);
    inFlightFetch = fetchPromise;
    fetchPromise.then(
        () => {
            if (inFlightFetch === fetchPromise) {
                inFlightFetch = null;
            }
        },
        () => {
            if (inFlightFetch === fetchPromise) {
                inFlightFetch = null;
            }
        }
    );
    return fetchPromise;
}

async function forceRefreshAfterMutation() {
    const prior = inFlightFetch;
    if (prior) {
        try {
            await prior;
        } catch (e) {}
    }
    const freshPromise = doFetchData(true);
    inFlightFetch = freshPromise;
    freshPromise.then(
        () => {
            if (inFlightFetch === freshPromise) {
                inFlightFetch = null;
            }
        },
        () => {
            if (inFlightFetch === freshPromise) {
                inFlightFetch = null;
            }
        }
    );
    try {
        return await freshPromise;
    } finally {
        if (inFlightFetch === freshPromise) {
            inFlightFetch = null;
        }
    }
}

// ---- Rendering Engine ----
function renderAll(data) {
    const { summary, conversations } = data;

    renderInvestigationDashboard(conversations || []);
    renderTimeSeriesAnalytics(summary.time_series);
    populateModelFilter(summary.models_distribution || {});
    applyFiltersAndSort();
    renderLeaderboard(
        summary.leaderboard || [],
        summary.models_breakdown || [],
        summary.codex_usage || {}
    );
    renderAASyncStatus(summary.aa_sync || {}, summary.leaderboard || []);
    renderQuotas(summary.quotas, summary.source_breakdowns || summary.source_breakdown);
    renderCodexRateLimits(summary.codex_usage);
    renderCodexWeeklyCapacity(summary.codex_usage);
    renderAccountManager(summary.current_account, summary.accounts_manager);
    renderModelsBreakdown(summary.models_breakdown || [], summary.codex_usage || {}, summary.models_daily_timeline);
    renderCodexTaskOutcomes(summary.codex_usage || {});
}

function renderTimeSeriesAnalytics(tsData) {
    if (!tsData) return;

    const fiveH = tsData.five_hour_timeline;
    const weekly = tsData.weekly_timeline;
    const snapshots = tsData.recent_snapshots || [];
    const policyShifts = tsData.policy_shifts || [];

    // 1. Render 5-Hour Area Spline Chart
    const canvas5h = document.getElementById('canvas-5h-timeseries');
    if (canvas5h && fiveH && fiveH.labels) {
        const datasets5h = [
            { label: 'Gemini (5h)', data: fiveH.gemini_tokens || [], color: THEME.cyan },
            { label: 'Model Ngoài (5h)', data: fiveH.external_tokens || [], color: THEME.amber }
        ];
        CanvasCharts.drawAreaSpline(canvas5h, fiveH.labels, datasets5h, { limit: fiveH.gemini_limit });

        const badgeUsed5h = document.getElementById('ts-5h-used-badge');
        if (badgeUsed5h) badgeUsed5h.textContent = `${formatNumber(fiveH.current_used_5h || 0)} tokens (${(fiveH.current_pct_5h || 100).toFixed(1)}% ${window.UsageI18n?.language === 'en' ? 'remaining' : 'còn lại'})`;
    }

    // 2. Render Weekly Timeline Chart
    const canvasWk = document.getElementById('canvas-weekly-timeseries');
    if (canvasWk && weekly && weekly.labels) {
        const datasetsWk = [
            { label: 'Gemini', data: weekly.daily_gemini_tokens || [], color: THEME.cyan },
            { label: 'Model Ngoài', data: weekly.daily_external_tokens || [], color: THEME.amber }
        ];
        CanvasCharts.drawWeeklyTimeline(canvasWk, weekly.labels, datasetsWk, { limit: weekly.gemini_limit });

        const badgeUsedWk = document.getElementById('ts-wk-used-badge');
        if (badgeUsedWk) badgeUsedWk.textContent = `${formatNumber(weekly.current_used_weekly || 0)} tokens (${(weekly.current_pct_weekly || 100).toFixed(1)}% ${window.UsageI18n?.language === 'en' ? 'remaining' : 'còn lại'})`;
    }

    // 3. Render Capacity Evolution Chart
    const canvasCap = document.getElementById('canvas-capacity-evolution');
    const canvasCapWeekly = document.getElementById('canvas-capacity-evolution-weekly');
    const capEvo = tsData.capacity_evolution;
    if (canvasCap && capEvo && capEvo.labels && capEvo.labels.length > 0) {
        const fiveHour = capEvo.five_hour || capEvo;
        const capacityLabels = fiveHour.labels || capEvo.labels;
        const datasetsCap = [
            { label: 'Transcript-estimated capacity (5h)', data: normalizeAlignedSeries(capacityLabels, fiveHour.transcript_capacity || capEvo.gemini_5h_transcript_capacity || capEvo.gemini_5h_limits), color: THEME.cyan },
            { label: 'Recent-cycle early candidate (5h)', data: normalizeAlignedSeries(capacityLabels, fiveHour.recent_cycle_capacity), color: THEME.rose },
            { label: 'Exact worker capacity (5h)', data: normalizeAlignedSeries(capacityLabels, fiveHour.worker_capacity || capEvo.gemini_5h_worker_capacity), color: THEME.purple },
            { label: 'Source-mix-dependent mixed capacity (5h)', data: normalizeAlignedSeries(capacityLabels, fiveHour.mixed_capacity || capEvo.gemini_5h_mixed_capacity), color: THEME.amber }
        ].filter(dataset => dataset.data);
        CanvasCharts.drawAreaSpline(canvasCap, capacityLabels, datasetsCap, { limit: fiveHour.current_capacity || capEvo.current_capacity });

        const badgeCap = document.getElementById('ts-cap-badge');
        if (badgeCap) {
            const recentCandidate = fiveHour.current_recent_cycle_capacity;
            badgeCap.textContent = `5H long-term: ${formatNumber(fiveHour.current_capacity || capEvo.current_capacity || 1000000)}${recentCandidate ? ` • recent: ${formatNumber(recentCandidate)}` : ''} tokens • ${capacityLabels.length} calibration points`;
        }
        const evidenceNote = document.getElementById('ts-cap-evidence-note');
        const latest5h = capEvo.calibration_history?.gemini_5h?.slice(-1)[0];
        const latestShift = policyShifts.slice(-1)[0];
        if (evidenceNote && latest5h) {
            const conf = Number.isFinite(Number(latest5h.confidence)) ? `${(Number(latest5h.confidence) * 100).toFixed(1)}%` : '—';
            const recentText = latest5h.recent_cycle_capacity_tokens
                ? ` · recent-cycle ${formatNumber(latest5h.recent_cycle_capacity_tokens)} (${latest5h.recent_cycle_evidence_count || 0} obs/${latest5h.recent_cycle_pair_count || 0} pairs)`
                : '';
            const shiftText = latestShift ? ` · ⚠ ${latestShift.bucket}: ${latestShift.old_capacity?.toLocaleString?.() || latestShift.old_capacity} → ${latestShift.new_capacity?.toLocaleString?.() || latestShift.new_capacity} (${latestShift.change_pct})` : '';
            evidenceNote.textContent = `Evidence: ${latest5h.evidence_count || 0} observations / ${latest5h.pair_count || 0} pairs · confidence ${conf} · ${latest5h.accepted ? 'accepted' : 'prior retained'} (${latest5h.reason || 'no new evidence'})${recentText}${shiftText}`;
        } else if (evidenceNote) {
            evidenceNote.textContent = 'Chưa có điểm hiệu chỉnh mới; biểu đồ đang hiển thị lịch sử/prior hiện có.';
        }
    }
    if (canvasCapWeekly && capEvo && capEvo.weekly && capEvo.weekly.labels?.length > 0) {
        const weekly = capEvo.weekly;
        const weeklyDatasets = [
            { label: 'Transcript-estimated capacity (weekly)', data: normalizeAlignedSeries(weekly.labels, weekly.transcript_capacity), color: THEME.cyan },
            { label: 'Recent-cycle early candidate (weekly)', data: normalizeAlignedSeries(weekly.labels, weekly.recent_cycle_capacity), color: THEME.rose },
            { label: 'Exact worker capacity (weekly)', data: normalizeAlignedSeries(weekly.labels, weekly.worker_capacity), color: THEME.purple },
            { label: 'Source-mix-dependent mixed capacity (weekly)', data: normalizeAlignedSeries(weekly.labels, weekly.mixed_capacity), color: THEME.amber }
        ].filter(dataset => dataset.data);
        CanvasCharts.drawAreaSpline(canvasCapWeekly, weekly.labels, weeklyDatasets, { limit: weekly.current_capacity });
        const badgeWeekly = document.getElementById('ts-cap-weekly-badge');
        if (badgeWeekly) badgeWeekly.textContent = window.UsageI18n?.language === 'en'
            ? `Weekly transcript: ${formatNumber(weekly.current_capacity || 0)} tokens • mixed estimate depends on source mix`
            : `Weekly transcript: ${formatNumber(weekly.current_capacity || 0)} tokens • mixed phụ thuộc source mix`;
    }

    // 4. Render Policy Shifts & Empirical Audit Table Body
    const tbodyShifts = document.getElementById('policy-shifts-table-body');
    const shifts = policyShifts;
    if (tbodyShifts) {
        if (shifts.length === 0) {
            tbodyShifts.innerHTML = '<tr><td colspan="6" style="text-align:center;color:var(--text-muted);padding:24px;">Chưa phát hiện biến động hạn mức bất thường nào.</td></tr>';
        } else {
            tbodyShifts.innerHTML = shifts.map(s => {
                let badgeType = 'badge-emerald';
                let tagText = '● Chuẩn Cơ Sở';
                if (s.type === 'INCREASE') {
                    badgeType = 'badge-cyan';
                    tagText = '▲ Tăng Hạn Mức';
                } else if (s.type === 'DECREASE') {
                    badgeType = 'badge-rose';
                    tagText = '▼ Siết Hạn Mức';
                }

                return `
                    <tr>
                        <td style="font-family:'JetBrains Mono';font-size:0.8rem;font-weight:600;color:var(--cyan-400);">${s.time_str || '—'} <span style="color:var(--text-muted);font-size:0.72rem;">(${s.date_str || ''})</span></td>
                        <td><span class="badge-tag" style="background:rgba(6,182,212,0.15);color:var(--cyan-400);font-weight:600;">${escapeHtml(s.provider || 'Google Gemini')}</span></td>
                        <td style="font-family:'JetBrains Mono';font-size:0.8rem;">${escapeHtml(s.bucket || 'Gemini 5H')}</td>
                        <td><span class="badge-tag ${badgeType}" style="font-weight:700;">${tagText} (${s.change_pct || '0%'})</span></td>
                        <td data-sort-value="${Number(s.new_capacity) || 0}" style="font-family:'JetBrains Mono';font-weight:600;">${formatNumber(s.old_capacity)} → <strong style="color:var(--cyan-400);">${formatNumber(s.new_capacity)}</strong></td>
                        <td style="font-size:0.78rem;color:var(--text-secondary);max-width:280px;">${escapeHtml(s.note || 'Bằng chứng thực nghiệm ghi nhận từ hệ thống.')}</td>
                    </tr>
                `;
            }).join('');
        }
    }

    // 5. Render Snapshots Table Body
    const tbody = document.getElementById('ts-snapshots-table-body');
    if (tbody) {
        if (snapshots.length === 0) {
            tbody.innerHTML = '<tr><td colspan="7" style="text-align:center;color:var(--text-muted);padding:24px;">Chưa có điểm đo snapshot nào được ghi nhận.</td></tr>';
        } else {
            const rows = [...snapshots].reverse().slice(0, 30);
            tbody.innerHTML = rows.map(s => {
                const g5Pct = (s.gemini_5h_pct ?? 100).toFixed(1);
                const gwPct = (s.gemini_wk_pct ?? 100).toFixed(1);
                let g5Color = 'var(--emerald-400)';
                if (s.gemini_5h_pct < 25) g5Color = 'var(--rose-400)';
                else if (s.gemini_5h_pct < 60) g5Color = 'var(--amber-400)';

                return `
                    <tr>
                        <td style="font-family:'JetBrains Mono';font-size:0.8rem;font-weight:600;color:var(--cyan-400);">${s.time_str || '—'} <span style="color:var(--text-muted);font-size:0.72rem;">(${s.date_str || ''})</span></td>
                        <td><span class="badge-tag" style="background:rgba(99,102,241,0.15);color:var(--indigo-400);">${escapeHtml(s.active_model || 'Gemini')}</span></td>
                        <td style="font-family:'JetBrains Mono';font-weight:600;">${formatNumber(s.gemini_5h_used || 0)}</td>
                        <td style="font-family:'JetBrains Mono';font-weight:600;color:${g5Color};">${g5Pct}%</td>
                        <td style="font-family:'JetBrains Mono';font-weight:600;">${formatNumber(s.gemini_wk_used || 0)}</td>
                        <td style="font-family:'JetBrains Mono';font-weight:600;color:var(--cyan-400);">${gwPct}%</td>
                        <td><span style="display:inline-flex;align-items:center;gap:4px;color:var(--emerald-400);font-size:0.75rem;font-weight:600;">● Live</span></td>
                    </tr>
                `;
            }).join('');
        }
    }
}

function conversationDiagnosticKey(conversation) {
    return `${conversation?.source || 'unknown'}:${conversation?.id || ''}`;
}

function median(values) {
    const numbers = values
        .map(value => Number(value))
        .filter(value => Number.isFinite(value) && value >= 0)
        .sort((a, b) => a - b);
    if (numbers.length === 0) return 0;
    const middle = Math.floor(numbers.length / 2);
    return numbers.length % 2 === 0
        ? (numbers[middle - 1] + numbers[middle]) / 2
        : numbers[middle];
}

function conversationMetric(conversation, metric) {
    if (metric === 'tokens') return Number(conversation.input_tokens_est || 0) + Number(conversation.output_tokens_est || 0);
    if (metric === 'cost') return Number(conversation.estimated_cost_usd || 0);
    if (metric === 'duration') return Number(conversation.duration_minutes || 0);
    if (metric === 'tools') return Number(conversation.tool_calls || 0);
    return 0;
}

function buildConversationBaseline(conversations, label) {
    return {
        label,
        sampleCount: conversations.length,
        tokens: median(conversations.map(c => conversationMetric(c, 'tokens'))),
        cost: median(conversations.map(c => conversationMetric(c, 'cost'))),
        duration: median(conversations.map(c => conversationMetric(c, 'duration'))),
        tools: median(conversations.map(c => conversationMetric(c, 'tools')))
    };
}

function safeRatio(value, baseline) {
    const current = Number(value || 0);
    const reference = Number(baseline || 0);
    if (!Number.isFinite(current) || !Number.isFinite(reference) || reference <= 0) return 0;
    return current / reference;
}

function buildConversationDiagnostics(conversations) {
    const byModelAndSource = new Map();
    const bySource = new Map();

    conversations.forEach(conversation => {
        const source = conversation.source || 'unknown';
        const model = conversation.model_name || 'Mặc định';
        const groupKey = `${source}:${model}`;
        if (!byModelAndSource.has(groupKey)) byModelAndSource.set(groupKey, []);
        if (!bySource.has(source)) bySource.set(source, []);
        byModelAndSource.get(groupKey).push(conversation);
        bySource.get(source).push(conversation);
    });

    const allBaseline = buildConversationBaseline(conversations, 'toàn bộ phiên');
    const diagnostics = new Map();

    conversations.forEach(conversation => {
        const source = conversation.source || 'unknown';
        const model = conversation.model_name || 'Mặc định';
        const exactGroup = byModelAndSource.get(`${source}:${model}`) || [];
        const sourceGroup = bySource.get(source) || [];
        let baseline;
        let baselineLevel;
        if (exactGroup.length >= 3) {
            baseline = buildConversationBaseline(exactGroup, `cùng nguồn + model (${model})`);
            baselineLevel = 'model';
        } else if (sourceGroup.length >= 3) {
            baseline = buildConversationBaseline(sourceGroup, `cùng nguồn (${conversation.source_label || source})`);
            baselineLevel = 'source';
        } else {
            baseline = allBaseline;
            baselineLevel = 'all';
        }

        const values = {
            tokens: conversationMetric(conversation, 'tokens'),
            cost: conversationMetric(conversation, 'cost'),
            duration: conversationMetric(conversation, 'duration'),
            tools: conversationMetric(conversation, 'tools')
        };
        // Transcript start/end is wall-clock time. Very long sessions usually
        // contain idle/suspended gaps, so they are shown but excluded from the
        // comparative ratio and anomaly score.
        const durationExcluded = values.duration > 360;
        const ratios = {
            tokens: safeRatio(values.tokens, baseline.tokens),
            cost: safeRatio(values.cost, baseline.cost),
            duration: durationExcluded ? 0 : safeRatio(values.duration, baseline.duration),
            tools: safeRatio(values.tools, baseline.tools)
        };
        const flags = [];
        if (ratios.tokens >= 2 && values.tokens >= 5000) flags.push({ key: 'TOKENS', label: 'Token cao', level: 'warning', ratio: ratios.tokens });
        if (ratios.cost >= 2 && values.cost >= 0.005) flags.push({ key: 'COST', label: 'Chi phí cao', level: 'warning', ratio: ratios.cost });
        if (durationExcluded) flags.push({ key: 'DURATION', label: 'Có khoảng nghỉ dài', level: 'info', ratio: 0 });
        else if (ratios.duration >= 2 && values.duration >= 5) flags.push({ key: 'DURATION', label: 'Chạy lâu', level: 'info', ratio: ratios.duration });
        if (ratios.tools >= 2 && values.tools >= 5) flags.push({ key: 'TOOLS', label: 'Nhiều tools', level: 'info', ratio: ratios.tools });
        if (Number(conversation.errors || 0) > 0) flags.push({ key: 'ERRORS', label: `${conversation.errors} lỗi`, level: 'critical', ratio: 0 });
        if (Array.isArray(conversation.models_used) && conversation.models_used.length > 1) {
            flags.push({ key: 'MODEL_SWITCH', label: 'Đổi model', level: 'critical', ratio: 0 });
        }

        const maxRatio = Math.max(ratios.tokens, ratios.cost, ratios.duration, ratios.tools, 0);
        const score = flags.reduce((total, flag) => {
            if (flag.key === 'ERRORS') return total + 3 + Math.min(Number(conversation.errors || 0), 5);
            if (flag.key === 'MODEL_SWITCH') return total + 3;
            return total + Math.min(Math.max((flag.ratio || 1) - 1, 0), 5);
        }, 0);
        diagnostics.set(conversationDiagnosticKey(conversation), {
            conversation,
            baseline,
            baselineLevel,
            values,
            ratios,
            flags,
            maxRatio,
            score,
            durationExcluded,
            requiresReview: flags.length > 0
        });
    });

    return diagnostics;
}

function renderSignalBadges(diagnostic, limit = 3) {
    if (!diagnostic || diagnostic.flags.length === 0) {
        return '<span class="signal-badge signal-normal">Bình thường</span>';
    }
    const visible = diagnostic.flags.slice(0, limit);
    const badges = visible.map(flag => `<span class="signal-badge signal-${flag.level}">${escapeHtml(flag.label)}</span>`);
    if (diagnostic.flags.length > visible.length) {
        badges.push(`<span class="signal-badge signal-more">+${diagnostic.flags.length - visible.length}</span>`);
    }
    return `<div class="signal-badge-list">${badges.join('')}</div>`;
}

function formatDiagnosticRatio(ratio) {
    return Number.isFinite(ratio) && ratio > 0 ? `${ratio.toFixed(1)}×` : '—';
}

function renderDiagnosticComparison(diagnostic) {
    if (!diagnostic) return '';
    const statusTitle = diagnostic.requiresReview ? 'Tín hiệu cần kiểm tra' : 'Chưa thấy bất thường đáng kể';
    return `
        <div class="diagnostic-panel ${diagnostic.requiresReview ? 'needs-review' : 'is-normal'}">
            <div class="diagnostic-panel-header">
                <div>
                    <strong>${statusTitle}</strong>
                    <div class="diagnostic-baseline">So với trung vị ${escapeHtml(diagnostic.baseline.label)}, ${diagnostic.baseline.sampleCount} phiên mẫu.</div>
                </div>
                ${renderSignalBadges(diagnostic, 5)}
            </div>
            <div class="diagnostic-comparison-grid">
                <div><span>Tokens</span><strong>${formatDiagnosticRatio(diagnostic.ratios.tokens)}</strong><small>Chuẩn ${formatNumber(diagnostic.baseline.tokens)}</small></div>
                <div><span>Chi phí</span><strong>${formatDiagnosticRatio(diagnostic.ratios.cost)}</strong><small>Chuẩn $${diagnostic.baseline.cost.toFixed(4)}</small></div>
                <div><span>Thời lượng lịch</span><strong>${diagnostic.durationExcluded ? 'Loại khỏi so sánh' : formatDiagnosticRatio(diagnostic.ratios.duration)}</strong><small>${diagnostic.durationExcluded ? 'Trên 6h, có thể gồm thời gian nghỉ' : `Chuẩn ${formatDuration(diagnostic.baseline.duration)}`}</small></div>
                <div><span>Tool calls</span><strong>${formatDiagnosticRatio(diagnostic.ratios.tools)}</strong><small>Chuẩn ${formatNumber(diagnostic.baseline.tools)}</small></div>
            </div>
        </div>
    `;
}

function renderInvestigationDashboard(conversations) {
    conversationDiagnostics = buildConversationDiagnostics(conversations);
    const diagnostics = Array.from(conversationDiagnostics.values());
    const candidates = diagnostics
        .filter(item => item.requiresReview)
        .sort((a, b) => b.score - a.score || b.maxRatio - a.maxRatio || b.values.tokens - a.values.tokens);
    const errorCount = diagnostics.filter(item => item.flags.some(flag => flag.key === 'ERRORS')).length;
    const modelSwitchCount = diagnostics.filter(item => item.flags.some(flag => flag.key === 'MODEL_SWITCH')).length;
    const highest = candidates.reduce((current, item) => !current || item.maxRatio > current.maxRatio ? item : current, null);

    const cards = [
        {
            icon: '⚠', cls: 'rose', cardCls: 'c-rose',
            val: formatNumber(candidates.length),
            label: 'Phiên Cần Kiểm Tra',
            sub: `Trong ${formatNumber(conversations.length)} phiên đã ghi nhận`
        },
        {
            icon: '✕', cls: 'amber', cardCls: 'c-amber',
            val: formatNumber(errorCount),
            label: 'Phiên Có Lỗi',
            sub: 'Ưu tiên xem lại transcript và lần retry'
        },
        {
            icon: '⇄', cls: 'purple', cardCls: 'c-purple',
            val: formatNumber(modelSwitchCount),
            label: 'Phiên Đổi Model',
            sub: 'Có thể làm sai lệch so sánh chi phí và quota'
        },
        {
            icon: '↗', cls: 'cyan', cardCls: 'c-cyan',
            val: highest && highest.maxRatio > 0 ? `${highest.maxRatio.toFixed(1)}×` : '—',
            label: 'Mức Lệch Cao Nhất',
            sub: highest ? `Phiên ${escapeHtml(highest.conversation.short_id || highest.conversation.id || '')}` : 'Chưa có tín hiệu bất thường'
        }
    ];

    const grid = document.getElementById('investigation-summary-grid');
    if (grid) {
        grid.innerHTML = cards.map(card => `
            <div class="summary-card ${card.cardCls}">
                <div class="card-top"><div class="card-icon-badge ${card.cls}">${card.icon}</div></div>
                <div class="card-value">${card.val}</div>
                <div class="card-label">${card.label}</div>
                <div class="card-sub">${card.sub}</div>
            </div>
        `).join('');
    }

    const badge = document.getElementById('anomaly-count-badge');
    if (badge) badge.textContent = `${formatNumber(candidates.length)} phiên`;
    const note = document.getElementById('anomaly-baseline-note');
    if (note) {
        note.textContent = 'Mốc chuẩn là trung vị của cùng nguồn + model khi có từ 3 phiên. Thời lượng lịch trên 6h vẫn được báo nhưng không dùng để tính mức lệch vì có thể chứa thời gian nghỉ.';
    }
    renderAnomalyTable(candidates);
}

function renderAnomalyTable(candidates) {
    const tbody = document.getElementById('anomaly-tbody');
    if (!tbody) return;
    if (!candidates || candidates.length === 0) {
        tbody.innerHTML = '<tr><td colspan="10" class="investigation-empty">Chưa phát hiện phiên nào vượt ngưỡng cần kiểm tra.</td></tr>';
        return;
    }

    tbody.innerHTML = candidates.slice(0, 15).map(item => {
        const conversation = item.conversation;
        const sourceKey = conversation.source || 'ide';
        return `
            <tr class="investigation-row" onclick="inspectConversation('${escapeHtml(conversation.id)}', '${escapeHtml(sourceKey)}')">
                <td><span class="conv-id-badge">${escapeHtml(conversation.short_id || conversation.id)}</span></td>
                <td><span class="model-pill">${escapeHtml(conversation.model_name || 'Mặc định')}</span></td>
                <td>${renderSignalBadges(item, 3)}</td>
                <td class="num"><span class="anomaly-ratio">${item.maxRatio > 0 ? `${item.maxRatio.toFixed(1)}×` : '—'}</span></td>
                <td class="num">${formatNumber(item.values.tokens)}</td>
                <td class="num"><span class="cost-tag">$${item.values.cost.toFixed(4)}</span></td>
                <td class="num">${formatDuration(item.values.duration)}</td>
                <td class="num">${formatNumber(item.values.tools)}</td>
                <td><span class="investigation-date">${formatDate(conversation.start_time)}</span></td>
                <td class="action-col"><button class="btn-inspect" onclick="event.stopPropagation();inspectConversation('${escapeHtml(conversation.id)}', '${escapeHtml(sourceKey)}')">Điều tra</button></td>
            </tr>
        `;
    }).join('');
}

function populateModelFilter(modelsDist) {
    const select = document.getElementById('model-filter');
    const curVal = select.value !== 'ALL'
        ? select.value
        : (typeof uiPreferences.investigationModelFilter === 'string' ? uiPreferences.investigationModelFilter : 'ALL');
    const models = Object.keys(modelsDist);

    select.innerHTML = '<option value="ALL">Tất cả Models</option>' +
        models.map(m => `<option value="${escapeHtml(m)}">${escapeHtml(m)} (${modelsDist[m]})</option>`).join('');

    select.value = models.includes(curVal) ? curVal : 'ALL';
}

// ---- Filter, Search & Sort Logic ----
function applyFiltersAndSort() {
    if (!currentData || !currentData.conversations) return;

    const searchTerm = (document.getElementById('search-input').value || '').toLowerCase().trim();
    const modelFilter = document.getElementById('model-filter').value;
    const signalFilter = document.getElementById('session-signal-filter')?.value || 'ALL';
    const sortType = document.getElementById('sort-select').value;

    let list = [...currentData.conversations];

    // Filter by model
    if (modelFilter !== 'ALL') {
        list = list.filter(c => (c.model_name || '') === modelFilter || (c.models_used && c.models_used.includes(modelFilter)));
    }

    if (signalFilter !== 'ALL') {
        list = list.filter(conversation => {
            const diagnostic = conversationDiagnostics.get(conversationDiagnosticKey(conversation));
            if (!diagnostic) return false;
            if (signalFilter === 'REVIEW') return diagnostic.requiresReview;
            return diagnostic.flags.some(flag => flag.key === signalFilter);
        });
    }

    // Filter by search query
    if (searchTerm) {
        list = list.filter(c =>
            c.id.toLowerCase().includes(searchTerm) ||
            (c.first_user_msg || '').toLowerCase().includes(searchTerm) ||
            (c.model_name || '').toLowerCase().includes(searchTerm) ||
            (c.source || '').toLowerCase().includes(searchTerm) ||
            (c.source_label || '').toLowerCase().includes(searchTerm)
        );
    }

    // Sorting
    list.sort((a, b) => {
        if (sortType === 'anomaly-desc') {
            const scoreA = conversationDiagnostics.get(conversationDiagnosticKey(a))?.score || 0;
            const scoreB = conversationDiagnostics.get(conversationDiagnosticKey(b))?.score || 0;
            return scoreB - scoreA || (b.start_time || '').localeCompare(a.start_time || '');
        }
        if (sortType === 'date-desc') return (b.start_time || '').localeCompare(a.start_time || '');
        if (sortType === 'date-asc') return (a.start_time || '').localeCompare(b.start_time || '');
        if (sortType === 'tokens-desc') return ((b.input_tokens_est + b.output_tokens_est) - (a.input_tokens_est + a.output_tokens_est));
        if (sortType === 'tools-desc') return (b.tool_calls - a.tool_calls);
        if (sortType === 'cost-desc') return ((b.estimated_cost_usd || 0) - (a.estimated_cost_usd || 0));
        return 0;
    });

    filteredConversations = list;
    renderConversationTable(filteredConversations);
}

function renderConversationTable(conversations) {
    const tbody = document.getElementById('conv-tbody');
    if (conversations.length === 0) {
        tbody.innerHTML = `
            <tr>
                <td colspan="14" style="text-align:center;padding:32px;color:var(--text-muted);">
                    Không tìm thấy phiên làm việc nào phù hợp với bộ lọc.
                </td>
            </tr>
        `;
        return;
    }

    tbody.innerHTML = conversations.map(c => {
        const srcKey = c.source || 'ide';
        const srcLabel = c.source_label || (srcKey === 'gemini_cli' ? 'Gemini CLI (Official)' : (srcKey === 'cli' ? 'Antigravity CLI' : 'Antigravity IDE'));
        const diagnostic = conversationDiagnostics.get(conversationDiagnosticKey(c));
        return `
            <tr onclick="inspectConversation('${escapeHtml(c.id)}', '${escapeHtml(srcKey)}')">
                <td><span class="conv-id-badge">${escapeHtml(c.short_id)}</span></td>
                <td><span class="source-pill source-${escapeHtml(srcKey)}">${escapeHtml(srcLabel)}</span></td>
                <td><div class="conv-prompt-text" title="${escapeHtml(c.first_user_msg)}">${escapeHtml(c.first_user_msg || 'Chưa có prompt')}</div></td>
                <td><span class="model-pill">${escapeHtml(c.model_name || 'Mặc định')}</span></td>
                <td><span style="font-family:'JetBrains Mono';font-size:0.75rem;">${formatDate(c.start_time)}</span></td>
                <td><span style="color:var(--cyan-400);font-weight:600;">${formatDuration(c.duration_minutes)}</span></td>
                <td class="num">${c.user_messages}</td>
                <td class="num">${c.model_responses}</td>
                <td class="num"><strong style="color:var(--amber-400);">${c.tool_calls}</strong></td>
                <td class="num">${formatNumber(c.input_tokens_est)}</td>
                <td class="num">${formatNumber(c.output_tokens_est)}</td>
                <td class="num"><span class="cost-tag">$${(c.estimated_cost_usd || 0).toFixed(4)}</span></td>
                <td>${renderSignalBadges(diagnostic, 2)}</td>
                <td class="action-col" style="text-align:center;">
                    <button class="btn-inspect" onclick="event.stopPropagation();inspectConversation('${escapeHtml(c.id)}', '${escapeHtml(srcKey)}')">Xem bước</button>
                </td>
            </tr>
        `;
    }).join('');
}

// ---- Step Inspector Modal ----
async function inspectConversation(convId, source = null) {
    const conv = currentData?.conversations?.find(c => c.id === convId && (!source || c.source === source))
        || currentData?.conversations?.find(c => c.id === convId);
    if (!conv) return;

    const sourceKey = conv.source || source || 'ide';
    const sourceLabel = conv.source_label || (sourceKey === 'gemini_cli' ? 'Gemini CLI (Official)' : (sourceKey === 'cli' ? 'Antigravity CLI' : 'Antigravity IDE'));
    const diagnostic = conversationDiagnostics.get(conversationDiagnosticKey(conv));

    const modalContent = document.getElementById('modal-content');
    const totalTokens = conv.input_tokens_est + conv.output_tokens_est;

    modalContent.innerHTML = `
        <div style="margin-bottom:20px;">
            <div style="display:flex;align-items:center;gap:10px;margin-bottom:6px;">
                <h2 style="font-size:1.3rem;">Phiên: ${escapeHtml(conv.short_id)}</h2>
                <span class="source-pill source-${escapeHtml(sourceKey)}">${escapeHtml(sourceLabel)}</span>
                <span class="model-pill">${escapeHtml(conv.model_name)}</span>
            </div>
            <p style="font-size:0.85rem;color:var(--text-secondary);line-height:1.4;">
                "${escapeHtml(conv.first_user_msg || 'Không có mô tả')}"
            </p>
            <div style="font-size:0.75rem;color:var(--text-muted);margin-top:6px;font-family:'JetBrains Mono';">
                Nguồn: ${escapeHtml(sourceLabel)} • Bắt đầu: ${formatDate(conv.start_time)} • Thời lượng: ${formatDuration(conv.duration_minutes)} • Chi phí ước tính: $${(conv.estimated_cost_usd || 0).toFixed(4)}
            </div>
        </div>

        <div class="modal-stats-grid" style="display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-bottom:20px;">
            <div class="summary-card" style="padding:14px;text-align:center;">
                <div style="font-size:1.3rem;font-weight:800;color:var(--indigo-400);font-family:'JetBrains Mono';">${formatNumber(totalTokens)}</div>
                <div style="font-size:0.7rem;color:var(--text-muted);text-transform:uppercase;">Tổng Tokens</div>
            </div>
            <div class="summary-card" style="padding:14px;text-align:center;">
                <div style="font-size:1.3rem;font-weight:800;color:var(--cyan-400);font-family:'JetBrains Mono';">${formatNumber(conv.input_tokens_est)}</div>
                <div style="font-size:0.7rem;color:var(--text-muted);text-transform:uppercase;">Input Tokens</div>
            </div>
            <div class="summary-card" style="padding:14px;text-align:center;">
                <div style="font-size:1.3rem;font-weight:800;color:var(--amber-400);font-family:'JetBrains Mono';">${formatNumber(conv.output_tokens_est)}</div>
                <div style="font-size:0.7rem;color:var(--text-muted);text-transform:uppercase;">Output Tokens</div>
            </div>
        </div>

        ${renderDiagnosticComparison(diagnostic)}

        <div id="modal-steps-container">
            <div style="text-align:center;padding:24px;color:var(--text-muted);">
                <span>⏳ Đang tải chi tiết các bước...</span>
            </div>
        </div>
    `;

    document.getElementById('modal-overlay').classList.add('active');

    // Fetch step-by-step transcript details via API
    try {
        const srcParam = sourceKey ? `&source=${encodeURIComponent(sourceKey)}` : '';
        const resp = await fetch(`/api/conversation?id=${encodeURIComponent(convId)}${srcParam}`);
        if (resp.ok) {
            const details = await resp.json();
            renderModalSteps(details.steps || []);
        } else {
            renderModalStepsFallback(conv);
        }
    } catch (e) {
        renderModalStepsFallback(conv);
    }
}

function renderModalSteps(steps) {
    const container = document.getElementById('modal-steps-container');
    if (!container) return;

    if (steps.length === 0) {
        container.innerHTML = '<div style="color:var(--text-muted);padding:16px;">Không có bước nào được ghi lại.</div>';
        return;
    }

    container.innerHTML = `
        <h3 style="font-size:0.95rem;margin-bottom:12px;display:flex;align-items:center;gap:8px;">
            <span>Lịch Sử Tiến Trình (${steps.length} bước)</span>
        </h3>
        <div class="steps-list">
            ${steps.map(s => {
                let badgeClass = 'system';
                if (s.type === 'USER_INPUT') badgeClass = 'user';
                else if (s.type === 'PLANNER_RESPONSE') badgeClass = 'model';
                else if (['RUN_COMMAND', 'VIEW_FILE', 'LIST_DIRECTORY', 'GREP_SEARCH'].includes(s.type)) badgeClass = 'tool';

                return `
                    <div class="step-card">
                        <div class="step-header">
                            <span class="step-badge ${badgeClass}">${s.type}</span>
                            <span style="font-size:0.72rem;color:var(--text-muted);font-family:'JetBrains Mono';">
                                ~${formatNumber(s.tokens_est)} tokens • ${formatBytes(s.size_bytes)}
                            </span>
                        </div>
                        ${s.preview ? `<div class="step-preview">${escapeHtml(s.preview)}</div>` : ''}
                    </div>
                `;
            }).join('')}
        </div>
    `;
}

function renderModalStepsFallback(conv) {
    const container = document.getElementById('modal-steps-container');
    if (!container) return;

    const toolEntries = Object.entries(conv.tool_types || {});
    container.innerHTML = `
        <h3 style="font-size:0.95rem;margin-bottom:12px;">Phân Bổ Công Cụ Đã Dùng</h3>
        <div style="display:flex;flex-direction:column;gap:6px;">
            ${toolEntries.length > 0 ? toolEntries.map(([tool, count]) => `
                <div style="display:flex;justify-content:space-between;background:var(--bg-surface);padding:8px 12px;border-radius:6px;font-family:'JetBrains Mono';font-size:0.8rem;">
                    <span>${tool}</span>
                    <strong style="color:var(--cyan-400);">${count} lần</strong>
                </div>
            `).join('') : '<div style="color:var(--text-muted);">Không dùng tool nào</div>'}
        </div>
    `;
}

function closeModal() {
    const modal = document.getElementById('modal-overlay');
    if (modal) modal.classList.remove('active');
}

// ---- Auto-Sync & Countdown Engine ----
function setupAutoSync() {
    const select = document.getElementById('auto-sync-select');
    const countdownWrapper = document.getElementById('countdown-wrapper');
    const countdownLabel = document.getElementById('countdown-timer');
    const liveBadge = document.getElementById('live-badge');
    const syncModeLabel = document.getElementById('sync-mode-label');
    select.value = String(autoSyncInterval);

    function updateSyncConfig() {
        autoSyncInterval = parseInt(select.value, 10);
        countdownSeconds = autoSyncInterval;

        if (syncTimer) clearInterval(syncTimer);
        if (countdownTimer) clearInterval(countdownTimer);

        if (autoSyncInterval <= 0) {
            countdownWrapper.style.display = 'none';
            liveBadge.style.opacity = '0.5';
            syncModeLabel.textContent = 'MANUAL';
        } else {
            countdownWrapper.style.display = 'flex';
            liveBadge.style.opacity = '1';
            syncModeLabel.textContent = `LIVE (${autoSyncInterval}s)`;
            countdownLabel.textContent = `${countdownSeconds}s`;

            // 1-second interval for countdown timer
            countdownTimer = setInterval(() => {
                countdownSeconds--;
                if (countdownSeconds <= 0) {
                    countdownSeconds = autoSyncInterval;
                    loadUsageData(false).catch(() => {});
                }
                countdownLabel.textContent = `${countdownSeconds}s`;
            }, 1000);
        }
    }

    select.addEventListener('change', () => {
        saveUiPreferences({ autoSyncInterval: select.value });
        updateSyncConfig();
    });
    updateSyncConfig();
}

// ---- Export JSON Report ----
function setupExport() {
    const btn = document.getElementById('btn-export');
    if (!btn) return;

    btn.addEventListener('click', () => {
        if (!currentData) {
            showToast('Chưa có dữ liệu để xuất!', true);
            return;
        }

        const dataStr = "data:text/json;charset=utf-8," + encodeURIComponent(JSON.stringify(currentData, null, 2));
        const dlAnchor = document.createElement('a');
        const filename = `usage_tracker_report_${new Date().toISOString().slice(0, 10)}.json`;
        dlAnchor.setAttribute("href", dataStr);
        dlAnchor.setAttribute("download", filename);
        document.body.appendChild(dlAnchor);
        dlAnchor.click();
        dlAnchor.remove();
        showToast(`Đã xuất file báo cáo ${filename}!`);
    });
}

// Tab Switching Engine
function setupTabs() {
    const tabs = document.querySelectorAll('.nav-tab');
    const tabIds = [...tabs].map(tab => tab.getAttribute('data-tab')).filter(Boolean);
    const restoredTabId = savedChoice('activeTab', tabIds, 'tab-usage');
    const restoredTab = [...tabs].find(tab => tab.getAttribute('data-tab') === restoredTabId);
    if (restoredTab) {
        tabs.forEach(tab => tab.classList.toggle('active', tab === restoredTab));
        document.querySelectorAll('.tab-pane').forEach(pane => {
            pane.classList.toggle('active', pane.id === restoredTabId);
        });
    }
    tabs.forEach(tab => {
        tab.addEventListener('click', () => {
            tabs.forEach(t => t.classList.remove('active'));
            document.querySelectorAll('.tab-pane').forEach(p => p.classList.remove('active'));

            tab.classList.add('active');
            const targetId = tab.getAttribute('data-tab');
            saveUiPreferences({ activeTab: targetId });
            const pane = document.getElementById(targetId);
            if (pane) pane.classList.add('active');

            // Trigger redraw of charts on tab switch
            if (currentData) {
                setTimeout(() => {
                    if (targetId === 'tab-usage') {
                        renderInvestigationDashboard(currentData.conversations || []);
                        applyFiltersAndSort();
                    } else if (targetId === 'tab-quota') {
                        renderQuotas(currentData.summary.quotas, currentData.summary.source_breakdowns || currentData.summary.source_breakdown);
                        renderCodexRateLimits(currentData.summary.codex_usage);
                        renderCodexWeeklyCapacity(currentData.summary.codex_usage);
                        renderAccountManager(currentData.summary.current_account, currentData.summary.accounts_manager);
                        renderTimeSeriesAnalytics(currentData.summary.time_series);
                    } else if (targetId === 'tab-leaderboard') {
                        renderLeaderboard(
                            currentData.summary.leaderboard || [],
                            currentData.summary.models_breakdown || [],
                            currentData.summary.codex_usage || {}
                        );
                    } else if (targetId === 'tab-models') {
                        renderModelsBreakdown(
                            currentData.summary.models_breakdown || [],
                            currentData.summary.codex_usage || {},
                            currentData.summary.models_daily_timeline
                        );
                    } else if (targetId === 'tab-task-outcomes') {
                        renderCodexTaskOutcomes(currentData.summary.codex_usage || {});
                    }
                }, 50);
            }
        });
    });
}

// ---- Leaderboard Rendering Engine ----
let enrichedLeaderboardRows = [];
let leaderboardRepairCapabilities = { pairs: [] };

function leaderboardText(value) {
    const text = String(value ?? '');
    return window.UsageI18n?.t ? window.UsageI18n.t(text) : text;
}

function normalizeLeaderboardKey(value) {
    return String(value || '')
        .trim()
        .toLowerCase()
        .replace(/[-_/]+/g, ' ')
        .replace(/\s+/g, ' ')
        .replace(/ medium$/, ' standard');
}

function enrichLeaderboardData(leaderboard, breakdown, codexUsage) {
    const usageByKey = new Map();
    (breakdown || []).forEach(row => {
        const key = normalizeLeaderboardKey(row.model_id || row.model_name);
        if (!key) return;
        const current = usageByKey.get(key) || {
            total_tokens: 0, total_cost_usd: 0, sessions: 0, responses: 0, cost_known: true,
        };
        current.total_tokens += Number(row.total_tokens || 0);
        current.total_cost_usd += Number(row.total_cost_usd || 0);
        current.sessions += Number(row.sessions || 0);
        current.responses += Number(row.responses || 0);
        current.cost_known = current.cost_known && row.cost_known !== false;
        usageByKey.set(key, current);
    });

    const automatic = codexUsage?.automatic_model_usage || {};
    const automaticModelRows = Array.isArray(automatic.models)
        ? automatic.models
        : Object.entries(automatic.models || {}).map(([modelName, row]) => ({ model_name: modelName, ...(row || {}) }));
    const automaticUsageByKey = new Map(
        automaticModelRows.map(row => [normalizeLeaderboardKey(row.model_name || row.model_key || row.model_id), row])
    );
    const efficiencyByKey = new Map(
        (automatic.quota_efficiency?.models || []).map(row => [normalizeLeaderboardKey(row.model_key), row])
    );
    const taskQuotaByKey = new Map(
        (automatic.quota_per_task?.models || []).map(row => [normalizeLeaderboardKey(row.model_key), row])
    );

    return (leaderboard || []).map(item => {
        const key = normalizeLeaderboardKey(item.model_name);
        const local = usageByKey.get(key);
        const automaticLocal = automaticUsageByKey.get(key);
        const efficiency = efficiencyByKey.get(key);
        const taskQuota = taskQuotaByKey.get(key);
        // Model Breakdown already includes automatic Codex logs. Add the raw
        // automatic row only when a caller did not supply a breakdown row.
        const fallbackAutomatic = local ? null : automaticLocal;
        const legacyTokens = Number(local?.total_tokens ?? item.local_total_tokens ?? 0);
        const automaticTokens = Number(fallbackAutomatic?.total_tokens ?? 0);
        const legacyCost = Number(local?.total_cost_usd ?? item.local_cost_usd ?? 0);
        const automaticCost = Number(fallbackAutomatic?.cost_usd ?? 0);
        const legacySessions = Number(local?.sessions ?? item.local_sessions ?? 0);
        const automaticSessions = Number(fallbackAutomatic?.sessions ?? 0);
        const legacyResponses = Number(local?.responses ?? item.local_responses ?? 0);
        const automaticResponses = Number(fallbackAutomatic?.responses ?? 0);
        return {
            ...item,
            local_total_tokens: legacyTokens + automaticTokens,
            local_cost_usd: legacyCost + automaticCost,
            local_cost_known: (local ? local.cost_known !== false : true) && (fallbackAutomatic ? fallbackAutomatic.cost_known !== false : true),
            local_sessions: legacySessions + automaticSessions,
            local_responses: legacyResponses + automaticResponses,
            local_quota_pct_per_1m_tokens: efficiency?.quota_pct_per_1m_tokens ?? null,
            local_relative_quota_burn: efficiency?.relative_quota_burn_vs_sol_high ?? null,
            local_quota_sample_count: efficiency?.sample_count ?? 0,
            local_quota_confidence: efficiency?.confidence ?? null,
            local_estimated_quota_pct_per_task: taskQuota?.estimated_quota_pct_per_task ?? null,
            local_task_relative_quota_burn: taskQuota?.relative_task_quota_burn_vs_sol_high ?? null,
            local_task_count: taskQuota?.task_count ?? 0,
            local_task_confidence: taskQuota?.confidence ?? null,
        };
    });
}

function renderLeaderboard(leaderboard, breakdown = [], codexUsage = {}) {
    enrichedLeaderboardRows = enrichLeaderboardData(leaderboard, breakdown, codexUsage);
    leaderboardRepairCapabilities = codexUsage?.automatic_model_usage?.task_outcomes?.repair_capabilities || { pairs: [] };
    if (enrichedLeaderboardRows.length === 0) return;
    renderLeaderboardViews();
    renderLeaderboardRepairCapabilities();
    renderModelCards(enrichedLeaderboardRows);
}

function renderAASyncStatus(sync, leaderboard) {
    const rows = leaderboard.filter(row => row.benchmark_source === 'Artificial Analysis' && row.benchmark_as_of);
    const newest = [...rows].sort((a, b) => String(b.benchmark_as_of).localeCompare(String(a.benchmark_as_of)))[0];
    const version = newest?.benchmark_index_version || '4.3.2';
    const date = newest?.benchmark_as_of || sync.snapshot_as_of || '';
    const isEnglish = window.UsageI18n?.language === 'en';
    const tag = document.getElementById('aa-hero-tag');
    if (tag) tag.textContent = `⚡ ARTIFICIAL ANALYSIS INTELLIGENCE INDEX v${version} • ${isEnglish ? 'UPDATED' : 'CẬP NHẬT'} ${date}`;
    const indexLabel = document.getElementById('aa-index-label');
    if (indexLabel) indexLabel.textContent = `Artificial Analysis v${version}`;
    const comparison = document.getElementById('aa-comparison-title');
    if (comparison) comparison.textContent = isEnglish
        ? `Intelligence Index v${version} and Output Speed`
        : `So Sánh Intelligence Index v${version} Và Tốc Độ Output`;
    const chartTitle = document.getElementById('aa-iq-chart-title');
    if (chartTitle) chartTitle.textContent = `Intelligence Index v${version}`;

    const status = document.getElementById('aa-sync-status');
    if (!status) return;
    const lastGood = sync.last_success_at?.slice(0, 10);
    if (sync.state === 'needs_key') {
        status.textContent = isEnglish
            ? `Official AA daily sync needs a server-side API key. Showing the verified snapshot from ${date}.`
            : `Tự đồng bộ AA hằng ngày cần API key ở server. Đang dùng bản chụp đã xác minh ngày ${date}.`;
    } else if (sync.state === 'refreshing') {
        status.textContent = isEnglish
            ? `Refreshing from the official AA API in the background${lastGood ? `; last successful sync: ${lastGood}` : ''}.`
            : `Đang cập nhật nền từ API chính thức của AA${lastGood ? `; lần đồng bộ thành công gần nhất: ${lastGood}` : ''}.`;
    } else if (sync.state === 'error') {
        status.textContent = isEnglish
            ? `AA sync failed (${sync.error || 'unknown error'}); showing the last verified data${lastGood ? ` from ${lastGood}` : ''}.`
            : `Đồng bộ AA lỗi (${sync.error || 'không rõ'}); giữ dữ liệu đã xác minh${lastGood ? ` ngày ${lastGood}` : ''}.`;
    } else if (lastGood) {
        status.textContent = isEnglish
            ? `Official AA API sync: ${lastGood} · ${sync.cached_models || 0} matched model variants · checked daily.`
            : `Đồng bộ API chính thức của AA: ${lastGood} · ${sync.cached_models || 0} biến thể đã ghép · kiểm tra hằng ngày.`;
    } else {
        status.textContent = isEnglish ? `Verified AA snapshot: ${date}.` : `Bản chụp AA đã xác minh: ${date}.`;
    }
}

function hasLeaderboardMetric(value) {
    return value !== null && value !== undefined && value !== '' && Number.isFinite(Number(value));
}

function leaderboardMetric(value, suffix = '') {
    return hasLeaderboardMetric(value) ? `${value}${suffix}` : '—';
}

function formatAACostPerTask(value) {
    if (!hasLeaderboardMetric(value)) return '—';
    const cost = Number(value);
    const digits = cost > 0 && cost < 0.01 ? 8 : cost < 0.1 ? 4 : 2;
    return `$${cost.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: digits })}`;
}

function compareMetricDesc(field) {
    return (a, b) => {
        const aHas = hasLeaderboardMetric(a[field]);
        const bHas = hasLeaderboardMetric(b[field]);
        if (aHas !== bHas) return aHas ? -1 : 1;
        if (!aHas) return 0;
        return Number(b[field]) - Number(a[field]);
    };
}

function compareMetricAsc(field) {
    return (a, b) => {
        const aHas = hasLeaderboardMetric(a[field]);
        const bHas = hasLeaderboardMetric(b[field]);
        if (aHas !== bHas) return aHas ? -1 : 1;
        if (!aHas) return 0;
        return Number(a[field]) - Number(b[field]);
    };
}

function getFilteredLeaderboardRows() {
    const scope = document.getElementById('leaderboard-scope-select')?.value || 'selectable';
    const family = document.getElementById('leaderboard-family-select')?.value || 'all';
    return enrichedLeaderboardRows.filter(item => {
        const inScope = scope === 'all'
            || (scope === 'selectable' && (item.selectable_in_codex || item.observed_locally))
            || (scope === 'recommended' && item.recommended)
            || (scope === 'local' && Number(item.local_total_tokens || 0) > 0)
            || (scope === 'current' && item.generation_status !== 'previous');
        return inScope && (family === 'all' || item.family === family);
    });
}

function renderLeaderboardViews() {
    const filtered = getFilteredLeaderboardRows();
    renderLeaderboardHighlights(filtered);
    renderLeaderboardCharts(filtered);
    renderLeaderboardTable(filtered);
    const count = document.getElementById('leaderboard-result-count');
    if (count) count.textContent = leaderboardText(`${filtered.length}/${enrichedLeaderboardRows.length} biến thể`);
}

function renderLeaderboardHighlights(leaderboard) {
    const grid = document.getElementById('leaderboard-highlights');
    if (!grid) return;

    const topIQ = [...leaderboard].filter(m => hasLeaderboardMetric(m.intelligence_index)).sort(compareMetricDesc('intelligence_index'))[0];
    const topSpeed = [...leaderboard].filter(m => hasLeaderboardMetric(m.speed_tps)).sort(compareMetricDesc('speed_tps'))[0];
    const topValue = [...leaderboard].filter(m => hasLeaderboardMetric(m.value_score)).sort(compareMetricDesc('value_score'))[0];
    const topUsage = [...leaderboard].filter(m => Number(m.local_total_tokens || 0) > 0).sort((a, b) => b.local_total_tokens - a.local_total_tokens)[0];

    const cards = [
        {
            cls: 'purple', cardCls: 'c-purple', icon: '🧠',
            val: topIQ ? `${topIQ.intelligence_index} IQ` : 'N/A',
            label: `#1 Intelligence Index v${topIQ?.benchmark_index_version || '4.3.2'}`,
            sub: topIQ ? `${topIQ.display_name || topIQ.model_name} • ${leaderboardText(topIQ.benchmark_status === 'estimate' ? 'AA ước tính' : (topIQ.benchmark_status === 'published' ? 'AA công bố' : 'AA đo độc lập'))}` : leaderboardText('Không có model trong bộ lọc')
        },
        {
            cls: 'cyan', cardCls: 'c-cyan', icon: '⚡',
            val: topSpeed ? `${topSpeed.speed_tps} t/s` : 'N/A',
            label: `#1 ${leaderboardText('Tốc Độ Sinh Token')}`,
            sub: topSpeed ? `${topSpeed.display_name || topSpeed.model_name} • ${leaderboardText('output tokens/giây')}` : leaderboardText('Không có model trong bộ lọc')
        },
        {
            cls: 'emerald', cardCls: 'c-emerald', icon: '💎',
            val: topValue ? `${topValue.value_score} IQ/$` : 'N/A',
            label: `#1 ${leaderboardText('Giá Trị Theo AA Task')}`,
            sub: topValue ? `${topValue.display_name || topValue.model_name} • $${topValue.cost_per_task}/task` : leaderboardText('AA chưa công bố đủ cost/task')
        },
        {
            cls: 'indigo', cardCls: 'c-indigo', icon: '📊',
            val: topUsage ? formatNumber(topUsage.local_total_tokens) : 'N/A',
            label: `#1 ${leaderboardText('Dùng Nhiều Nhất Cục Bộ')}`,
            sub: topUsage ? `${topUsage.display_name || topUsage.model_name} • ${leaderboardText(`${topUsage.local_sessions} phiên`)}` : leaderboardText('Chưa có usage trong bộ lọc')
        }
    ];

    grid.innerHTML = cards.map(c => `
        <div class="summary-card ${c.cardCls}">
            <div class="card-top"><div class="card-icon-badge ${c.cls}">${c.icon}</div></div>
            <div class="card-value">${c.val}</div>
            <div class="card-label">${c.label}</div>
            <div class="card-sub">${c.sub}</div>
        </div>
    `).join('');
}

function renderLeaderboardCharts(leaderboard) {
    const iqCanvas = document.getElementById('leaderboard-iq-chart');
    if (iqCanvas) {
        const iqData = [...leaderboard]
            .filter(m => hasLeaderboardMetric(m.intelligence_index))
            .sort(compareMetricDesc('intelligence_index'))
            .slice(0, 14)
            .map(m => ({
                label: (m.display_name || m.model_name).replace('Gemini ', 'G-'),
                value: m.intelligence_index,
                color: colorForModelKey(m.model_name || m.display_name),
            }));
        CanvasCharts.drawHorizontalBars(iqCanvas, iqData);
    }

    const speedCanvas = document.getElementById('leaderboard-speed-chart');
    if (speedCanvas) {
        const speedData = leaderboard.filter(m => hasLeaderboardMetric(m.speed_tps)).map(m => ({
            label: (m.display_name || m.model_name).replace('Gemini ', 'G-'),
            value: m.speed_tps,
            color: colorForModelKey(m.model_name || m.display_name),
        })).sort((a, b) => b.value - a.value).slice(0, 14);
        CanvasCharts.drawHorizontalBars(speedCanvas, speedData);
    }
}

function formatLeaderboardPrice(item) {
    const hasApiPrice = hasLeaderboardMetric(item.price_in_1m) && hasLeaderboardMetric(item.price_out_1m);
    const cached = hasLeaderboardMetric(item.price_cached_in_1m) ? `$${item.price_cached_in_1m}` : '—';
    const estimate = item.pricing_estimated ? ` • ${leaderboardText('ước tính')}` : '';
    const isFast = String(item.model_name || '').toLowerCase().endsWith('(fast)');
    const fastPriceLabel = window.UsageI18n?.language === 'en'
        ? ' · Fast credit-equivalent' : ' · quy đổi credit Fast';
    const apiPrice = hasApiPrice
        ? `<strong>${isFast ? '~' : ''}$${item.price_in_1m}</strong><div class="leaderboard-cell-sub">cache ${cached} • out $${item.price_out_1m}${estimate}${isFast ? fastPriceLabel : ''}</div>`
        : '<span class="leaderboard-na">—</span>';
    const creditSource = item.codex_credit_rate_source_url === 'https://learn.chatgpt.com/docs/pricing'
        ? ` <a href="${item.codex_credit_rate_source_url}" target="_blank" rel="noopener noreferrer">↗</a>`
        : '';
    const credits = hasLeaderboardMetric(item.codex_credit_in_1m)
        ? `<div class="leaderboard-cell-sub" title="${escapeHtml(isFast ? 'Fast credit rates derived from the official Standard rates and the documented Fast multiplier.' : leaderboardText('Credit Codex chính thức · tốc độ Standard; khác credit chuẩn hóa trong biểu đồ chi phí'))}">${isFast ? 'Codex credit Fast' : escapeHtml(leaderboardText('Codex credit Standard'))}: ${item.codex_credit_in_1m} / ${item.codex_credit_cached_in_1m} / ${item.codex_credit_out_1m}${creditSource}</div>`
        : '';
    return `${apiPrice}${credits}`;
}

function formatLocalQuota(item) {
    const task = Number(item.local_estimated_quota_pct_per_task);
    const perMillion = Number(item.local_quota_pct_per_1m_tokens);
    if (Number.isFinite(task) && task > 0) {
        const relative = Number(item.local_task_relative_quota_burn);
        return `<strong>${task.toFixed(2)}% / task</strong>
            <div class="leaderboard-cell-sub">${item.local_task_count} task${Number.isFinite(relative) ? ` • ${relative.toFixed(2)}× Sol High` : ''}</div>`;
    }
    if (Number.isFinite(perMillion) && perMillion > 0) {
        return `<strong>${perMillion.toFixed(2)}% / 1M raw</strong>
            <div class="leaderboard-cell-sub">${escapeHtml(leaderboardText(`${item.local_quota_sample_count} khoảng đo • ${item.local_quota_confidence || ''}`))}</div>`;
    }
    return `<span class="leaderboard-na">${escapeHtml(leaderboardText('Chưa đủ mẫu'))}</span>`;
}

function renderLeaderboardTable(leaderboard) {
    const tbody = document.getElementById('leaderboard-tbody');
    const sortSelect = document.getElementById('leaderboard-sort-select');
    if (!tbody) return;

    const sortType = sortSelect ? sortSelect.value : 'recommendation';
    let list = [...leaderboard];
    if (sortType === 'recommendation') list.sort((a, b) => {
        const aOrder = a.recommendation_order == null ? 999 : Number(a.recommendation_order);
        const bOrder = b.recommendation_order == null ? 999 : Number(b.recommendation_order);
        const aSelection = a.selection_order == null ? 999 : Number(a.selection_order);
        const bSelection = b.selection_order == null ? 999 : Number(b.selection_order);
        return aOrder - bOrder || aSelection - bSelection || (a.rank || 999) - (b.rank || 999);
    });
    else if (sortType === 'iq-desc') list.sort(compareMetricDesc('intelligence_index'));
    else if (sortType === 'speed-desc') list.sort(compareMetricDesc('speed_tps'));
    else if (sortType === 'value-desc') list.sort(compareMetricDesc('value_score'));
    else if (sortType === 'usage-desc') list.sort((a, b) => b.local_total_tokens - a.local_total_tokens);
    else if (sortType === 'cost-asc') list.sort(compareMetricAsc('cost_per_task'));
    else if (sortType === 'quota-task-asc') list.sort(compareMetricAsc('local_estimated_quota_pct_per_task'));

    if (list.length === 0) {
        tbody.innerHTML = '<tr><td colspan="9" class="leaderboard-empty">Không có model phù hợp với bộ lọc hiện tại.</td></tr>';
        return;
    }

    tbody.innerHTML = list.map(item => {
        let rankClass = 'rank-other';
        if (item.rank === 1) rankClass = 'rank-1';
        else if (item.rank === 2) rankClass = 'rank-2';
        else if (item.rank === 3) rankClass = 'rank-3';
        const rankLabel = item.rank ? `#${item.rank}` : '—';
        const provClass = item.provider.toLowerCase().includes('openai') ? 'provider-openai' : 'provider-google';
        const statusText = leaderboardText(item.benchmark_status === 'estimate'
            ? 'AA ước tính'
            : (item.benchmark_status === 'measured' ? 'AA đo độc lập'
                : (item.benchmark_status === 'published' ? 'AA công bố' : 'AA chưa công bố')));
        const statusClass = item.benchmark_status === 'estimate'
            ? 'aa-estimate'
            : (item.benchmark_status === 'measured' || item.benchmark_status === 'published' ? 'aa-measured' : 'aa-unavailable');
        const selectableBadge = item.selectable_in_codex
            ? `<span class="aa-status aa-selectable">${escapeHtml(leaderboardText('có thể chọn'))}</span>`
            : (item.observed_locally ? `<span class="aa-status aa-selectable">${escapeHtml(leaderboardText('đã thấy trong log'))}</span>` : '');
        const priorBadge = item.generation_status === 'previous' ? `<span class="aa-status aa-previous">${escapeHtml(leaderboardText('đời trước'))}</span>` : '';
        const sourceLine = item.benchmark_source_url
            ? `<a class="leaderboard-source-link" href="${escapeHtml(item.benchmark_source_url)}" target="_blank" rel="noopener">AA v${escapeHtml(item.benchmark_index_version || '4.3.2')} • ${escapeHtml(item.benchmark_as_of || '')}</a>`
            : (item.metadata_source_url
                ? `<a class="leaderboard-source-link" href="${escapeHtml(item.metadata_source_url)}" target="_blank" rel="noopener">${escapeHtml(item.metadata_source || leaderboardText('Nguồn nhà cung cấp'))}</a> • ${escapeHtml(leaderboardText('AA chưa chấm'))}`
                : escapeHtml(leaderboardText('AA chưa có dữ liệu cho model này')));
        const costTask = hasLeaderboardMetric(item.cost_per_task)
            ? `<strong>${formatAACostPerTask(item.cost_per_task)}</strong><div class="leaderboard-cell-sub">${leaderboardMetric(item.value_score)} IQ/$</div>`
            : `<span class="leaderboard-na">${escapeHtml(leaderboardText('AA chưa công bố'))}</span>`;
        const localCost = item.local_cost_known === false ? leaderboardText('chưa định giá đủ') : `$${Number(item.local_cost_usd || 0).toFixed(2)}`;
        const decisionLabel = leaderboardText(item.decision_label || (item.selectable_in_codex ? 'Có thể chọn trong Codex' : (item.generation_status === 'previous' ? 'Dữ liệu lịch sử' : item.badge)));
        const decisionNote = leaderboardText(item.decision_note || item.best_for || '');
        const localSessions = leaderboardText(`${item.local_sessions} phiên`);

        return `
            <tr>
                <td data-sort-value="${item.rank || ''}"><span class="rank-badge ${rankClass}">${rankLabel}</span></td>
                <td>
                    <div class="leaderboard-model-name">${escapeHtml(item.display_name || item.model_name)} <span class="provider-badge ${provClass}">${escapeHtml(item.provider)}</span></div>
                    <div class="leaderboard-status-row"><span class="aa-status ${statusClass}">${statusText}</span>${selectableBadge}${priorBadge}</div>
                    <div class="leaderboard-cell-sub">${sourceLine}</div>
                </td>
                <td class="num" data-sort-value="${hasLeaderboardMetric(item.intelligence_index) ? Number(item.intelligence_index) : ''}"><strong class="leaderboard-iq">${leaderboardMetric(item.intelligence_index)}</strong></td>
                <td class="num" data-sort-value="${hasLeaderboardMetric(item.speed_tps) ? Number(item.speed_tps) : ''}"><strong class="leaderboard-speed">${leaderboardMetric(item.speed_tps, ' t/s')}</strong></td>
                <td class="num" data-sort-value="${hasLeaderboardMetric(item.cost_per_task) ? Number(item.cost_per_task) : ''}">${costTask}</td>
                <td class="num leaderboard-price-cell" data-sort-value="${hasLeaderboardMetric(item.price_in_1m) ? Number(item.price_in_1m) : ''}">${formatLeaderboardPrice(item)}</td>
                <td class="num" data-sort-value="${hasLeaderboardMetric(item.local_estimated_quota_pct_per_task) && Number(item.local_estimated_quota_pct_per_task) > 0 ? Number(item.local_estimated_quota_pct_per_task) : ''}">${formatLocalQuota(item)}</td>
                <td class="num" data-sort-value="${Number(item.local_total_tokens || 0)}"><strong class="leaderboard-local-tokens">${formatNumber(item.local_total_tokens)}</strong><div class="leaderboard-cell-sub">${localCost} all-time • ${escapeHtml(localSessions)}</div></td>
                <td class="leaderboard-decision-cell"><strong>${escapeHtml(decisionLabel)}</strong><div>${escapeHtml(decisionNote)}</div></td>
            </tr>
        `;
    }).join('');
}

function leaderboardDisplayName(modelKey) {
    const normalized = normalizeLeaderboardKey(modelKey);
    const match = enrichedLeaderboardRows.find(row => normalizeLeaderboardKey(row.model_name) === normalized);
    return match?.display_name || modelKey || '—';
}

function renderLeaderboardRepairCapabilities() {
    const tbody = document.getElementById('leaderboard-rescue-tbody');
    const count = document.getElementById('leaderboard-rescue-count');
    if (!tbody) return;

    const categoryLabels = {
        trading_setup: 'Học setup giao dịch',
        web: 'Làm web',
        simulation_3d: 'Mô phỏng 3D',
        software_debugging: 'Sửa lỗi phần mềm',
        research: 'Nghiên cứu & kiểm chứng',
        documents: 'Xử lý tài liệu',
        system_diagnostics: 'Chẩn đoán hệ thống',
        other: 'Công việc khác',
    };
    const pairs = (leaderboardRepairCapabilities?.pairs || [])
        .filter(row => Number(row.accepted_repairs || 0) > 0)
        .slice(0, 20);
    if (count) count.textContent = leaderboardText(`${pairs.length} cặp đã quan sát`);
    if (!pairs.length) {
        tbody.innerHTML = '<tr><td colspan="6" class="leaderboard-empty">Chưa có chuỗi sửa khác model đã đạt yêu cầu.</td></tr>';
        return;
    }

    tbody.innerHTML = pairs.map(row => {
        const accepted = Number(row.accepted_repairs || 0);
        const attempts = Number(row.attempts || 0);
        const rate = Number(row.success_rate);
        const hasQuota = hasLeaderboardMetric(row.median_repair_quota_pct_5h);
        const quota = hasQuota ? Number(row.median_repair_quota_pct_5h) : null;
        const hasDelta = hasLeaderboardMetric(row.intelligence_delta);
        const delta = hasDelta ? Number(row.intelligence_delta) : null;
        const categories = (row.categories || []).map(key => leaderboardText(categoryLabels[key] || key)).join(' · ') || leaderboardText('Chưa phân loại');
        let prior = leaderboardText('AA chưa đủ dữ liệu để so sánh năng lực');
        if (hasDelta && Number.isFinite(delta)) {
            prior = leaderboardText(delta > 0
                ? `AA Intelligence cao hơn ${delta.toFixed(0)} điểm`
                : (delta === 0 ? 'AA Intelligence ngang nhau' : `AA Intelligence thấp hơn ${Math.abs(delta).toFixed(0)} điểm`));
        }
        const confidence = row.empirical_confidence || 'low';
        return `
            <tr>
                <td><strong>${escapeHtml(leaderboardDisplayName(row.from_model))}</strong></td>
                <td><strong>${escapeHtml(leaderboardDisplayName(row.to_model))}</strong></td>
                <td>${escapeHtml(categories)}</td>
                <td class="num" data-sort-value="${Number.isFinite(rate) ? rate : ''}"><strong>${accepted}/${attempts}</strong><div class="leaderboard-cell-sub">${Number.isFinite(rate) ? (rate * 100).toFixed(0) + '%' : '—'}</div></td>
                <td class="num">${hasQuota && Number.isFinite(quota) ? `<strong>${quota.toFixed(2)}% 5h</strong><div class="leaderboard-cell-sub">n=${row.quota_sample_count || 0}</div>` : '<span class="leaderboard-na">Chưa đo được</span>'}</td>
                <td><strong>${escapeHtml(leaderboardText(`Đã sửa thành công · tin cậy ${confidence}`))}</strong><div class="leaderboard-cell-sub"><span>${escapeHtml(prior)}</span><br><span>${escapeHtml(leaderboardText('AA chỉ là tín hiệu bổ trợ.'))}</span></div></td>
            </tr>
        `;
    }).join('');
}

function renderModelCards(leaderboard) {
    const grid = document.getElementById('model-cards-grid');
    if (!grid) return;
    const recommended = [...leaderboard]
        .filter(m => m.recommended)
        .sort((a, b) => Number(a.recommendation_order || 999) - Number(b.recommendation_order || 999));

    grid.innerHTML = recommended.map(m => `
        <div class="model-card">
            <div>
                <div class="model-card-top"><h3>${escapeHtml(m.display_name || m.model_name)}</h3><span class="recommendation-order">${escapeHtml(leaderboardText(`Lựa chọn ${m.recommendation_order}`))}</span></div>
                <div class="model-card-badge">${escapeHtml(leaderboardText(m.decision_label || m.badge))}</div>
                <p class="model-card-desc">${escapeHtml(leaderboardText(m.decision_note || m.best_for))}</p>
                <div class="model-card-metrics">
                    <div class="m-metric"><div class="m-metric-val" style="color:var(--purple-400);">${leaderboardMetric(m.intelligence_index)}</div><div class="m-metric-lbl">Intelligence</div></div>
                    <div class="m-metric"><div class="m-metric-val" style="color:var(--cyan-400);">${leaderboardMetric(m.speed_tps)}</div><div class="m-metric-lbl">Speed (t/s)</div></div>
                    <div class="m-metric"><div class="m-metric-val" style="color:var(--emerald-400);">${hasLeaderboardMetric(m.cost_per_task) ? '$' + m.cost_per_task : '—'}</div><div class="m-metric-lbl">AA Cost / Task</div></div>
                </div>
            </div>
            <div class="model-card-empirical">
                <span>${escapeHtml(leaderboardText('Cục bộ:'))} <strong>${formatNumber(m.local_total_tokens)} tokens</strong></span>
                <span>${hasLeaderboardMetric(m.local_estimated_quota_pct_per_task) ? Number(m.local_estimated_quota_pct_per_task).toFixed(2) + '% quota/task' : escapeHtml(leaderboardText('chưa đủ mẫu quota/task'))}</span>
            </div>
        </div>
    `).join('');
}

// Global State for Live Quotas Real-time Clocks
let liveQuotasState = null;
let liveClockInterval = null;

// Helper: Format exact reset time and countdown (Supports Full Reset vs First Partial Batch)
function formatResetClock(resetsAtIso, nextBatchIso, fallbackMin, isFull, isWeeklyCapped, weeklyResetAtIso) {
    const english = window.UsageI18n?.language === 'en';
    const locale = english ? 'en-US' : 'vi-VN';
    if (isWeeklyCapped) {
        if (weeklyResetAtIso) {
            try {
                const wdt = new Date(weeklyResetAtIso);
                const dateStr = wdt.toLocaleDateString(locale, { weekday: 'short', day: '2-digit', month: '2-digit' });
                const timeStr = wdt.toLocaleTimeString(locale, { hour: '2-digit', minute: '2-digit' });
                return english ? `⛔ Weekly limit (opens: ${dateStr} ${timeStr})` : `⛔ Khóa theo Tuần (Mở: ${dateStr} ${timeStr})`;
            } catch {}
        }
        return english ? '⛔ Weekly quota depleted (0%)' : '⛔ Khóa do hết Hạn Mức Tuần (0%)';
    }

    if (isFull) return english ? '🟢 Fully recovered (100%)' : '🟢 Đã đầy 100% (Tối ưu)';

    if (!resetsAtIso && !nextBatchIso) {
        if (fallbackMin && fallbackMin > 0) return english ? `In ~${fallbackMin} minutes` : `Sau ~${fallbackMin} phút`;
        return english ? '🟢 Fully recovered (100%)' : '🟢 Đã đầy 100%';
    }

    try {
        const now = new Date();
        const fullDt = resetsAtIso ? new Date(resetsAtIso) : null;
        const batchDt = nextBatchIso ? new Date(nextBatchIso) : null;

        // Target for 100% recovery
        const targetDt = fullDt || batchDt;
        const diffMs = targetDt - now;

        if (diffMs <= 0) {
            return english ? '⚡ Recovering quota...' : '⚡ Đang hồi phục token...';
        }

        const totalSec = Math.floor(diffMs / 1000);
        const hours = Math.floor(totalSec / 3600);
        const mins = Math.floor((totalSec % 3600) / 60);
        const secs = totalSec % 60;

        let countdownStr = '';
        if (hours > 0) {
            countdownStr = `${hours}h ${mins.toString().padStart(2, '0')}${english ? 'm' : 'p'} ${secs.toString().padStart(2, '0')}s`;
        } else {
            countdownStr = `${mins}${english ? 'm' : 'p'} ${secs.toString().padStart(2, '0')}s`;
        }

        const timeStr = targetDt.toLocaleTimeString(locale, { hour: '2-digit', minute: '2-digit', second: '2-digit' });

        // If there's an earlier partial batch recovery
        if (batchDt && fullDt && batchDt.getTime() < fullDt.getTime() && (batchDt - now) > 0) {
            const batchSec = Math.floor((batchDt - now) / 1000);
            const bMin = Math.floor(batchSec / 60);
            const bSec = batchSec % 60;
            return english ? `${timeStr} (~${countdownStr} remaining • first batch: ~${bMin}m ${bSec}s)` : `${timeStr} (Còn ~${countdownStr} • Đợt đầu: ~${bMin}p ${bSec}s)`;
        }

        return english ? `${timeStr} (~${countdownStr} remaining)` : `${timeStr} (Còn ~${countdownStr})`;
    } catch {
        return english ? `In ~${fallbackMin} minutes` : `Sau ~${fallbackMin} phút`;
    }
}

// Helper: Format weekly reset time and countdown
function formatWeeklyResetClock(resetsAtIso, fallbackHours, isFull) {
    const english = window.UsageI18n?.language === 'en';
    const ready = english ? '🟢 Ready (100%)' : '🟢 Đã sẵn sàng 100%';
    if (isFull) return ready;
    if (!resetsAtIso) {
        if (fallbackHours && fallbackHours > 0) {
            const d = Math.floor(fallbackHours / 24);
            const h = Math.round(fallbackHours % 24);
            return d > 0 ? (english ? `In ~${d} days ${h}h` : `Sau ~${d} ngày ${h}h`) : (english ? `In ~${h} hours` : `Sau ~${h} giờ`);
        }
        return ready;
    }
    try {
        const dt = new Date(resetsAtIso);
        const locale = english ? 'en-US' : 'vi-VN';
        const dateStr = dt.toLocaleDateString(locale, { weekday: 'short', day: '2-digit', month: '2-digit' });
        const timeStr = dt.toLocaleTimeString(locale, { hour: '2-digit', minute: '2-digit' });
        const now = new Date();
        const diffMs = dt - now;
        if (diffMs <= 0) return english ? '⚡ Weekly reset in progress...' : '⚡ Đang reset tuần...';

        const totalSec = Math.floor(diffMs / 1000);
        const d = Math.floor(totalSec / 86400);
        const h = Math.floor((totalSec % 86400) / 3600);
        const m = Math.floor((totalSec % 3600) / 60);
        const s = totalSec % 60;

        let countdownStr = '';
        if (d > 0) {
            countdownStr = english ? `${d} days ${h}h ${m}m` : `${d} ngày ${h}h ${m}p`;
        } else if (h > 0) {
            countdownStr = `${h}h ${m}${english ? 'm' : 'p'} ${s}s`;
        } else {
            countdownStr = `${m}${english ? 'm' : 'p'} ${s}s`;
        }

        return english ? `${dateStr} ${timeStr} (~${countdownStr} remaining)` : `${dateStr} ${timeStr} (Còn ~${countdownStr})`;
    } catch {
        return english ? `In ~${fallbackHours} hours` : `Sau ~${fallbackHours} giờ`;
    }
}

function startLiveResetCountdown() {
    if (liveClockInterval) clearInterval(liveClockInterval);
    liveClockInterval = setInterval(() => {
        if (!liveQuotasState) return;

        const win5h = liveQuotasState.five_hour_window;
        const winWk = liveQuotasState.weekly_window;

        if (win5h) {
            const g5 = win5h.gemini;
            if (g5) {
                const g5Clock = document.getElementById('gemini-5h-reset-clock');
                const isFull = (g5.percentage_remaining >= 99.9 && g5.used_tokens === 0);
                if (g5Clock) {
                    g5Clock.textContent = formatResetClock(g5.resets_at, g5.next_batch_resets_at, g5.resets_in_minutes, isFull, g5.is_weekly_capped, winWk?.gemini?.resets_at);
                }
            }
            const e5 = win5h.external;
            if (e5) {
                const e5Clock = document.getElementById('external-5h-reset-clock');
                const isFull = (e5.percentage_remaining >= 99.9 && e5.used_tokens === 0);
                if (e5Clock) {
                    e5Clock.textContent = formatResetClock(e5.resets_at, e5.next_batch_resets_at, e5.resets_in_minutes, isFull, e5.is_weekly_capped, winWk?.external?.resets_at);
                }
            }
        }

        if (winWk) {
            const gw = winWk.gemini;
            if (gw) {
                const gwClock = document.getElementById('gemini-wk-reset-clock');
                const isFull = (gw.percentage_remaining >= 99.9 && gw.used_tokens === 0);
                if (gwClock) {
                    gwClock.textContent = formatWeeklyResetClock(gw.resets_at, gw.resets_in_hours, isFull);
                }
            }
            const ew = winWk.external;
            if (ew) {
                const ewClock = document.getElementById('external-wk-reset-clock');
                const isFull = (ew.percentage_remaining >= 99.9 && ew.used_tokens === 0);
                if (ewClock) {
                    ewClock.textContent = formatWeeklyResetClock(ew.resets_at, ew.resets_in_hours, isFull);
                }
            }
        }

        const codexLimits = currentData?.summary?.codex_usage?.rate_limits;
        if (codexLimits?.available && codexLimits?.windows) {
            codexLimits.windows.forEach((w, idx) => {
                const el = document.getElementById(`codex-window-reset-clock-${idx}`);
                if (el && w.resets_at) {
                    const isFull = (Number(w.remaining_percent) >= 99.9);
                    el.textContent = (w.window_minutes >= 1440)
                        ? formatWeeklyResetClock(w.resets_at, null, isFull)
                        : formatResetClock(w.resets_at, null, null, isFull, false, null);
                }
            });
        }
    }, 1000);
}

function renderQuotaSourceSummary(sourceBreakdowns) {
    const container = document.getElementById('quota-source-grid');
    if (!container) return;
    const sb = sourceBreakdowns || {};

    const ideData = sb['ide'] || { label: 'Antigravity IDE', gemini_5h_tokens: 0, gemini_weekly_tokens: 0, total_tokens: 0 };
    const cliData = sb['cli'] || { label: 'Antigravity CLI', gemini_5h_tokens: 0, gemini_weekly_tokens: 0, total_tokens: 0 };
    const geminiCliData = sb['gemini_cli'] || { label: 'Gemini CLI (Official)', gemini_5h_tokens: 0, gemini_weekly_tokens: 0, total_tokens: 0 };

    const sourcesList = [
        { key: 'ide', ...ideData, label: ideData.label || 'Antigravity IDE' },
        { key: 'cli', ...cliData, label: cliData.label || 'Antigravity CLI' },
        { key: 'gemini_cli', ...geminiCliData, label: geminiCliData.label || 'Gemini CLI (Official)' }
    ];

    for (const [k, v] of Object.entries(sb)) {
        if (k !== 'ide' && k !== 'cli' && k !== 'gemini_cli' && v && typeof v === 'object') {
            sourcesList.push({ key: k, ...v, label: v.label || k });
        }
    }

    container.innerHTML = sourcesList.map(s => {
        const g5Toks = Number(s.gemini_5h_tokens || 0);
        const gwToks = Number(s.gemini_weekly_tokens || 0);
        const totalToks = Number(s.total_tokens || 0);
        const unitLabel = window.UsageI18n?.language === 'en'
            ? (s.token_unit ? 'measured report tokens' : 'estimated tokens')
            : (s.token_unit ? 'token thực từ report' : 'token ước lượng');
        const key = escapeHtml(s.key || 'source');
        const label = escapeHtml(s.label || key);

        return `
            <div class="quota-source-card">
                <div class="quota-source-card-top">
                    <span class="source-pill source-${key}">${label}</span>
                    <span class="quota-source-total">${formatNumber(totalToks)} ${unitLabel}</span>
                </div>
                <div class="quota-source-metrics">
                    <div class="q-source-metric">
                        <span class="q-source-lbl">Gemini 5 Giờ:</span>
                        <strong class="q-source-val" style="color:var(--cyan-400);">${formatNumber(g5Toks)}</strong>
                    </div>
                    <div class="q-source-metric">
                        <span class="q-source-lbl">Gemini Tuần (7D):</span>
                        <strong class="q-source-val" style="color:var(--indigo-400);">${formatNumber(gwToks)}</strong>
                    </div>
                </div>
            </div>
        `;
    }).join('');
}

// ---- Quota & Remaining Limit Rendering Engine ----
function renderQuotas(quotas, sourceBreakdowns = null) {
    if (!quotas) return;
    liveQuotasState = quotas;
    startLiveResetCountdown();

    renderQuotaSourceSummary(sourceBreakdowns || currentData?.summary?.source_breakdowns || currentData?.summary?.source_breakdown || {});

    // 1. 5-Hour Window
    const win5h = quotas.five_hour_window;
    const winWk = quotas.weekly_window;

    if (win5h) {
        // Reset countdown timer (Top Box)
        const resetEl = document.getElementById('quota-5h-reset-time');
        if (resetEl) {
            const g5Next = win5h.gemini?.next_batch_resets_at;
            const g5Full = win5h.gemini?.resets_at;
            if (win5h.gemini?.is_weekly_capped) {
                resetEl.textContent = `⛔ Khóa do hết Hạn Mức Tuần`;
            } else if (g5Full) {
                try {
                    const dt = new Date(g5Full);
                    resetEl.textContent = dt.toLocaleTimeString('vi-VN', { hour: '2-digit', minute: '2-digit' });
                } catch {
                    resetEl.textContent = `~${win5h.resets_in_minutes} phút`;
                }
            } else {
                resetEl.textContent = `● Đã hồi phục 100%`;
            }
        }

        // Gemini 5h
        const g5 = win5h.gemini;
        if (g5) {
            const displayPct = (g5.percentage_remaining !== undefined && g5.percentage_remaining !== null)
                ? g5.percentage_remaining
                : (g5.percentage_real ?? 100);

            setElementText('gemini-5h-pct', `${displayPct}%`);
            setElementText('gemini-5h-used', `${formatNumber(g5.used_tokens)} tokens`);
            setElementText('gemini-5h-rem', `${formatNumber(g5.remaining_tokens)} tokens`);
            setElementText('gemini-5h-limit', formatNumber(g5.limit_tokens));
            const workerRuns5h = Number(g5.worker_runs_count || 0);
            setElementText('gemini-5h-reqs', workerRuns5h > 0
                ? `${g5.requests_count} prompts + ${workerRuns5h} worker runs`
                : `${g5.requests_count} prompts`);

            const g5Bar = document.getElementById('gemini-5h-bar');
            if (g5Bar) g5Bar.style.width = `${Math.min(100, Math.max(0, displayPct))}%`;

            const isFull = (displayPct >= 99.9 && g5.used_tokens === 0);
            const isExhausted = displayPct <= 0.0 || g5.status === 'Locked (Weekly Capped)' || g5.is_weekly_capped;
            const g5Status = document.getElementById('gemini-5h-status');
            if (g5Status) {
                const isNormal = displayPct > 25;
                if (Number(g5.worker_used_tokens || 0) > 0 && !g5.worker_prediction_applied) {
                    g5Status.textContent = `⚠️ Đã thấy ${formatNumber(g5.worker_used_tokens)} token worker; nhập % chính thức 1 lần để hiệu chuẩn`;
                    g5Status.className = `quota-status-pill pill-warning`;
                } else if (g5.status === 'Locked (Weekly Capped)' || g5.is_weekly_capped) {
                    g5Status.textContent = `⛔ Khóa do hết Hạn Mức Tuần (0%)`;
                    g5Status.className = `quota-status-pill pill-danger`;
                } else if (isExhausted) {
                    g5Status.textContent = `⛔ Hết hạn mức (0%)`;
                    g5Status.className = `quota-status-pill pill-danger`;
                } else if (isFull) {
                    g5Status.textContent = `● Đầy 100% (Tối ưu)`;
                    g5Status.className = `quota-status-pill pill-success`;
                } else {
                    g5Status.textContent = isNormal ? `● Đang dùng (${displayPct}%)` : `⚠️ Gần hết (${displayPct}%)`;
                    g5Status.className = `quota-status-pill ${isNormal ? 'pill-info' : 'pill-warning'}`;
                }
            }

            const g5Clock = document.getElementById('gemini-5h-reset-clock');
            if (g5Clock) {
                g5Clock.textContent = formatResetClock(g5.resets_at, g5.next_batch_resets_at, g5.resets_in_minutes, isFull, g5.is_weekly_capped, winWk?.gemini?.resets_at);
            }
        }

        // External 5h
        const e5 = win5h.external;
        if (e5) {
            const displayPct = (e5.percentage_remaining !== undefined && e5.percentage_remaining !== null)
                ? e5.percentage_remaining
                : (e5.percentage_real ?? 100);

            setElementText('external-5h-pct', `${displayPct}%`);
            setElementText('external-5h-used', `${formatNumber(e5.used_tokens)} tokens`);
            setElementText('external-5h-rem', `${formatNumber(e5.remaining_tokens)} tokens`);
            setElementText('external-5h-limit', formatNumber(e5.limit_tokens));
            setElementText('external-5h-reqs', `${e5.requests_count} prompts`);

            const e5Bar = document.getElementById('external-5h-bar');
            if (e5Bar) e5Bar.style.width = `${Math.min(100, Math.max(0, displayPct))}%`;

            const isFull = (displayPct >= 99.9 && e5.used_tokens === 0);
            const isExhausted = displayPct <= 0.0 || e5.status === 'Locked (Weekly Capped)' || e5.is_weekly_capped;
            const e5Status = document.getElementById('external-5h-status');
            if (e5Status) {
                const isNormal = displayPct > 25;
                if (e5.status === 'Locked (Weekly Capped)' || e5.is_weekly_capped) {
                    e5Status.textContent = `⛔ Khóa do hết Hạn Mức Tuần (0%)`;
                    e5Status.className = `quota-status-pill pill-danger`;
                } else if (isExhausted) {
                    e5Status.textContent = `⛔ Hết hạn mức (0%)`;
                    e5Status.className = `quota-status-pill pill-danger`;
                } else if (isFull) {
                    e5Status.textContent = `● Đầy 100% (Tối ưu)`;
                    e5Status.className = `quota-status-pill pill-success`;
                } else {
                    e5Status.textContent = isNormal ? `● Đang dùng (${displayPct}%)` : `⚠️ Gần hết (${displayPct}%)`;
                    e5Status.className = `quota-status-pill ${isNormal ? 'pill-info' : 'pill-warning'}`;
                }
            }

            const e5Clock = document.getElementById('external-5h-reset-clock');
            if (e5Clock) {
                e5Clock.textContent = formatResetClock(e5.resets_at, e5.next_batch_resets_at, e5.resets_in_minutes, isFull, e5.is_weekly_capped, winWk?.external?.resets_at);
            }
        }
    }

    // 2. Weekly Window
    if (winWk) {
        // Gemini Weekly
        const gw = winWk.gemini;
        if (gw) {
            const displayPct = (gw.percentage_remaining !== undefined && gw.percentage_remaining !== null)
                ? gw.percentage_remaining
                : (gw.percentage_real ?? 100);

            setElementText('gemini-wk-pct', `${displayPct}%`);
            setElementText('gemini-wk-used', `${formatNumber(gw.used_tokens)} tokens`);
            setElementText('gemini-wk-rem', `${formatNumber(gw.remaining_tokens)} tokens`);
            setElementText('gemini-wk-limit', formatNumber(gw.limit_tokens));
            const workerRunsWeek = Number(gw.worker_runs_count || 0);
            setElementText('gemini-wk-reqs', workerRunsWeek > 0
                ? `${gw.requests_count} prompts + ${workerRunsWeek} worker runs`
                : `${gw.requests_count} prompts`);

            const gwBar = document.getElementById('gemini-wk-bar');
            if (gwBar) gwBar.style.width = `${Math.min(100, Math.max(0, displayPct))}%`;

            const isFull = (displayPct >= 99.9 && gw.used_tokens === 0);
            const isExhausted = displayPct <= 0.0;
            const gwStatus = document.getElementById('gemini-wk-status');
            if (gwStatus) {
                const isNormal = displayPct > 25;
                if (isExhausted) {
                    gwStatus.textContent = `⛔ Hết hạn mức tuần (0%)`;
                    gwStatus.className = `quota-status-pill pill-danger`;
                } else if (isFull) {
                    gwStatus.textContent = `● Đầy 100% (Tối ưu)`;
                    gwStatus.className = `quota-status-pill pill-success`;
                } else {
                    gwStatus.textContent = isNormal ? `● Đang dùng (${displayPct}%)` : `⚠️ Gần hết (${displayPct}%)`;
                    gwStatus.className = `quota-status-pill ${isNormal ? 'pill-info' : 'pill-warning'}`;
                }
            }

            const gwClock = document.getElementById('gemini-wk-reset-clock');
            if (gwClock) {
                gwClock.textContent = formatWeeklyResetClock(gw.resets_at, gw.resets_in_hours, isFull);
            }
        }

        // External Weekly
        const ew = winWk.external;
        if (ew) {
            const displayPct = (ew.percentage_remaining !== undefined && ew.percentage_remaining !== null)
                ? ew.percentage_remaining
                : (ew.percentage_real ?? 100);

            setElementText('external-wk-pct', `${displayPct}%`);
            setElementText('external-wk-used', `${formatNumber(ew.used_tokens)} tokens`);
            setElementText('external-wk-rem', `${formatNumber(ew.remaining_tokens)} tokens`);
            setElementText('external-wk-limit', formatNumber(ew.limit_tokens));
            setElementText('external-wk-reqs', `${ew.requests_count} prompts`);

            const ewBar = document.getElementById('external-wk-bar');
            if (ewBar) ewBar.style.width = `${Math.min(100, Math.max(0, displayPct))}%`;

            const isFull = (displayPct >= 99.9 && ew.used_tokens === 0);
            const isExhausted = displayPct <= 0.0;
            const ewStatus = document.getElementById('external-wk-status');
            if (ewStatus) {
                const isNormal = displayPct > 25;
                if (isExhausted) {
                    ewStatus.textContent = `⛔ Hết hạn mức tuần (0%)`;
                    ewStatus.className = `quota-status-pill pill-danger`;
                } else if (isFull) {
                    ewStatus.textContent = `● Đầy 100% (Tối ưu)`;
                    ewStatus.className = `quota-status-pill pill-success`;
                } else {
                    ewStatus.textContent = isNormal ? `● Đang dùng (${displayPct}%)` : `⚠️ Gần hết (${displayPct}%)`;
                    ewStatus.className = `quota-status-pill ${isNormal ? 'pill-info' : 'pill-warning'}`;
                }
            }

            const ewClock = document.getElementById('external-wk-reset-clock');
            if (ewClock) {
                ewClock.textContent = formatWeeklyResetClock(ew.resets_at, ew.resets_in_hours, isFull);
            }
        }
    }
}

// ---- Live Codex Rate Limits Rendering Engine ----
function drawCodexWeeklyCapacity(canvas, points, windowDays) {
    const chart = CanvasCharts.initCanvas(canvas);
    if (!chart) return;
    const { ctx, width, height } = chart;
    ctx.clearRect(0, 0, width, height);
    const pad = { top: 20, right: 24, bottom: 40, left: 72 };
    const chartW = Math.max(1, width - pad.left - pad.right);
    const chartH = Math.max(1, height - pad.top - pad.bottom);
    const values = points.flatMap(point => [
        Number(point.full_week_usd), Number(point.recent?.[windowDays]?.median_usd),
        Number(point.cumulative_median_usd)
    ]).filter(value => Number.isFinite(value) && value > 0);
    canvas.onmousemove = null;
    if (!values.length) return;
    const maxVal = Math.max(...values) * 1.12;
    ctx.font = '500 10px "JetBrains Mono", monospace';
    ctx.textBaseline = 'middle';
    ctx.textAlign = 'right';
    for (let i = 0; i <= 4; i++) {
        const value = maxVal * i / 4;
        const y = pad.top + chartH * (1 - i / 4);
        ctx.strokeStyle = 'rgba(255,255,255,0.07)';
        ctx.beginPath(); ctx.moveTo(pad.left, y); ctx.lineTo(pad.left + chartW, y); ctx.stroke();
        ctx.fillStyle = '#94a3b8';
        ctx.fillText(`$${formatNumber(Math.round(value))}`, pad.left - 8, y);
    }
    const xAt = index => pad.left + chartW * (points.length === 1 ? 0.5 : index / (points.length - 1));
    const labelStep = Math.max(1, Math.ceil(points.length / 6));
    points.forEach((point, index) => {
        if (index % labelStep !== 0 && index !== points.length - 1) return;
        const date = new Date(point.observed_at);
        const label = `${date.getDate()}/${date.getMonth() + 1}/${String(date.getFullYear()).slice(2)}`;
        ctx.fillStyle = '#94a3b8'; ctx.textBaseline = 'top'; ctx.textAlign = 'center';
        ctx.fillText(label, xAt(index), pad.top + chartH + 8);
    });
    const drawLine = (getValue, color, dashed) => {
        ctx.beginPath();
        points.forEach((point, index) => {
            const value = Number(getValue(point));
            const x = xAt(index);
            const y = pad.top + chartH * (1 - value / maxVal);
            if (index === 0) ctx.moveTo(x, y);
            else ctx.lineTo(x, y);
        });
        ctx.setLineDash(dashed ? [5, 5] : []);
        ctx.strokeStyle = color; ctx.lineWidth = dashed ? 1.5 : 2.5; ctx.stroke();
        ctx.setLineDash([]);
    };
    drawLine(point => point.cumulative_median_usd, THEME.purple, true);
    drawLine(point => point.recent?.[windowDays]?.median_usd, THEME.cyan, false);
    points.forEach((point, index) => {
        const value = Number(point.full_week_usd);
        const x = xAt(index);
        const y = pad.top + chartH * (1 - value / maxVal);
        ctx.fillStyle = THEME.amber;
        ctx.beginPath(); ctx.arc(x, y, points.length > 150 ? 2 : 3, 0, Math.PI * 2); ctx.fill();
    });
    canvas.onmousemove = event => {
        const rect = canvas.getBoundingClientRect();
        const x = event.clientX - rect.left;
        const index = points.length === 1 ? 0 : Math.max(0, Math.min(points.length - 1,
            Math.round((x - pad.left) / chartW * (points.length - 1))));
        const point = points[index];
        if (!point) return;
        canvas.title = `${point.observed_at}\n${point.model_key}\n${point.quota_delta_pct}% quota • ${formatNumber(point.token_delta)} tokens\nEstimated cost equivalent: $${point.api_cost_delta_usd} → $${point.full_week_usd} / week`;
    };
}

function renderCodexWeeklyCapacity(codexUsage) {
    const history = codexUsage?.automatic_model_usage?.weekly_capacity_history;
    const identityEvents = codexUsage?.automatic_model_usage?.quota_identity_events || [];
    const canvas = document.getElementById('canvas-codex-weekly-capacity');
    const accountSelect = document.getElementById('codex-weekly-capacity-account');
    const windowSelect = document.getElementById('codex-weekly-capacity-window');
    const rangeSelect = document.getElementById('codex-weekly-capacity-range');
    const status = document.getElementById('codex-weekly-capacity-status');
    const note = document.getElementById('codex-weekly-capacity-note');
    const eventsBox = document.getElementById('codex-weekly-capacity-events');
    if (!canvas || !accountSelect || !windowSelect || !rangeSelect || !status) return;
    const en = document.documentElement.lang === 'en';
    const allPoints = Array.isArray(history?.points) ? history.points : [];
    const currentAccount = codexUsage?.rate_limits?.account_id || null;
    const accountCounts = history?.account_sample_counts || {};
    const accountIds = [...new Set([
        ...Object.keys(accountCounts).filter(id => id !== '__unknown__'),
        ...identityEvents.map(event => event.account_id).filter(Boolean),
        currentAccount
    ].filter(Boolean))].sort();
    const accountLabel = id => id === '__unknown__' ?
        (en ? 'Unattributed history' : 'Lịch sử chưa rõ tài khoản') :
        `${en ? 'Account' : 'Tài khoản'} …${id.slice(-8)}${id === currentAccount ? (en ? ' (signed in)' : ' (đang đăng nhập)') : ''}`;
    const choices = [
        ...(accountCounts.__unknown__ ? ['__unknown__'] : []),
        ...accountIds
    ];
    const fallbackAccount = accountCounts.__unknown__ ? '__unknown__' : (currentAccount || choices[0]);
    const selectedAccount = savedChoice('codexWeeklyCapacityAccount', choices, fallbackAccount);
    accountSelect.replaceChildren(...choices.map(id => new Option(
        `${accountLabel(id)} (${accountCounts[id] || 0})`, id
    )));
    if (selectedAccount) accountSelect.value = selectedAccount;
    const accountPoints = allPoints.filter(point => (point.account_id || '__unknown__') === selectedAccount);
    const windowDays = savedChoice('codexWeeklyCapacityWindow', ['7', '14', '30'], '14');
    const range = savedChoice('codexWeeklyCapacityRange', ['30', '90', '180', '365'], '90');
    windowSelect.value = windowDays;
    rangeSelect.value = range;
    if (!accountPoints.length) {
        const chart = CanvasCharts.initCanvas(canvas);
        chart?.ctx.clearRect(0, 0, chart.width, chart.height);
        status.textContent = en ? 'Not enough weekly quota observations for this account yet.' : 'Chưa đủ phép đo hạn mức tuần cho tài khoản này.';
        if (note) note.textContent = en ? 'Only nearby live quota snapshots can identify an account; older logs remain unattributed.' :
            'Chỉ phép đo khớp với bản chụp hạn mức trực tiếp gần thời điểm đó mới được gắn tài khoản; log cũ vẫn chưa rõ tài khoản.';
        renderCodexWeeklyCapacityEvents(eventsBox, identityEvents, selectedAccount, accountLabel, en);
        return;
    }
    const cutoff = Date.now() - Number(range) * 86400000;
    const points = accountPoints.filter(point => Date.parse(point.observed_at) >= cutoff);
    drawCodexWeeklyCapacity(canvas, points, windowDays);
    const latest = points.at(-1);
    if (!latest) {
        status.textContent = en ? 'No comparable measurements in this period.' :
            'Chưa có phép đo trong khoảng này.';
    } else {
        const recentNow = accountPoints.filter(point => Date.parse(point.observed_at) >= Date.now() - Number(windowDays) * 86400000);
        const recentMedian = recentNow.length ? [...recentNow].map(point => Number(point.full_week_usd)).sort((a, b) => a - b) : [];
        const middle = Math.floor(recentMedian.length / 2);
        const current = recentMedian.length ? (recentMedian.length % 2 ? recentMedian[middle] :
            (recentMedian[middle - 1] + recentMedian[middle]) / 2) : null;
        const sessionCount = new Set(recentNow.map(point => point.session).filter(Boolean)).size;
        const quotaSpan = recentNow.reduce((sum, point) => sum + (Number(point.quota_delta_pct) || 0), 0);
        const confidence = recentNow.length >= 5 && sessionCount >= 3 && quotaSpan >= 30 ?
            (en ? 'high confidence' : 'độ tin cậy cao') :
            recentNow.length >= 3 && sessionCount >= 2 && quotaSpan >= 10 ?
                (en ? 'medium confidence' : 'độ tin cậy trung bình') :
                (en ? 'low confidence' : 'độ tin cậy thấp');
        const recentText = current === null ? (en ? `no measurements in the last ${windowDays} days` : `không có phép đo trong ${windowDays} ngày qua`) :
            (en ? `${windowDays}-day median $${current.toFixed(2)} (${recentNow.length} measurements, ${confidence})` :
                `trung vị ${windowDays} ngày gần đây $${current.toFixed(2)} (${recentNow.length} phép đo, ${confidence})`);
        status.textContent = en ? `Latest individual estimate ${new Date(latest.observed_at).toLocaleString()}: $${Number(latest.full_week_usd).toFixed(2)} per full week • ${recentText} • cumulative median $${Number(latest.cumulative_median_usd).toFixed(2)}` :
            `Lần đo cuối ${new Date(latest.observed_at).toLocaleString()}: $${Number(latest.full_week_usd).toFixed(2)} cho 100% hạn mức tuần • ${recentText} • trung vị hội tụ $${Number(latest.cumulative_median_usd).toFixed(2)}`;
    }
    if (note) note.textContent = en ?
        `Showing ${points.length}/${accountPoints.length} intervals for ${accountLabel(selectedAccount)}. X-axis: measurement sequence. USD is an estimated cost equivalent, not official quota or credit. Fast uses the ChatGPT credit multiplier. Concurrent sessions and rounded percentages can distort estimates; old logs have no account ID.` :
        `Đang hiện ${points.length}/${accountPoints.length} khoảng đo của ${accountLabel(selectedAccount)}. Trục ngang: thứ tự lần đo. USD là chi phí quy đổi ước tính, không phải hạn mức chính thức hay credit. Fast dùng hệ số credit ChatGPT. Phiên song song và % làm tròn có thể gây lệch; log cũ không có ID tài khoản.`;
    renderCodexWeeklyCapacityEvents(eventsBox, identityEvents, selectedAccount, accountLabel, en);
}

function renderCodexWeeklyCapacityEvents(box, events, accountId, accountLabel, en) {
    if (!box) return;
    const labels = {
        account_switch_observed: en ? 'Account switch confirmed' : 'Đã xác nhận đổi tài khoản',
        scheduled_weekly_reset_observed: en ? 'Scheduled weekly reset observed' : 'Đã quan sát reset tuần đúng lịch',
        early_weekly_reset_unverified: en ? 'Early reset; cause unverified' : 'Reset sớm; chưa rõ nguyên nhân',
        cycle_change_after_gap: en ? 'Cycle changed during a data gap' : 'Chu kỳ đổi trong lúc thiếu dữ liệu',
        weekly_state_change_unverified: en ? 'Weekly state changed; cause unverified' : 'Trạng thái tuần đổi; chưa rõ nguyên nhân'
    };
    const visible = (Array.isArray(events) ? events : [])
        .filter(event => accountId === '__unknown__' || event.account_id === accountId)
        .slice(-6).reverse();
    const heading = document.createElement('div');
    heading.textContent = en ? 'Recent account / reset evidence:' : 'Dấu vết đổi tài khoản / reset gần đây:';
    box.replaceChildren(heading);
    if (!visible.length) {
        const line = document.createElement('div');
        line.textContent = en ? 'No directly observed transitions yet.' : 'Chưa có chuyển tiếp được quan sát trực tiếp.';
        box.append(line);
    }
    visible.forEach(event => {
        const line = document.createElement('div');
        line.textContent = `${new Date(event.observed_at).toLocaleString()} · ${labels[event.kind] || event.kind} · ${accountLabel(event.account_id)}`;
        box.append(line);
    });
    const explanation = document.createElement('div');
    explanation.textContent = en ? 'An early reset alone cannot prove a global OpenAI reset; confirmation requires provider evidence.' :
        'Reset sớm tự nó chưa chứng minh là global reset của OpenAI; cần đối chiếu nguồn công bố.';
    box.append(explanation);
}

function renderCodexRateLimits(codexUsage) {
    const container = document.getElementById('codex-rate-limit-cards');
    const freshnessBox = document.getElementById('codex-freshness-box');
    const freshnessLabel = document.getElementById('codex-freshness-label');
    if (!container) return;

    const rateLimits = codexUsage?.rate_limits;
    if (!rateLimits || !rateLimits.available || !rateLimits.windows || rateLimits.windows.length === 0) {
        if (freshnessBox) freshnessBox.style.display = 'none';
        container.innerHTML = `
            <div class="quota-unavailable-card">
                <div style="font-size:1.4rem;margin-bottom:6px;">📡</div>
                <div style="font-weight:700;color:var(--text-main);font-size:0.95rem;margin-bottom:4px;">Chưa Có Dữ Liệu Hạn Mức Codex</div>
                <div style="font-size:0.8rem;color:var(--text-muted);max-width:540px;margin:0 auto;line-height:1.4;">
                    Chưa đọc được hạn mức trực tiếp từ Codex và chưa có bản ghi phiên làm việc để đối chiếu. Hãy kiểm tra Codex đã được cài đặt và đăng nhập.
                </div>
            </div>
        `;
        return;
    }

    const isLive = rateLimits.source === 'codex_app_server';
    const sourceLabel = isLive ? 'Codex trực tiếp' : 'Bản ghi phiên Codex';
    if (freshnessBox && freshnessLabel) {
        freshnessBox.style.display = 'inline-flex';
        const obsTime = rateLimits.observed_at ? formatDate(rateLimits.observed_at) : 'Gần đây';
        const fallbackNote = rateLimits.fallback_reason ? ' • Chưa kết nối trực tiếp; số liệu có thể chưa cập nhật.' : '';
        freshnessLabel.innerHTML = `Nguồn: <strong>${sourceLabel}</strong> • Ghi nhận: <strong>${obsTime}</strong>${fallbackNote}`;
    }

    const planBadge = rateLimits.plan_type ? `<span class="brand-badge brand-codex" style="text-transform:uppercase;font-size:0.68rem;font-weight:700;">${window.UsageI18n?.language === 'en' ? 'Plan' : 'Gói'}: ${escapeHtml(rateLimits.plan_type)}</span>` : '';

    container.innerHTML = rateLimits.windows.map((w, idx) => {
        const remPct = (w.remaining_percent !== undefined && w.remaining_percent !== null) ? Number(w.remaining_percent).toFixed(1) : '100.0';
        const usedPct = (w.used_percent !== undefined && w.used_percent !== null) ? Number(w.used_percent).toFixed(1) : '0.0';
        const durationText = window.UsageI18n?.language === 'en'
            ? (w.window_minutes >= 60 ? `${w.window_minutes / 60}-Hour Window` : `${w.window_minutes}-Minute Window`)
            : (w.label || (w.window_minutes >= 60 ? `Khung ${w.window_minutes / 60} Giờ` : `Khung ${w.window_minutes} Phút`));
        const clockId = `codex-window-reset-clock-${idx}`;

        let statusClass = 'pill-info';
        if (w.status === 'Optimal') statusClass = 'pill-success';
        else if (w.status === 'Near Limit') statusClass = 'pill-warning';
        else if (w.status === 'Depleted') statusClass = 'pill-danger';

        let barClass = 'quota-progress-bar';
        if (parseFloat(remPct) < 20) barClass += ' q-bar-rose';
        else if (parseFloat(remPct) < 50) barClass += ' q-bar-amber';
        else barClass += ' q-bar-emerald';

        const isFull = (parseFloat(remPct) >= 99.9);
        const resetText = w.resets_at ? (w.window_minutes >= 1440 ? formatWeeklyResetClock(w.resets_at, null, isFull) : formatResetClock(w.resets_at, null, null, isFull, false, null)) : '—';

        return `
            <div class="quota-card quota-codex">
                <div class="quota-card-header">
                    <div class="quota-model-brand">
                        <span class="brand-badge brand-codex">CODEX</span>
                        <h3 style="font-size:0.95rem;">${escapeHtml(durationText)}</h3>
                    </div>
                    <div style="display:flex;gap:6px;align-items:center;">
                        ${planBadge}
                        <span class="quota-status-pill ${statusClass}">${escapeHtml(w.status || 'Bình Thường')}</span>
                    </div>
                </div>

                <div class="quota-body">
                    <div class="quota-metric-main">
                        <div class="quota-big-val" style="color:var(--emerald-400);">${remPct}%</div>
                        <div class="quota-big-label">Hạn Mức Còn Lại</div>
                    </div>

                    <div class="quota-progress-bar-wrap">
                        <div class="${barClass}" style="width: ${Math.min(100, Math.max(0, parseFloat(remPct)))}%;"></div>
                    </div>

                    <div class="quota-stats-grid">
                        <div class="q-stat">
                            <span class="q-stat-label">% Đã Dùng</span>
                            <span class="q-stat-val">${usedPct}%</span>
                        </div>
                        <div class="q-stat">
                            <span class="q-stat-label">% Còn Lại</span>
                            <span class="q-stat-val q-rem">${remPct}%</span>
                        </div>
                        <div class="q-stat">
                            <span class="q-stat-label">Thời Lượng Cửa Sổ</span>
                            <span class="q-stat-val">${w.window_minutes} phút</span>
                        </div>
                        <div class="q-stat">
                            <span class="q-stat-label">Nguồn Dữ Liệu</span>
                            <span class="q-stat-val" style="font-size:0.75rem;">${sourceLabel}</span>
                        </div>
                    </div>

                    <div class="quota-reset-indicator" style="margin-top:14px;padding:9px 12px;background:rgba(16,185,129,0.04);border:1px solid rgba(16,185,129,0.15);border-radius:8px;font-size:0.8rem;display:flex;align-items:center;justify-content:space-between;">
                        <span style="color:var(--text-muted);display:flex;align-items:center;gap:6px;">
                            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="var(--emerald-400)" stroke-width="2"><circle cx="12" cy="12" r="10"></circle><polyline points="12 6 12 12 16 14"></polyline></svg>
                            Mốc reset:
                        </span>
                        <strong id="${clockId}" style="color:var(--emerald-400);font-family:'JetBrains Mono',monospace;">
                            ${resetText}
                        </strong>
                    </div>
                </div>
            </div>
        `;
    }).join('');
}

// Modal Calibrate Controls with self-calibration preview.
let currentCalibrateUsed = { g5: 0, e5: 0, gw: 0, ew: 0, g5Lim: 500000, e5Lim: 60000, gwLim: 2500000, ewLim: 250000 };
let currentCalibrateMeta = { g5: null, e5: null, gw: null, ew: null };

function updateCalibrateLivePreview() {
    const calc = (trackedUsed, pct, currentLim, calibration) => {
        const clampedPct = Math.min(100, Math.max(0, isNaN(pct) ? 100 : pct));
        const totalLim = Math.max(0, currentLim || calibration?.capacity_tokens || 0);
        const rem = Math.max(0, Math.round(totalLim * clampedPct / 100));
        return {
            trackedUsed,
            pct: clampedPct,
            totalLim,
            rem,
            calibration: calibration || {}
        };
    };

    const g5Val = parseFloat(document.getElementById('input-cal-gem-5h')?.value || '100');
    const e5Val = parseFloat(document.getElementById('input-cal-ext-5h')?.value || '100');
    const gwVal = parseFloat(document.getElementById('input-cal-gem-wk')?.value || '100');
    const ewVal = parseFloat(document.getElementById('input-cal-ext-wk')?.value || '100');

    const resG5 = calc(currentCalibrateUsed.g5, g5Val, currentCalibrateUsed.g5Lim, currentCalibrateMeta.g5);
    const resE5 = calc(currentCalibrateUsed.e5, e5Val, currentCalibrateUsed.e5Lim, currentCalibrateMeta.e5);
    const resGW = calc(currentCalibrateUsed.gw, gwVal, currentCalibrateUsed.gwLim, currentCalibrateMeta.gw);
    const resEW = calc(currentCalibrateUsed.ew, ewVal, currentCalibrateUsed.ewLim, currentCalibrateMeta.ew);

    const renderBox = (elId, r, label) => {
        const el = document.getElementById(elId);
        if (!el) return;
        const c = r.calibration || {};
        const confidence = Number(c.confidence_pct || 0).toFixed(1);
        const obs = c.observations || 0;
        const pairs = c.usable_pairs || 0;
        const statusMap = { learning: 'Đang học', calibrated: 'Đã hiệu chuẩn', stable: 'Ổn định' };
        const status = statusMap[c.status] || 'Đang học';
        const onePct = c.one_percent_tokens || (r.totalLim ? r.totalLim / 100 : 0);
        el.innerHTML = `<div>Token tracker đang thấy: <strong>${formatNumber(r.trackedUsed)} tokens</strong></div>
            <div>% IDE nhập lần này: <strong>${r.pct.toFixed(1)}%</strong> → neo còn khoảng <strong style="color:var(--emerald-400);">${formatNumber(r.rem)} tokens</strong></div>
            <div>Ước lượng tổng hiện tại: <strong style="color:var(--cyan-400);">${formatNumber(r.totalLim)} tokens</strong> · 1% ≈ ${formatNumber(Math.round(onePct))} tokens</div>
            <div>Hiệu chuẩn: <strong>${status}</strong> · tin cậy ${confidence}% · ${obs} điểm đo / ${pairs} cặp hữu ích</div>
            <div style="opacity:.72;">Một lần nhập % chỉ được ghi thành điểm đo; hệ thống không dùng riêng điểm này để ép tính lại tổng quota.</div>`;
    };

    renderBox('preview-gem-5h', resG5, 'Gemini 5h');
    renderBox('preview-ext-5h', resE5, 'External 5h');
    renderBox('preview-gem-wk', resGW, 'Gemini Tuần');
    renderBox('preview-ext-wk', resEW, 'External Tuần');
}

function openCalibrateModal() {
    const modal = document.getElementById('modal-calibrate');
    if (!modal) return;

    // Reset-cycle flags are one-shot actions. Never carry a checked flag into
    // the next modal opening, otherwise a normal calibration could reset history.
    [
        'chk-gem-5h-reset-cycle',
        'chk-ext-5h-reset-cycle',
        'chk-gem-wk-reset-cycle',
        'chk-ext-wk-reset-cycle'
    ].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.checked = false;
    });

    // Pre-fill inputs with current values
    const q = (currentData && currentData.summary && currentData.summary.quotas) ? currentData.summary.quotas : (currentData?.quotas || null);
    if (q) {
        currentCalibrateUsed.g5 = q.five_hour_window?.gemini?.tracked_used_tokens ?? q.five_hour_window?.gemini?.used_tokens ?? 0;
        currentCalibrateUsed.e5 = q.five_hour_window?.external?.tracked_used_tokens ?? q.five_hour_window?.external?.used_tokens ?? 0;
        currentCalibrateUsed.gw = q.weekly_window?.gemini?.tracked_used_tokens ?? q.weekly_window?.gemini?.used_tokens ?? 0;
        currentCalibrateUsed.ew = q.weekly_window?.external?.tracked_used_tokens ?? q.weekly_window?.external?.used_tokens ?? 0;

        currentCalibrateUsed.g5Lim = q.five_hour_window?.gemini?.limit_tokens || 500000;
        currentCalibrateUsed.e5Lim = q.five_hour_window?.external?.limit_tokens || 60000;
        currentCalibrateUsed.gwLim = q.weekly_window?.gemini?.limit_tokens || 2500000;
        currentCalibrateUsed.ewLim = q.weekly_window?.external?.limit_tokens || 250000;

        currentCalibrateMeta.g5 = q.five_hour_window?.gemini?.calibration || null;
        currentCalibrateMeta.e5 = q.five_hour_window?.external?.calibration || null;
        currentCalibrateMeta.gw = q.weekly_window?.gemini?.calibration || null;
        currentCalibrateMeta.ew = q.weekly_window?.external?.calibration || null;

        const g5 = q.five_hour_window?.gemini?.percentage_remaining ?? 100;
        const e5 = q.five_hour_window?.external?.percentage_remaining ?? 100;
        const gw = q.weekly_window?.gemini?.percentage_remaining ?? 100;
        const ew = q.weekly_window?.external?.percentage_remaining ?? 100;

        const inG5 = document.getElementById('input-cal-gem-5h');
        const inE5 = document.getElementById('input-cal-ext-5h');
        const inGW = document.getElementById('input-cal-gem-wk');
        const inEW = document.getElementById('input-cal-ext-wk');
        if (inG5) { inG5.value = g5; inG5.oninput = updateCalibrateLivePreview; }
        if (inE5) { inE5.value = e5; inE5.oninput = updateCalibrateLivePreview; }
        if (inGW) { inGW.value = gw; inGW.oninput = updateCalibrateLivePreview; }
        if (inEW) { inEW.value = ew; inEW.oninput = updateCalibrateLivePreview; }

        const inG5Reset = document.getElementById('input-cal-gem-reset-min');
        const inE5Reset = document.getElementById('input-cal-ext-reset-min');
        if (inG5Reset) inG5Reset.value = q.five_hour_window?.gemini?.resets_in_minutes || '';
        if (inE5Reset) inE5Reset.value = q.five_hour_window?.external?.resets_in_minutes || '';

        const inGWReset = document.getElementById('input-cal-gem-wk-reset-hours');
        const inEWReset = document.getElementById('input-cal-ext-wk-reset-hours');
        if (inGWReset) inGWReset.value = q.weekly_window?.gemini?.resets_in_hours || '';
        if (inEWReset) inEWReset.value = q.weekly_window?.external?.resets_in_hours || '';

        updateCalibrateLivePreview();
    }

    modal.classList.add('show');
}

function closeCalibrateModal() {
    const modal = document.getElementById('modal-calibrate');
    if (modal) modal.classList.remove('show');
}

async function saveCalibrationData() {
    const btnSaveCal = document.getElementById('btn-save-calibrate');
    const originalText = btnSaveCal ? btnSaveCal.innerHTML : '';

    const g5 = parseFloat(document.getElementById('input-cal-gem-5h')?.value || '100');
    const e5 = parseFloat(document.getElementById('input-cal-ext-5h')?.value || '100');
    const gw = parseFloat(document.getElementById('input-cal-gem-wk')?.value || '100');
    const ew = parseFloat(document.getElementById('input-cal-ext-wk')?.value || '100');

    if ([g5, e5, gw, ew].some(v => !Number.isFinite(v) || v < 0 || v > 100)) {
        showToast('Các giá trị quota phải nằm trong khoảng 0–100%.', true);
        return;
    }

    const g5ResetMin = document.getElementById('input-cal-gem-reset-min')?.value?.trim();
    const e5ResetMin = document.getElementById('input-cal-ext-reset-min')?.value?.trim();
    const gwResetHours = document.getElementById('input-cal-gem-wk-reset-hours')?.value?.trim();
    const ewResetHours = document.getElementById('input-cal-ext-wk-reset-hours')?.value?.trim();

    const g5ResetCycle = document.getElementById('chk-gem-5h-reset-cycle')?.checked || false;
    const e5ResetCycle = document.getElementById('chk-ext-5h-reset-cycle')?.checked || false;
    const gwResetCycle = document.getElementById('chk-gem-wk-reset-cycle')?.checked || false;
    const ewResetCycle = document.getElementById('chk-ext-wk-reset-cycle')?.checked || false;

    if (btnSaveCal) {
        btnSaveCal.disabled = true;
        btnSaveCal.innerHTML = '<span>⏳ Đang lưu & hiệu chuẩn...</span>';
    }

    try {
        const payload = {
            gemini_5h_pct: g5,
            external_5h_pct: e5,
            gemini_weekly_pct: gw,
            external_weekly_pct: ew,
            gemini_5h_reset_cycle: g5ResetCycle,
            external_5h_reset_cycle: e5ResetCycle,
            gemini_weekly_reset_cycle: gwResetCycle,
            external_weekly_reset_cycle: ewResetCycle,
            gemini_5h_reset_minutes: g5ResetMin !== '' ? parseFloat(g5ResetMin) : null,
            external_5h_reset_minutes: e5ResetMin !== '' ? parseFloat(e5ResetMin) : null,
            gemini_weekly_reset_hours: gwResetHours !== '' ? parseFloat(gwResetHours) : null,
            external_weekly_reset_hours: ewResetHours !== '' ? parseFloat(ewResetHours) : null
        };

        const res = await fetch('/api/account/real-quotas', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });

        const data = await res.json().catch(() => ({}));

        if (res.ok && data.status !== 'error') {
            closeCalibrateModal();
            const calValues = Object.values(data.calibration || {});
            const stable = calValues.filter(c => c?.status === 'stable').length;
            const calibrated = calValues.filter(c => c?.status === 'calibrated').length;
            const retained = calValues.filter(c => c?.method === 'prior_retained' || (c?.retained_reasons || []).length > 0).length;
            showToast(`Đã ghi nhận điểm đo mới. Hiệu chuẩn: ${stable} ổn định, ${calibrated} đã hiệu chuẩn; ${retained} bucket giữ prior khi thiếu thông tin.`);
            await forceRefreshAfterMutation();
        } else {
            const msg = data.message || `Lỗi máy chủ (${res.status})`;
            showToast(`Không thể lưu hiệu chỉnh: ${msg}`, true);
        }
    } catch (err) {
        console.error("Save calibration error:", err);
        showToast(`Lỗi kết nối khi lưu: ${err.message}`, true);
    } finally {
        if (btnSaveCal) {
            btnSaveCal.disabled = false;
            btnSaveCal.innerHTML = originalText;
        }
    }
}

// Global Quick Reset Quota Cycle Function
window.quickResetCycle = async function(target) {
    const isGem = target === 'gemini_5h';
    const name = isGem ? 'Gemini (5 Giờ)' : 'Model Ngoài (5 Giờ)';
    const defaultPct = isGem ? '90.0' : '100.0';
    const inputVal = prompt(`Xác nhận vừa reset chu kỳ mới cho ${name}!\nNhập số % còn lại hiện tại trong IDE Settings:`, defaultPct);
    if (inputVal === null) return;
    const pct = parseFloat(inputVal);
    if (isNaN(pct) || pct < 0 || pct > 100) {
        showToast('Vui lòng nhập số % hợp lệ từ 0 đến 100!', true);
        return;
    }
    try {
        const res = await fetch('/api/account/reset-cycle', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                target: target,
                remaining_pct: pct
            })
        });
        if (res.ok) {
            showToast(`Đã bắt đầu chu kỳ quan sát mới cho ${name} ở ${pct}% còn lại; năng lực quota đã học được giữ nguyên.`);
            await forceRefreshAfterMutation();
        } else {
            showToast('Lỗi khi đặt lại chu kỳ!', true);
        }
    } catch (e) {
        showToast('Lỗi kết nối khi đặt lại chu kỳ!', true);
    }
};

function setElementText(id, text) {
    const el = document.getElementById(id);
    if (el) el.textContent = text;
}

// ---- Multi-Account Gmail Manager Engine ----
function renderAccountManager(currentAcc, accountsManager) {
    if (!currentAcc) return;

    // Header Pill
    setElementText('account-name', currentAcc.name || 'Người dùng');
    setElementText('account-email', currentAcc.email || 'Chưa xác định');
    const headerAvatar = document.getElementById('user-avatar');
    if (headerAvatar && currentAcc.profile_pic) {
        headerAvatar.src = currentAcc.profile_pic;
    }

    // Quota Tab Active Account Card
    setElementText('quota-user-name', currentAcc.name || 'Người dùng');
    setElementText('quota-user-email', currentAcc.email || 'Chưa xác định');
    setElementText('quota-user-tier', `${window.UsageI18n?.language === 'en' ? 'Plan' : 'Gói'}: ${currentAcc.tier || 'Not detected'}`);
    setElementText('quota-account-status', currentAcc.is_logged_in ? 'Đang Đăng Nhập (Active)' : 'Not connected');
    const quotaAvatar = document.getElementById('quota-user-avatar');
    if (quotaAvatar && currentAcc.profile_pic) {
        quotaAvatar.src = currentAcc.profile_pic;
    }

    // Account Switcher Select
    const switcher = document.getElementById('account-switcher-select');
    if (switcher && accountsManager && accountsManager.accounts) {
        const accountsList = Object.values(accountsManager.accounts).filter(acc => acc && acc.email);
        switcher.innerHTML = accountsList.length
            ? accountsList.map(acc => `
                <option value="${escapeHtml(acc.email)}" ${acc.email === accountsManager.active_email ? 'selected' : ''}>
                    ${escapeHtml(acc.email)} (${escapeHtml(acc.name)})
                </option>
            `).join('')
            : '<option value="">No account detected</option>';
        switcher.disabled = accountsList.length === 0;

        switcher.onchange = async () => {
            const targetEmail = switcher.value;
            if (!targetEmail) return;
            try {
                await fetch(`/api/account/switch?email=${encodeURIComponent(targetEmail)}`, { method: 'POST' });
                showToast(`Đã chuyển sang xem tài khoản: ${targetEmail}`);
                await forceRefreshAfterMutation();
            } catch (err) {
                console.error("Account switch error:", err);
            }
        };
    }

    // Saved Accounts Table
    const tbody = document.getElementById('accounts-tbody');
    if (tbody && accountsManager && accountsManager.accounts) {
        const accountsList = Object.values(accountsManager.accounts);
        tbody.innerHTML = accountsList.map(acc => {
            const isActive = acc.email === accountsManager.active_email;
            const lim = acc.limits || {};
            const lastActiveStr = acc.last_active ? new Date(acc.last_active).toLocaleString('vi-VN') : 'Gần đây';

            return `
                <tr>
                    <td>
                        <div style="display:flex;align-items:center;gap:8px;">
                            <img src="${acc.profile_pic || 'https://lh3.googleusercontent.com/a/default-user'}" style="width:26px;height:26px;border-radius:50%;border:1.5px solid var(--indigo-500);">
                            <strong style="color:var(--cyan-400);font-family:'JetBrains Mono',monospace;">${escapeHtml(acc.email)}</strong>
                        </div>
                    </td>
                    <td><strong>${escapeHtml(acc.name || 'Người dùng')}</strong></td>
                    <td><span class="step-badge model">${escapeHtml(acc.tier || 'Pro Tier')}</span></td>
                    <td class="num">${formatNumber(lim.gemini_5h_tokens || 500000)} / ${formatNumber(lim.external_5h_tokens || 60000)}</td>
                    <td class="num">${formatNumber(lim.gemini_weekly_tokens || 2500000)} / ${formatNumber(lim.external_weekly_tokens || 250000)}</td>
                    <td style="font-size:0.75rem;color:var(--text-muted);">${lastActiveStr}</td>
                    <td>
                        <span class="quota-status-pill ${isActive ? 'pill-success' : 'pill-info'}">
                            ${isActive ? '● Đang kích hoạt' : 'Đã lưu'}
                        </span>
                    </td>
                    <td>
                        <div style="display:flex;gap:6px;align-items:center;">
                            ${!isActive ? `
                                <button class="btn-table-action" onclick="switchAccount('${escapeHtml(acc.email)}')" title="Chuyển sang tài khoản này">
                                    👉 Chọn
                                </button>
                                <button class="btn-table-action btn-danger" onclick="deleteAccount('${escapeHtml(acc.email)}')" title="Xóa tài khoản khỏi danh bạ">
                                    ✕
                                </button>
                            ` : `<span style="font-size:0.75rem;color:var(--emerald-400);font-weight:700;">Hiện tại</span>`}
                        </div>
                    </td>
                </tr>
            `;
        }).join('');
    }
}

// Add Account Modal Controls
function openAddAccountModal() {
    const modal = document.getElementById('modal-add-account');
    if (modal) {
        document.getElementById('input-new-email').value = '';
        document.getElementById('input-new-name').value = '';
        modal.classList.add('show');
    }
}

function closeAddAccountModal() {
    const modal = document.getElementById('modal-add-account');
    if (modal) modal.classList.remove('show');
}

async function saveNewAccount() {
    const email = document.getElementById('input-new-email')?.value?.trim();
    const name = document.getElementById('input-new-name')?.value?.trim();
    const tier = document.getElementById('select-new-tier')?.value;

    if (!email || !email.includes('@')) {
        showToast('Vui lòng nhập địa chỉ email hợp lệ!', true);
        return;
    }

    try {
        const res = await fetch('/api/account/add', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                email: email,
                name: name || email.split('@')[0],
                tier: tier,
                make_active: true
            })
        });
        if (res.ok) {
            closeAddAccountModal();
            showToast(`Đã thêm và kích hoạt tài khoản: ${email}`);
            await forceRefreshAfterMutation();
        } else {
            showToast('Không thể thêm tài khoản!', true);
        }
    } catch (err) {
        console.error('Add account error:', err);
        showToast('Lỗi kết nối khi thêm tài khoản!', true);
    }
}

async function switchAccount(targetEmail) {
    try {
        await fetch(`/api/account/switch?email=${encodeURIComponent(targetEmail)}`, { method: 'POST' });
        showToast(`Đã chuyển sang tài khoản: ${targetEmail}`);
        await forceRefreshAfterMutation();
    } catch (err) {
        console.error('Account switch error:', err);
    }
}

async function deleteAccount(targetEmail) {
    if (!confirm(`Bạn có chắc muốn xóa tài khoản ${targetEmail} khỏi danh bạ không?`)) return;
    try {
        const res = await fetch('/api/account/delete', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ email: targetEmail })
        });
        if (res.ok) {
            showToast(`Đã xóa tài khoản: ${targetEmail}`);
            await forceRefreshAfterMutation();
        }
    } catch (err) {
        console.error('Delete account error:', err);
    }
}

// ---- MODEL BREAKDOWN TAB (Tab 4) ----
let modelsSortKey = savedChoice('modelsSortKey', [
    'model_id', 'platform', 'source_label', 'today_tokens', 'weekly_tokens',
    'total_tokens', 'input_tokens', 'output_tokens', 'thinking_tokens',
    'avg_tokens_per_response', 'weekly_quota_pct_used', 'sessions', 'responses',
], 'total_tokens');
let modelsSortAsc = uiPreferences.modelsSortAsc === true;

let currentModelTimelineSelection = savedModelSelection('modelTimelineSelection'); // null = all models; Set = explicit multi-selection
let cachedModelsDailyTimeline = null;
let currentModelTimelineRange = savedChoice('modelTimelineRange', ['7', '30', '90', '180', '365', 'custom'], '7');
let currentModelTimelineStart = savedDateValue('modelTimelineStart');
let currentModelTimelineEnd = savedDateValue('modelTimelineEnd');
if (currentModelTimelineRange === 'custom' && (!currentModelTimelineStart || !currentModelTimelineEnd)) {
    currentModelTimelineRange = '7';
}
let currentModelCostUnit = savedChoice('modelCostUnit', ['usd', 'credit'], 'usd');
let currentModelTimelineGranularity = savedChoice('modelTimelineGranularity', ['day', 'month', 'year'], 'day');
const NORMALIZED_CREDIT_USD = 0.01;
let cachedCodexQuotaTimeline = null;
let currentCodexQuotaTimelineRange = savedChoice('codexQuotaTimelineRange', ['7', '30', '90'], '30');
let currentCodexQuotaTimelineWindow = savedChoice('codexQuotaTimelineWindow', ['1', '3', '7'], '3');
let currentCodexQuotaTimelineSelection = savedModelSelection('codexQuotaTimelineSelection');
let cachedCodexTaskTimeline = null;
let currentCodexTaskTimelineRange = savedChoice('codexTaskTimelineRange', ['30', '90', '180'], '90');
let currentCodexTaskTimelineWindow = savedChoice('codexTaskTimelineWindow', ['7', '14', '30'], '14');
let currentCodexTaskTimelineSelection = savedModelSelection('codexTaskTimelineSelection');
let cachedCodexTaskOutcomes = null;
let currentTaskOutcomeRange = savedChoice('taskOutcomeRange', ['90', '180', '365', 'all'], '180');
let currentTaskOutcomeCategoryFilter = typeof uiPreferences.taskOutcomeCategoryFilter === 'string' ? uiPreferences.taskOutcomeCategoryFilter : 'all';
let currentTaskOutcomeStatusFilter = savedChoice('taskOutcomeStatusFilter', ['all', 'accepted', 'unresolved', 'abandoned', 'user_repaired', 'excluded'], 'all');
let currentTaskOutcomeUnconfirmedGroupFilter = typeof uiPreferences.taskOutcomeUnconfirmedGroupFilter === 'string'
    ? uiPreferences.taskOutcomeUnconfirmedGroupFilter
    : 'all';
let currentTaskOutcomeRouteFilter = savedChoice('taskOutcomeRouteFilter', ['all', 'pure', 'mixed'], 'all');
let currentTaskOutcomeReviewFilter = savedChoice('taskOutcomeReviewFilter', ['all', 'audit', 'hard'], 'all');
let currentTaskOutcomeAuditReasonFilter = typeof uiPreferences.taskOutcomeAuditReasonFilter === 'string'
    ? uiPreferences.taskOutcomeAuditReasonFilter
    : 'all';
let currentTaskDurationRange = savedChoice('taskDurationRange', ['30', '90', '180'], '90');
let currentTaskDurationWindow = savedChoice('taskDurationWindow', ['7', '14', '30'], '14');
let currentTaskDurationSelection = savedModelSelection('taskDurationSelection');

function renderModelsBreakdown(breakdown, codexUsage, modelsDailyTimeline) {
    renderModelsSummaryCards(breakdown, codexUsage);
    renderModelsTable(breakdown);
    renderCodexQuotaEfficiency(codexUsage?.automatic_model_usage?.quota_efficiency);
    const quotaTimeline = codexUsage?.automatic_model_usage?.quota_efficiency_timeline;
    if (quotaTimeline) cachedCodexQuotaTimeline = quotaTimeline;
    renderCodexQuotaEfficiencyTimeline(quotaTimeline || cachedCodexQuotaTimeline);
    const taskTimeline = codexUsage?.automatic_model_usage?.quota_per_task_timeline;
    if (taskTimeline) cachedCodexTaskTimeline = taskTimeline;
    renderCodexQuotaPerTaskTimeline(taskTimeline || cachedCodexTaskTimeline);
    renderCodexQuotaPerTask(codexUsage?.automatic_model_usage?.quota_per_task);
    if (modelsDailyTimeline) {
        cachedModelsDailyTimeline = modelsDailyTimeline;
        renderModelsDailyChart(modelsDailyTimeline);
        renderModelsCostChart(modelsDailyTimeline);
    } else if (cachedModelsDailyTimeline) {
        renderModelsDailyChart(cachedModelsDailyTimeline);
        renderModelsCostChart(cachedModelsDailyTimeline);
    }
}

function quotaTimelinePreviousPoint(points, latest, windowDays) {
    if (!latest || points.length < 2) return null;
    const target = Date.parse(`${latest.date}T00:00:00Z`) - windowDays * 86400000;
    for (let index = points.length - 2; index >= 0; index--) {
        if (Date.parse(`${points[index].date}T00:00:00Z`) <= target) return points[index];
    }
    return points[points.length - 2];
}

function quotaTimelineDateLabel(dateKey) {
    const parts = String(dateKey || '').split('-');
    return parts.length === 3 ? `${parts[2]}/${parts[1]}` : String(dateKey || '');
}

function renderCodexQuotaEfficiencyTimeline(data) {
    const canvas = document.getElementById('canvas-codex-quota-timeline');
    const body = document.getElementById('codex-quota-efficiency-recent-body');
    const legend = document.getElementById('codex-quota-timeline-legend');
    const note = document.getElementById('codex-quota-timeline-note');
    const status = document.getElementById('codex-quota-timeline-status');
    if (!canvas || !body || !legend) return;
    const english = window.UsageI18n?.language === 'en';

    const windowData = data?.windows?.[currentCodexQuotaTimelineWindow];
    const allDates = Array.isArray(data?.dates) ? data.dates : [];
    const rangeDays = Math.max(7, Number(currentCodexQuotaTimelineRange) || 30);
    const dates = allDates.slice(-rangeDays);
    const dateSet = new Set(dates);
    const windowDays = Math.max(1, Number(currentCodexQuotaTimelineWindow) || 3);
    const sourceRows = Array.isArray(windowData?.models) ? windowData.models : [];
    const rows = sourceRows.map(row => {
        const points = (Array.isArray(row.points) ? row.points : []).filter(point => dateSet.has(point.date));
        if (!points.length) return null;
        const latest = points[points.length - 1];
        const previous = quotaTimelinePreviousPoint(points, latest, windowDays);
        const latestValue = latest.relative_quota_burn_vs_sol_high == null
            ? null : Number(latest.relative_quota_burn_vs_sol_high);
        const previousValue = previous?.relative_quota_burn_vs_sol_high == null
            ? null : Number(previous.relative_quota_burn_vs_sol_high);
        const changePct = Number.isFinite(latestValue) && latestValue > 0 && Number.isFinite(previousValue) && previousValue > 0
            ? (latestValue / previousValue - 1) * 100
            : null;
        return { ...row, points, latest, previous, changePct };
    }).filter(Boolean).sort((a, b) => (
        (b.latest?.relative_quota_burn_vs_sol_high == null ? -Infinity : Number(b.latest.relative_quota_burn_vs_sol_high))
        - (a.latest?.relative_quota_burn_vs_sol_high == null ? -Infinity : Number(a.latest.relative_quota_burn_vs_sol_high))
    ));

    if (!data?.available || !windowData?.available || !rows.length) {
        body.innerHTML = `<tr><td colspan="8" style="text-align:center;color:var(--text-muted,#94a3b8);padding:1.15rem;">${english ? 'No valid 5h quota intervals in this range.' : 'Chưa có khoảng đo quota 5h hợp lệ trong phạm vi đang xem.'}</td></tr>`;
        legend.innerHTML = '';
        if (status) status.textContent = english
            ? 'Model measurements remain separate; the Sol High comparison appears only with direct or sufficiently supported bridged evidence.'
            : 'Dữ liệu model vẫn được giữ riêng; so sánh với Sol High chỉ hiện khi có mốc trực tiếp hoặc bắc cầu đủ bằng chứng.';
        const c = CanvasCharts.initCanvas(canvas);
        if (c) c.ctx.clearRect(0, 0, c.width, c.height);
        return;
    }

    const colorFor = colorForModelKey;
    legend.innerHTML = [
        `<button type="button" data-quota-model="all" class="${currentCodexQuotaTimelineSelection === null ? 'active' : ''}" style="--quota-color:${THEME.cyan}">${english ? 'All' : 'Tất cả'} (${rows.length})</button>`,
        ...rows.map(row => {
            const color = colorFor(row.model_key);
            const active = currentCodexQuotaTimelineSelection !== null && currentCodexQuotaTimelineSelection.has(row.model_key);
            const labelColor = currentCodexQuotaTimelineSelection === null || active ? color : '#cbd5e1';
            return `<button type="button" data-quota-model="${escapeHtml(row.model_key)}" class="${active ? 'active' : ''}" style="--quota-color:${color};color:${labelColor}"><span class="quota-timeline-swatch"></span>${escapeHtml(row.model_key)}</button>`;
        }),
    ].join('');
    legend.querySelectorAll('button[data-quota-model]').forEach(button => {
        button.addEventListener('click', () => toggleCodexQuotaTimelineModel(button.dataset.quotaModel));
    });

    const selectedRows = currentCodexQuotaTimelineSelection === null
        ? rows
        : rows.filter(row => currentCodexQuotaTimelineSelection.has(row.model_key));
    const datasets = selectedRows.map(row => {
        const byDate = new Map(row.points.map(point => [
            point.date,
            point.relative_quota_burn_vs_sol_high == null ? null : Number(point.relative_quota_burn_vs_sol_high),
        ]));
        return {
            label: row.model_key,
            color: colorFor(row.model_key),
            data: dates.map(date => byDate.has(date) ? byDate.get(date) : null),
        };
    });
    CanvasCharts.drawQuotaRatioTimeline(canvas, dates.map(quotaTimelineDateLabel), datasets);

    const generatedAt = Date.parse(data.generated_at || '');
    const confidenceLabel = english ? { low: 'Low', medium: 'Medium', high: 'High' } : { low: 'Thấp', medium: 'Vừa', high: 'Cao' };
    body.innerHTML = rows.map(row => {
        const latest = row.latest || {};
        const previous = row.previous || {};
        const latestValue = latest.relative_quota_burn_vs_sol_high == null
            ? null : Number(latest.relative_quota_burn_vs_sol_high);
        const previousValue = previous.relative_quota_burn_vs_sol_high == null
            ? null : Number(previous.relative_quota_burn_vs_sol_high);
        const tokensPerPct = Number(latest.tokens_per_quota_pct);
        const change = row.changePct;
        const changeClass = Number.isFinite(change) ? (change > 2 ? 'quota-change-up' : (change < -2 ? 'quota-change-down' : 'quota-change-flat')) : 'quota-change-flat';
        const changeText = Number.isFinite(change) ? `${change > 0 ? '+' : ''}${change.toFixed(1)}%` : '—';
        const observedAt = latest.last_observed_at;
        const ageHours = Number.isFinite(generatedAt) && observedAt ? (generatedAt - Date.parse(observedAt)) / 3600000 : NaN;
        const stale = Number.isFinite(ageHours) && ageHours > windowDays * 24;
        const confidenceKey = ['low', 'medium', 'high'].includes(latest.confidence) ? latest.confidence : 'low';
        const confidence = confidenceLabel[confidenceKey];
        const modelConfidence = confidenceLabel[latest.model_confidence] || confidenceLabel.low;
        const baselineConfidence = confidenceLabel[latest.baseline_confidence] || confidenceLabel.low;
        const baselineSource = latest.baseline_source || 'unavailable';
        const bridgePath = Array.isArray(latest.bridge_path) ? latest.bridge_path : [];
        const bridgeText = bridgePath.length > 1 ? bridgePath.join(' → ') : '';
        const comparisonBadge = baselineSource === 'bridge'
            ? `<span class="task-cell-meta">${english ? 'bridged estimate' : 'ước lượng bắc cầu'}</span>`
            : (baselineSource === 'unavailable' ? `<span class="task-cell-meta">${english ? 'no Sol High baseline' : 'chưa có mốc Sol High'}</span>` : '');
        const baselineSamplesText = baselineSource === 'bridge'
            ? (english ? `estimated from ${formatNumber(Number(latest.baseline_estimate_anchor_count || 0))} anchors` : `ước lượng từ ${formatNumber(Number(latest.baseline_estimate_anchor_count || 0))} mốc`)
            : formatNumber(Number(latest.baseline_sample_count || 0));
        const confidenceTitle = baselineSource === 'bridge'
            ? (english ? `Model: ${modelConfidence} · Sol High baseline: ${baselineConfidence} (bridged${bridgeText ? ` ${bridgeText}` : ''}, support ${Number(latest.bridge_support || 0)})` : `Model: ${modelConfidence} · mốc Sol High: ${baselineConfidence} (bắc cầu${bridgeText ? ` ${bridgeText}` : ''}, support ${Number(latest.bridge_support || 0)})`)
            : (baselineSource === 'direct'
                ? (english ? `Model: ${modelConfidence} · direct Sol High: ${baselineConfidence}` : `Model: ${modelConfidence} · Sol High trực tiếp: ${baselineConfidence}`)
                : (english ? `Model: ${modelConfidence} · insufficient Sol High baseline evidence` : `Model: ${modelConfidence} · chưa có mốc Sol High đủ bằng chứng`));
        return `<tr>
            <td style="font-weight:600;color:#e2e8f0;">${escapeHtml(row.model_key || row.model_id || 'Unknown')}</td>
            <td class="num"><strong style="color:${colorFor(row.model_key)};">${Number.isFinite(latestValue) ? latestValue.toFixed(2) + '×' : '—'}</strong>${comparisonBadge}</td>
            <td class="num">${Number.isFinite(previousValue) ? previousValue.toFixed(2) + '×' : '—'}</td>
            <td class="num"><span class="${changeClass}">${changeText}</span></td>
            <td class="num">${Number.isFinite(tokensPerPct) ? formatNumber(Math.round(tokensPerPct)) : '—'}</td>
            <td class="num">${formatNumber(Number(latest.sample_count || 0))} / ${escapeHtml(baselineSamplesText)}</td>
            <td title="${escapeHtml(confidenceTitle)}"><span class="quota-confidence-${confidenceKey}">${escapeHtml(confidence)}</span></td>
            <td>${observedAt ? formatDate(observedAt) : '—'}${stale ? `<span class="quota-recent-stale"> · ${english ? 'stale' : 'dữ liệu cũ'}</span>` : ''}</td>
        </tr>`;
    }).join('');

    if (status) {
        const visiblePoints = rows.reduce((sum, row) => sum + row.points.length, 0);
        const bridgePoints = rows.reduce((sum, row) => sum + row.points.filter(point => point.baseline_source === 'bridge').length, 0);
        const unavailablePoints = rows.reduce((sum, row) => sum + row.points.filter(point => point.baseline_source === 'unavailable').length, 0);
        status.textContent = english
            ? `${rangeDays}-day range · ${windowDays}-day rolling median · ${formatNumber(Number(data.interval_count || 0))} valid intervals · ${formatNumber(visiblePoints)} model points · ${formatNumber(bridgePoints)} bridged points · ${formatNumber(unavailablePoints)} points without baseline.`
            : `${rangeDays} ngày · median trượt ${windowDays} ngày · ${formatNumber(Number(data.interval_count || 0))} khoảng đo hợp lệ · ${formatNumber(visiblePoints)} điểm model · ${formatNumber(bridgePoints)} điểm bắc cầu · ${formatNumber(unavailablePoints)} điểm chưa có mốc.`;
    }
    if (note) {
        note.textContent = english
            ? 'Each point keeps the model’s own median quota efficiency. Sol High in the same window is the preferred direct baseline; otherwise the tracker uses only historically overlapping model measurements and lowers confidence. Without enough bridging evidence, the ratio stays blank rather than becoming zero. >1× means faster 5h quota consumption for the same raw token count. This is not an official OpenAI weight.'
            : 'Mỗi điểm luôn giữ median quota-efficiency riêng của model. Sol High cùng cửa sổ được ưu tiên làm mốc trực tiếp; khi thiếu, tracker chỉ bắc cầu qua quan hệ model đã từng đo chồng lấp trong lịch sử và hạ độ tin cậy. Nếu không có đường bắc cầu đủ bằng chứng, tỷ lệ để trống thay vì ép thành 0. >1× nghĩa là model tiêu hao quota 5h nhanh hơn trên cùng raw token. Đây không phải trọng số chính thức của OpenAI.';
    }
}

function toggleCodexQuotaTimelineModel(modelKey) {
    if (modelKey === 'all') {
        currentCodexQuotaTimelineSelection = null;
    } else if (currentCodexQuotaTimelineSelection === null) {
        currentCodexQuotaTimelineSelection = new Set([modelKey]);
    } else if (currentCodexQuotaTimelineSelection.has(modelKey)) {
        currentCodexQuotaTimelineSelection.delete(modelKey);
    } else {
        currentCodexQuotaTimelineSelection.add(modelKey);
    }
    saveUiPreferences({
        codexQuotaTimelineSelection: serializeModelSelection(currentCodexQuotaTimelineSelection),
    });
    if (cachedCodexQuotaTimeline) renderCodexQuotaEfficiencyTimeline(cachedCodexQuotaTimeline);
}

function renderCodexQuotaPerTaskTimeline(data) {
    const canvas = document.getElementById('canvas-codex-task-timeline');
    const body = document.getElementById('codex-quota-per-task-recent-body');
    const legend = document.getElementById('codex-task-timeline-legend');
    const note = document.getElementById('codex-task-timeline-note');
    const status = document.getElementById('codex-task-timeline-status');
    if (!canvas || !body || !legend) return;
    const english = window.UsageI18n?.language === 'en';

    const windowData = data?.windows?.[currentCodexTaskTimelineWindow];
    const allDates = Array.isArray(data?.dates) ? data.dates : [];
    const rangeDays = Math.max(30, Number(currentCodexTaskTimelineRange) || 90);
    const dates = allDates.slice(-rangeDays);
    const dateSet = new Set(dates);
    const windowDays = Math.max(1, Number(currentCodexTaskTimelineWindow) || 14);
    const minimumTasks = Math.max(1, Number(data?.min_tasks_per_model) || 5);
    const sourceRows = Array.isArray(windowData?.models) ? windowData.models : [];
    const rows = sourceRows.map(row => {
        const points = (Array.isArray(row.points) ? row.points : []).filter(point => dateSet.has(point.date));
        if (!points.length) return null;
        const latest = points[points.length - 1];
        const previous = quotaTimelinePreviousPoint(points, latest, windowDays);
        const latestValue = Number(latest.relative_task_quota_burn_vs_sol_high);
        const previousValue = Number(previous?.relative_task_quota_burn_vs_sol_high);
        const changePct = Number.isFinite(latestValue) && latestValue > 0 && Number.isFinite(previousValue) && previousValue > 0
            ? (latestValue / previousValue - 1) * 100
            : null;
        return { ...row, points, latest, previous, changePct };
    }).filter(Boolean).sort((a, b) => (
        Number(b.latest?.relative_task_quota_burn_vs_sol_high || 0) - Number(a.latest?.relative_task_quota_burn_vs_sol_high || 0)
    ));

    if (!data?.available || !windowData?.available || !rows.length) {
        body.innerHTML = `<tr><td colspan="9" style="text-align:center;color:var(--text-muted,#94a3b8);padding:1.15rem;">${english ? `Fewer than ${formatNumber(minimumTasks)} tasks for both the model and Sol High in the same window.` : `Chưa đủ ít nhất ${formatNumber(minimumTasks)} task của model và Sol High trong cùng cửa sổ để so sánh.`}</td></tr>`;
        legend.innerHTML = '';
        if (status) {
            status.textContent = english
                ? `${formatNumber(Number(data?.task_sample_count || 0))} valid tasks recorded; each point needs at least ${formatNumber(minimumTasks)} model tasks and ${formatNumber(minimumTasks)} Sol High tasks in the same window.`
                : `Đã có ${formatNumber(Number(data?.task_sample_count || 0))} task hợp lệ; mỗi điểm cần tối thiểu ${formatNumber(minimumTasks)} task của model và ${formatNumber(minimumTasks)} task Sol High trong cùng cửa sổ.`;
        }
        const c = CanvasCharts.initCanvas(canvas);
        if (c) c.ctx.clearRect(0, 0, c.width, c.height);
        return;
    }

    const colorFor = colorForModelKey;
    legend.innerHTML = [
        `<button type="button" data-task-quota-model="all" class="${currentCodexTaskTimelineSelection === null ? 'active' : ''}" style="--quota-color:${THEME.cyan}">${english ? 'All' : 'Tất cả'} (${rows.length})</button>`,
        ...rows.map(row => {
            const color = colorFor(row.model_key);
            const active = currentCodexTaskTimelineSelection !== null && currentCodexTaskTimelineSelection.has(row.model_key);
            const labelColor = currentCodexTaskTimelineSelection === null || active ? color : '#cbd5e1';
            return `<button type="button" data-task-quota-model="${escapeHtml(row.model_key)}" class="${active ? 'active' : ''}" style="--quota-color:${color};color:${labelColor}"><span class="quota-timeline-swatch"></span>${escapeHtml(row.model_key)}</button>`;
        }),
    ].join('');
    legend.querySelectorAll('button[data-task-quota-model]').forEach(button => {
        button.addEventListener('click', () => toggleCodexTaskTimelineModel(button.dataset.taskQuotaModel));
    });

    const selectedRows = currentCodexTaskTimelineSelection === null
        ? rows
        : rows.filter(row => currentCodexTaskTimelineSelection.has(row.model_key));
    const datasets = selectedRows.map(row => {
        const byDate = new Map(row.points.map(point => [point.date, Number(point.relative_task_quota_burn_vs_sol_high)]));
        return {
            label: row.model_key,
            color: colorFor(row.model_key),
            data: dates.map(date => byDate.has(date) ? byDate.get(date) : null),
        };
    });
    CanvasCharts.drawQuotaRatioTimeline(canvas, dates.map(quotaTimelineDateLabel), datasets);

    const confidenceLabel = english ? { low: 'Low', medium: 'Medium', high: 'High' } : { low: 'Thấp', medium: 'Vừa', high: 'Cao' };
    const generatedAt = Date.parse(data.generated_at || '');
    body.innerHTML = rows.map(row => {
        const latest = row.latest || {};
        const previous = row.previous || {};
        const estimated = Number(latest.estimated_quota_pct_per_task);
        const latestValue = Number(latest.relative_task_quota_burn_vs_sol_high);
        const previousValue = Number(previous.relative_task_quota_burn_vs_sol_high);
        const tokensPerTask = Number(latest.tokens_per_task);
        const coverage = Number(latest.median_token_coverage);
        const change = row.changePct;
        const changeClass = Number.isFinite(change) ? (change > 2 ? 'quota-change-up' : (change < -2 ? 'quota-change-down' : 'quota-change-flat')) : 'quota-change-flat';
        const changeText = Number.isFinite(change) ? `${change > 0 ? '+' : ''}${change.toFixed(1)}%` : '—';
        const observedAt = latest.last_observed_at;
        const ageHours = Number.isFinite(generatedAt) && observedAt ? (generatedAt - Date.parse(observedAt)) / 3600000 : NaN;
        const stale = Number.isFinite(ageHours) && ageHours > windowDays * 24;
        const confidence = confidenceLabel[latest.confidence] || confidenceLabel.low;
        return `<tr>
            <td style="font-weight:600;color:#e2e8f0;">${escapeHtml(row.model_key || row.model_id || 'Unknown')}</td>
            <td class="num"><strong style="color:${colorFor(row.model_key)};">${Number.isFinite(estimated) ? estimated.toFixed(2) + '%' : '—'}</strong></td>
            <td class="num">${Number.isFinite(latestValue) ? latestValue.toFixed(2) + '×' : '—'}</td>
            <td class="num">${Number.isFinite(previousValue) ? previousValue.toFixed(2) + '×' : '—'}</td>
            <td class="num"><span class="${changeClass}">${changeText}</span></td>
            <td class="num">${Number.isFinite(tokensPerTask) ? formatNumber(Math.round(tokensPerTask)) : '—'}</td>
            <td class="num">${formatNumber(Number(latest.task_count || 0))} / ${formatNumber(Number(latest.baseline_task_count || 0))}</td>
            <td>${Number.isFinite(coverage) ? (coverage * 100).toFixed(0) + '%' : '—'} · ${escapeHtml(confidence)}</td>
            <td>${observedAt ? formatDate(observedAt) : '—'}${stale ? `<span class="quota-recent-stale"> · ${english ? 'stale' : 'dữ liệu cũ'}</span>` : ''}</td>
        </tr>`;
    }).join('');

    if (status) {
        const visiblePoints = rows.reduce((sum, row) => sum + row.points.length, 0);
        status.textContent = english
            ? `${rangeDays}-day range · ${windowDays}-day rolling median · ${rows.length} models meet the ${minimumTasks}-task threshold for both model and Sol High · ${formatNumber(Number(data.task_sample_count || 0))} valid tasks · ${formatNumber(visiblePoints)} comparison points.`
            : `${rangeDays} ngày · median trượt ${windowDays} ngày · ${rows.length} model đạt ngưỡng ${minimumTasks} task của cả model và Sol High trong cùng cửa sổ · ${formatNumber(Number(data.task_sample_count || 0))} task hợp lệ · ${formatNumber(visiblePoints)} điểm so sánh.`;
    }
    if (note) {
        note.textContent = english
            ? 'Each point uses the median 5h quota percentage per recently completed task and compares it with Sol High in the same window. >1× means more quota consumed per recent task. Tokens per task and sample counts help reveal changes in task size or difficulty. These are empirical measurements from local logs.'
            : 'Mỗi điểm dùng median %5h/task của các task hoàn tất gần đó và so với Sol High trong cùng cửa sổ. >1× nghĩa là model tiêu hao nhiều quota hơn cho một task gần đây. Tokens/task và số mẫu giúp nhận ra khi độ khó hoặc kích thước task thay đổi. Đây là số đo thực nghiệm từ log cục bộ.';
    }
}

function toggleCodexTaskTimelineModel(modelKey) {
    if (modelKey === 'all') {
        currentCodexTaskTimelineSelection = null;
    } else if (currentCodexTaskTimelineSelection === null) {
        currentCodexTaskTimelineSelection = new Set([modelKey]);
    } else if (currentCodexTaskTimelineSelection.has(modelKey)) {
        currentCodexTaskTimelineSelection.delete(modelKey);
    } else {
        currentCodexTaskTimelineSelection.add(modelKey);
    }
    saveUiPreferences({
        codexTaskTimelineSelection: serializeModelSelection(currentCodexTaskTimelineSelection),
    });
    if (cachedCodexTaskTimeline) renderCodexQuotaPerTaskTimeline(cachedCodexTaskTimeline);
}

function renderCodexQuotaEfficiency(quotaEfficiency) {
    const body = document.getElementById('codex-quota-efficiency-body');
    const note = document.getElementById('codex-quota-efficiency-note');
    if (!body) return;

    const rows = Array.isArray(quotaEfficiency?.models) ? quotaEfficiency.models : [];
    if (!quotaEfficiency?.available || rows.length === 0) {
        body.innerHTML = '<tr><td colspan="7" style="text-align:center;color:var(--text-muted,#94a3b8);padding:1.15rem;">Chưa đủ dữ liệu để ước tính hiệu suất hạn mức Codex 5h.</td></tr>';
        if (note) {
            note.textContent = 'Cần ít nhất một khoảng đo hợp lệ có thay đổi từ 2% hạn mức trở lên trong cùng phiên, cùng model và cùng chu kỳ reset 5h.';
        }
        return;
    }

    const confidenceLabel = {
        low: 'Thấp',
        medium: 'Vừa',
        high: 'Cao',
    };
    body.innerHTML = rows.map(row => {
        const tokensPerPct = Number(row.tokens_per_quota_pct);
        const pctPerMillion = Number(row.quota_pct_per_1m_tokens);
        const relative = Number(row.relative_quota_burn_vs_sol_high);
        const hasRelative = Number.isFinite(relative) && relative > 0;
        const confidence = confidenceLabel[row.confidence] || String(row.confidence || 'Thấp');
        const modelName = escapeHtml(row.model_key || row.model_id || 'Unknown');
        const relativeText = hasRelative ? `${relative.toFixed(2)}×` : '—';
        const relativeTitle = hasRelative
            ? (relative > 1
                ? `Ước tính đốt hạn mức nhanh hơn Sol High ${relative.toFixed(2)} lần trên cùng số raw token.`
                : `Ước tính đốt hạn mức bằng ${relative.toFixed(2)} lần Sol High trên cùng số raw token.`)
            : 'Chưa có baseline Sol High đủ dữ liệu.';
        return `
            <tr>
                <td style="font-weight:600;color:#e2e8f0;">${modelName}</td>
                <td class="num" title="Median raw tokens tiêu thụ cho mỗi 1 điểm phần trăm hạn mức 5h.">${Number.isFinite(tokensPerPct) ? formatNumber(Math.round(tokensPerPct)) : '—'}</td>
                <td class="num">${Number.isFinite(pctPerMillion) ? pctPerMillion.toFixed(2) + '%' : '—'}</td>
                <td class="num" title="${escapeHtml(relativeTitle)}">${relativeText}</td>
                <td class="num">${Number(row.quota_span_pct || 0).toFixed(1)}% / ${formatNumber(Number(row.session_count || 0))}</td>
                <td class="num">${formatNumber(Number(row.sample_count || 0))}</td>
                <td><span style="font-weight:600;">${escapeHtml(confidence)}</span></td>
            </tr>
        `;
    }).join('');

    if (note) {
        const observations = Number(quotaEfficiency.observation_count || 0);
        const intervals = Number(quotaEfficiency.interval_count || 0);
        note.textContent = window.UsageI18n?.language === 'en'
            ? `Empirical measurements from local Codex logs: ${formatNumber(observations)} snapshots and ${formatNumber(intervals)} valid intervals. >1× means faster 5h quota consumption than Sol High for the same raw token count. This is not an official OpenAI weighting formula.`
            : `Đo thực nghiệm từ log Codex cục bộ: ${formatNumber(observations)} snapshot, ${formatNumber(intervals)} khoảng đo hợp lệ. >1× nghĩa là model tiêu hao hạn mức 5h nhanh hơn Sol High trên cùng số raw token. Đây không phải công thức trọng số chính thức của OpenAI.`;
    }
}

function renderCodexQuotaPerTask(data) {
    const body = document.getElementById('codex-quota-per-task-body');
    const note = document.getElementById('codex-quota-per-task-note');
    if (!body) return;
    const rows = Array.isArray(data?.models) ? data.models : [];
    if (!data?.available || !rows.length) {
        body.innerHTML = '<tr><td colspan="8" style="text-align:center;color:var(--text-muted,#94a3b8);padding:1.15rem;">Chưa đủ dữ liệu task hoàn tất để ước tính mức tiêu hao 5h theo task.</td></tr>';
        if (note) {
            note.textContent = 'Cần một task hoàn tất, dùng một model trong cùng chu kỳ reset 5h, có ít nhất hai snapshot quota và độ phủ token từ 25%.';
        }
        return;
    }

    const confidenceLabel = { low: 'Thấp', medium: 'Vừa', high: 'Cao' };
    body.innerHTML = rows.map(row => {
        const estimated = Number(row.estimated_quota_pct_per_task);
        const observed = Number(row.observed_quota_pct_per_task);
        const tokens = Number(row.tokens_per_task);
        const coverage = Number(row.median_token_coverage);
        const relative = Number(row.relative_task_quota_burn_vs_sol_high);
        const hasRelative = Number.isFinite(relative) && relative > 0;
        const relativeTitle = hasRelative
            ? (relative > 1
                ? `Ước tính task tiêu hao hạn mức nhiều hơn Sol High ${relative.toFixed(2)} lần.`
                : `Ước tính task tiêu hao ${relative.toFixed(2)} lần hạn mức của Sol High.`)
            : 'Chưa có baseline Sol High đủ dữ liệu.';
        return `
            <tr>
                <td style="font-weight:600;color:#e2e8f0;">${escapeHtml(row.model_key || row.model_id || 'Unknown')}</td>
                <td class="num" title="Ngoại suy từ phần quota quan sát được theo độ phủ token của task.">${Number.isFinite(estimated) ? estimated.toFixed(2) + '%' : '—'}</td>
                <td class="num" title="Median thay đổi quota giữa snapshot đầu và cuối trong task.">${Number.isFinite(observed) ? observed.toFixed(2) + '%' : '—'}</td>
                <td class="num">${Number.isFinite(tokens) ? formatNumber(Math.round(tokens)) : '—'}</td>
                <td class="num">${Number.isFinite(coverage) ? (coverage * 100).toFixed(0) + '%' : '—'}</td>
                <td class="num" title="${escapeHtml(relativeTitle)}">${hasRelative ? relative.toFixed(2) + '×' : '—'}</td>
                <td class="num">${formatNumber(Number(row.task_count || 0))} / ${formatNumber(Number(row.session_count || 0))}</td>
                <td><span style="font-weight:600;">${escapeHtml(confidenceLabel[row.confidence] || String(row.confidence || 'Thấp'))}</span></td>
            </tr>
        `;
    }).join('');

    if (note) {
        const sampled = Number(data.task_sample_count || 0);
        const mixed = Number(data.excluded_mixed_model_tasks || 0);
        const reset = Number(data.excluded_cross_cycle_tasks || 0);
        const insufficient = Number(data.excluded_insufficient_tasks || 0);
        const lowCoverage = Number(data.excluded_low_coverage_tasks || 0);
        note.textContent = window.UsageI18n?.language === 'en'
            ? `Empirical measurements from ${formatNumber(sampled)} completed tasks. 5h quota percentage per task is extrapolated from token coverage; excluded ${formatNumber(mixed)} model-switching tasks, ${formatNumber(reset)} reset-crossing tasks, ${formatNumber(insufficient)} tasks without sufficient quota changes or snapshots, and ${formatNumber(lowCoverage)} low-coverage tasks. This is not an official OpenAI formula.`
            : `Đo thực nghiệm từ ${formatNumber(sampled)} task hoàn tất. %5h/task được ngoại suy theo độ phủ token; đã loại ${formatNumber(mixed)} task đổi model, ${formatNumber(reset)} task qua mốc reset, ${formatNumber(insufficient)} task thiếu biến động/snapshot và ${formatNumber(lowCoverage)} task có coverage thấp. Đây không phải công thức chính thức của OpenAI.`;
    }
}

const MODEL_TIMELINE_GRANULARITY_LABELS = {
    day: 'Theo ngày',
    month: 'Theo tháng',
    year: 'Theo năm',
};

function aggregateModelTimeline(timelineData, granularity = currentModelTimelineGranularity) {
    if (!timelineData || granularity === 'day') return timelineData;
    const dateKeys = Array.isArray(timelineData.date_keys) ? timelineData.date_keys : [];
    if (!dateKeys.length) return timelineData;

    const bucketKeys = [];
    const bucketLabels = [];
    const bucketIndexes = new Map();
    const dayToBucket = [];
    dateKeys.forEach((dateKey, dayIndex) => {
        const parts = String(dateKey || '').split('-');
        const year = parts[0] || '';
        const month = parts[1] || '';
        const bucketKey = granularity === 'year' ? year : `${year}-${month}`;
        if (!bucketIndexes.has(bucketKey)) {
            bucketIndexes.set(bucketKey, bucketKeys.length);
            bucketKeys.push(bucketKey);
            bucketLabels.push(granularity === 'year' ? year : `T${month}/${year}`);
        }
        dayToBucket[dayIndex] = bucketIndexes.get(bucketKey);
    });

    const sumDailyValues = values => {
        const sums = Array(bucketKeys.length).fill(0);
        dayToBucket.forEach((bucketIndex, dayIndex) => {
            sums[bucketIndex] += Math.max(0, Number(values?.[dayIndex]) || 0);
        });
        return sums;
    };

    const models = (timelineData.models || []).map(model => ({
        ...model,
        daily_tokens: sumDailyValues(model.daily_tokens).map(value => Math.round(value)),
        daily_cost_usd: sumDailyValues(model.daily_cost_usd).map(value => Number(value.toFixed(8))),
        daily_unknown_cost_tokens: sumDailyValues(model.daily_unknown_cost_tokens).map(value => Math.round(value)),
    }));
    return {
        ...timelineData,
        dates: bucketLabels,
        date_keys: bucketKeys,
        models,
        display_granularity: granularity,
    };
}

function sizeModelTimelineStage(scrollId, stageId, bucketCount) {
    const scroll = document.getElementById(scrollId);
    const stage = document.getElementById(stageId);
    if (!scroll || !stage) return;
    const hadOverflow = scroll.scrollWidth > scroll.clientWidth + 2;
    const wasAtLatest = !hadOverflow || scroll.scrollLeft + scroll.clientWidth >= scroll.scrollWidth - 12;
    const minimumPerBucket = currentModelTimelineGranularity === 'day'
        ? 30
        : (currentModelTimelineGranularity === 'month' ? 72 : 110);
    const viewportWidth = Math.max(320, scroll.clientWidth || 0);
    const requiredWidth = Math.max(viewportWidth, 24 + Math.max(1, Number(bucketCount) || 1) * minimumPerBucket);
    stage.style.width = `${Math.ceil(requiredWidth)}px`;
    const hasOverflow = requiredWidth > viewportWidth + 2;
    scroll.classList.toggle('has-overflow', hasOverflow);
    scroll.closest('.model-timeline-chart-frame')?.classList.toggle('has-overflow', hasOverflow);
    if (wasAtLatest) scroll.scrollLeft = Math.max(0, requiredWidth - viewportWidth);
}

function formatTimelineRangeText(timelineData) {
    const range = timelineData?.range || {};
    const english = window.UsageI18n?.language === 'en';
    const granularityLabel = english
        ? ({ day: 'By day', month: 'By month', year: 'By year' }[currentModelTimelineGranularity] || 'By day')
        : (MODEL_TIMELINE_GRANULARITY_LABELS[currentModelTimelineGranularity] || 'Theo ngày');
    if (range.mode === 'custom' && range.start_date && range.end_date) {
        const formatDateKey = key => {
            const parts = String(key).split('-');
            return parts.length === 3 ? `${parts[2]}/${parts[1]}/${parts[0]}` : key;
        };
        return `(${formatDateKey(range.start_date)} – ${formatDateKey(range.end_date)} · ${granularityLabel})`;
    }
    const count = range.days || timelineData?.dates?.length || 0;
    return english ? `(Last ${count} days · ${granularityLabel})` : `(${count} ngày qua · ${granularityLabel})`;
}

function renderModelsDailyChart(timelineData) {
    const canvas = document.getElementById('canvas-models-daily-stacked');
    const legendContainer = document.getElementById('models-daily-chart-legend');
    if (!canvas || !timelineData || !timelineData.dates || !timelineData.models) return;
    const displayTimeline = aggregateModelTimeline(timelineData);

    const rangeLabel = document.getElementById('model-timeline-range-label');
    if (rangeLabel) {
        rangeLabel.textContent = formatTimelineRangeText(timelineData);
    }

    if (legendContainer) {
        const allModels = timelineData.models;
        let legendHtml = `
            <button class="legend-btn ${currentModelTimelineSelection === null ? 'active' : ''}" onclick="filterModelDailyChart('all')" style="cursor:pointer;padding:3px 10px;border-radius:6px;font-size:0.78rem;font-weight:600;border:1px solid ${currentModelTimelineSelection === null ? 'var(--cyan-400)' : 'rgba(255,255,255,0.1)'};background:${currentModelTimelineSelection === null ? 'rgba(6,182,212,0.15)' : 'transparent'};color:${currentModelTimelineSelection === null ? '#06b6d4' : 'var(--text-muted)'};transition:all 0.2s;">
                ${window.UsageI18n?.language === 'en' ? 'All' : 'Tất cả'} (${allModels.length})
            </button>
        `;
        allModels.forEach(m => {
            const isActive = currentModelTimelineSelection !== null && currentModelTimelineSelection.has(m.name);
            const color = m.color || '#06b6d4';
            legendHtml += `
                <button class="legend-btn ${isActive ? 'active' : ''}" onclick="filterModelDailyChart('${escapeHtml(m.name).replace(/'/g, "\'")}')" style="cursor:pointer;padding:3px 10px;border-radius:6px;font-size:0.78rem;font-weight:600;border:1px solid ${isActive ? color : 'rgba(255,255,255,0.08)'};background:${isActive ? color + '22' : 'transparent'};color:${currentModelTimelineSelection === null || isActive ? color : '#cbd5e1'};display:flex;align-items:center;gap:6px;transition:all 0.2s;">
                    <span style="display:inline-block;width:11px;height:11px;flex:0 0 11px;border-radius:3px;background:${color};border:1px solid rgba(255,255,255,0.35);box-shadow:0 0 6px ${color};"></span>
                    <span>${escapeHtml(m.name)}</span>
                    <span style="font-size:0.72rem;opacity:0.8;font-family:monospace;">(${formatNumber(m.total_period_tokens)})</span>
                </button>
            `;
        });
        legendContainer.innerHTML = legendHtml;
    }

    sizeModelTimelineStage('models-daily-chart-scroll', 'models-daily-chart-stage', displayTimeline.dates.length);
    CanvasCharts.drawMultiModelDailyStackedBar(canvas, displayTimeline, currentModelTimelineSelection === null ? 'all' : currentModelTimelineSelection, {
        axisCanvas: document.getElementById('canvas-models-daily-stacked-axis'),
        tooltipValueLabel: () => window.UsageI18n?.language === 'en' ? 'Tokens consumed' : 'Token tiêu thụ',
        tooltipFormatter: value => `${Math.round(value).toLocaleString(window.UsageI18n?.language === 'en' ? 'en-US' : 'vi-VN')} token`,
    });
}

function formatModelCostDisplay(value, unit = currentModelCostUnit) {
    const numeric = Math.max(0, Number(value) || 0);
    if (unit === 'credit') {
        const digits = numeric >= 100 ? 0 : (numeric >= 10 ? 1 : (numeric >= 1 ? 2 : 3));
        return `${numeric.toFixed(digits)} cr`;
    }
    const digits = numeric >= 10 ? 2 : (numeric >= 1 ? 3 : (numeric >= 0.01 ? 4 : 6));
    return `$${numeric.toFixed(digits)}`;
}

function renderModelsCostChart(timelineData) {
    const canvas = document.getElementById('canvas-models-daily-cost-stacked');
    const legendContainer = document.getElementById('models-daily-cost-chart-legend');
    const rangeLabel = document.getElementById('model-cost-range-label');
    const coverageLabel = document.getElementById('model-cost-coverage');
    if (!canvas || !timelineData || !timelineData.dates || !timelineData.models) return;
    const displayTimeline = aggregateModelTimeline(timelineData);

    if (rangeLabel) rangeLabel.textContent = formatTimelineRangeText(timelineData);

    const allModels = timelineData.models;
    const unknownModels = allModels.filter(m => !m.cost_complete && Number(m.total_period_tokens || 0) > 0);
    const proxyModels = allModels.filter(m => m.pricing_source === 'assumed_equivalent' && Number(m.total_period_tokens || 0) > 0);
    const fastModels = allModels.filter(m => m.pricing_source === 'chatgpt_fast_credit_equivalent' && Number(m.total_period_tokens || 0) > 0);
    if (coverageLabel) {
        const english = window.UsageI18n?.language === 'en';
        const coverageParts = [];
        if (proxyModels.length) {
            coverageParts.push(english
                ? `${proxyModels.length} models use GPT-5.6 Sol Thinking proxy pricing`
                : `${proxyModels.length} model dùng proxy giá GPT-5.6 Sol Thinking`);
        }
        if (fastModels.length) coverageParts.push(english
            ? `${fastModels.length} Fast models use credit-weighted Standard rates`
            : `${fastModels.length} model Fast dùng mức credit quy đổi từ Standard`);
        if (unknownModels.length) {
            coverageParts.push(english
                ? `${unknownModels.length} models have unpriced usage; it is not counted as $0`
                : `${unknownModels.length} model có usage chưa định giá; phần đó không được tính thành $0`);
        }
        coverageLabel.textContent = coverageParts.length
            ? coverageParts.join(' • ') + '.'
            : (english ? 'All usage in this range has an estimated rate.' : 'Toàn bộ usage trong khoảng đang có giá ước tính.');
        coverageLabel.style.color = unknownModels.length ? '#fbbf24' : '#86efac';
    }

    if (legendContainer) {
        let legendHtml = `
            <button class="legend-btn ${currentModelTimelineSelection === null ? 'active' : ''}" onclick="filterModelDailyChart('all')" style="cursor:pointer;padding:3px 10px;border-radius:6px;font-size:0.78rem;font-weight:600;border:1px solid ${currentModelTimelineSelection === null ? 'var(--cyan-400)' : 'rgba(255,255,255,0.1)'};background:${currentModelTimelineSelection === null ? 'rgba(6,182,212,0.15)' : 'transparent'};color:${currentModelTimelineSelection === null ? '#06b6d4' : 'var(--text-muted)'};transition:all 0.2s;">
                ${window.UsageI18n?.language === 'en' ? 'All' : 'Tất cả'} (${allModels.length})
            </button>
        `;
        allModels.forEach(m => {
            const isActive = currentModelTimelineSelection !== null && currentModelTimelineSelection.has(m.name);
            const color = m.color || '#06b6d4';
            const totalUsd = Number(m.total_period_cost_usd || 0);
            const displayValue = currentModelCostUnit === 'credit'
                ? totalUsd / NORMALIZED_CREDIT_USD
                : totalUsd;
            const incompleteMark = m.cost_complete ? '' : ' + ?';
            const estimatedMark = m.pricing_estimated === true ? ' ~' : '';
            const pricingTitle = m.pricing_estimated === true
                ? escapeHtml(m.pricing_note || `Ước tính theo ${m.pricing_basis_model || 'GPT-5.6 Sol Thinking'}`)
                : '';
            legendHtml += `
                <button class="legend-btn ${isActive ? 'active' : ''}" onclick="filterModelDailyChart('${escapeHtml(m.name).replace(/'/g, "\\'")}')" title="${pricingTitle}" style="cursor:pointer;padding:3px 10px;border-radius:6px;font-size:0.78rem;font-weight:600;border:1px solid ${isActive ? color : 'rgba(255,255,255,0.08)'};background:${isActive ? color + '22' : 'transparent'};color:${currentModelTimelineSelection === null || isActive ? color : '#cbd5e1'};display:flex;align-items:center;gap:6px;transition:all 0.2s;">
                    <span style="display:inline-block;width:11px;height:11px;flex:0 0 11px;border-radius:3px;background:${color};border:1px solid rgba(255,255,255,0.35);box-shadow:0 0 6px ${color};"></span>
                    <span>${escapeHtml(m.name)}${estimatedMark}</span>
                    <span style="font-size:0.72rem;opacity:0.8;font-family:monospace;">(${formatModelCostDisplay(displayValue)}${incompleteMark})</span>
                </button>
            `;
        });
        legendContainer.innerHTML = legendHtml;
    }

    const isCredit = currentModelCostUnit === 'credit';
    sizeModelTimelineStage('models-daily-cost-chart-scroll', 'models-daily-cost-chart-stage', displayTimeline.dates.length);
    CanvasCharts.drawMultiModelDailyStackedBar(canvas, displayTimeline, currentModelTimelineSelection === null ? 'all' : currentModelTimelineSelection, {
        axisCanvas: document.getElementById('canvas-models-daily-cost-stacked-axis'),
        dataKey: 'daily_cost_usd',
        valueMultiplier: isCredit ? (1 / NORMALIZED_CREDIT_USD) : 1,
        fallbackMax: isCredit ? 0.1 : 0.001,
        axisFormatter: value => formatModelCostDisplay(value),
        totalFormatter: value => formatModelCostDisplay(value),
        tooltipValueLabel: () => window.UsageI18n?.language === 'en' ? 'Estimated cost' : 'Chi phí ước tính',
        tooltipFormatter: value => formatModelCostDisplay(value),
    });
}

function filterModelDailyChart(modelName) {
    if (modelName === 'all') currentModelTimelineSelection = null; else if (currentModelTimelineSelection === null) currentModelTimelineSelection = new Set([modelName]); else if (currentModelTimelineSelection.has(modelName)) currentModelTimelineSelection.delete(modelName); else currentModelTimelineSelection.add(modelName);
    saveUiPreferences({
        modelTimelineSelection: serializeModelSelection(currentModelTimelineSelection),
    });
    if (cachedModelsDailyTimeline) {
        renderModelsDailyChart(cachedModelsDailyTimeline);
        renderModelsCostChart(cachedModelsDailyTimeline);
    }
}

function localDateInputValue(date) {
    const year = date.getFullYear();
    const month = String(date.getMonth() + 1).padStart(2, '0');
    const day = String(date.getDate()).padStart(2, '0');
    return `${year}-${month}-${day}`;
}

function initModelTimelineRangeControls() {
    const rangeSelect = document.getElementById('model-timeline-range');
    const customControls = document.getElementById('model-timeline-custom');
    const startInput = document.getElementById('model-timeline-start');
    const endInput = document.getElementById('model-timeline-end');
    const applyBtn = document.getElementById('model-timeline-apply');
    if (!rangeSelect || !customControls || !startInput || !endInput || !applyBtn) return;

    const today = new Date();
    const todayKey = localDateInputValue(today);
    startInput.max = todayKey;
    endInput.max = todayKey;

    const restoredDayCount = currentModelTimelineStart && currentModelTimelineEnd
        ? Math.floor((Date.parse(`${currentModelTimelineEnd}T00:00:00Z`) - Date.parse(`${currentModelTimelineStart}T00:00:00Z`)) / 86400000) + 1
        : 0;
    const restoredCustomValid = currentModelTimelineRange !== 'custom' || (
        currentModelTimelineStart <= currentModelTimelineEnd &&
        currentModelTimelineEnd <= todayKey &&
        restoredDayCount >= 1 && restoredDayCount <= 366
    );
    if (!restoredCustomValid) {
        currentModelTimelineRange = '7';
        currentModelTimelineStart = '';
        currentModelTimelineEnd = '';
        saveUiPreferences({
            modelTimelineRange: '7',
            modelTimelineStart: '',
            modelTimelineEnd: '',
        });
    }
    rangeSelect.value = currentModelTimelineRange;
    startInput.value = currentModelTimelineStart;
    endInput.value = currentModelTimelineEnd;
    customControls.style.display = currentModelTimelineRange === 'custom' ? 'flex' : 'none';

    const seedCustomRange = () => {
        if (!endInput.value) endInput.value = todayKey;
        if (!startInput.value) {
            const start = new Date(today.getFullYear(), today.getMonth(), today.getDate());
            start.setDate(start.getDate() - 29);
            startInput.value = localDateInputValue(start);
        }
    };

    rangeSelect.addEventListener('change', () => {
        const selected = rangeSelect.value;
        if (selected === 'custom') {
            customControls.style.display = 'flex';
            seedCustomRange();
            return;
        }

        customControls.style.display = 'none';
        currentModelTimelineRange = selected;
        currentModelTimelineStart = '';
        currentModelTimelineEnd = '';
        saveUiPreferences({
            modelTimelineRange: currentModelTimelineRange,
            modelTimelineStart: '',
            modelTimelineEnd: '',
        });
        forceRefreshAfterMutation().catch(() => {});
    });

    applyBtn.addEventListener('click', () => {
        const start = startInput.value;
        const end = endInput.value;
        if (!start || !end) {
            showToast('Vui lòng chọn đủ ngày bắt đầu và ngày kết thúc.', true);
            return;
        }
        if (start > end) {
            showToast('Ngày kết thúc phải bằng hoặc sau ngày bắt đầu.', true);
            return;
        }
        if (end > todayKey) {
            showToast('Ngày kết thúc không được nằm trong tương lai.', true);
            return;
        }

        const dayCount = Math.floor((Date.parse(`${end}T00:00:00Z`) - Date.parse(`${start}T00:00:00Z`)) / 86400000) + 1;
        if (!Number.isFinite(dayCount) || dayCount < 1 || dayCount > 366) {
            showToast('Khoảng thời gian tùy chọn tối đa là 366 ngày.', true);
            return;
        }

        currentModelTimelineRange = 'custom';
        currentModelTimelineStart = start;
        currentModelTimelineEnd = end;
        saveUiPreferences({
            modelTimelineRange: 'custom',
            modelTimelineStart: start,
            modelTimelineEnd: end,
        });
        forceRefreshAfterMutation().catch(() => {});
    });
}

function initModelTimelineGranularityControls() {
    const granularitySelect = document.getElementById('model-timeline-granularity');
    if (!granularitySelect) return;
    granularitySelect.value = currentModelTimelineGranularity;
    granularitySelect.addEventListener('change', () => {
        currentModelTimelineGranularity = ['day', 'month', 'year'].includes(granularitySelect.value)
            ? granularitySelect.value
            : 'day';
        saveUiPreferences({ modelTimelineGranularity: currentModelTimelineGranularity });
        if (cachedModelsDailyTimeline) {
            renderModelsDailyChart(cachedModelsDailyTimeline);
            renderModelsCostChart(cachedModelsDailyTimeline);
        }
    });
}

function initModelCostControls() {
    const unitSelect = document.getElementById('model-cost-unit');
    if (!unitSelect) return;
    unitSelect.value = currentModelCostUnit;
    unitSelect.addEventListener('change', () => {
        currentModelCostUnit = unitSelect.value === 'credit' ? 'credit' : 'usd';
        saveUiPreferences({ modelCostUnit: currentModelCostUnit });
        if (cachedModelsDailyTimeline) renderModelsCostChart(cachedModelsDailyTimeline);
    });
}

function initCodexQuotaTimelineControls() {
    const rangeSelect = document.getElementById('codex-quota-timeline-range');
    const windowSelect = document.getElementById('codex-quota-timeline-window');
    if (!rangeSelect || !windowSelect) return;
    rangeSelect.value = currentCodexQuotaTimelineRange;
    windowSelect.value = currentCodexQuotaTimelineWindow;
    rangeSelect.addEventListener('change', () => {
        currentCodexQuotaTimelineRange = ['7', '30', '90'].includes(rangeSelect.value) ? rangeSelect.value : '30';
        saveUiPreferences({ codexQuotaTimelineRange: currentCodexQuotaTimelineRange });
        if (cachedCodexQuotaTimeline) renderCodexQuotaEfficiencyTimeline(cachedCodexQuotaTimeline);
    });
    windowSelect.addEventListener('change', () => {
        currentCodexQuotaTimelineWindow = ['1', '3', '7'].includes(windowSelect.value) ? windowSelect.value : '3';
        saveUiPreferences({ codexQuotaTimelineWindow: currentCodexQuotaTimelineWindow });
        if (cachedCodexQuotaTimeline) renderCodexQuotaEfficiencyTimeline(cachedCodexQuotaTimeline);
    });
}

function initCodexTaskTimelineControls() {
    const rangeSelect = document.getElementById('codex-task-timeline-range');
    const windowSelect = document.getElementById('codex-task-timeline-window');
    if (!rangeSelect || !windowSelect) return;
    rangeSelect.value = currentCodexTaskTimelineRange;
    windowSelect.value = currentCodexTaskTimelineWindow;
    rangeSelect.addEventListener('change', () => {
        currentCodexTaskTimelineRange = ['30', '90', '180'].includes(rangeSelect.value) ? rangeSelect.value : '90';
        saveUiPreferences({ codexTaskTimelineRange: currentCodexTaskTimelineRange });
        if (cachedCodexTaskTimeline) renderCodexQuotaPerTaskTimeline(cachedCodexTaskTimeline);
    });
    windowSelect.addEventListener('change', () => {
        currentCodexTaskTimelineWindow = ['7', '14', '30'].includes(windowSelect.value) ? windowSelect.value : '14';
        saveUiPreferences({ codexTaskTimelineWindow: currentCodexTaskTimelineWindow });
        if (cachedCodexTaskTimeline) renderCodexQuotaPerTaskTimeline(cachedCodexTaskTimeline);
    });
}

function renderModelsSummaryCards(breakdown, codexUsage) {
    const grid = document.getElementById('models-summary-grid');
    if (!grid) return;
    const english = window.UsageI18n?.language === 'en';

    const totalModels = breakdown.length;
    const pricedModels = breakdown.filter(m => m.cost_known !== false);
    const unpricedModels = breakdown.filter(m => m.cost_known === false);
    const proxyPricedModels = pricedModels.filter(m => m.pricing_source === 'assumed_equivalent');
    const fastPricedModels = pricedModels.filter(m => m.pricing_source === 'chatgpt_fast_credit_equivalent');
    const totalCostAll = pricedModels.reduce((s, m) => s + (m.total_cost_usd || 0), 0);
    const agiModels = breakdown.filter(m => m.platform === 'Antigravity');
    const codexModels = breakdown.filter(m => m.platform === 'Codex');
    const codexExactModels = codexModels.filter(m => m.source_kind === 'automatic');
    const codexManualModels = codexModels.filter(m => m.source_kind !== 'automatic');
    const sumTokens = models => models.reduce((sum, model) => {
        const value = Number(model.total_tokens);
        return sum + (Number.isFinite(value) ? value : 0);
    }, 0);
    const agyTotalTokens = sumTokens(agiModels);
    const codexExactTokens = sumTokens(codexExactModels);
    const codexManualTokens = sumTokens(codexManualModels);
    const topModel = breakdown.length > 0 ? breakdown[0] : null;
    const allTimeDiagnostics = codexUsage?.automatic_model_usage?.diagnostics || {};
    const rootCounts = Object.entries(allTimeDiagnostics.root_file_counts || {});
    const activeLogCount = rootCounts
        .filter(([path]) => !String(path).toLowerCase().includes('archived_sessions'))
        .reduce((sum, [, count]) => sum + Number(count || 0), 0);
    const archivedLogCount = rootCounts
        .filter(([path]) => String(path).toLowerCase().includes('archived_sessions'))
        .reduce((sum, [, count]) => sum + Number(count || 0), 0);
    const ledgerCoverage = rootCounts.length
        ? (english ? ` • ledger: ${activeLogCount} active + ${archivedLogCount} archived logs; all-time at current rates`
            : ` • sổ cái ${activeLogCount} active + ${archivedLogCount} archived log; không lọc ngày; định giá theo rate hiện hành`)
        : (english ? ' • all-time at current rates' : ' • all-time, không lọc ngày; định giá theo rate hiện hành');

    const cards = [
        {
            icon: '📊', cls: 'indigo', cardCls: 'c-indigo',
            label: 'Tổng Models Đang Theo Dõi',
            value: totalModels,
            sub: `Gemini/Antigravity: ${agiModels.length} • Codex: ${codexModels.length}`
        },
        {
            icon: '🔢', cls: 'cyan', cardCls: 'c-cyan',
            label: 'Tokens Theo Nguồn (Không Cộng Chéo)',
            value: `<span style="display:block;">Gemini/Antigravity ${english ? 'estimated' : 'ước tính'}: ${formatNumber(agyTotalTokens)}</span><span style="display:block;">Codex ${english ? 'exact' : 'chính xác'}: ${formatNumber(codexExactTokens)}</span>${codexManualModels.length ? `<span style="display:block;font-size:0.72em;">Codex ${english ? 'manual' : 'thủ công'}: ${formatNumber(codexManualTokens)}</span>` : ''}`,
            sub: english ? 'Gemini/Antigravity transcript estimate • exact Codex session-log tokens • manual fallback separate' : 'Gemini/Antigravity ước tính từ transcript • Codex chính xác từ session log • dữ liệu thủ công tách riêng'
        },
        {
            icon: '💵', cls: 'emerald', cardCls: 'c-emerald',
            label: 'Tổng Chi Phí Ước Tính (All-Time)',
            value: '$' + totalCostAll.toFixed(2),
            sub: `Gemini/Antigravity: $${agiModels.filter(m=>m.cost_known!==false).reduce((s,m)=>s+m.total_cost_usd,0).toFixed(2)} • Codex: $${codexModels.filter(m=>m.cost_known!==false).reduce((s,m)=>s+m.total_cost_usd,0).toFixed(2)}${proxyPricedModels.length ? (english ? ` • ${proxyPricedModels.length} models use GPT-5.6 Sol Thinking proxy pricing` : ` • ${proxyPricedModels.length} model dùng proxy GPT-5.6 Sol Thinking`) : ''}${fastPricedModels.length ? (english ? ` • ${fastPricedModels.length} Fast models use credit-weighted rates` : ` • ${fastPricedModels.length} model Fast quy đổi theo credit`) : ''}${unpricedModels.length ? (english ? ` • ${unpricedModels.length} models unpriced` : ` • ${unpricedModels.length} model chưa có giá`) : ''}${ledgerCoverage}`
        },
        {
            icon: '🏆', cls: 'amber', cardCls: 'c-amber',
            label: 'Model Tốn Nhiều Token Nhất',
            value: topModel ? topModel.model_id : '—',
            sub: topModel ? `${formatNumber(topModel.total_tokens)} tokens • ${topModel.cost_known === false ? (english ? 'cost —' : 'chi phí —') : (topModel.pricing_estimated === true ? '~$' : '$') + topModel.total_cost_usd.toFixed(2)}` : ''
        }
    ];

    grid.innerHTML = cards.map(c => `
        <div class="summary-card ${c.cardCls}">
            <div class="card-icon">${c.icon}</div>
            <div class="card-body">
                <span class="card-label">${c.label}</span>
                <span class="card-value">${c.value}</span>
                <span class="card-sub">${c.sub}</span>
            </div>
        </div>
    `).join('');
}

function renderAveragePerResponse(inputTokens, outputTokens, thinkingTokens, responses) {
    const count = Math.max(0, Number(responses) || 0);
    if (count <= 0) return '—';
    const avg = value => formatNumber(Math.round((Number(value) || 0) / count));
    return `<span style="display:block;color:var(--cyan-400);">In: ${avg(inputTokens)}</span>` +
        `<span style="display:block;color:var(--emerald-400);">Out: ${avg(outputTokens)}</span>` +
        `<span style="display:block;color:var(--amber-400);">Think: ${avg(thinkingTokens)}</span>`;
}

function renderTokenCostPeriod(tokens, costUsd, costKnown, tokenColor, estimated = false, pricingBasisHtml = '') {
    const tokenValue = Math.max(0, Number(tokens) || 0);
    const costValue = Math.max(0, Number(costUsd) || 0);
    const digits = costValue >= 100 ? 2 : (costValue >= 1 ? 3 : 4);
    const costText = costKnown === false ? (window.UsageI18n?.language === 'en' ? 'Cost: —' : 'Chi phí: —') : `${estimated ? '~' : ''}$${costValue.toFixed(digits)}`;
    return `<strong style="display:block;color:${tokenColor};">${formatNumber(tokenValue)}</strong>` +
        `<span class="period-cost-value">${costText}</span>${pricingBasisHtml}`;
}

function renderFooterPeriodCost(costUsd, unknownCount) {
    return `<span class="period-cost-value">$${Number(costUsd || 0).toFixed(2)}</span>` +
        (unknownCount ? `<span style="display:block;font-size:0.68rem;color:var(--text-muted);">+ ${unknownCount} ${window.UsageI18n?.language === 'en' ? 'unpriced' : 'chưa định giá'}</span>` : '');
}

function renderModelsTable(breakdown) {
    const tbody = document.getElementById('models-breakdown-tbody');
    const tfoot = document.getElementById('models-breakdown-tfoot');
    if (!tbody) return;

    // Sort
    const sorted = [...breakdown].sort((a, b) => {
        const sortValue = model => modelsSortKey === 'avg_tokens_per_response'
            ? (Number(model.responses) > 0
                ? (Number(model.input_tokens || 0) + Number(model.output_tokens || 0)
                    + Number(model.thinking_tokens || 0)) / Number(model.responses)
                : null)
            : model[modelsSortKey];
        let va = sortValue(a), vb = sortValue(b);
        if (va === null || va === undefined) return vb === null || vb === undefined ? 0 : 1;
        if (vb === null || vb === undefined) return -1;
        if (typeof va === 'string') va = va.toLowerCase();
        if (typeof vb === 'string') vb = vb.toLowerCase();
        if (va < vb) return modelsSortAsc ? -1 : 1;
        if (va > vb) return modelsSortAsc ? 1 : -1;
        return 0;
    });

    // Update sort indicators
    document.querySelectorAll('.models-breakdown-table th.sortable').forEach(th => {
        th.classList.remove('sort-asc', 'sort-desc');
        const active = th.dataset.sort === modelsSortKey;
        if (active) {
            th.classList.add(modelsSortAsc ? 'sort-asc' : 'sort-desc');
        }
        th.setAttribute('aria-sort', active ? (modelsSortAsc ? 'ascending' : 'descending') : 'none');
    });

    const platformBadge = (platform) => {
        if (platform === 'Codex') {
            return '<span class="platform-badge platform-codex">Codex</span>';
        }
        return '<span class="platform-badge platform-agy">Gemini</span>';
    };

    const sourceBadge = (sourceKind, platform) => {
        if (sourceKind === 'automatic') {
            return '<span class="model-source-badge badge-auto" title="Tự động quét từ local logs / transcripts">⚡ Tự động (Logs)</span>';
        }
        if (sourceKind === 'manual_fallback' || platform === 'Codex') {
            return '<span class="model-source-badge badge-manual" title="Dữ liệu nhập thủ công (Manual fallback)">📝 Thủ công (Fallback)</span>';
        }
        return '<span class="model-source-badge badge-auto">⚡ Tự động</span>';
    };

    const quotaBar = (pct, wkUsed, wkLimit, quotaMeta = {}) => {
        const numericPct = (pct === null || pct === undefined || pct === '') ? NaN : Number(pct);
        const sourceLabel = quotaMeta.weekly_quota_pct_label || quotaMeta.weekly_quota_basis || 'Weekly denominator unavailable';
        const titleText = `${formatNumber(wkUsed || 0)} / ${formatNumber(wkLimit || 0)} tokens (7 ngày qua) • ${sourceLabel}`;
        if (!Number.isFinite(numericPct)) {
            return `<div class="quota-mini-bar quota-unavailable" title="${escapeHtml(titleText)}" aria-label="${escapeHtml(sourceLabel)}">
                <span class="quota-mini-label">—</span>
                <span style="display:block;font-size:0.62rem;color:var(--text-muted);">${escapeHtml(sourceLabel)}</span>
            </div>`;
        }
        const clamped = Math.min(100, Math.max(0, numericPct));
        let barColor = 'var(--emerald-400)';
        if (clamped > 80) barColor = 'var(--rose-400)';
        else if (clamped > 50) barColor = 'var(--amber-400)';
        else if (clamped > 25) barColor = 'var(--cyan-400)';
        const estimateTag = quotaMeta.weekly_quota_pct_is_estimate ? ' • estimate' : '';
        return `<div class="quota-mini-bar" title="${titleText}">
            <div class="quota-mini-fill" style="width:${clamped}%;background:${barColor};"></div>
            <span class="quota-mini-label">${clamped.toFixed(1)}%${estimateTag}</span>
            <span style="display:block;font-size:0.62rem;color:var(--text-muted);">${escapeHtml(sourceLabel)}</span>
        </div>`;
    };

    tbody.innerHTML = sorted.map((m, i) => {
        const isCodex = m.platform === 'Codex';
        const isAutomatic = m.source_kind === 'automatic';
        const isManual = isCodex && !isAutomatic;
        const rowCls = isCodex ? 'row-codex' : '';
        const wkToks = m.weekly_tokens !== undefined ? m.weekly_tokens : m.total_tokens;
        const wkCost = m.weekly_cost_usd !== undefined ? m.weekly_cost_usd : m.total_cost_usd;
        const pricingBasisText = m.pricing_estimated === true
            ? `<span title="${escapeHtml(m.pricing_note || '')}" style="display:block;font-size:0.66rem;color:#fbbf24;">${m.pricing_source === 'chatgpt_fast_credit_equivalent' ? `~ Fast ×${Number(m.fast_credit_multiplier || 1).toFixed(1)} credits` : `~ theo ${escapeHtml(m.pricing_basis_model || 'GPT-5.6 Sol Thinking')}`}</span>`
            : (m.pricing_source === 'official_feature_mapping'
                ? `<span title="${escapeHtml(m.pricing_note || '')}" style="display:block;font-size:0.66rem;color:#86efac;">theo ${escapeHtml(m.pricing_basis_model || 'GPT-5.4')} (OpenAI)</span>`
                : '');
        const deleteButton = isManual ? '<button class="btn-delete-codex" onclick="deleteCodexModel(\'' + escapeHtml(m.model_id).replace(/'/g, "\\'") + '\')" title="Xóa model thủ công">🗑️</button>' : '';
        return `<tr class="${rowCls}" data-model-id="${escapeHtml(m.model_id)}">
            <td>
                <div class="model-name-cell">
                    <span class="model-rank">#${i + 1}</span>
                    <div>
                        <strong class="model-name-text">${escapeHtml(m.model_id)}</strong>
                        <span class="model-provider-text">${escapeHtml(m.provider)}</span>
                    </div>
                </div>
            </td>
            <td>${platformBadge(m.platform)}</td>
            <td>${sourceBadge(m.source_kind, m.platform)}</td>
            <td class="num">${renderTokenCostPeriod(m.today_tokens, m.today_cost_usd, m.today_cost_known, 'var(--emerald-400)', m.pricing_estimated === true)}</td>
            <td class="num">${renderTokenCostPeriod(wkToks, wkCost, m.weekly_cost_known, 'var(--cyan-400)', m.pricing_estimated === true)}</td>
            <td class="num">${renderTokenCostPeriod(m.total_tokens, m.total_cost_usd, m.cost_known, 'var(--text-secondary)', m.pricing_estimated === true, pricingBasisText)}</td>
            <td class="num">${formatNumber(m.input_tokens)}</td>
            <td class="num">${formatNumber(m.output_tokens)}</td>
            <td class="num"><span class="thinking-tokens">${formatNumber(m.thinking_tokens)}</span></td>
            <td class="num">${renderAveragePerResponse(m.input_tokens, m.output_tokens, m.thinking_tokens, m.responses)}</td>
            <td class="num">${quotaBar(m.weekly_quota_pct_used, wkToks, m.weekly_limit_tokens, m)}</td>
            <td class="num">${m.sessions}</td>
            <td class="num">${m.responses}${deleteButton}</td>
        </tr>`;
    }).join('');

    // Footer totals
    if (tfoot && sorted.length > 0) {
        const tokenValue = value => {
            const numeric = Number(value);
            return Number.isFinite(numeric) ? numeric : 0;
        };
        const totals = sorted.reduce((acc, m) => {
            const bucket = m.platform === 'Codex'
                ? (m.source_kind === 'automatic' ? acc.codexExact : acc.codexManual)
                : acc.agy;
            bucket.total_tokens += tokenValue(m.total_tokens);
            bucket.weekly_tokens += tokenValue(m.weekly_tokens !== undefined ? m.weekly_tokens : m.total_tokens);
            bucket.today_tokens += tokenValue(m.today_tokens);
            bucket.input_tokens += tokenValue(m.input_tokens);
            bucket.output_tokens += tokenValue(m.output_tokens);
            bucket.thinking_tokens += tokenValue(m.thinking_tokens);
            if (m.cost_known !== false) acc.total_cost_usd += m.total_cost_usd;
            else acc.unknown_total_cost += 1;
            if (m.today_cost_known !== false) acc.today_cost_usd += tokenValue(m.today_cost_usd);
            else acc.unknown_today_cost += 1;
            if (m.weekly_cost_known !== false) acc.weekly_cost_usd += (m.weekly_cost_usd !== undefined ? m.weekly_cost_usd : m.total_cost_usd);
            else acc.unknown_weekly_cost += 1;
            acc.sessions += tokenValue(m.sessions);
            acc.responses += tokenValue(m.responses);
            return acc;
        }, {
            agy: { total_tokens: 0, weekly_tokens: 0, today_tokens: 0, input_tokens: 0, output_tokens: 0, thinking_tokens: 0 },
            codexExact: { total_tokens: 0, weekly_tokens: 0, today_tokens: 0, input_tokens: 0, output_tokens: 0, thinking_tokens: 0 },
            codexManual: { total_tokens: 0, weekly_tokens: 0, today_tokens: 0, input_tokens: 0, output_tokens: 0, thinking_tokens: 0 },
            total_cost_usd: 0, weekly_cost_usd: 0, today_cost_usd: 0,
            unknown_total_cost: 0, unknown_weekly_cost: 0, unknown_today_cost: 0,
            sessions: 0, responses: 0
        });

        const splitTokens = (agy, codexExact, codexManual) => `<span style="display:block;color:var(--cyan-400);">Gemini ${window.UsageI18n?.language === 'en' ? 'est.' : 'ước tính'}: ${formatNumber(agy)}</span><span style="display:block;color:var(--emerald-400);">Codex ${window.UsageI18n?.language === 'en' ? 'exact' : 'chính xác'}: ${formatNumber(codexExact)}</span>${codexManual ? `<span style="display:block;color:var(--text-muted);font-size:0.72rem;">Codex ${window.UsageI18n?.language === 'en' ? 'manual' : 'thủ công'}: ${formatNumber(codexManual)}</span>` : ''}`;

        tfoot.innerHTML = `<tr class="tfoot-row">
            <td><strong>TỔNG CỘNG (THEO NGUỒN)</strong></td>
            <td>${sorted.length} models</td>
            <td>—</td>
            <td class="num">${splitTokens(totals.agy.today_tokens, totals.codexExact.today_tokens, totals.codexManual.today_tokens)}${renderFooterPeriodCost(totals.today_cost_usd, totals.unknown_today_cost)}</td>
            <td class="num">${splitTokens(totals.agy.weekly_tokens, totals.codexExact.weekly_tokens, totals.codexManual.weekly_tokens)}${renderFooterPeriodCost(totals.weekly_cost_usd, totals.unknown_weekly_cost)}</td>
            <td class="num">${splitTokens(totals.agy.total_tokens, totals.codexExact.total_tokens, totals.codexManual.total_tokens)}${renderFooterPeriodCost(totals.total_cost_usd, totals.unknown_total_cost)}</td>
            <td class="num">${splitTokens(totals.agy.input_tokens, totals.codexExact.input_tokens, totals.codexManual.input_tokens)}</td>
            <td class="num">${splitTokens(totals.agy.output_tokens, totals.codexExact.output_tokens, totals.codexManual.output_tokens)}</td>
            <td class="num">${splitTokens(totals.agy.thinking_tokens, totals.codexExact.thinking_tokens, totals.codexManual.thinking_tokens)}</td>
            <td class="num">${renderAveragePerResponse(
                totals.agy.input_tokens + totals.codexExact.input_tokens + totals.codexManual.input_tokens,
                totals.agy.output_tokens + totals.codexExact.output_tokens + totals.codexManual.output_tokens,
                totals.agy.thinking_tokens + totals.codexExact.thinking_tokens + totals.codexManual.thinking_tokens,
                totals.responses
            )}</td>
            <td class="num">—</td>
            <td class="num">${totals.sessions}</td>
            <td class="num">${totals.responses}</td>
        </tr>`;
    }
}

// Codex Modal Controls
function openCodexModal() {
    const modal = document.getElementById('modal-codex');
    if (modal) modal.classList.add('active');
}

function closeCodexModal() {
    const modal = document.getElementById('modal-codex');
    if (modal) modal.classList.remove('active');
}

function openCodexLimitModal() {
    const modal = document.getElementById('modal-codex-limit');
    if (modal) {
        // Pre-fill with current value
        if (currentData && currentData.summary && currentData.summary.codex_usage) {
            const el = document.getElementById('input-codex-weekly-limit');
            if (el) el.value = currentData.summary.codex_usage.weekly_limit_tokens || '';
        }
        modal.classList.add('active');
    }
}

function closeCodexLimitModal() {
    const modal = document.getElementById('modal-codex-limit');
    if (modal) modal.classList.remove('active');
}

async function submitCodexModel() {
    const modelName = (document.getElementById('input-codex-model-name')?.value || '').trim();
    if (!modelName) {
        showToast('Vui lòng nhập tên model!', true);
        return;
    }

    const mode = document.querySelector('input[name="codex-mode"]:checked')?.value || 'add';

    const body = {
        action: 'upsert_model',
        mode: mode,
        model_name: modelName,
        total_tokens: parseInt(document.getElementById('input-codex-total-tokens')?.value || '0') || 0,
        input_tokens: parseInt(document.getElementById('input-codex-input-tokens')?.value || '0') || 0,
        output_tokens: parseInt(document.getElementById('input-codex-output-tokens')?.value || '0') || 0,
        thinking_tokens: parseInt(document.getElementById('input-codex-thinking-tokens')?.value || '0') || 0,
        cost_usd: parseFloat(document.getElementById('input-codex-cost')?.value || '0') || 0,
        sessions: parseInt(document.getElementById('input-codex-sessions')?.value || '0') || 0,
        responses: parseInt(document.getElementById('input-codex-responses')?.value || '0') || 0,
        tool_calls: parseInt(document.getElementById('input-codex-tool-calls')?.value || '0') || 0
    };
    const weeklyTokensRaw = (document.getElementById('input-codex-weekly-tokens')?.value || '').trim();
    const weeklyCostRaw = (document.getElementById('input-codex-weekly-cost')?.value || '').trim();
    if (weeklyTokensRaw !== '') body.weekly_tokens = parseInt(weeklyTokensRaw, 10) || 0;
    if (weeklyCostRaw !== '') body.weekly_cost_usd = parseFloat(weeklyCostRaw) || 0;

    try {
        const resp = await fetch('/api/codex-usage', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body)
        });
        if (resp.ok) {
            const modeText = mode === 'add' ? 'cộng dồn' : 'cập nhật';
            showToast(`Đã ${modeText} model "${modelName}" thành công!`);
            closeCodexModal();
            // Clear form
            ['input-codex-model-name','input-codex-total-tokens','input-codex-weekly-tokens','input-codex-cost','input-codex-weekly-cost','input-codex-input-tokens','input-codex-output-tokens','input-codex-thinking-tokens','input-codex-sessions','input-codex-responses','input-codex-tool-calls'].forEach(id => {
                const el = document.getElementById(id);
                if (el) el.value = '';
            });
            await forceRefreshAfterMutation();
        } else {
            const err = await resp.json();
            showToast('Lỗi: ' + (err.message || 'Unknown error'), true);
        }
    } catch (e) {
        showToast('Lỗi kết nối server: ' + e.message, true);
    }
}

async function submitCodexWeeklyLimit() {
    const limit = parseInt(document.getElementById('input-codex-weekly-limit')?.value || '0') || 0;
    try {
        const resp = await fetch('/api/codex-usage', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ action: 'set_weekly_limit', weekly_limit_tokens: limit })
        });
        if (resp.ok) {
            showToast(`Đã cập nhật hạn mức Codex: ${formatNumber(limit)} tokens/tuần`);
            closeCodexLimitModal();
            await forceRefreshAfterMutation();
        } else {
            showToast('Lỗi cập nhật hạn mức!', true);
        }
    } catch (e) {
        showToast('Lỗi kết nối server: ' + e.message, true);
    }
}

async function deleteCodexModel(modelName) {
    if (!confirm(`Xóa model "${modelName}" khỏi danh sách Codex?`)) return;
    try {
        const resp = await fetch('/api/codex-usage', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ action: 'delete_model', model_name: modelName })
        });
        if (resp.ok) {
            showToast(`Đã xóa model "${modelName}"`);
            await forceRefreshAfterMutation();
        }
    } catch (e) {
        showToast('Lỗi: ' + e.message, true);
    }
}

// ---- Goal-to-acceptance task efficiency ----
function taskOutcomeStatusGroup(mission) {
    const status = String(mission?.status || 'unresolved');
    if (status.startsWith('accepted')) return 'accepted';
    if (status.startsWith('failed_user_repaired')) return 'user_repaired';
    if (status.startsWith('abandoned')) return 'abandoned';
    if (status.startsWith('excluded')) return 'excluded';
    return 'unresolved';
}

function taskOutcomeStatusLabel(mission) {
    const status = String(mission?.status || 'unresolved');
    const labels = {
        accepted_manual: 'Đã đạt · anh xác nhận',
        accepted_explicit: 'Đã đạt · phát hiện câu xác nhận',
        accepted_inferred: 'Đã đạt · suy ra từ nhiệm vụ kế',
        accepted_inferred_review: 'Đã đạt · suy ra do không có lượt sửa',
        unresolved_manual: 'Chưa xác nhận · anh đánh dấu',
        abandoned_manual: 'Đã bỏ dở · anh đánh dấu',
        abandoned_auto_repaired: 'Model không đạt · model khác sửa tiếp',
        failed_user_repaired_auto: 'Model không đạt · anh tự sửa bằng tay',
        failed_user_repaired_manual: 'Model không đạt · anh xác nhận tự sửa',
        excluded_manual: 'Không phải nhiệm vụ · loại khỏi ma trận',
        unresolved: 'Chưa xác nhận',
    };
    return labels[status] || status;
}

function taskOutcomeConfidence(mission) {
    const rank = { low: 0, medium: 1, high: 2 };
    const category = rank[mission?.category_confidence] ?? 0;
    const status = rank[mission?.status_confidence] ?? 0;
    return ['low', 'medium', 'high'][Math.min(category, status)] || 'low';
}

function renderCodexTaskOutcomeSummary(outcomes) {
    const container = document.getElementById('task-outcome-summary-grid');
    if (!container) return;
    const summary = outcomes?.summary || {};
    const cards = [
        ['Nhiệm vụ đã ghép', formatNumber(Number(summary.mission_count || 0)), 'Từ các lượt Codex trực tiếp'],
        ['Đã đạt yêu cầu', formatNumber(Number(summary.accepted_count || 0)), 'Xác nhận hoặc suy ra có độ tin cậy'],
        ['Chưa xác nhận', formatNumber(Number(summary.unresolved_count || 0)), 'Cần kiểm tra trước khi dùng'],
        ['Nhóm chưa xác nhận', formatNumber(Number(summary.unconfirmed_group_count || 0)), 'Gom theo chủ đề và dạng yêu cầu để duyệt hàng loạt'],
        ['Loại khỏi ma trận', formatNumber(Number(summary.excluded_count || 0)), 'Lời chào, thử kết nối hoặc mục không phải nhiệm vụ'],
        ['Đổi model / subagent', formatNumber(Number(summary.mixed_model_count || 0)), 'Tuyến không quy được bị loại; lượt sửa lỗi được quy riêng'],
        ['Đã được anh kiểm tra', formatNumber(Number(summary.reviewed_count || 0)), 'Loại việc hoặc kết quả đã sửa tay'],
        ['Case khó cần xem', formatNumber(Number(summary.review_case_count || 0)), 'Chỉ lượt mơ hồ, nhãn khác hoặc độ tin cậy thấp'],
        ['Audit đáng nghi', formatNumber(Number(summary.soft_audit_case_count || 0)), `${formatNumber(Number(summary.audit_pattern_count || 0))} nhóm tín hiệu lặp lại, chưa phải lỗi chắc chắn`],
        ['Phạt sửa lỗi', formatNumber(Number(summary.repair_penalty_tokens || 0)), 'Token sửa bằng model khác cộng thêm cho model gây lỗi'],
    ];
    container.innerHTML = cards.map(([label, value, note]) => `
        <div class="summary-card">
            <div class="summary-card-label">${escapeHtml(label)}</div>
            <div class="summary-card-value">${escapeHtml(String(value))}</div>
            <div class="summary-card-sub">${escapeHtml(note)}</div>
        </div>
    `).join('');
}

function renderCodexTaskOutcomeMatrix(outcomes = cachedCodexTaskOutcomes) {
    const head = document.getElementById('task-outcome-matrix-head');
    const body = document.getElementById('task-outcome-matrix-body');
    const note = document.getElementById('task-outcome-matrix-note');
    if (!head || !body) return;
    const matrix = outcomes?.matrices?.[currentTaskOutcomeRange];
    if (!matrix || !Array.isArray(matrix.models) || matrix.models.length === 0) {
        head.innerHTML = '<tr><th>Loại công việc</th><th>Kết quả</th></tr>';
        body.innerHTML = '<tr><td colspan="2" style="text-align:center;padding:1.2rem;color:var(--text-muted);">Chưa có nhiệm vụ một model đã đạt yêu cầu trong khoảng này.</td></tr>';
        if (note) note.textContent = 'Chưa đủ dữ liệu đã đạt yêu cầu để lập ma trận.';
        return;
    }

    head.innerHTML = `<tr><th data-column-key="task-type">Loại công việc</th>${matrix.models.map(model => `
        <th class="task-model-heading" data-column-key="${escapeHtml(model)}" data-sort-type="number">${escapeHtml(model)}${model === matrix.baseline_model ? '<span class="task-cell-meta">Mốc Sol High</span>' : ''}</th>
    `).join('')}</tr>`;

    body.innerHTML = (matrix.rows || []).map(row => {
        const cells = matrix.models.map(model => {
            const cell = row.cells?.[model] || { sample_status: 'no_data', sample_count: 0 };
            if (cell.sample_status === 'no_data') {
                return '<td class="task-matrix-cell is-missing" data-sort-value="">Chưa có dữ liệu</td>';
            }
            const quotaStatus = String(cell.quota_sample_status || 'no_data');
            if (quotaStatus === 'no_data') {
                return `<td class="task-matrix-cell is-sparse" data-sort-value="">Chưa đo được hạn mức 5h<span class="task-cell-meta">n=${Number(cell.sample_count || 0)} · ${formatNumber(Number(cell.median_total_tokens || 0))} token tham khảo</span></td>`;
            }
            if (quotaStatus === 'insufficient') {
                return `<td class="task-matrix-cell is-sparse" data-sort-value="">Chưa đủ mẫu hạn mức (n=${Number(cell.quota_sample_count || 0)})<span class="task-cell-meta">${Number(cell.median_quota_pct_5h || 0).toFixed(2)}% 5h · ${formatNumber(Number(cell.median_total_tokens || 0))} token</span></td>`;
            }
            if (quotaStatus === 'no_baseline') {
                return `<td class="task-matrix-cell is-sparse" data-sort-value="">Chưa đủ mốc hạn mức Sol High<span class="task-cell-meta">Model này: ${Number(cell.median_quota_pct_5h || 0).toFixed(2)}% 5h · n=${Number(cell.quota_sample_count || 0)}</span></td>`;
            }
            const ratio = cell.relative_quota_vs_sol_high == null ? null : Number(cell.relative_quota_vs_sol_high);
            const estimated = quotaStatus === 'estimated' || cell.quota_comparison_estimated === true;
            const sparseQuota = quotaStatus === 'sparse_direct';
            const cellClass = model === matrix.baseline_model
                ? 'is-baseline'
                : ((estimated || sparseQuota) ? 'is-sparse' : (ratio < 0.95 ? 'is-efficient' : (ratio > 1.05 ? 'is-costly' : '')));
            const costText = cell.median_cost_usd === null || cell.median_cost_usd === undefined
                ? 'phí chưa rõ'
                : `$${Number(cell.median_cost_usd).toFixed(2)}`;
            const effectiveTokens = Number(cell.effective_total_tokens);
            const directMedian = Number(cell.median_total_tokens);
            const rawMedian = Number(cell.median_raw_total_tokens);
            const repairPenaltyMedian = Number(cell.median_repair_penalty_tokens);
            const effectiveQuota = Number(cell.effective_quota_pct_5h);
            const directQuota = Number(cell.median_quota_pct_5h);
            const rawQuota = Number(cell.median_raw_quota_pct_5h);
            const repairPenaltyQuota = Number(cell.median_repair_penalty_quota_pct_5h);
            const quotaBridgePath = Array.isArray(cell.quota_bridge_path) ? cell.quota_bridge_path : [];
            const quotaBaselineBridgePath = Array.isArray(cell.quota_baseline_bridge_path) ? cell.quota_baseline_bridge_path : [];
            const estimateDetails = [];
            if (estimated && cell.estimated_quota_pct_5h != null) {
                estimateDetails.push(`hạn mức model ${quotaBridgePath.length > 1 ? quotaBridgePath.join(' → ') : 'bắc cầu'} · support ${Number(cell.quota_bridge_support || 0)}`);
            }
            if (cell.quota_baseline_source === 'bridge') {
                estimateDetails.push(`mốc hạn mức Sol ${quotaBaselineBridgePath.length > 1 ? quotaBaselineBridgePath.join(' → ') : 'bắc cầu'} · support ${Number(cell.quota_baseline_bridge_support || 0)}`);
            }
            const bridgeMeta = estimateDetails.length
                ? `<span class="task-cell-meta" title="${escapeHtml(estimateDetails.join(' | '))}">${escapeHtml(estimateDetails.join(' · '))}</span>`
                : '';
            const quotaText = String(cell.quota_value_source || '') === 'sparse_direct'
                ? `Tạm tính từ median trực tiếp ${Number.isFinite(directQuota) ? directQuota.toFixed(2) : '—'}% hạn mức 5h`
                : (estimated
                    ? `Ước lượng ${Number.isFinite(effectiveQuota) ? effectiveQuota.toFixed(2) : '—'}% hạn mức 5h`
                    : `${Number.isFinite(directQuota) ? directQuota.toFixed(2) : '—'}% hạn mức 5h`);
            const tokenText = `${formatNumber(Number(cell.median_total_tokens || 0))} token tham khảo`;
            const rawEvidenceText = estimated
                ? `Dữ liệu trực tiếp: n=${Number(cell.sample_count || 0)}${Number(cell.sample_count || 0) > 0 ? ` · median ${formatNumber(Number.isFinite(directMedian) ? Math.round(directMedian) : 0)} token` : ''}`
                : `n=${Number(cell.sample_count || 0)} · sửa ${Number(cell.median_correction_turns || 0).toFixed(1)} lượt · đạt lần đầu ${Number(cell.first_pass_pct || 0).toFixed(0)}%`;
            const repairPenaltyText = Number.isFinite(repairPenaltyMedian) && repairPenaltyMedian > 0
                ? `<span class="task-cell-meta">Gốc ${formatNumber(Number.isFinite(rawMedian) ? Math.round(rawMedian) : 0)} + phạt sửa lỗi ${formatNumber(Math.round(repairPenaltyMedian))} token</span>`
                : '';
            const repairPenaltyQuotaText = Number.isFinite(repairPenaltyQuota) && repairPenaltyQuota > 0
                ? `<span class="task-cell-meta">Hạn mức gốc ${Number.isFinite(rawQuota) ? rawQuota.toFixed(2) : '0.00'}% + phạt ${repairPenaltyQuota.toFixed(2)}%</span>`
                : '';
            return `<td class="task-matrix-cell ${cellClass}" data-sort-value="${Number.isFinite(ratio) ? ratio : ''}">
                <span class="task-cell-ratio">${Number.isFinite(ratio) ? ratio.toFixed(2) + '×' : '—'}${estimated ? ' · ước lượng' : (sparseQuota ? ' · mẫu ít' : '')}</span>
                <span class="task-cell-meta">${quotaText}</span>
                <span class="task-cell-meta">${tokenText} · ${costText}</span>
                <span class="task-cell-meta">${rawEvidenceText}</span>
                ${repairPenaltyQuotaText}
                ${repairPenaltyText}
                ${bridgeMeta}
                <span class="task-cell-meta">${estimated ? '' : `Công dạy ~${formatNumber(Number(cell.median_added_guidance_tokens || 0))} token · `}tin cậy ${escapeHtml(cell.confidence || 'low')}</span>
            </td>`;
        }).join('');
        return `<tr><td><strong>${escapeHtml(row.label || row.category || 'Khác')}</strong></td>${cells}</tr>`;
    }).join('');

    if (note) {
        const rangeLabel = currentTaskOutcomeRange === 'all' ? 'toàn bộ dữ liệu' : `${currentTaskOutcomeRange} ngày`;
        note.textContent = `${rangeLabel} · ${formatNumber(Number(matrix.eligible_missions || 0))} nhiệm vụ hợp lệ; hệ số chính là % hạn mức Codex 5h hiệu dụng đến khi đạt yêu cầu, còn token chỉ là dữ liệu giải thích. Mỗi n là một nhiệm vụ hoàn chỉnh sau khi cộng các lượt/model/loại việc liên quan. Nhiệm vụ chưa xác nhận vẫn được dùng với độ tin cậy thấp hơn; ${formatNumber(Number(matrix.repair_attribution_samples || 0))} mẫu có quy hao phí sửa lỗi lũy kế. Cần ít nhất ${Number(matrix.minimum_samples || 3)} nhiệm vụ để có số trực tiếp. Mốc ${matrix.baseline_model || 'Sol High'} cùng loại việc được ưu tiên; dữ liệu hạn mức đo trực tiếp được dùng trước, sau đó mới đến tốc độ tiêu hao thực nghiệm theo model và ước lượng bắc cầu.`;
    }
    initTableColumnResizers();
}

function taskOutcomeCategoryEditor(outcomes, mission) {
    const automatic = !mission?.category_reviewed;
    const selected = new Set(Array.isArray(mission?.categories) ? mission.categories : [mission?.category].filter(Boolean));
    const anchor = escapeHtml(String(mission?.anchor_turn_id || ''));
    const autoLabel = `Tự động (${(mission?.category_labels || [mission?.category_label || 'Khác']).join(' + ')})`;
    const categoryChoices = (outcomes?.categories || []).map(category => `
        <label class="task-category-choice">
            <input type="checkbox" data-task-review-categories data-anchor="${anchor}" value="${escapeHtml(category.key)}"
                ${!automatic && selected.has(category.key) ? 'checked' : ''}>
            <span>${escapeHtml(category.label)}</span>
        </label>
    `).join('');
    return `<div class="task-category-editor" data-task-category-editor data-anchor="${anchor}">
        <label class="task-category-choice is-auto">
            <input type="checkbox" data-task-review-categories data-category-auto="1" data-anchor="${anchor}" value="auto" ${automatic ? 'checked' : ''}>
            <span>${escapeHtml(autoLabel)}</span>
        </label>
        <div class="task-category-choice-grid">${categoryChoices}</div>
    </div>`;
}

function taskOutcomeReviewOptions(mission) {
    const automatic = !mission?.outcome_reviewed;
    const status = String(mission?.status || 'unresolved');
    const inferredReview = status === 'accepted_inferred_review';
    const autoLabel = `Tự động (${taskOutcomeStatusLabel(mission)})`;
    return [
        `<option value="auto" ${automatic ? 'selected' : ''}>${escapeHtml(autoLabel)}</option>`,
        `<option value="accepted" ${!automatic && mission.accepted && !inferredReview ? 'selected' : ''}>Đã đạt yêu cầu · xác nhận trực tiếp</option>`,
        `<option value="inferred" ${!automatic && inferredReview ? 'selected' : ''}>Đã đạt · suy ra do không có lượt sửa</option>`,
        `<option value="unresolved" ${!automatic && taskOutcomeStatusGroup(mission) === 'unresolved' ? 'selected' : ''}>Chưa xác nhận</option>`,
        `<option value="abandoned" ${!automatic && taskOutcomeStatusGroup(mission) === 'abandoned' && !status.startsWith('failed_user_repaired') ? 'selected' : ''}>Đã bỏ dở</option>`,
        `<option value="user_repaired" ${!automatic && status.startsWith('failed_user_repaired') ? 'selected' : ''}>Model không đạt · tôi tự sửa bằng tay</option>`,
        `<option value="excluded" ${!automatic && taskOutcomeStatusGroup(mission) === 'excluded' ? 'selected' : ''}>Không phải nhiệm vụ · loại khỏi ma trận</option>`,
    ].join('');
}

function taskOutcomeRepairOptions(mission) {
    const mode = String(mission?.repair_link_mode || 'auto');
    const manualAnchor = String(mission?.repair_of_anchor_turn_id_override || '');
    const currentModel = mission?.repair_original_model ? `: ${mission.repair_original_model}` : '';
    const confidence = mission?.repair_link_confidence && mission.repair_link_confidence !== 'manual'
        ? ` · tin cậy ${mission.repair_link_confidence}`
        : '';
    const options = [
        `<option value="auto" ${mode === 'auto' ? 'selected' : ''}>Tự động${escapeHtml(currentModel + confidence)}</option>`,
        `<option value="none" ${mode === 'none' ? 'selected' : ''}>Không phải lượt sửa</option>`,
    ];
    const seen = new Set();
    for (const candidate of mission?.repair_link_candidates || []) {
        const anchor = String(candidate?.anchor_turn_id || '');
        if (!anchor || seen.has(anchor)) continue;
        seen.add(anchor);
        const model = String(candidate?.model_key || 'Không rõ model');
        const date = candidate?.start_at ? formatDate(candidate.start_at) : '';
        const title = String(candidate?.title || '').replace(/\s+/g, ' ').slice(0, 72);
        const label = [model, date, title].filter(Boolean).join(' · ');
        options.push(`<option value="${escapeHtml(anchor)}" ${mode === 'manual' && manualAnchor === anchor ? 'selected' : ''}>${escapeHtml(label)}</option>`);
    }
    if (mode === 'manual' && manualAnchor && !seen.has(manualAnchor)) {
        options.push(`<option value="${escapeHtml(manualAnchor)}" selected>Liên kết đã lưu · ${escapeHtml(manualAnchor)}</option>`);
    }
    return options.join('');
}

function renderCodexTaskOutcomeAudit(outcomes = cachedCodexTaskOutcomes) {
    const body = document.getElementById('task-outcome-audit-body');
    const count = document.getElementById('task-outcome-visible-count');
    const note = document.getElementById('task-outcome-audit-note');
    if (!body) return;
    const search = String(document.getElementById('task-outcome-search')?.value || '').trim().toLocaleLowerCase('vi');
    const missions = Array.isArray(outcomes?.missions) ? outcomes.missions : [];
    const filtered = missions.filter(mission => {
        const missionCategories = Array.isArray(mission.categories) && mission.categories.length
            ? mission.categories
            : [mission.category].filter(Boolean);
        if (currentTaskOutcomeCategoryFilter !== 'all' && !missionCategories.includes(currentTaskOutcomeCategoryFilter)) return false;
        if (currentTaskOutcomeStatusFilter !== 'all' && taskOutcomeStatusGroup(mission) !== currentTaskOutcomeStatusFilter) return false;
        if (currentTaskOutcomeUnconfirmedGroupFilter !== 'all' && mission.unconfirmed_group_key !== currentTaskOutcomeUnconfirmedGroupFilter) return false;
        if (currentTaskOutcomeRouteFilter === 'pure' && !mission.pure_model) return false;
        if (currentTaskOutcomeRouteFilter === 'mixed' && mission.pure_model) return false;
        if (currentTaskOutcomeReviewFilter === 'audit' && !mission.soft_audit_needed) return false;
        if (currentTaskOutcomeReviewFilter === 'hard' && !(mission.needs_review ?? mission.needs_category_review)) return false;
        if (currentTaskOutcomeAuditReasonFilter !== 'all' && mission.audit_pattern_key !== currentTaskOutcomeAuditReasonFilter) return false;
        if (search) {
            const categoryLabels = Array.isArray(mission.category_labels) ? mission.category_labels.join(' ') : (mission.category_label || '');
            const haystack = `${mission.title || ''} ${mission.route_label || ''} ${categoryLabels} ${(mission.category_review_reasons || []).join(' ')} ${(mission.audit_reasons || []).join(' ')}`.toLocaleLowerCase('vi');
            if (!haystack.includes(search)) return false;
        }
        return true;
    });
    const visible = filtered.slice(0, 300);
    if (count) count.textContent = `${formatNumber(visible.length)} / ${formatNumber(filtered.length)} nhiệm vụ`;
    if (!visible.length) {
        body.innerHTML = '<tr><td colspan="10" style="text-align:center;padding:1.2rem;color:var(--text-muted);">Không có nhiệm vụ phù hợp bộ lọc.</td></tr>';
    } else {
        body.innerHTML = visible.map(mission => {
            const anchor = escapeHtml(String(mission.anchor_turn_id || ''));
            const lastTurn = escapeHtml(String((mission.turn_ids || [])[mission.turn_ids.length - 1] || ''));
            const statusGroup = taskOutcomeStatusGroup(mission);
            const confidence = taskOutcomeConfidence(mission);
            const mixed = !mission.pure_model;
            const reviewReasonLabels = {
                other_category: 'chưa xác định loại',
                low_category_confidence: 'tin cậy phân loại thấp',
                legacy_no_turn_detail: 'thiếu chi tiết theo lượt',
                hard_turns: 'có lượt khó phân loại',
                low_repair_link_confidence: 'liên kết lượt sửa chưa chắc',
            };
            const auditReasonLabels = {
                hard_review: 'đang thuộc review cứng',
                high_confidence_competing_categories: 'tin cậy cao nhưng có category cạnh tranh',
                inherited_followup_in_mixed_mission: 'follow-up kế thừa trong mission trộn',
                tied_category_signals: 'tín hiệu category hòa điểm',
                auto_multi_category: 'mission đa chức năng do máy tự gán',
                mixed_models_and_functions: 'vừa đổi model vừa đổi chức năng',
                auto_repair_link_not_high: 'nối lượt sửa tự động chưa đủ chắc',
                long_turn_gap: 'có khoảng nghỉ dài giữa các lượt',
            };
            const reviewReasons = (mission.category_review_reasons || []).map(reason => reviewReasonLabels[reason] || reason);
            const reviewHint = (mission.needs_review ?? mission.needs_category_review)
                ? `<div class="task-mission-subline task-review-warning">Cần xem lại: ${escapeHtml(reviewReasons.join(' · '))}</div>`
                : '';
            const auditReasons = (mission.audit_reasons || [])
                .filter(reason => reason !== 'hard_review')
                .map(reason => auditReasonLabels[reason] || reason);
            const auditHint = mission.audit_needed && auditReasons.length
                ? `<div class="task-mission-subline task-audit-warning">Đáng audit (${escapeHtml(String(mission.audit_priority || 'low'))}): ${escapeHtml(auditReasons.join(' · '))}</div>`
                : '';
            const repairHint = mission.repair_of_mission_id
                ? `<div class="task-mission-subline">Sửa cho ${escapeHtml(mission.repair_original_model || 'model trước')} · ${formatNumber(Number(mission.repair_tokens || 0))} token${Number(mission.repair_penalty_tokens || 0) > 0 ? ` · phạt ${formatNumber(Number(mission.repair_penalty_tokens || 0))}` : ''}</div>`
                : '';
            const penaltyReceived = Number(mission.repair_penalty_received_tokens || 0);
            const penaltyQuota = Number(mission.repair_penalty_received_quota_pct_5h || 0);
            const penaltyHint = penaltyReceived > 0
                ? `<div class="task-mission-subline task-repair-penalty">Phạt sửa lỗi +${formatNumber(penaltyReceived)} token${penaltyQuota > 0 ? ` · +${penaltyQuota.toFixed(2)}% hạn mức` : ''}</div>`
                : '';
            const chainModels = Array.isArray(mission.repair_chain_models) ? mission.repair_chain_models : [];
            const chainHint = chainModels.length > 1
                ? `<div class="task-mission-subline task-repair-chain">Chuỗi sửa: ${escapeHtml(chainModels.join(' → '))}</div>`
                : '';
            const quotaKnown = mission.quota_known === true && mission.quota_pct_5h != null;
            const usageValue = quotaKnown
                ? `<strong>${Number(mission.quota_pct_5h).toFixed(2)}% 5h</strong><div class="task-mission-subline">${formatNumber(Number(mission.total_tokens || 0))} token · ${mission.cost_known ? '$' + Number(mission.cost_usd || 0).toFixed(2) : 'phí chưa rõ'}</div>`
                : `<strong>${formatNumber(Number(mission.total_tokens || 0))} token</strong><div class="task-mission-subline">chưa đo được hạn mức · ${mission.cost_known ? '$' + Number(mission.cost_usd || 0).toFixed(2) : 'phí chưa rõ'}</div>`;
            const turnDetails = (mission.turns || []).map((turn, index) => `
                <li><strong>Lượt ${index + 1}</strong> · ${escapeHtml(turn.model_key || 'Không rõ model')} · ${formatNumber(Number(turn.total_tokens || 0))} token<br>${escapeHtml(turn.prompt || '')}</li>
            `).join('');
            return `<tr data-mission-anchor="${anchor}">
                <td>
                    <div class="task-mission-title">${escapeHtml(mission.title || '')}</div>
                    <div class="task-mission-subline">${escapeHtml(String(mission.id || ''))}${mission.reviewed ? ' · đã hiệu chỉnh' : ' · tự động'}</div>
                    ${reviewHint}
                    ${auditHint}
                    <details class="task-mission-turns"><summary>Xem ${Number(mission.turn_count || 0)} lượt làm việc</summary><ol>${turnDetails}</ol></details>
                </td>
                <td>${taskOutcomeCategoryEditor(outcomes, mission)}</td>
                <td>
                    <span class="task-status-pill ${statusGroup}">${escapeHtml(taskOutcomeStatusLabel(mission))}</span>
                    <select class="task-review-select" data-task-review="outcome" data-anchor="${anchor}" style="margin-top:6px;">${taskOutcomeReviewOptions(mission)}</select>
                </td>
                <td><div class="task-route ${mixed ? 'task-route-mixed' : ''}">${escapeHtml(mission.route_label || 'Không rõ model')}</div>${mission.has_delegated_work ? `<div class="task-mission-subline">Có ${Number(mission.delegated_turn_count || 0)} lượt subagent · ${formatNumber(Number(mission.delegated_tokens || 0))} token</div>` : ''}${repairHint}${chainHint}<label class="task-repair-link"><span>Nối lượt sửa với nhiệm vụ</span><select class="task-review-select" data-task-review="repair_of_anchor_turn_id" data-anchor="${anchor}">${taskOutcomeRepairOptions(mission)}</select></label></td>
                <td class="num"><strong>${Number(mission.turn_count || 0)}</strong> / ${Number(mission.correction_turns || 0)}</td>
                <td class="num" data-sort-value="${quotaKnown ? Number(mission.quota_pct_5h) : ''}">${usageValue}${penaltyHint}</td>
                <td class="num">~${formatNumber(Number(mission.added_guidance_tokens_est || 0))}</td>
                <td>${formatDate(mission.start_at)}</td>
                <td><span class="task-confidence-pill ${confidence}">${confidence === 'high' ? 'Cao' : (confidence === 'medium' ? 'Vừa' : 'Thấp')}</span></td>
                <td><div class="task-boundary-actions">
                    <button class="task-boundary-btn" data-task-action="merge_previous" data-anchor="${anchor}" ${mission.previous_anchor_turn_id ? '' : 'disabled'}>Ghép mục trước</button>
                    <button class="task-boundary-btn" data-task-action="split_last" data-turn="${lastTurn}" ${Number(mission.turn_count || 0) > 1 ? '' : 'disabled'}>Tách lượt cuối</button>
                    <button class="task-boundary-btn" data-task-action="reset_boundary" data-anchor="${anchor}" ${mission.boundary_override ? '' : 'disabled'}>Trả tự động</button>
                </div></td>
            </tr>`;
        }).join('');
    }
    if (note) {
        const diagnostics = outcomes?.diagnostics || {};
        const truncated = filtered.length > visible.length ? ` Chỉ hiện 300/${formatNumber(filtered.length)} kết quả phù hợp.` : '';
        note.textContent = `Đã lập chỉ mục ${formatNumber(Number(diagnostics.turns_indexed || 0))} lượt; gắn ${formatNumber(Number(diagnostics.delegated_turns_attached || 0))} lượt subagent vào nhiệm vụ cha. “Review cứng” là case yếu hoặc chưa rõ; “audit đáng nghi” là case máy đang khá tự tin nhưng có tổ hợp tín hiệu nên kiểm tra. Chọn một nhóm audit để duyệt các mẫu tương đồng và chốt rule.${truncated}`;
    }
    initTableColumnResizers();
}

function formatTaskDuration(minutes) {
    if (!Number.isFinite(minutes)) return '—';
    return minutes >= 120 ? `${(minutes / 60).toFixed(1)} h` : `${minutes.toFixed(1)} min`;
}

function renderTaskDurationTimeline(data) {
    const canvas = document.getElementById('canvas-task-duration');
    const status = document.getElementById('task-duration-status');
    const details = document.getElementById('task-duration-details');
    const legend = document.getElementById('task-duration-legend');
    const body = document.getElementById('task-duration-recent-body');
    if (!canvas || !status || !details || !legend || !body) return;
    const english = window.UsageI18n?.language === 'en';
    const days = Number(currentTaskDurationRange);
    const windowDays = Number(currentTaskDurationWindow);
    const dates = (data?.dates || []).slice(-days);
    const rows = (data?.windows?.[String(windowDays)]?.models || []).map(row => ({
        ...row,
        points: (row.points || []).slice(-days),
        rangeTaskCount: (row.daily_task_counts || []).slice(-days)
            .reduce((sum, count) => sum + Number(count || 0), 0),
    })).filter(row => row.points.some(point => point.task_count > 0));
    const c = CanvasCharts.initCanvas(canvas);
    if (!c) return;
    const { ctx, width, height } = c;
    ctx.clearRect(0, 0, width, height);
    canvas.onmousemove = null;
    canvas.onmouseleave = null;
    if (!rows.length) {
        status.textContent = english
            ? 'No accepted, single-model Codex tasks have valid work intervals in this range.'
            : 'Chưa có nhiệm vụ Codex một model đã đạt yêu cầu với khoảng xử lý hợp lệ trong phạm vi này.';
        legend.innerHTML = '';
        body.innerHTML = `<tr><td colspan="7" style="text-align:center;color:var(--text-muted);padding:1.1rem;">${english ? 'No attributable task-duration samples.' : 'Chưa có mẫu thời gian nhiệm vụ quy được cho một model.'}</td></tr>`;
        details.textContent = '';
        return;
    }

    legend.innerHTML = [
        `<button type="button" data-duration-model="all" class="${currentTaskDurationSelection === null ? 'active' : ''}" style="--quota-color:${THEME.cyan}">${english ? 'All' : 'Tất cả'} (${rows.length})</button>`,
        ...rows.map(row => {
            const color = colorForModelKey(row.model_key);
            const active = currentTaskDurationSelection !== null && currentTaskDurationSelection.has(row.model_key);
            const labelColor = currentTaskDurationSelection === null || active ? color : '#cbd5e1';
            return `<button type="button" data-duration-model="${escapeHtml(row.model_key)}" class="${active ? 'active' : ''}" style="--quota-color:${color};color:${labelColor}"><span class="quota-timeline-swatch"></span>${escapeHtml(row.model_key)} (${formatNumber(row.rangeTaskCount)})</button>`;
        }),
    ].join('');
    legend.querySelectorAll('button[data-duration-model]').forEach(button => {
        button.addEventListener('click', () => toggleTaskDurationModel(button.dataset.durationModel));
    });
    const selectedRows = currentTaskDurationSelection === null
        ? rows : rows.filter(row => currentTaskDurationSelection.has(row.model_key));
    const measuredInRange = rows.reduce((sum, row) => sum + row.rangeTaskCount, 0);
    status.textContent = english
        ? `${days}-day range · ${windowDays}-day rolling mean · ${rows.length} models · ${formatNumber(measuredInRange)} accepted single-model tasks · ${selectedRows.length} ${selectedRows.length === 1 ? 'model' : 'models'} shown.`
        : `Phạm vi ${days} ngày · trung bình trượt ${windowDays} ngày · ${rows.length} model · ${formatNumber(measuredInRange)} nhiệm vụ một model đã đạt yêu cầu · đang hiện ${selectedRows.length} model.`;
    details.textContent = english
        ? `Only completed, single-model tasks are attributed. In retained history, ${formatNumber(Number(data.mixed_excluded_count || 0))} mixed-model and ${formatNumber(Number(data.repair_excluded_count || 0))} separately linked repair missions are excluded; ${formatNumber(Number(data.missing_duration_count || 0))} eligible tasks have missing or invalid work intervals. Waiting between turns is excluded. Inferred acceptances are included, and task type or difficulty is not normalized.`
        : `Chỉ quy thời gian cho nhiệm vụ hoàn tất bằng một model. Trong lịch sử giữ lại, loại ${formatNumber(Number(data.mixed_excluded_count || 0))} nhiệm vụ trộn model và ${formatNumber(Number(data.repair_excluded_count || 0))} nhiệm vụ sửa được nối riêng; ${formatNumber(Number(data.missing_duration_count || 0))} nhiệm vụ đủ điều kiện có khoảng xử lý thiếu hoặc không hợp lệ. Bỏ khoảng chờ giữa các lượt. Vẫn gồm các ca đạt yêu cầu do máy suy luận; chưa chuẩn hóa theo loại và độ khó công việc.`;
    body.innerHTML = selectedRows.length ? selectedRows.map(row => {
        const latest = row.points[row.points.length - 1] || {};
        const count = Number(latest.task_count || 0);
        const color = colorForModelKey(row.model_key);
        const countNote = count > 0 && count < 3
            ? `<span class="task-duration-sparse"> · ${english ? 'few samples' : 'ít mẫu'}</span>` : '';
        return `<tr>
            <td style="font-weight:600;color:${color};">${escapeHtml(row.model_key)}</td>
            <td class="num">${count ? formatTaskDuration(Number(latest.mean_minutes)) : '—'}</td>
            <td class="num">${count ? formatTaskDuration(Number(latest.median_minutes)) : '—'}</td>
            <td class="num">${formatNumber(count)}${countNote}</td>
            <td class="num">${formatNumber(row.rangeTaskCount)}</td>
            <td class="num">${formatNumber(Number(latest.reviewed_count || 0))} / ${formatNumber(count)}</td>
            <td>${row.last_observed_at ? formatDate(row.last_observed_at) : '—'}</td>
        </tr>`;
    }).join('') : `<tr><td colspan="7" style="text-align:center;color:var(--text-muted);padding:1.1rem;">${english ? 'Select one or more models above.' : 'Chọn một hoặc nhiều model ở phía trên.'}</td></tr>`;
    if (!selectedRows.length) return;

    const pad = { top: 22, right: 24, bottom: 42, left: 66 };
    const chartW = Math.max(1, width - pad.left - pad.right);
    const chartH = Math.max(1, height - pad.top - pad.bottom);
    const validValues = selectedRows.flatMap(row => row.points
        .filter(point => point.task_count > 0 && Number.isFinite(point.mean_minutes))
        .map(point => point.mean_minutes));
    const maxValue = Math.max(1, ...validValues) * 1.15;
    ctx.font = '500 10px "JetBrains Mono", monospace';
    ctx.textAlign = 'right';
    ctx.textBaseline = 'middle';
    for (let tick = 0; tick <= 4; tick++) {
        const value = maxValue * tick / 4;
        const y = pad.top + chartH * (1 - tick / 4);
        ctx.beginPath();
        ctx.strokeStyle = 'rgba(255,255,255,0.08)';
        ctx.moveTo(pad.left, y);
        ctx.lineTo(pad.left + chartW, y);
        ctx.stroke();
        ctx.fillStyle = '#94a3b8';
        ctx.fillText(formatTaskDuration(value), pad.left - 8, y);
    }
    const xFor = index => pad.left + (dates.length <= 1 ? chartW / 2 : index * chartW / (dates.length - 1));
    const yFor = minutes => pad.top + chartH * (1 - minutes / maxValue);
    selectedRows.forEach(row => {
        const color = colorForModelKey(row.model_key);
        let drawing = false;
        ctx.lineWidth = 2.3;
        ctx.strokeStyle = color;
        ctx.beginPath();
        row.points.forEach((point, index) => {
            if (!Number.isFinite(point.mean_minutes) || point.task_count <= 0) {
                drawing = false;
                return;
            }
            const x = xFor(index), y = yFor(point.mean_minutes);
            if (drawing) ctx.lineTo(x, y);
            else ctx.moveTo(x, y);
            drawing = true;
        });
        ctx.stroke();
        row.points.forEach((point, index) => {
            if (!Number.isFinite(point.mean_minutes) || point.task_count <= 0) return;
            ctx.beginPath();
            ctx.arc(xFor(index), yFor(point.mean_minutes), 2.5, 0, Math.PI * 2);
            ctx.fillStyle = color;
            ctx.fill();
        });
    });
    const labelStep = dates.length > 120 ? 30 : (dates.length > 60 ? 14 : 7);
    ctx.textAlign = 'center';
    ctx.textBaseline = 'top';
    dates.forEach((date, index) => {
        if (index % labelStep !== 0 && index !== dates.length - 1) return;
        ctx.fillStyle = '#94a3b8';
        ctx.fillText(quotaTimelineDateLabel(date), xFor(index), pad.top + chartH + 9);
    });
    canvas.onmousemove = event => {
        const rect = canvas.getBoundingClientRect();
        if (!rect.width || !dates.length) return;
        const x = (event.clientX - rect.left) * width / rect.width;
        const index = Math.max(0, Math.min(dates.length - 1,
            Math.round((x - pad.left) * (dates.length - 1) / chartW)));
        const lines = selectedRows.map(row => {
            const point = row.points[index];
            return point?.task_count > 0
                ? `${row.model_key}: ${formatTaskDuration(point.mean_minutes)} (n=${point.task_count})`
                : null;
        }).filter(Boolean);
        canvas.title = `${dates[index]}${lines.length ? '\n' + lines.join('\n') : ''}`;
    };
    canvas.onmouseleave = () => { canvas.title = ''; };
}

function toggleTaskDurationModel(modelKey) {
    if (modelKey === 'all') {
        currentTaskDurationSelection = null;
    } else if (currentTaskDurationSelection === null) {
        currentTaskDurationSelection = new Set([modelKey]);
    } else if (currentTaskDurationSelection.has(modelKey)) {
        currentTaskDurationSelection.delete(modelKey);
    } else {
        currentTaskDurationSelection.add(modelKey);
    }
    saveUiPreferences({ taskDurationSelection: serializeModelSelection(currentTaskDurationSelection) });
    renderTaskDurationTimeline(cachedCodexTaskOutcomes?.duration_timeline);
}

function initTaskDurationControls() {
    const range = document.getElementById('task-duration-range');
    const window = document.getElementById('task-duration-window');
    if (!range || !window) return;
    range.value = currentTaskDurationRange;
    window.value = currentTaskDurationWindow;
    range.addEventListener('change', () => {
        currentTaskDurationRange = ['30', '90', '180'].includes(range.value) ? range.value : '90';
        saveUiPreferences({ taskDurationRange: currentTaskDurationRange });
        renderTaskDurationTimeline(cachedCodexTaskOutcomes?.duration_timeline);
    });
    window.addEventListener('change', () => {
        currentTaskDurationWindow = ['7', '14', '30'].includes(window.value) ? window.value : '14';
        saveUiPreferences({ taskDurationWindow: currentTaskDurationWindow });
        renderTaskDurationTimeline(cachedCodexTaskOutcomes?.duration_timeline);
    });
}

function renderCodexTaskOutcomes(codexUsage) {
    const outcomes = codexUsage?.automatic_model_usage?.task_outcomes;
    if (!outcomes) return;
    cachedCodexTaskOutcomes = outcomes;
    renderCodexTaskOutcomeSummary(outcomes);
    renderTaskDurationTimeline(outcomes.duration_timeline);
    const categoryFilter = document.getElementById('task-outcome-category-filter');
    if (categoryFilter) {
        const allowed = new Set(['all', ...(outcomes.categories || []).map(item => item.key)]);
        if (!allowed.has(currentTaskOutcomeCategoryFilter)) currentTaskOutcomeCategoryFilter = 'all';
        categoryFilter.innerHTML = '<option value="all">Tất cả loại công việc</option>' + (outcomes.categories || []).map(item => (
            `<option value="${escapeHtml(item.key)}">${escapeHtml(item.label)}</option>`
        )).join('');
        categoryFilter.value = currentTaskOutcomeCategoryFilter;
    }
    const auditReasonFilter = document.getElementById('task-outcome-audit-reason-filter');
    if (auditReasonFilter) {
        const auditReasonLabels = {
            hard_review: 'Review cứng hiện tại',
            high_confidence_competing_categories: 'Tin cậy cao + category cạnh tranh',
            inherited_followup_in_mixed_mission: 'Follow-up kế thừa trong mission trộn',
            tied_category_signals: 'Tín hiệu category hòa điểm',
            auto_multi_category: 'Mission đa chức năng tự động',
            mixed_models_and_functions: 'Đổi model + đổi chức năng',
            auto_repair_link_not_high: 'Nối lượt sửa tự động chưa chắc',
            long_turn_gap: 'Khoảng nghỉ dài giữa các lượt',
        };
        const groups = Array.isArray(outcomes.audit_patterns) ? outcomes.audit_patterns : [];
        const allowedReasons = new Set(['all', ...groups.map(group => group.key)]);
        if (!allowedReasons.has(currentTaskOutcomeAuditReasonFilter)) currentTaskOutcomeAuditReasonFilter = 'all';
        auditReasonFilter.innerHTML = '<option value="all">Tất cả nhóm audit</option>' + groups.map(group => (
            `<option value="${escapeHtml(group.key)}">${escapeHtml((group.reasons || []).map(reason => auditReasonLabels[reason] || reason).join(' + '))} (${formatNumber(Number(group.mission_count || 0))})</option>`
        )).join('');
        auditReasonFilter.value = currentTaskOutcomeAuditReasonFilter;
    }
    const unconfirmedGroupFilter = document.getElementById('task-outcome-unconfirmed-group-filter');
    if (unconfirmedGroupFilter) {
        const groups = Array.isArray(outcomes.unconfirmed_groups) ? outcomes.unconfirmed_groups : [];
        const allowedGroups = new Set(['all', ...groups.map(group => group.key)]);
        if (!allowedGroups.has(currentTaskOutcomeUnconfirmedGroupFilter)) currentTaskOutcomeUnconfirmedGroupFilter = 'all';
        unconfirmedGroupFilter.innerHTML = '<option value="all">Tất cả nhóm chưa xác nhận</option>' + groups.map(group => (
            `<option value="${escapeHtml(group.key)}">${escapeHtml(group.label)} (${formatNumber(Number(group.mission_count || 0))})</option>`
        )).join('');
        unconfirmedGroupFilter.value = currentTaskOutcomeUnconfirmedGroupFilter;
    }
    renderCodexTaskOutcomeMatrix(outcomes);
    renderCodexTaskOutcomeAudit(outcomes);
}

async function saveCodexTaskOutcomeReview(payload) {
    try {
        const response = await fetch('/api/codex-missions/review', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        });
        const result = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(result.message || `HTTP ${response.status}`);
        showToast('Đã lưu hiệu chỉnh nhiệm vụ.');
        await forceRefreshAfterMutation();
    } catch (error) {
        showToast(`Không thể lưu hiệu chỉnh: ${error.message}`, true);
        if (cachedCodexTaskOutcomes) renderCodexTaskOutcomeAudit(cachedCodexTaskOutcomes);
    }
}

function initCodexTaskOutcomeControls() {
    const range = document.getElementById('task-outcome-range');
    const category = document.getElementById('task-outcome-category-filter');
    const status = document.getElementById('task-outcome-status-filter');
    const unconfirmedGroup = document.getElementById('task-outcome-unconfirmed-group-filter');
    const route = document.getElementById('task-outcome-route-filter');
    const review = document.getElementById('task-outcome-review-filter');
    const auditReason = document.getElementById('task-outcome-audit-reason-filter');
    const search = document.getElementById('task-outcome-search');
    if (range) {
        range.value = currentTaskOutcomeRange;
        range.addEventListener('change', () => {
            currentTaskOutcomeRange = ['90', '180', '365', 'all'].includes(range.value) ? range.value : '180';
            saveUiPreferences({ taskOutcomeRange: currentTaskOutcomeRange });
            renderCodexTaskOutcomeMatrix();
        });
    }
    if (category) {
        category.addEventListener('change', () => {
            currentTaskOutcomeCategoryFilter = category.value || 'all';
            saveUiPreferences({ taskOutcomeCategoryFilter: currentTaskOutcomeCategoryFilter });
            renderCodexTaskOutcomeAudit();
        });
    }
    if (status) {
        status.value = currentTaskOutcomeStatusFilter;
        status.addEventListener('change', () => {
            currentTaskOutcomeStatusFilter = status.value;
            if (currentTaskOutcomeStatusFilter !== 'unresolved') {
                currentTaskOutcomeUnconfirmedGroupFilter = 'all';
                if (unconfirmedGroup) unconfirmedGroup.value = 'all';
            }
            saveUiPreferences({
                taskOutcomeStatusFilter: currentTaskOutcomeStatusFilter,
                taskOutcomeUnconfirmedGroupFilter: currentTaskOutcomeUnconfirmedGroupFilter,
            });
            renderCodexTaskOutcomeAudit();
        });
    }
    if (unconfirmedGroup) {
        unconfirmedGroup.addEventListener('change', () => {
            currentTaskOutcomeUnconfirmedGroupFilter = unconfirmedGroup.value || 'all';
            if (currentTaskOutcomeUnconfirmedGroupFilter !== 'all') {
                currentTaskOutcomeStatusFilter = 'unresolved';
                if (status) status.value = 'unresolved';
            }
            saveUiPreferences({
                taskOutcomeStatusFilter: currentTaskOutcomeStatusFilter,
                taskOutcomeUnconfirmedGroupFilter: currentTaskOutcomeUnconfirmedGroupFilter,
            });
            renderCodexTaskOutcomeAudit();
        });
    }
    if (route) {
        route.value = currentTaskOutcomeRouteFilter;
        route.addEventListener('change', () => {
            currentTaskOutcomeRouteFilter = route.value;
            saveUiPreferences({ taskOutcomeRouteFilter: currentTaskOutcomeRouteFilter });
            renderCodexTaskOutcomeAudit();
        });
    }
    if (review) {
        review.value = currentTaskOutcomeReviewFilter;
        review.addEventListener('change', () => {
            currentTaskOutcomeReviewFilter = ['all', 'audit', 'hard'].includes(review.value) ? review.value : 'all';
            if (currentTaskOutcomeReviewFilter === 'hard') {
                currentTaskOutcomeAuditReasonFilter = 'all';
                if (auditReason) auditReason.value = 'all';
            }
            saveUiPreferences({
                taskOutcomeReviewFilter: currentTaskOutcomeReviewFilter,
                taskOutcomeAuditReasonFilter: currentTaskOutcomeAuditReasonFilter,
            });
            renderCodexTaskOutcomeAudit();
        });
    }
    if (auditReason) {
        auditReason.addEventListener('change', () => {
            currentTaskOutcomeAuditReasonFilter = auditReason.value || 'all';
            if (currentTaskOutcomeAuditReasonFilter !== 'all') {
                currentTaskOutcomeReviewFilter = 'audit';
                if (review) review.value = 'audit';
            }
            saveUiPreferences({
                taskOutcomeReviewFilter: currentTaskOutcomeReviewFilter,
                taskOutcomeAuditReasonFilter: currentTaskOutcomeAuditReasonFilter,
            });
            renderCodexTaskOutcomeAudit();
        });
    }
    search?.addEventListener('input', () => renderCodexTaskOutcomeAudit());

    document.getElementById('task-outcome-audit-body')?.addEventListener('change', event => {
        const categoryInput = event.target.closest('input[data-task-review-categories]');
        if (categoryInput) {
            const editor = categoryInput.closest('[data-task-category-editor]');
            if (!editor) return;
            const automatic = editor.querySelector('input[data-category-auto="1"]');
            const manual = Array.from(editor.querySelectorAll('input[data-task-review-categories]:not([data-category-auto="1"])'));
            if (categoryInput.dataset.categoryAuto === '1') {
                if (categoryInput.checked) {
                    manual.forEach(input => { input.checked = false; });
                } else if (!manual.some(input => input.checked)) {
                    categoryInput.checked = true;
                    return;
                }
            } else if (categoryInput.checked && automatic) {
                automatic.checked = false;
            }
            const categories = manual.filter(input => input.checked).map(input => input.value);
            if (!categories.length && automatic) automatic.checked = true;
            saveCodexTaskOutcomeReview({
                action: 'review',
                anchor_turn_id: categoryInput.dataset.anchor,
                categories,
            });
            return;
        }
        const select = event.target.closest('select[data-task-review]');
        if (!select) return;
        saveCodexTaskOutcomeReview({
            action: 'review',
            anchor_turn_id: select.dataset.anchor,
            [select.dataset.taskReview]: select.value,
        });
    });
    document.getElementById('task-outcome-audit-body')?.addEventListener('click', event => {
        const button = event.target.closest('button[data-task-action]');
        if (!button || button.disabled) return;
        const payload = { action: button.dataset.taskAction };
        if (button.dataset.anchor) payload.anchor_turn_id = button.dataset.anchor;
        if (button.dataset.turn) payload.turn_id = button.dataset.turn;
        button.disabled = true;
        saveCodexTaskOutcomeReview(payload);
    });
}

// ---- Initialization ----
document.addEventListener('DOMContentLoaded', () => {
    // Tab setup
    setupTabs();

    window.addEventListener('usage-tracker:language-changed', () => {
        if (currentData?.summary) renderAll(currentData);
    });

    restoreSelectPreference('session-signal-filter', 'investigationSignalFilter');
    restoreSelectPreference('sort-select', 'investigationSort');
    restoreSelectPreference('leaderboard-sort-select', 'leaderboardSort');
    restoreSelectPreference('leaderboard-scope-select', 'leaderboardScope');
    restoreSelectPreference('leaderboard-family-select', 'leaderboardFamily');

    // Event listeners
    document.getElementById('btn-refresh').addEventListener('click', () => { loadUsageData(true).catch(() => {}); });
    document.getElementById('search-input').addEventListener('input', applyFiltersAndSort);
    document.getElementById('model-filter').addEventListener('change', event => {
        saveUiPreferences({ investigationModelFilter: event.target.value });
        applyFiltersAndSort();
    });
    document.getElementById('session-signal-filter')?.addEventListener('change', event => {
        saveUiPreferences({ investigationSignalFilter: event.target.value });
        applyFiltersAndSort();
    });
    document.getElementById('sort-select').addEventListener('change', event => {
        if (event.target.value !== 'column') clearTableSortState(document.getElementById('conv-table'));
        saveUiPreferences({ investigationSort: event.target.value });
        applyFiltersAndSort();
    });

    ['leaderboard-sort-select', 'leaderboard-scope-select', 'leaderboard-family-select'].forEach(id => {
        document.getElementById(id)?.addEventListener('change', event => {
            if (id === 'leaderboard-sort-select' && event.target.value !== 'column') {
                clearTableSortState(document.getElementById('leaderboard-table'));
            }
            const keyById = {
                'leaderboard-sort-select': 'leaderboardSort',
                'leaderboard-scope-select': 'leaderboardScope',
                'leaderboard-family-select': 'leaderboardFamily',
            };
            saveUiPreferences({ [keyById[id]]: event.target.value });
            renderLeaderboardViews();
        });
    });

    // Modal close listeners
    document.getElementById('modal-overlay').addEventListener('click', (e) => {
        if (e.target === e.currentTarget) closeModal();
    });

    const modalCal = document.getElementById('modal-calibrate');
    if (modalCal) {
        modalCal.addEventListener('click', (e) => {
            if (e.target === e.currentTarget) closeCalibrateModal();
        });
    }

    const modalAdd = document.getElementById('modal-add-account');
    if (modalAdd) {
        modalAdd.addEventListener('click', (e) => {
            if (e.target === e.currentTarget) closeAddAccountModal();
        });
    }

    const btnOpenCal = document.getElementById('btn-open-calibrate');
    if (btnOpenCal) {
        btnOpenCal.addEventListener('click', openCalibrateModal);
    }

    const btnSaveCal = document.getElementById('btn-save-calibrate');
    if (btnSaveCal) {
        btnSaveCal.addEventListener('click', saveCalibrationData);
    }

    const btnOpenAdd = document.getElementById('btn-open-add-account');
    if (btnOpenAdd) {
        btnOpenAdd.addEventListener('click', openAddAccountModal);
    }

    const btnSaveAdd = document.getElementById('btn-save-new-account');
    if (btnSaveAdd) {
        btnSaveAdd.addEventListener('click', saveNewAccount);
    }

    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') {
            closeModal();
            closeCalibrateModal();
            closeAddAccountModal();
            closeCodexModal();
            closeCodexLimitModal();
        }
    });

    // Models Breakdown table sort
    document.querySelectorAll('.models-breakdown-table th.sortable').forEach(th => {
        th.addEventListener('click', () => {
            const key = th.dataset.sort;
            if (modelsSortKey === key) {
                modelsSortAsc = !modelsSortAsc;
            } else {
                modelsSortKey = key;
                modelsSortAsc = false;
            }
            saveUiPreferences({ modelsSortKey, modelsSortAsc });
            if (currentData && currentData.summary && currentData.summary.models_breakdown) {
                renderModelsTable(currentData.summary.models_breakdown);
            }
        });
    });

    // Codex modal close on overlay click
    const modalCodex = document.getElementById('modal-codex');
    if (modalCodex) {
        modalCodex.addEventListener('click', (e) => {
            if (e.target === e.currentTarget) closeCodexModal();
        });
    }
    const modalCodexLimit = document.getElementById('modal-codex-limit');
    if (modalCodexLimit) {
        modalCodexLimit.addEventListener('click', (e) => {
            if (e.target === e.currentTarget) closeCodexLimitModal();
        });
    }

    // Resize handler for Canvas charts
    let resizeTimer;
    window.addEventListener('resize', () => {
        clearTimeout(resizeTimer);
        resizeTimer = setTimeout(() => {
            if (currentData) {
                if (document.getElementById('tab-quota')?.classList.contains('active')) {
                    renderTimeSeriesAnalytics(currentData.summary.time_series);
                    renderCodexWeeklyCapacity(currentData.summary.codex_usage);
                }
                if (enrichedLeaderboardRows.length) {
                    renderLeaderboardCharts(getFilteredLeaderboardRows());
                }
                if (cachedModelsDailyTimeline) {
                    renderModelsDailyChart(cachedModelsDailyTimeline);
                    renderModelsCostChart(cachedModelsDailyTimeline);
                }
                if (cachedCodexQuotaTimeline) {
                    renderCodexQuotaEfficiencyTimeline(cachedCodexQuotaTimeline);
                }
                if (cachedCodexTaskTimeline) {
                    renderCodexQuotaPerTaskTimeline(cachedCodexTaskTimeline);
                }
            }
        }, 200);
    });

    setupExport();
    setupAutoSync();
    initTableDensityControls();
    initTableColumnResizers();
    initTimeSeriesViewControls();
    initCodexWeeklyCapacityControls();
    initModelTimelineRangeControls();
    initModelTimelineGranularityControls();
    initModelCostControls();
    initCodexQuotaTimelineControls();
    initCodexTaskTimelineControls();
    initTaskDurationControls();
    initCodexTaskOutcomeControls();
    loadUsageData(false).catch(() => {});
});

function initCodexWeeklyCapacityControls() {
    const accountSelect = document.getElementById('codex-weekly-capacity-account');
    const windowSelect = document.getElementById('codex-weekly-capacity-window');
    const rangeSelect = document.getElementById('codex-weekly-capacity-range');
    if (windowSelect) windowSelect.value = savedChoice('codexWeeklyCapacityWindow', ['7', '14', '30'], '14');
    if (rangeSelect) rangeSelect.value = savedChoice('codexWeeklyCapacityRange', ['30', '90', '180', '365'], '90');
    const choices = [
        ['codex-weekly-capacity-account', 'codexWeeklyCapacityAccount'],
        ['codex-weekly-capacity-window', 'codexWeeklyCapacityWindow'],
        ['codex-weekly-capacity-range', 'codexWeeklyCapacityRange'],
    ];
    for (const [id, key] of choices) {
        document.getElementById(id)?.addEventListener('change', event => {
            saveUiPreferences({ [key]: event.target.value });
            if (currentData) renderCodexWeeklyCapacity(currentData.summary.codex_usage);
        });
    }
}

function initTimeSeriesViewControls() {
    const btn5h = document.getElementById('btn-ts-view-5h');
    const btnWk = document.getElementById('btn-ts-view-weekly');
    const btnCap = document.getElementById('btn-ts-view-capacity');
    const btnSnaps = document.getElementById('btn-ts-view-snapshots');

    const view5h = document.getElementById('ts-view-5h');
    const viewWk = document.getElementById('ts-view-weekly');
    const viewCap = document.getElementById('ts-view-capacity');
    const viewSnaps = document.getElementById('ts-view-snapshots');

    function switchTsView(activeBtn, activeView) {
        [btn5h, btnWk, btnCap, btnSnaps].forEach(b => b?.classList.remove('active'));
        [view5h, viewWk, viewCap, viewSnaps].forEach(v => {
            if (v) v.style.display = 'none';
        });

        activeBtn?.classList.add('active');
        if (activeView) activeView.style.display = 'block';
        if (activeView?.id) saveUiPreferences({ timeSeriesView: activeView.id });

        // Redraw canvas
        if (currentData && currentData.summary && currentData.summary.time_series) {
            setTimeout(() => {
                renderTimeSeriesAnalytics(currentData.summary.time_series);
            }, 50);
        }
    }

    if (btn5h) btn5h.addEventListener('click', () => switchTsView(btn5h, view5h));
    if (btnWk) btnWk.addEventListener('click', () => switchTsView(btnWk, viewWk));
    if (btnCap) btnCap.addEventListener('click', () => switchTsView(btnCap, viewCap));
    if (btnSnaps) btnSnaps.addEventListener('click', () => switchTsView(btnSnaps, viewSnaps));

    const restoredViewId = savedChoice('timeSeriesView', [
        'ts-view-5h', 'ts-view-weekly', 'ts-view-capacity', 'ts-view-snapshots',
    ], 'ts-view-5h');
    const restoredViews = {
        'ts-view-5h': [btn5h, view5h],
        'ts-view-weekly': [btnWk, viewWk],
        'ts-view-capacity': [btnCap, viewCap],
        'ts-view-snapshots': [btnSnaps, viewSnaps],
    };
    switchTsView(...restoredViews[restoredViewId]);
}

// ==========================================================================
// Table Density & Dimension Controller (Presets, Sliders & Column Resizing)
// ==========================================================================

function applyTableDensity(mode, customValues = null) {
    const body = document.body;
    body.classList.remove('density-compact', 'density-comfortable', 'density-spacious');

    // Update preset button states
    document.querySelectorAll('.btn-density').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.density === mode);
    });

    if (customValues) {
        // Custom Slider Values
        document.documentElement.style.setProperty('--table-pad-y', `${customValues.padY}px`);
        document.documentElement.style.setProperty('--table-pad-x', `${customValues.padX}px`);
        document.documentElement.style.setProperty('--table-font-size', `${customValues.fontSize}px`);
        document.documentElement.style.setProperty('--table-head-font-size', `${Math.max(10, customValues.fontSize - 1.5)}px`);

        // Update slider UI
        const sY = document.getElementById('slider-pad-y');
        const sX = document.getElementById('slider-pad-x');
        const sF = document.getElementById('slider-font-size');
        const vY = document.getElementById('val-pad-y');
        const vX = document.getElementById('val-pad-x');
        const vF = document.getElementById('val-font-size');
        if (sY) sY.value = customValues.padY;
        if (sX) sX.value = customValues.padX;
        if (sF) sF.value = customValues.fontSize;
        if (vY) vY.textContent = `${customValues.padY}px`;
        if (vX) vX.textContent = `${customValues.padX}px`;
        if (vF) vF.textContent = `${customValues.fontSize}px`;

        localStorage.setItem('table_density_mode', 'custom');
        localStorage.setItem('table_density_custom', JSON.stringify(customValues));
    } else {
        // Standard Presets
        document.documentElement.style.removeProperty('--table-pad-y');
        document.documentElement.style.removeProperty('--table-pad-x');
        document.documentElement.style.removeProperty('--table-font-size');
        document.documentElement.style.removeProperty('--table-head-font-size');

        body.classList.add(`density-${mode}`);
        localStorage.setItem('table_density_mode', mode);
        localStorage.removeItem('table_density_custom');

        // Sync slider values to preset
        const presets = {
            compact: { padY: 5, padX: 8, fontSize: 11.5 },
            comfortable: { padY: 9, padX: 12, fontSize: 12.5 },
            spacious: { padY: 14, padX: 16, fontSize: 13.5 }
        };
        const p = presets[mode] || presets.comfortable;
        const sY = document.getElementById('slider-pad-y');
        const sX = document.getElementById('slider-pad-x');
        const sF = document.getElementById('slider-font-size');
        const vY = document.getElementById('val-pad-y');
        const vX = document.getElementById('val-pad-x');
        const vF = document.getElementById('val-font-size');
        if (sY) sY.value = p.padY;
        if (sX) sX.value = p.padX;
        if (sF) sF.value = p.fontSize;
        if (vY) vY.textContent = `${p.padY}px`;
        if (vX) vX.textContent = `${p.padX}px`;
        if (vF) vF.textContent = `${p.fontSize}px`;
    }
}

function initTableDensityControls() {
    // 1. Preset buttons
    document.querySelectorAll('.btn-density').forEach(btn => {
        btn.addEventListener('click', () => {
            const mode = btn.dataset.density;
            applyTableDensity(mode);
        });
    });

    // 2. Toggle Popover
    const btnToggle = document.getElementById('btn-toggle-density-slider');
    const popover = document.getElementById('density-popover');
    const btnClose = document.getElementById('btn-close-density-popover');
    const btnApply = document.getElementById('btn-apply-density');
    const btnReset = document.getElementById('btn-reset-density');

    if (btnToggle && popover) {
        btnToggle.addEventListener('click', (e) => {
            e.stopPropagation();
            popover.classList.toggle('show');
        });
    }

    if (btnClose && popover) {
        btnClose.addEventListener('click', () => popover.classList.remove('show'));
    }

    if (btnApply && popover) {
        btnApply.addEventListener('click', () => popover.classList.remove('show'));
    }

    // Close popover when clicking outside
    document.addEventListener('click', (e) => {
        if (popover && popover.classList.contains('show') && !popover.contains(e.target) && e.target !== btnToggle) {
            popover.classList.remove('show');
        }
    });

    // 3. Sliders live adjustment
    const sY = document.getElementById('slider-pad-y');
    const sX = document.getElementById('slider-pad-x');
    const sF = document.getElementById('slider-font-size');
    const vY = document.getElementById('val-pad-y');
    const vX = document.getElementById('val-pad-x');
    const vF = document.getElementById('val-font-size');

    const updateFromSliders = () => {
        const padY = parseFloat(sY?.value || 9);
        const padX = parseFloat(sX?.value || 12);
        const fontSize = parseFloat(sF?.value || 12.5);
        if (vY) vY.textContent = `${padY}px`;
        if (vX) vX.textContent = `${padX}px`;
        if (vF) vF.textContent = `${fontSize}px`;

        applyTableDensity('custom', { padY, padX, fontSize });
    };

    if (sY) sY.addEventListener('input', updateFromSliders);
    if (sX) sX.addEventListener('input', updateFromSliders);
    if (sF) sF.addEventListener('input', updateFromSliders);

    // 4. Reset Button
    if (btnReset) {
        btnReset.addEventListener('click', () => {
            applyTableDensity('comfortable');
        });
    }

    // 5. Restore saved density from localStorage
    const savedMode = localStorage.getItem('table_density_mode') || 'comfortable';
    if (savedMode === 'custom') {
        try {
            const custom = JSON.parse(localStorage.getItem('table_density_custom'));
            if (custom) applyTableDensity('custom', custom);
            else applyTableDensity('comfortable');
        } catch {
            applyTableDensity('comfortable');
        }
    } else {
        applyTableDensity(savedMode);
    }
}

// 6. Shared table sizing and column resizing. Both settings persist per table.
const TABLE_COLUMN_WIDTHS_STORAGE_PREFIX = 'usage-tracker:column-widths:v1:';
const TABLE_VIEWPORT_SIZE_STORAGE_PREFIX = 'usage-tracker:table-size:v1:';
const TABLE_SORT_STORAGE_PREFIX = 'usage-tracker:table-sort:v1:';
const tableSortStates = new WeakMap();

function getTableStableId(table, tableIndex) {
    return table.id || table.querySelector('tbody[id]')?.id || `table-${tableIndex}`;
}

function getTableColumnWidthsStorageKey(table, tableIndex) {
    return `${TABLE_COLUMN_WIDTHS_STORAGE_PREFIX}${getTableStableId(table, tableIndex)}`;
}

function getTableViewportSizeStorageKey(table, tableIndex) {
    return `${TABLE_VIEWPORT_SIZE_STORAGE_PREFIX}${getTableStableId(table, tableIndex)}`;
}

function readSavedTableColumnWidths(storageKey) {
    try {
        const parsed = JSON.parse(localStorage.getItem(storageKey) || '{}');
        return parsed && typeof parsed === 'object' && parsed.widths && typeof parsed.widths === 'object'
            ? parsed.widths
            : {};
    } catch (e) {
        return {};
    }
}

function getTableColumnStableKey(th, index) {
    const explicit = th.dataset.columnKey || th.dataset.sort;
    if (explicit) return `key:${explicit}`;
    const label = String(th.textContent || '').replace(/\s+/g, ' ').trim().toLowerCase();
    return label ? `label:${label}` : `index:${index}`;
}

function saveTableColumnWidths(storageKey, headers) {
    try {
        const widths = { _schema: 2 };
        headers.forEach((th, index) => {
            if (th.classList.contains('action-col')) return;
            const width = Math.round(th.getBoundingClientRect().width);
            if (Number.isFinite(width) && width >= 50) widths[getTableColumnStableKey(th, index)] = width;
        });
        localStorage.setItem(storageKey, JSON.stringify({ widths }));
    } catch (e) {
        // localStorage can be unavailable in privacy-restricted browser contexts.
    }
}

function readSavedTableViewportSize(storageKey) {
    try {
        const parsed = JSON.parse(localStorage.getItem(storageKey) || '{}');
        return parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? parsed : {};
    } catch (e) {
        return {};
    }
}

function saveTableViewportSize(storageKey, state) {
    try {
        const payload = {};
        const widthPercent = Number(state?.widthPercent);
        const heightPx = Number(state?.heightPx);
        if (Number.isFinite(widthPercent)) payload.widthPercent = Math.min(100, Math.max(45, widthPercent));
        if (Number.isFinite(heightPx)) payload.heightPx = Math.min(900, Math.max(180, heightPx));
        if (Object.keys(payload).length) localStorage.setItem(storageKey, JSON.stringify(payload));
        else localStorage.removeItem(storageKey);
    } catch (e) {
        // The controls remain usable for the current page when storage is unavailable.
    }
}

function applyTableViewportSize(wrapper, state) {
    const widthPercent = Number(state?.widthPercent);
    const heightPx = Number(state?.heightPx);
    if (Number.isFinite(widthPercent)) {
        wrapper.style.width = `${Math.min(100, Math.max(45, widthPercent))}%`;
        wrapper.style.maxWidth = '100%';
        wrapper.style.marginRight = 'auto';
    }
    if (Number.isFinite(heightPx)) {
        wrapper.style.height = `${Math.min(900, Math.max(180, heightPx))}px`;
        wrapper.style.maxHeight = 'none';
        wrapper.style.overflow = 'auto';
    }
}

function restoreOriginalTableViewportSize(wrapper) {
    wrapper.style.width = wrapper.dataset.originalTableWidth || '';
    wrapper.style.height = wrapper.dataset.originalTableHeight || '';
    wrapper.style.maxWidth = wrapper.dataset.originalTableMaxWidth || '';
    wrapper.style.maxHeight = wrapper.dataset.originalTableMaxHeight || '';
    wrapper.style.marginRight = wrapper.dataset.originalTableMarginRight || '';
    wrapper.style.overflow = wrapper.dataset.originalTableOverflow || '';
}

function resetTableColumnWidths(table, tableIndex) {
    try {
        localStorage.removeItem(getTableColumnWidthsStorageKey(table, tableIndex));
    } catch (e) {
        // Reset the visible table even when persistent storage is unavailable.
    }
    table.querySelectorAll('thead th').forEach(th => {
        th.style.width = th.dataset.originalTableColumnWidth || '';
        th.style.minWidth = th.dataset.originalTableColumnMinWidth || '';
    });
}

function initTableSizeControls(tables = Array.from(document.querySelectorAll('table'))) {
    tables.forEach((table, tableIndex) => {
        const wrapper = table.closest('.table-container, .table-responsive');
        if (!wrapper) return;

        const stableId = getTableStableId(table, tableIndex);
        const storageKey = getTableViewportSizeStorageKey(table, tableIndex);
        if (wrapper.dataset.tableSizeBound !== '1') {
            wrapper.dataset.tableSizeBound = '1';
            wrapper.dataset.originalTableWidth = wrapper.style.width || '';
            wrapper.dataset.originalTableHeight = wrapper.style.height || '';
            wrapper.dataset.originalTableMaxWidth = wrapper.style.maxWidth || '';
            wrapper.dataset.originalTableMaxHeight = wrapper.style.maxHeight || '';
            wrapper.dataset.originalTableMarginRight = wrapper.style.marginRight || '';
            wrapper.dataset.originalTableOverflow = wrapper.style.overflow || '';
            wrapper.classList.add('table-resizable-viewport');
        }

        const savedSize = readSavedTableViewportSize(storageKey);
        applyTableViewportSize(wrapper, savedSize);

        let controls = wrapper.previousElementSibling;
        if (!controls || controls.dataset.tableSizeControlsFor !== stableId) {
            const english = window.UsageI18n?.language === 'en';
            controls = document.createElement('details');
            controls.className = 'table-size-controls';
            controls.dataset.tableSizeControlsFor = stableId;
            controls.innerHTML = `
                <summary title="${english ? 'Adjust this table' : 'Điều chỉnh riêng cho bảng này'}">${english ? 'Table size' : 'Kích thước bảng'}</summary>
                <div class="table-size-control-panel">
                    <label><span data-table-size-label="width">${english ? 'Width' : 'Rộng'}</span> <input type="range" data-table-size="width" min="45" max="100" step="1"><output data-table-size-output="width">${english ? 'Automatic' : 'Tự động'}</output></label>
                    <label><span data-table-size-label="height">${english ? 'Height' : 'Cao'}</span> <input type="range" data-table-size="height" min="180" max="900" step="10"><output data-table-size-output="height">${english ? 'Automatic' : 'Tự động'}</output></label>
                    <button type="button" data-table-size-action="reset-size">${english ? 'Reset size' : 'Đặt lại kích thước'}</button>
                    <button type="button" data-table-size-action="reset-columns">${english ? 'Reset column widths' : 'Đặt lại độ rộng cột'}</button>
                </div>`;
            wrapper.parentNode.insertBefore(controls, wrapper);
        }

        const english = window.UsageI18n?.language === 'en';
        const sizeSummary = controls.querySelector('summary');
        if (sizeSummary) {
            sizeSummary.textContent = english ? 'Table size' : 'Kích thước bảng';
            sizeSummary.title = english ? 'Adjust this table' : 'Điều chỉnh riêng cho bảng này';
        }
        const widthLabel = controls.querySelector('[data-table-size-label="width"]');
        const heightLabel = controls.querySelector('[data-table-size-label="height"]');
        if (widthLabel) widthLabel.textContent = english ? 'Width' : 'Rộng';
        if (heightLabel) heightLabel.textContent = english ? 'Height' : 'Cao';
        const resetSize = controls.querySelector('[data-table-size-action="reset-size"]');
        const resetColumns = controls.querySelector('[data-table-size-action="reset-columns"]');
        if (resetSize) resetSize.textContent = english ? 'Reset size' : 'Đặt lại kích thước';
        if (resetColumns) resetColumns.textContent = english ? 'Reset column widths' : 'Đặt lại độ rộng cột';

        const widthInput = controls.querySelector('[data-table-size="width"]');
        const heightInput = controls.querySelector('[data-table-size="height"]');
        const widthOutput = controls.querySelector('[data-table-size-output="width"]');
        const heightOutput = controls.querySelector('[data-table-size-output="height"]');
        const currentWidth = Number(savedSize.widthPercent);
        const currentHeight = Number(savedSize.heightPx);
        const measuredHeight = Math.round(wrapper.getBoundingClientRect().height);
        widthInput.value = Number.isFinite(currentWidth) ? String(currentWidth) : '100';
        heightInput.value = Number.isFinite(currentHeight)
            ? String(currentHeight)
            : String(Math.min(900, Math.max(180, measuredHeight || 360)));
        widthOutput.textContent = Number.isFinite(currentWidth) ? `${currentWidth}%` : 'Tự động';
        heightOutput.textContent = Number.isFinite(currentHeight) ? `${currentHeight}px` : 'Tự động';

        if (controls.dataset.tableSizeEventsBound === '1') return;
        controls.dataset.tableSizeEventsBound = '1';
        const state = { ...savedSize };

        widthInput.addEventListener('input', () => {
            state.widthPercent = Number(widthInput.value);
            widthOutput.textContent = `${state.widthPercent}%`;
            applyTableViewportSize(wrapper, state);
            saveTableViewportSize(storageKey, state);
        });
        heightInput.addEventListener('input', () => {
            state.heightPx = Number(heightInput.value);
            heightOutput.textContent = `${state.heightPx}px`;
            applyTableViewportSize(wrapper, state);
            saveTableViewportSize(storageKey, state);
        });
        controls.querySelector('[data-table-size-action="reset-size"]')?.addEventListener('click', () => {
            delete state.widthPercent;
            delete state.heightPx;
            saveTableViewportSize(storageKey, state);
            restoreOriginalTableViewportSize(wrapper);
            widthInput.value = '100';
            heightInput.value = String(Math.min(900, Math.max(180, Math.round(wrapper.getBoundingClientRect().height) || 360)));
            widthOutput.textContent = 'Tự động';
            heightOutput.textContent = 'Tự động';
        });
        controls.querySelector('[data-table-size-action="reset-columns"]')?.addEventListener('click', () => {
            resetTableColumnWidths(table, tableIndex);
        });
        controls.addEventListener('toggle', () => {
            if (!controls.open) return;
            if (!Number.isFinite(Number(state.heightPx))) {
                heightInput.value = String(Math.min(900, Math.max(180, Math.round(wrapper.getBoundingClientRect().height) || 360)));
            }
        });
    });
}

function tableSortColumnKey(th, index) {
    return th.dataset.columnKey ? `key:${th.dataset.columnKey}` : `index:${index}`;
}

function tableSortStorageKey(table, tableIndex) {
    return `${TABLE_SORT_STORAGE_PREFIX}${getTableStableId(table, tableIndex)}`;
}

function getTableSortState(table, tableIndex) {
    if (tableSortStates.has(table)) return tableSortStates.get(table);
    let state = null;
    try {
        const saved = JSON.parse(localStorage.getItem(tableSortStorageKey(table, tableIndex)) || 'null');
        if (saved && typeof saved.key === 'string' && ['asc', 'desc'].includes(saved.direction)) {
            state = saved;
        }
    } catch (_) {}
    tableSortStates.set(table, state);
    return state;
}

function syncTableSortHeaders(table, state) {
    const headers = Array.from(table.tHead?.querySelectorAll('th') || []);
    headers.forEach((th, index) => {
        const active = state?.key === tableSortColumnKey(th, index);
        th.classList.toggle('sort-asc', active && state.direction === 'asc');
        th.classList.toggle('sort-desc', active && state.direction === 'desc');
        if (th.classList.contains('table-sortable')) {
            th.setAttribute('aria-sort', active
                ? (state.direction === 'asc' ? 'ascending' : 'descending') : 'none');
        }
    });
}

function applyTableSort(table, tableIndex) {
    if (table.classList.contains('models-breakdown-table')) return;
    const state = getTableSortState(table, tableIndex);
    syncTableSortHeaders(table, state);
    if (!state || !table.tBodies.length) return;
    const headers = Array.from(table.tHead?.querySelectorAll('th') || []);
    const columnIndex = headers.findIndex((th, index) => tableSortColumnKey(th, index) === state.key);
    if (columnIndex < 0) return;
    const th = headers[columnIndex];
    const type = window.UsageTableSort.inferType(th.textContent, th.classList.contains('num'), th.dataset.sortType);
    const tbody = table.tBodies[0];
    const rows = Array.from(tbody.rows);
    if (rows.length < 2) return;
    const entries = rows.map((row, index) => {
        const cell = row.cells[columnIndex];
        const raw = !cell || (row.cells.length === 1 && cell.colSpan > 1)
            ? '' : (cell.hasAttribute('data-sort-value') ? cell.dataset.sortValue : cell.textContent);
        return { row, index, parsed: window.UsageTableSort.value(raw, type) };
    });
    entries.sort((a, b) => window.UsageTableSort.compare(a.parsed, b.parsed, state.direction)
        || a.index - b.index);
    if (entries.every((entry, index) => entry.row === rows[index])) return;
    const fragment = document.createDocumentFragment();
    entries.forEach(entry => fragment.appendChild(entry.row));
    tbody.appendChild(fragment);
}

function clearTableSortState(table) {
    if (!table) return;
    const tableIndex = Array.from(document.querySelectorAll('table')).indexOf(table);
    if (tableIndex < 0) return;
    tableSortStates.set(table, null);
    try { localStorage.removeItem(tableSortStorageKey(table, tableIndex)); } catch (_) {}
    syncTableSortHeaders(table, null);
}

function initTableSorting(tables = Array.from(document.querySelectorAll('table'))) {
    if (!window.UsageTableSort) return;
    tables.forEach((table, tableIndex) => {
        const headers = Array.from(table.tHead?.querySelectorAll('th') || []);
        if (!headers.length || !table.tBodies.length) return;
        if (table.classList.contains('models-breakdown-table')) {
            headers.filter(th => th.classList.contains('sortable')).forEach(th => {
                th.classList.add('table-sortable');
                th.tabIndex = 0;
                if (th.dataset.tableSortKeyboardBound === '1') return;
                th.dataset.tableSortKeyboardBound = '1';
                th.addEventListener('keydown', event => {
                    if (event.key === 'Enter' || event.key === ' ') {
                        event.preventDefault();
                        th.click();
                    }
                });
            });
            return;
        }
        const state = getTableSortState(table, tableIndex);
        headers.forEach((th, index) => {
            const label = String(th.textContent || '').replace(/\s+/g, ' ').trim();
            if (th.classList.contains('action-col') || th.colSpan > 1
                || /^(?:thao tác|actions|ranh giới|boundary)$/i.test(label)) return;
            th.classList.add('table-sortable');
            th.tabIndex = 0;
            if (th.dataset.tableSortBound === '1') return;
            th.dataset.tableSortBound = '1';
            const toggle = () => {
                if (document.body.classList.contains('is-resizing-table-column')) return;
                const key = tableSortColumnKey(th, index);
                const previous = getTableSortState(table, tableIndex);
                const direction = previous?.key === key && previous.direction === 'asc' ? 'desc' : 'asc';
                const next = { key, direction };
                tableSortStates.set(table, next);
                try { localStorage.setItem(tableSortStorageKey(table, tableIndex), JSON.stringify(next)); } catch (_) {}
                if (table.id === 'conv-table' || table.id === 'leaderboard-table') {
                    const selectId = table.id === 'conv-table' ? 'sort-select' : 'leaderboard-sort-select';
                    const prefKey = table.id === 'conv-table' ? 'investigationSort' : 'leaderboardSort';
                    const select = document.getElementById(selectId);
                    if (select) select.value = 'column';
                    saveUiPreferences({ [prefKey]: 'column' });
                }
                applyTableSort(table, tableIndex);
            };
            th.addEventListener('click', event => {
                if (event.target.closest('.table-col-resizer, button, select, input')) return;
                toggle();
            });
            th.addEventListener('keydown', event => {
                if (event.key === 'Enter' || event.key === ' ') {
                    event.preventDefault();
                    toggle();
                }
            });
        });
        syncTableSortHeaders(table, state);
        const tbody = table.tBodies[0];
        if (tbody.dataset.tableSortObserved !== '1') {
            tbody.dataset.tableSortObserved = '1';
            new MutationObserver(() => applyTableSort(table, tableIndex))
                .observe(tbody, { childList: true });
        }
        applyTableSort(table, tableIndex);
    });
}

function initTableColumnResizers() {
    const tables = Array.from(document.querySelectorAll('table'));
    tables.forEach((table, tableIndex) => {
        table.classList.add('table-column-resizable');
        const headers = Array.from(table.querySelectorAll('thead th'));
        const storageKey = getTableColumnWidthsStorageKey(table, tableIndex);
        const savedWidths = readSavedTableColumnWidths(storageKey);

        headers.forEach((th, columnIndex) => {
            if (th.classList.contains('action-col')) return;

            if (!Object.hasOwn(th.dataset, 'originalTableColumnWidth')) {
                th.dataset.originalTableColumnWidth = th.style.width || '';
                th.dataset.originalTableColumnMinWidth = th.style.minWidth || '';
            }
            if (getComputedStyle(th).position === 'static') th.classList.add('has-table-col-resizer');

            const stableKey = getTableColumnStableKey(th, columnIndex);
            const legacyIndex = table.classList.contains('models-breakdown-table') && columnIndex >= 10
                ? columnIndex + 1
                : columnIndex;
            const savedWidth = Number(savedWidths[stableKey] ?? savedWidths[legacyIndex]);
            if (Number.isFinite(savedWidth) && savedWidth >= 50) {
                th.style.width = `${savedWidth}px`;
                th.style.minWidth = `${savedWidth}px`;
            }

            let resizer = th.querySelector('.table-col-resizer');
            if (!resizer) {
                resizer = document.createElement('div');
                resizer.className = 'table-col-resizer';
                th.appendChild(resizer);
            }
            if (resizer.dataset.widthPersistenceBound === '1') return;
            resizer.dataset.widthPersistenceBound = '1';

            let startX, startWidth;
            resizer.addEventListener('pointerdown', (e) => {
                e.preventDefault();
                e.stopPropagation();
                startX = e.clientX;
                startWidth = th.offsetWidth;
                resizer.classList.add('resizing');
                document.body.classList.add('is-resizing-table-column');

                const onPointerMove = (moveEvent) => {
                    const diff = moveEvent.clientX - startX;
                    const newWidth = Math.max(50, startWidth + diff);
                    th.style.width = `${newWidth}px`;
                    th.style.minWidth = `${newWidth}px`;
                };

                const onPointerUp = () => {
                    resizer.classList.remove('resizing');
                    document.body.classList.remove('is-resizing-table-column');
                    saveTableColumnWidths(storageKey, headers);
                    document.removeEventListener('pointermove', onPointerMove);
                    document.removeEventListener('pointerup', onPointerUp);
                    document.removeEventListener('pointercancel', onPointerUp);
                };

                document.addEventListener('pointermove', onPointerMove);
                document.addEventListener('pointerup', onPointerUp);
                document.addEventListener('pointercancel', onPointerUp);
            });
        });
    });
    initTableSizeControls(tables);
    initTableSorting(tables);
}
