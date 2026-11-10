const { test } = require("node:test");
const assert = require("node:assert/strict");
const { isColdAndFree, isFaultedWarmBreach } = require("./record_scenes.ts");
const buy = require("../backend/app/replay/traces/rate-limit-buy.json");
const compute = require("../backend/app/replay/traces/rate-limit-compute.json");

test("historical GPU measurements cannot stand in for a newly flushed or deliberately delayed call", () => {
  for (const archive of [buy, compute]) {
    for (const measured of archive.turns) {
      assert.equal(isColdAndFree(measured, measured.cache_flush_id), false);
      assert.equal(isFaultedWarmBreach(measured, measured.mode), false);
    }
  }
});


test("a global flush selects the exact recorded cold source without warming it", () => {
  const {recordingContext} = require("./record_scenes.ts");
  const contexts = [
    {slug:"fastapi", id:"public", can_manage:false, status:"hot"},
    {slug:"starlette", id:"parent", can_manage:true, status:"cold"},
    {slug:"fastapi", id:"mine", can_manage:true, status:"cold"}
  ];
  assert.equal(recordingContext(contexts), undefined);
  assert.equal(recordingContext(contexts, {flushedContextId:"mine"}).id, "mine");
  assert.equal(recordingContext(contexts, {flushedContextId:"public"}), undefined);
});
