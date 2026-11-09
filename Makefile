PYTHON ?= .venv/bin/python
.DEFAULT_GOAL := test

.PHONY: setup dev test verify verify-full e2e bench hooks
setup:
	python3.11 -m venv .venv
	$(PYTHON) -m pip install -r backend/requirements-dev.txt
	npm ci --prefix frontend
	$(PYTHON) scripts/local_env.py

dev:
	scripts/dev.sh

test:
	$(PYTHON) -m pytest -q backend/tests/unit

verify:
	$(PYTHON) -m pytest -q backend/tests/unit backend/tests/integration -m 'not sandbox'

verify-full:
	CACHET_VERIFY_FULL=1 $(PYTHON) -m pytest -q backend/tests/unit backend/tests/integration

e2e:
	$(PYTHON) scripts/e2e_demo.py

bench:
	$(PYTHON) -m bench.ttft_bench

hooks:
	git config --local core.hooksPath .githooks
