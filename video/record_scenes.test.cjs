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
