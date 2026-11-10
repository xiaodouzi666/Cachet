from backend.app.agents.tasks import MAX_OUTPUT_TOKENS, STEPS, requests_module, turn_question
from backend.app.agents.window import window_prompt
from backend.app.gateway.prompt_builder import token_hash


FAULT_FOLLOWUPS = (
    "Identify how the previous implementation handles the successful path.",
    "Identify one concrete boundary case for that implementation.",
    "Give the expected result of a test for that boundary case.",
)


def live_turns(
    engine,
    prefix,
    owners,
    tenant,
    task,
    buy,
    *,
    builtin_task=True,
    max_output=MAX_OUTPUT_TOKENS,
    fault_source=None,
    check_proposal=None,
    pending_flush=None,
    prior_answers=None,
):
    messages = []
    if prior_answers is not None:
        if len(prior_answers) != len(STEPS):
            raise ValueError("The guided follow-up needs the original completed conversation")
        for i, answer in enumerate(prior_answers):
            question = turn_question(task, i, STEPS[i], previous=messages[-1][1] if messages else "")
            messages.extend([("user", question), ("assistant", answer)])
    http_feedback = None
    for i, step in enumerate(STEPS if prior_answers is None else FAULT_FOLLOWUPS):
        if prior_answers is None:
            question = turn_question(task, i, step, previous=messages[-1][1] if messages else "", http_feedback=http_feedback)
        else:
            question = f"Task: {task}\nFollow-up {i + 1}: {step} Answer in at most 65 words."
        http_feedback = None
        messages.append(("user", question))
        prompt = window_prompt(engine, prefix, owners, tenant, messages, buy, max_output)
        yield {
            "kind": "estimate",
            "prompt_tokens": len(prompt.ids),
            "output_limit": max_output,
            "spans": prompt.spans,
            "turn": i + 1,
        }
        flush_id = pending_flush() if pending_flush else None
        result = engine.generate(
            prompt.ids,
            max_new_tokens=max_output,
            fault_ms=fault_source() if fault_source else 0,
            stop_at_module_fence=prior_answers is None and requests_module(task, i, builtin_task=builtin_task),
        )
        if result["prompt_tokens"] != len(prompt.ids):
            raise ValueError("The engine reported an unexpected prompt length")
        result.update(
            turn=i + 1,
            spans=prompt.spans,
            input_hash=token_hash(prompt.ids),
            mode="live",
        )
        if flush_id is not None:
            result["cache_flush_id"] = flush_id
        if check_proposal and i in (5, 8):
            http_feedback = check_proposal(result["text"])
            result["http_feedback"] = http_feedback
        messages.append(("assistant", result["text"]))
        yield {"kind": "result", **result}
