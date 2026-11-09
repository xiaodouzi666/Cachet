# Cachet

A marketplace for warm LLM context where engine-measured prefix hits earn publisher royalties.

## Development

Use Python 3.11, Node.js 18.19 or later, tmux, Make, and Gitleaks 8.28 or later.

```sh
python3.11 -m venv .venv
.venv/bin/pip install -r backend/requirements-dev.txt
npm ci --prefix frontend
cp .env.example .env
make hooks
make test
make dev
```

The development servers bind to loopback. Use `make verify` for unit tests and non-PayPal integration tests, or `make verify-full` with the GPU and sandbox services configured. The commit hook scans the staged changes and requires Gitleaks on PATH or at `.tools/bin/gitleaks`.

`make e2e` is reserved for the sandbox demo in T1.13. `make bench` invokes the TTFT benchmark once it has been implemented.
