import pytest
from backend.app.gateway.prompt_builder import Prefix
from backend.app.replay.recordings import load
from backend.app.agents.tasks import TASKS


@pytest.mark.gpu
def test_live_public_chain_cross_tenant(engine):
    prefix=Prefix(engine,"fastapi")
    assert prefix.warm(engine)>=.95
    owners={slug:slug for slug,_ in prefix.layers}
    messages=[("user","Name an ASGI framework.")]
    first=prefix.build(engine,owners,"5678abcd",messages)
    other=prefix.build(engine,owners,"21ef6821",messages)
    assert engine.generate(first.ids,12)["text"].strip()
    out=engine.generate(other.ids,12)
    public_end=other.spans[2]["end"]
    assert public_end <= out["cached_tokens"] <= other.spans[3]["end"]
    private=prefix.build(engine,owners,"987654ab",messages,buy=False)
    assert all(s["kind"]=="private" for s in private.spans)
    assert engine.generate(private.ids,12)["cached_tokens"] < public_end*.05


@pytest.mark.parametrize("task_id",TASKS)
@pytest.mark.parametrize("buy",[True,False])
def test_real_recordings_have_complete_metering(task_id,buy):
    record=load(task_id,buy)
    assert all(t["output_tokens"]>0 and t["ttft_ms"]<=t["total_ms"] and t["mode"]=="live"
               for t in record["turns"])
