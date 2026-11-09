"""The public coding contracts and conversation with actual compiler feedback."""

import builtins
import re
import symtable

TASKS = {
    "rate-limit": ("fastapi", "Add a rate-limiting middleware to a FastAPI app."),
    "route-class": (
        "starlette",
        "Explain how Starlette routes a request and add a custom route class.",
    ),
}

CONTRACTS = {
    TASKS["rate-limit"][1]: (
        "Deliver one standalone Python module exposing app = FastAPI(). GET /ping returns JSON "
        "{'pong': True}. Implement a request middleware that limits /ping by request.client.host: "
        "allow the first two requests by each client in a 60-second window; return a JSONResponse "
        "with status_code=429 on its third request. Other clients have independent quotas. "
        "Use FastAPI's @app.middleware('http') function and @app.get('/ping'); no middleware class. "
        "Use a dictionary of lists keyed by client host. At each request, retain only monotonic timestamps "
        "younger than 60 seconds; two retained timestamps mean reject. Otherwise append now and await call_next. "
        "Import FastAPI, JSONResponse and time.monotonic. Use no external storage or third-party rate limiter."
    ),
    TASKS["route-class"][1]: (
        "Deliver one standalone Python module exposing app = Starlette(...). Register GET /ping "
        "using a subclass of starlette.routing.Route. Its endpoint returns JSONResponse({'pong': True}). "
        "The Route subclass itself must add X-Route-Class: custom to its successful response. "
        "Use Starlette, not FastAPI: Starlette's Route supports handle(scope, receive, send), "
        "wrap send when passing it to super().handle. For the ASGI 'http.response.start' event, "
        "MutableHeaders(scope=message) can set the header; always forward messages. The endpoint only supplies JSON. "
        "Import Starlette from starlette.applications, Route from starlette.routing, JSONResponse from "
        "starlette.responses and MutableHeaders from starlette.datastructures. The Route constructor takes a path "
        "and an endpoint callable. Register the subclass with '/ping' and an async endpoint."
    ),
}

MAX_OUTPUT_TOKENS = 640

STEPS = (
    "In two sentences, explain the chosen strategy using the two quoted costs in microdollars.",
    "Describe the feature requirements and measurable acceptance conditions.",
    "Locate the relevant framework classes in the supplied repository context.",
    "Trace the request path through these classes.",
    "Design the smallest change that fulfills the task.",
    "Propose the entire runnable module, in one fenced Python code block with imports.",
    "Review your proposed code against the contract and the compiler diagnostic, correcting any mistaken framework APIs.",
    "Describe the HTTP requests and assertions that check the successful path.",
    "Correct all issues and propose the entire final candidate module for syntax and HTTP checks. Return just one fenced Python block.",
    "Return the final, complete, importable Python application as exactly one fenced python code block. "
    "Include imports and app. Implement every contract requirement. No tests, shell, placeholders or explanatory prose.",
)

FENCE = re.compile(
    r"^```[ \t]*(?:python|py)?[ \t]*\n(.*?)^```[ \t]*(?:\n|$)", re.MULTILINE | re.DOTALL
)


def app_code(text, *, allow_prose=False):
    blocks = FENCE.findall(text)
    if len(blocks) != 1 or (not allow_prose and FENCE.sub("", text).strip()):
        raise ValueError("Expected one fenced Python module as the entire final answer")
    return blocks[0]


def diagnostic(text):
    """Compile only. This process never imports or executes model-generated source."""
    blocks = FENCE.findall(text)
    if not blocks:
        return ""
    findings = []
    for index, source in enumerate(blocks, 1):
        try:
            compile(source, "<previous-proposal>", "exec", dont_inherit=True)
        except (SyntaxError, ValueError) as exc:
            findings.append(
                f"Block {index}: {type(exc).__name__} at line {getattr(exc, 'lineno', None)}: "
                f"{getattr(exc, 'msg', str(exc))}."
            )
        else:
            bindings = symtable.symtable(source, "<previous-proposal>", "exec")
            defined = set(dir(builtins)) | {
                s.get_name()
                for s in bindings.get_symbols()
                if s.is_assigned() or s.is_imported()
            }
            stack, missing = [bindings], set()
            while stack:
                table = stack.pop()
                stack.extend(table.get_children())
                missing.update(
                    s.get_name()
                    for s in table.get_symbols()
                    if s.is_referenced()
                    and s.is_global()
                    and s.get_name() not in defined
                )
            findings.append(
                f"Block {index}: Python compiled successfully."
                + (
                    " Unbound global names: " + ", ".join(sorted(missing)) + "."
                    if missing
                    else ""
                )
            )
    return "\nPython syntax check of the preceding answer:\n" + "\n".join(findings)


FRAMEWORK_NOTES = {
    TASKS["rate-limit"][1]: (
        "Use exactly these three import statements:\nfrom fastapi import FastAPI, Request\n"
        "from fastapi.responses import JSONResponse\nfrom time import monotonic\n"
        "Use an unannotated module-level dictionary, for example recent = {}; use no Dict, List or Callable. "
        "Register both @app.middleware('http') and @app.get('/ping'). Remember that a client can send two successful requests."
    ),
    TASKS["route-class"][1]: (
        "Keep the endpoint and the custom route as separate definitions. Use these imports:\n"
        "from starlette.applications import Starlette\nfrom starlette.routing import Route\n"
        "from starlette.responses import JSONResponse\nfrom starlette.datastructures import MutableHeaders\n"
        "The subclass inherits Route's constructor; its only method is async def handle(self, scope, receive, send). "
        "Inside handle use an async wrapped_send(message) callback. On 'http.response.start' use "
        "MutableHeaders(scope=message)['X-Route-Class'] = 'custom'. Await send(message) for each message. "
        "Delegate to await super().handle(scope, receive, wrapped_send). A separate async def ping(request) "
        "returns the JSONResponse. Use CustomRoute('/ping', endpoint=ping) inside Starlette(routes=[...])."
    ),
}


def turn_question(
    task, step, instruction, *, quote_text=None, previous="", http_feedback=None
):
    contract = CONTRACTS.get(task, "")
    if not contract and step == len(STEPS) - 1:
        instruction = "Deliver the final artifact or complete explanation requested by the user's task."
    # Once the first implementation is in the dialogue, review and test planning
    # need just the observable behavior. Code generation keeps the full contract.
    if task == TASKS["rate-limit"][1] and step in (6, 7):
        contract = "Per client: 200, 200, 429. Return JSONResponse directly from middleware on rejection."
    q = (
        f"Task: {task}\n"
        + (f"Application contract: {contract}\n" if contract else "")
        + f"Step {step + 1}: {instruction}\nUse the supplied framework source. Answer only this step, "
        "using up to 65 words for prose or a complete short module for code. Do not claim to have run tests or edited files."
    )
    if step == 0:
        return q + ("\nQuote: " + quote_text if quote_text is not None else "")
    q += diagnostic(previous)
    if http_feedback:
        q += (
            "\nIndependent sandbox HTTP check of the preceding proposal: "
            + http_feedback
        )
    if contract and step in (5, 8, 9):
        q += "\nFramework API reference for this application:\n" + FRAMEWORK_NOTES[task]
    if step == 9 and http_feedback:
        if http_feedback.startswith("HTTP smoke passed:"):
            q += "\nRepeat the preceding working module verbatim, including all its imports, in a single fenced python block."
        else:
            q += "\nCorrect the actual error above using the framework reference. Return the entire corrected module."
    return q
