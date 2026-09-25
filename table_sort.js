/* Value parsing shared by the dashboard's column sorting and its Node tests. */
(function (root, factory) {
    const api = factory();
    if (typeof module === 'object' && module.exports) module.exports = api;
    if (root) root.UsageTableSort = api;
})(typeof globalThis === 'object' ? globalThis : null, function () {
    const collator = new Intl.Collator('vi', { numeric: true, sensitivity: 'base' });
    const missingPattern = /^(?:[—–-]|n\/?a\b|no data\b|not measured\b|unavailable\b|unknown\b|chưa\b|không có\b)/i;

    function isMissing(raw) {
        const text = String(raw ?? '').replace(/\s+/g, ' ').trim();
        return !text || missingPattern.test(text);
    }

    function number(raw) {
        const text = String(raw ?? '').replace(/\u00a0/g, ' ').trim();
        if (isMissing(text)) return null;
        const match = text.match(/[+-]?\d[\d.,]*\s*([KMB])?/i);
        if (!match) return null;
        const suffix = (match[1] || '').toUpperCase();
        let token = match[0].replace(/\s*[KMB]$/i, '').trim();
        const comma = token.lastIndexOf(',');
        const dot = token.lastIndexOf('.');
        if (comma >= 0 && dot >= 0) {
            const decimal = comma > dot ? ',' : '.';
            const thousands = decimal === ',' ? '.' : ',';
            token = token.replaceAll(thousands, '').replace(decimal, '.');
        } else if (comma >= 0 || dot >= 0) {
            const separator = comma >= 0 ? ',' : '.';
            const pieces = token.split(separator);
            const last = pieces[pieces.length - 1];
            const decimalHint = suffix || /[$€£%×]/.test(text) || pieces[0].replace(/^[+-]/, '') === '0';
            const thousands = !decimalHint && last.length === 3;
            token = pieces.length > 2 || thousands
                ? pieces.join('')
                : token.replace(separator, '.');
        }
        const parsed = Number(token);
        if (!Number.isFinite(parsed)) return null;
        return parsed * ({ K: 1e3, M: 1e6, B: 1e9 }[suffix] || 1);
    }

    function date(raw) {
        const text = String(raw ?? '').trim();
        if (isMissing(text)) return null;
        const iso = text.match(/\b(\d{4})-(\d{1,2})-(\d{1,2})(?:[T\s](\d{1,2}):(\d{2})(?::(\d{2}))?)?/);
        if (iso) {
            const value = Date.UTC(+iso[1], +iso[2] - 1, +iso[3], +(iso[4] || 0), +(iso[5] || 0), +(iso[6] || 0));
            return Number.isFinite(value) ? value : null;
        }
        const local = text.match(/\b(\d{1,2})\/(\d{1,2})\/(\d{4})\b/);
        if (!local) return null;
        const before = text.slice(0, local.index);
        const after = text.slice(local.index + local[0].length);
        const clock = after.match(/\b(\d{1,2}):(\d{2})(?::(\d{2}))?\b/)
            || before.match(/\b(\d{1,2}):(\d{2})(?::(\d{2}))?\b/);
        const value = Date.UTC(+local[3], +local[2] - 1, +local[1],
            +(clock?.[1] || 0), +(clock?.[2] || 0), +(clock?.[3] || 0));
        return Number.isFinite(value) ? value : null;
    }

    function duration(raw) {
        const text = String(raw ?? '').trim();
        if (isMissing(text)) return null;
        if (/^<\s*1\s*(?:p|min)/i.test(text)) return 0.5;
        const hours = text.match(/(\d+(?:[.,]\d+)?)\s*h\b/i);
        const minutes = text.match(/(\d+(?:[.,]\d+)?)\s*(?:p\b|min\b|phút\b)/i);
        if (hours || minutes) return (hours ? (number(hours[1]) || 0) * 60 : 0)
            + (minutes ? (number(minutes[1]) || 0) : 0);
        return number(text);
    }

    function confidence(raw) {
        const text = String(raw ?? '').toLocaleLowerCase('vi');
        if (isMissing(text)) return null;
        if (/\bhigh\b|cao/.test(text)) return 3;
        if (/\bmedium\b|vừa/.test(text)) return 2;
        if (/\blow\b|thấp/.test(text)) return 1;
        return null;
    }

    function inferType(label, numeric = false, explicit = '') {
        if (explicit) return explicit;
        const text = String(label || '').toLocaleLowerCase('vi');
        if (/thời lượng|duration/.test(text)) return 'duration';
        if (/^(?:độ tin cậy|tin cậy|confidence)$/.test(text.trim())) return 'confidence';
        if (/mốc (?:thời gian|phát hiện)|bắt đầu|start time|started|detected at|time point|latest observation|ghi nhận gần nhất|lần hoạt động gần nhất|last active|^date$|timestamp/.test(text)) return 'date';
        if (numeric || /token|%|quota|hạn mức|dung lượng|capacity|cost|phí|mẫu|samples|sessions|phiên|responses|phản hồi|tool calls|thành công|tốc độ|speed|coverage|rank|hạng aa/.test(text)) return 'number';
        return 'text';
    }

    function value(raw, type) {
        if (isMissing(raw)) return { missing: true, value: null };
        let parsed;
        if (type === 'number') parsed = number(raw);
        else if (type === 'date') parsed = date(raw);
        else if (type === 'duration') parsed = duration(raw);
        else if (type === 'confidence') parsed = confidence(raw);
        else parsed = String(raw).replace(/\s+/g, ' ').trim();
        return parsed === null || parsed === ''
            ? { missing: true, value: null }
            : { missing: false, value: parsed };
    }

    function compare(a, b, direction = 'asc') {
        if (a.missing !== b.missing) return a.missing ? 1 : -1;
        if (a.missing) return 0;
        const raw = typeof a.value === 'number' && typeof b.value === 'number'
            ? a.value - b.value : collator.compare(String(a.value), String(b.value));
        return direction === 'desc' ? -raw : raw;
    }

    return { number, date, duration, confidence, inferType, value, compare };
});
