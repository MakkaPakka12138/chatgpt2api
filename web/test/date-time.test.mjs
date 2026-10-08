import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import ts from 'typescript';

const source = readFileSync(new URL('../src/lib/date-time.ts', import.meta.url), 'utf8');
const js = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 } }).outputText;
const moduleUrl = `data:text/javascript;base64,${Buffer.from(js).toString('base64')}`;
const run = (zone, checks) => execFileSync(process.execPath, ['--input-type=module', '-e', `import assert from 'node:assert/strict'; import * as time from '${moduleUrl}'; ${checks}`], { env: { ...process.env, TZ: zone } });

test('legacy UTC, explicit offsets and epoch values display in browser timezone', () => {
  for (const [zone, expected] of [['UTC','2026-10-01 05:10:58'], ['Asia/Tokyo','2026-10-01 14:10:58'], ['America/Los_Angeles','2026-09-30 22:10:58']]) {
    run(zone, `
      for (const value of ['2026-10-01 05:10:58', '2026-10-01T05:10:58', '2026-10-01T05:10:58Z', '2026-10-01T14:10:58+09:00', Date.parse('2026-10-01T05:10:58Z')]) assert.equal(time.formatBrowserDateTime(value), '${expected}');
      assert.equal(time.formatBrowserDateTime(null), '—');
      assert.equal(time.formatBrowserDateTime('invalid'), 'invalid');
    `);
  }
});
test('local date bounds preserve midnight and exclusive next-day boundary', () => {
  run('Asia/Tokyo', `assert.deepEqual(time.browserDateBounds('2026-10-01','2026-10-01'), {start_at:'2026-09-30T15:00:00.000Z',end_before:'2026-10-01T15:00:00.000Z'}); assert.deepEqual(time.browserDateBounds(), {}); assert.throws(() => time.browserDateBounds('2026-02-30'));`);
});
test('DST days have correct 23-hour and 25-hour ranges', () => {
  run('America/Los_Angeles', `
    for (const [day, hours] of [['2026-03-08',23],['2026-11-01',25]]) {
      const bounds=time.browserDateBounds(day,day);
      assert.equal((Date.parse(bounds.end_before)-Date.parse(bounds.start_at))/3600000,hours);
    }
  `);
});
test('nested diagnostic timestamps convert without changing request text or input', () => {
  run('Asia/Tokyo', `
    const input={started_at:'2026-10-01 05:10:58',request_text:'2026-10-01 05:10:58',errors:[{last_refresh_error_at:'2026-10-01T05:10:58Z'}]};
    const result=time.browserTimeDetails(input);
    assert.equal(result.started_at,'2026-10-01 14:10:58');
    assert.equal(result.errors[0].last_refresh_error_at,'2026-10-01 14:10:58');
    assert.equal(result.request_text,input.request_text);
    assert.equal(input.started_at,'2026-10-01 05:10:58');
  `);
});
