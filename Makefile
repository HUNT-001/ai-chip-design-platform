# AVA Verification Platform — Makefile
# Usage: make <target>

PYTHON  ?= python3
PYTEST  ?= $(PYTHON) -m pytest
AVA     ?= $(PYTHON) ava_patched.py

.PHONY: help install test test-full lint smoke clean push

help:
	@echo "AVA Verification Platform"
	@echo ""
	@echo "  make install     Install Python dependencies"
	@echo "  make test        Run pure-Python test suite (no EDA tools needed)"
	@echo "  make test-full   Run full test suite including async smoke test"
	@echo "  make lint        Syntax-check all .py files"
	@echo "  make smoke       Run AVA orchestrator smoke test"
	@echo "  make clean       Remove generated run dirs and caches"
	@echo "  make push        Stage all changes and push to GitHub"

install:
	$(PYTHON) -m pip install -r requirements.txt
	$(PYTHON) -m pip install pytest pytest-asyncio

test:
	$(PYTEST) tests/test_agents.py \
		-k "not test_ava_generate_suite_smoke" \
		--import-mode=importlib \
		-p no:cacheprovider \
		--tb=short -q

test-full:
	$(PYTEST) tests/test_agents.py \
		--import-mode=importlib \
		-p no:cacheprovider \
		--tb=short -v

lint:
	@echo "Checking syntax of all .py files (excluding _legacy/)..."
	@failed=0; \
	for f in $$(find . -name "*.py" -not -path "./_legacy/*" -not -path "./.git/*"); do \
		$(PYTHON) -m py_compile "$$f" || { echo "FAIL: $$f"; failed=1; }; \
	done; \
	[ $$failed -eq 0 ] && echo "All files OK" || exit 1

smoke:
	@echo "Running AVA smoke test..."
	$(PYTHON) -c " \
import asyncio, sys; sys.path.insert(0, '.'); import ava_patched; \
RTL = 'module riscv_core(input wire clk, output reg [31:0] data_out); reg [31:0] pc; endmodule'; \
async def run(): \
    ava = ava_patched.AVA(enable_llm=False, enable_database=False, timeout=30, target_coverage=50.0); \
    r = await ava.generate_suite(RTL, 'in_order', seed=1, save_results=False); \
    assert r['status'] == 'completed'; \
    print('Smoke test PASSED — confidence:', r['extended_reports'].get('confidence',{}).get('score','N/A')); \
asyncio.run(run())"

clean:
	find . -type d -name "__pycache__" -not -path "./_legacy/*" -exec rm -rf {} + 2>/dev/null || true
	find . -name "*.pyc" -not -path "./_legacy/*" -delete 2>/dev/null || true
	rm -rf sim_runs/ /tmp/ava_* 2>/dev/null || true
	@echo "Clean done"

push:
	git add -A
	git status --short
	@read -p "Commit message: " msg; \
	git commit -m "$$msg" && git push origin main
