import assert from 'node:assert/strict';
import test from 'node:test';
import { assignRows, buildRolloutLanes, chunkSpans, overlapSpans, ribbonSegments, hitTestRibbon, ribbonAnalysis, snapshotDisplay } from '../src/lerobot_monitor/web/static/rollout-lanes.js';
const block = (id, values = {}) => ({ id, kind:'rtc', start:1, end:1.4, accepted_at:1.5, active:1.6,
  steps:10, accepted_steps:8, original_steps:10, prefix_trimmed:2, step_s:.1, status:'active', ...values });
const build = (blocks, now = 102, options = {}) => buildRolloutLanes({ timeline:{ run_id:'run', epoch_ts:100,t_s:2,blocks }, chartNow:now, windowS:20, lookaheadS:2,...options });
test('all chunks occupy one row even at arbitrarily dense overlap',()=>{
  assert.deepEqual(assignRows(Array.from({length:20},(_,id)=>({id,start:id/100,end:3}))).map(s=>s.row),new Array(20).fill(0));
  assert.ok(Math.abs(chunkSpans([block(1),block(2)])[0].end-2.4)<1e-9);
});
test('accepted zero, failures and unstarted chunks never fabricate an action span',()=>{
  assert.equal(chunkSpans([block(1,{accepted_steps:0}),block(2,{active:null}),block(3,{failed:true})],{prediction:{actions:[{},{}],step_s:.1}}).length,0);
});
test('snapshot delivery time cannot change event coordinates',()=>{
  const first=build([block(1)],102), second=build([block(1)],102.65);
  assert.equal(first.offset,100);assert.equal(second.offset,100);
  assert.equal(first.chunks[0].start,second.chunks[0].start);
  assert.equal(first.inferences[0].start,second.inferences[0].start);
  assert.equal(buildRolloutLanes({timeline:{blocks:[block(1)],t_s:2},chartNow:102}),null);
  assert.equal(buildRolloutLanes({timeline:{blocks:[block(1)],t_s:2},epoch:100,chartNow:102}).offset,100);
});
test('window clipping preserves full duration and slope',()=>{
  const full=build([block(1)]), cropped=build([block(1)],102,{windowS:.3});
  assert.equal(cropped.chunks[0].end-cropped.chunks[0].start,full.chunks[0].end-full.chunks[0].start);
  const all=ribbonSegments(full,t=>(t-100)*100,0), clipped=ribbonSegments(cropped,t=>(t-100)*100,0);
  const a=all.find(s=>s.phase==='action'), b=clipped.find(s=>s.phase==='action');
  assert.ok(Math.abs((a.y2-a.y1)/(a.x2-a.x1)-(b.y2-b.y1)/(b.x2-b.x1))<1e-9);
});
test('all segments including wait and replaced tail select complete source chunk',()=>{
  const lanes=build([block(7,{replaced_at:1.9,action_end:2,replaced_by:8,replaced_steps:4,status:'replaced'})],102.2);
  const segments=ribbonSegments(lanes,t=>(t-100)*100,40);
  for(const phase of ['inference','wait','action','replaced']){
    const segment=segments.find(s=>s.phase===phase);assert.ok(segment,phase);
    const hit=hitTestRibbon(segments,(segment.x1+segment.x2)/2,(segment.y1+segment.y2)/2);
    assert.equal(hit.ribbon.id,7);assert.equal(hit.ribbon.block.prefix_trimmed,2);
  }
});
test('hidden phases do not participate in hit detection and crossings choose nearest/topmost',()=>{
  const lanes=build([block(1)]);
  assert.deepEqual(ribbonSegments(lanes,t=>t,0,{inference:false,chunkSpan:false}),[]);
  const a={x1:0,y1:0,x2:20,y2:20,ribbon:{id:1}},b={x1:0,y1:20,x2:20,y2:0,ribbon:{id:2}};
  assert.equal(hitTestRibbon([a,b],10,10).ribbon.id,2);
  assert.equal(hitTestRibbon([a,b],3,3).ribbon.id,1);
  assert.equal(hitTestRibbon([a,b],80,80),null);
});
test('coverage sweep reports exact count, not merged pairwise overstatement',()=>{
  const spans=[{id:1,start:0,end:3},{id:2,start:1,end:4},{id:3,start:2,end:5}];
  assert.deepEqual(overlapSpans(spans).map(s=>[s.start,s.end,s.count]),[[1,2,2],[2,3,3],[3,4,2]]);
});
test('unfinished inference grows with shared frame time and stage endpoints stay aligned',()=>{
  const lanes=build([block(1,{end:null,active:null,stages:[{name:'model',start:1.2,end:1.4,gpu_ms:20}]})],102);
  assert.equal(lanes.inferences[0].end,102);
  const expanded=ribbonSegments(lanes,t=>t*100,0,{inferenceStages:true});
  assert.ok(expanded.some(s=>s.stage==='model'));
  assert.equal(lanes.ribbons[0].block.stages[0].gpu_ms,20);
});
test('bounded history never retains more active chunks than the configured limit',()=>{
  const lanes=build(Array.from({length:300},(_,id)=>block(id)),102,{maxBlocks:30});
  assert.equal(lanes.ribbons.length,30);assert.equal(lanes.ribbons[0].id,270);
});
test('freeze copies nested stages, datasets and original metadata independently',()=>{
  const live={now:102,timeline:{blocks:[block(1,{stages:[{name:'model',end:1.4}]})]},points:[{x:102,y:7}]};
  const frozen=snapshotDisplay(live);
  live.now=103;live.timeline.blocks[0].stages[0].end=2;live.points.push({x:103,y:8});
  assert.equal(frozen.now,102);assert.equal(frozen.timeline.blocks[0].stages[0].end,1.4);assert.equal(frozen.points.length,1);
});

test('queue merge does not prematurely end active output and actual completion caps execution',()=>{
  const pending=build([block(1,{replaced_at:1.8,replaced_steps:4})],102.1);
  const segments=ribbonSegments(pending,t=>t,0);
  assert.equal(segments.some(s=>s.phase==='replaced'),false);
  assert.equal(segments.find(s=>s.phase==='action').end,102.1);
  const done=build([block(1,{status:'completed',action_end:1.9})],103);
  const completed=ribbonSegments(done,t=>t,0);
  assert.equal(completed.find(s=>s.phase==='action').end,101.9);
  assert.equal(completed.find(s=>s.phase==='replaced').start,101.9);
});

test('waiting counts advance, failed counts stop and accepted plans need no execution span',()=>{
  const waiting=build([block(1,{status:'waiting',active:null})],103).ribbons[0];
  assert.equal(ribbonAnalysis(waiting,103).total,2);
  assert.ok(Math.abs(ribbonAnalysis(waiting,103).plan-.8)<1e-9);
  assert.equal(ribbonAnalysis(waiting,103).actionElapsed,null);
  const failed=build([block(1,{status:'failed',failed:true,active:null})],103).ribbons[0];
  assert.deepEqual(ribbonAnalysis(failed,103),ribbonAnalysis(failed,108));
});
