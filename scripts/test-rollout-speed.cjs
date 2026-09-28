const assert = require('node:assert/strict');
const test = require('node:test');
const speed = require('../src/lerobot_monitor/web/static/rollout-speed.js');

test('low, high and custom execution speed keep base FPS unchanged', () => {
  for (const [multiplier, effective] of [[.25,3.75],[1,15],[2,30],[8,120],[1.234,18.51]]) {
    const result = speed.inspect(15,multiplier);
    assert.equal(result.error,'');
    assert.equal(result.baseHz,15);
    assert.ok(Math.abs(result.effectiveHz-effective)<1e-9);
  }
});
test('finite positive multipliers and base FPS are required', () => {
  for (const invalid of ['',0,-1,NaN,Infinity,-Infinity,'hello',null,true]) {
    assert.notEqual(speed.inspect(15,invalid).error,'',String(invalid));
    assert.notEqual(speed.inspect(invalid,1).error,'',String(invalid));
  }
  assert.notEqual(speed.inspect(300,.5).error,'');
});
test('effective frequency endpoints apply without an arbitrary multiplier ceiling', () => {
  assert.equal(speed.inspect(10,.01).error,'');
  assert.equal(speed.inspect(1,240).error,'');
  assert.notEqual(speed.inspect(.5,480).error,'');
  assert.notEqual(speed.inspect(15,.0001).error,'');
  assert.notEqual(speed.inspect(30,8.1).error,'');
});
test('Arm source multiplier uses base policy FPS independent of selected speed', () => {
  const rates={default_hz:30,modes:{rollout:{kind:'multiplier',value:2}}};
  assert.equal(speed.armRate(rates,30),60);
  assert.equal(speed.startError(30,.5,rates),'');
  assert.equal(speed.inspect(30,.5).effectiveHz,15);
  assert.notEqual(speed.startError(30,2,{modes:{rollout:{kind:'multiplier',value:1}}}),'');
});
test('insufficient or invalid Arm settings produce actionable startup errors without clamping', () => {
  assert.match(speed.startError(15,4,{default_hz:30}),/Raise.*Arm rate/);
  assert.equal(speed.startError(15,4,{modes:{rollout:{kind:'hz',value:60}}}),'');
  assert.equal(speed.armRate({modes:{rollout:{kind:'hz',value:400}}},15),400);
  assert.match(speed.startError(15,1,{modes:{rollout:{kind:'hz',value:400}}}),/between 1 and 240/);
});
