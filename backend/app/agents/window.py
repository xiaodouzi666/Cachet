"""Build a bounded, private dialogue while preserving licensed source and tenant spans."""
from backend.app.config import manifest

RECENT_TURNS = 6
WINDOW_TOKENS = 31_000


def excerpt(text, max_chars):
    if len(text) <= max_chars:
        return text
    # Keep both ends of code or prose when compacting an unusually long answer.
    if max_chars <= 16:
        return "…"
    head = (max_chars - 5) * 2 // 3
    tail = max_chars - 5 - head
    return text[:head] + " […] " + text[-tail:]


def window_prompt(engine, prefix, owners, tenant, messages, buy, output_limit):
    """Keep the current request intact and reserve space for its complete allowed answer."""
    limit = min(WINDOW_TOKENS, manifest()["context_window"]) - output_limit
    history, current = messages[:-1], messages[-1]
    older = history[:-2 * RECENT_TURNS]
    recent = history[-2 * RECENT_TURNS:]
    # Each current user message repeats the original task and this turn's instruction.
    # Preserve conclusions from evicted exchanges using excerpts of the actual answers.
    notes = [
        f"Turn {i}: {excerpt(' '.join(text.split()), 150)}"
        for i, (_, text) in enumerate(older[1::2], 1)
    ]
    summary = "Earlier exchange summary (answer excerpts):\n" + "\n".join(notes) if notes else ""
    char_limit = max((len(text) for _, text in recent), default=0)
    while True:
        kept = [(role, excerpt(text, char_limit)) for role, text in recent]
        if summary:
            role, text = kept[0]
            kept[0] = (role, summary + "\n\n" + text)
        prompt = prefix.build(engine, owners, tenant, [*kept, current], buy)
        if len(prompt.ids) <= limit:
            return prompt
        if char_limit == 0:
            raise ValueError("The current task and fixed source exceed the 31,000-token window. Shorten the task.")
        # Six very long recent exchanges can exceed the window even after evicting older turns.
        # Only the private excerpts shrink. Re-tokenize the actual final text before it is priced.
        char_limit //= 2
