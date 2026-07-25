.PHONY: help install migrate demo demo-s5 spike test test-unit test-isolation \
        test-scenarios lint api dashboard export chaos-up chaos-down clean

help:
	@echo "Quorum — memory consistency for multi-agent systems"
	@echo ""
	@echo "  make install         install python deps"
	@echo "  make migrate         apply schema, indexes, GC TTL, agent registry"
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

demo:
	python -m quorum.harness.report --all --delay-ms 40

demo-s5:
	python -m quorum.harness.report --scenario S5_concurrent_race --delay-ms 40

spike:
	python spikes/bootstrap.py
	python spikes/prove_race.py --iterations 200 --delay-ms 50
