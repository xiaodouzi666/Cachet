"""Generate replay tracks from real GPU output. Only built-in public tasks are recorded."""
import argparse
import json
import secrets
from datetime import datetime,timezone
from backend.app.agents.inference import live_turns
from backend.app.agents.tasks import TASKS
from backend.app.config import value
from backend.app.gateway.prompt_builder import Prefix
from backend.app.gateway.quote import quote
from backend.app.gateway.sglang_client import Engine
from backend.app.replay.recordings import path,signature,load


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task",choices=TASKS)
    args=parser.parse_args()
    with Engine() as engine:
        for task_id in [args.task] if args.task else TASKS:
            slug,task=TASKS[task_id]
            prefix=Prefix(engine,slug)
            owners={name:name for name,_ in prefix.layers}
            for buy in (True,False):
                engine.flush()
                if buy and prefix.warm(engine) < .95:
                    raise RuntimeError("Warm probe missed its threshold")
                q=quote(sum(len(ids) for _,ids in prefix.layers),10,.25 if buy else 0)
                quote_text=json.dumps(q,sort_keys=True)
                turns=[r for r in live_turns(engine,prefix,owners,secrets.token_hex(4),task,buy,quote_text) if r["kind"]=="result"]
                data={"task_id":task_id,"source":slug,"buy":buy,"quote":q,"model":value("MODEL_ID"),
                      "recorded_at":datetime.now(timezone.utc).isoformat(),"signature":signature(task_id,buy),"turns":turns}
                output=path(task_id,buy)
                tmp=output.with_suffix(".tmp")
                tmp.write_text(json.dumps(data,indent=2)+"\n")
                tmp.replace(output)
                load(task_id,buy)
                print(output.name, len(turns),"real GPU completions", flush=True)

if __name__=="__main__":
    main()
