"""Install the isolated PayPal SDK adapter used by the API."""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


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


if __name__ == "__main__":
    install()
