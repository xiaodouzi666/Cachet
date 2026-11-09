"""Built-in coding tasks, with each subsequent turn conditioned on prior model output."""
TASKS = {
    "rate-limit": ("fastapi", "Add a rate-limiting middleware to a FastAPI app."),
    "route-class": ("starlette", "Explain how Starlette routes a request and add a custom route class."),
}

STEPS = (
    "Explain the tradeoff between the two quoted inference options and the chosen strategy.",
    "Describe the feature requirements and measurable acceptance conditions.",
    "Locate the relevant framework classes in the supplied repository context.",
    "Trace the request path through these classes.",
    "Design the smallest change that fulfills the task.",
    "Write the core Python implementation.",
    "Show how an application registers and exercises this implementation.",
    "Write a test for the successful path.",
    "Write a test for an edge case and explain the expected behavior.",
    "Review your implementation, correct any issue you find, and summarize the result.",
)


def turn_question(task, step, instruction):
    return f"Task: {task}\nStep {step + 1}: {instruction}\nBe specific, using the supplied source. Keep the response concise."
