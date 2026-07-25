.PHONY: help install migrate demo demo-s5 spike test test-unit test-isolation \
        test-scenarios lint api dashboard export chaos-up chaos-down clean

help:
	@echo "Quorum — memory consistency for multi-agent systems"
	@echo ""
	@echo "  make install         install python deps"
	@echo "  make migrate         apply schema, indexes, GC TTL, agent registry"
	@echo "  make console         live console -- type your own contradictions"
	@echo "  make demo            all 5 scenarios x 3 modes"
	@echo "  make demo-s5         the flagship concurrent race"
	@echo "  make spike           the original M2 proof spike (200 races x 3 modes)"
	@echo "  make test            unit + scenario tests"
	@echo "  make test-isolation  the flagship isolation test, 100 consecutive races"
	@echo "  make lint            assert no mode branching outside factory.py (I8)"
	@echo "  make api             read-only FastAPI on :8000"
	@echo "  make dashboard       Next.js dev server on :3000"
	@echo "  make export          re-bake the dashboard demo snapshot"
	@echo "  make chaos-up        local 3-node cluster for the node-kill test"

install:
	python -m pip install -r requirements.txt

migrate:
	python -m quorum.db.migrate

console:
	python -m quorum.demo.console

demo:
	python -m quorum.harness.report --all --delay-ms 40

demo-s5:
	python -m quorum.harness.report --scenario S5_concurrent_race --delay-ms 40

spike:
	python spikes/bootstrap.py
	python spikes/prove_race.py --iterations 200 --delay-ms 50

test: lint test-unit test-scenarios

test-unit:
	python -m pytest tests/unit -q

test-scenarios:
	python -m pytest tests/scenarios -q

# The flagship. BUILD.md asks for 100 consecutive runs.
test-isolation:
	QUORUM_ISOLATION_ITERATIONS=100 python -m pytest \
	  tests/integration/test_txn_isolation.py -q -s

lint:
	python tools/lint_modes.py

api:
	python -m uvicorn quorum.api.server:app --reload --port 8000

dashboard:
	cd dashboard && npm run dev

export:
	python -m quorum.harness.export_demo --rerun

chaos-up:
	bash infra/chaos/start_cluster.sh

chaos-down:
	bash infra/chaos/stop_cluster.sh

clean:
	rm -rf runs/ .pytest_cache .cache dashboard/.next dashboard/out
	find . -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
