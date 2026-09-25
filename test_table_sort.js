const test = require('node:test');
const assert = require('node:assert/strict');
const sort = require('./table_sort.js');

test('parses displayed numeric units and keeps missing values out of rankings', () => {
    assert.equal(sort.number('2.15M tokens'), 2150000);
    assert.equal(sort.number('$1,234.56'), 1234.56);
    assert.equal(sort.number('1.234,56%'), 1234.56);
    assert.equal(sort.number('0.89× Sol High'), 0.89);
    assert.equal(sort.number('1,234 tokens'), 1234);
    assert.equal(sort.number('Chưa có dữ liệu n=0'), null);
    assert.equal(sort.compare(sort.value('—', 'number'), sort.value('5.2K', 'number'), 'desc'), 1);
});

test('sorts local dates and measured durations by their actual values', () => {
    assert.ok(sort.date('25/09/2026 10:15:00') > sort.date('24/09/2026 23:59:59'));
    assert.equal(sort.date('10:15:00 (25/09/2026)'), sort.date('25/09/2026 10:15:00'));
    assert.equal(sort.duration('1h 35p'), 95);
    assert.equal(sort.duration('2.5 h'), 150);
    assert.equal(sort.duration('< 1p'), 0.5);
    assert.equal(sort.inferType('Bắt đầu'), 'date');
    assert.equal(sort.inferType('Start Time'), 'date');
    assert.equal(sort.inferType('Detected At'), 'date');
    assert.equal(sort.inferType('Thời Lượng'), 'duration');
});

test('uses confidence order and natural text order', () => {
    assert.equal(sort.confidence('Cao'), 3);
    assert.equal(sort.confidence('Vừa'), 2);
    assert.equal(sort.confidence('Low'), 1);
    assert.equal(sort.compare(sort.value('Model 2', 'text'), sort.value('Model 10', 'text')), -1);
    assert.equal(sort.inferType('Độ Tin Cậy'), 'confidence');
    assert.equal(sort.inferType('Tin cậy'), 'confidence');
});
