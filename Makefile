.PHONY: help lint test deb clean

help:
	@echo "Targets:"
	@echo "  lint   ruff + shellcheck"
	@echo "  test   pytest unit tests (no embedder, no LanceDB)"
	@echo "  deb    build a .deb in dist/"
	@echo "  clean  rm -rf dist/ build/ *.egg-info"

lint:
	ruff check lib/ bin/
	shellcheck scripts/build-deb.sh scripts/get.sh debian/postinst debian/postrm

test:
	PYTHONPATH=lib pytest tests/ -q

deb:
	bash scripts/build-deb.sh

clean:
	rm -rf dist/ build/ *.egg-info lib/*.egg-info
