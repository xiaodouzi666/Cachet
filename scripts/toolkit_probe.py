"""Install or verify the official PayPal Agent Toolkit against a real sandbox journal."""

import argparse
import json
import subprocess
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.app.paypal.client import PayPalConfigError, PayPalError, require, settings
from backend.app.paypal.toolkit import chat_tools, run_tool


def install():
    env = ROOT / ".venv-toolkit"
    if not (env / "bin/python").is_file():
        subprocess.run([sys.executable, "-m", "venv", str(env)], check=True)
    subprocess.run([str(env / "bin/python"), "-m", "ensurepip", "--upgrade"], check=True)
    pip = [str(env / "bin/python"), "-m", "pip", "install", "--disable-pip-version-check"]
    # Only the OpenAI adapter is imported. CrewAI, LangChain and Bedrock plugins
    # are independent of the adapter used by Cachet and its live read tools.
    subprocess.run(pip + ["--no-deps", "paypal-agent-toolkit==1.11.0"], check=True)
    subprocess.run(pip + ["openai-agents==0.0.2", "openai==1.66.1", "requests>=2.31,<3", "httpx==0.28.1", "python-dotenv==1.2.4"], check=True)


def verify(journal):
    config = settings()
    require(config, "PAYPAL_CLIENT_ID")
    require(config, "PAYPAL_CLIENT_SECRET")
    data = json.loads(journal.read_text())
    if not data.get("completed") or data["scenario"] not in ("wallet", "all"):
        raise ValueError("A completed real wallet smoke journal is required.")
    order_id = data["operations"]["multi.order"]["id"]
    capture_id = data["operations"]["multi.first.capture"]["id"]
    hosted = config.get("COPILOT_LLM_BASE_URL")
    base = (hosted or require(config, "SGLANG_BASE_URL")).rstrip("/")
    if not base.endswith("/v1"):
        base += "/v1"
    key = require(config, "COPILOT_LLM_API_KEY" if hosted else "SGLANG_API_KEY")
    model = require(config, "COPILOT_LLM_MODEL" if hosted else "MODEL_ID")
    tools = chat_tools()
    evidence = {}
    with httpx.Client(headers={"Authorization": "Bearer " + key}, timeout=90) as client:
        for name, field, actual in (("get_order_details", "order_id", order_id),
                                    ("list_transactions", "transaction_id", capture_id)):
            response = client.post(base + "/chat/completions", json={
                "model": model, "temperature": 0, "max_tokens": 256, "tools": tools,
                "tool_choice": {"type": "function", "function": {"name": name}},
                "messages": [{"role": "user", "content": f"Use {name} for this {field}: {actual}."}],
            }).raise_for_status()
            calls = response.json()["choices"][0]["message"].get("tool_calls") or []
            if len(calls) != 1 or calls[0]["function"]["name"] != name:
                raise RuntimeError(f"The live model did not request {name}.")
            arguments = json.loads(calls[0]["function"]["arguments"])
            if arguments.get(field) != actual:
                raise RuntimeError("The model supplied a different identifier.")
            item = run_tool(name, arguments, order_ids=[order_id], transaction_ids=[capture_id])
            if name == "get_order_details" and item.get("id") != order_id:
                raise RuntimeError("Toolkit order did not match the real smoke order.")
            if name == "list_transactions" and not any(r["transaction_id"] == capture_id for r in item["transactions"]):
                raise RuntimeError("PayPal Reporting has not returned the real capture. Retry this journal after indexing.")
            evidence[name] = item
    return evidence


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    commands.add_parser("install")
    commands.add_parser("tools")
    commands.add_parser("verify").add_argument("journal", type=Path)
    args = parser.parse_args()
    try:
        if args.action == "install":
            install()
        elif args.action == "tools":
            print(json.dumps(chat_tools(), indent=2))
        else:
            print(json.dumps(verify(args.journal), indent=2))
    except (PayPalConfigError, PayPalError, ValueError, RuntimeError, httpx.HTTPError) as exc:
        parser.exit(1, f"{exc}\n")
