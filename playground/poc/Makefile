PY := ../../.venv/bin/python
MAX_MINUTES ?= 0
ENGINE ?= qari_v02
RETRY_FLAG := $(if $(RETRY),--retry-errors,)

.PHONY: install pages prep repair models models-mlx selftest quick full mlx status drafts promote report clean-outputs

install:
	cd ../.. && uv pip install --python .venv/bin/python -r playground/poc/requirements.txt
	@echo "Now run: brew install tesseract-lang"

pages:
	$(PY) extract_pages.py

prep:
	$(PY) preprocess.py

repair:
	$(PY) repair_text_layer.py

models:
	$(PY) prepare_models.py

models-mlx:
	$(PY) prepare_models.py --mlx

selftest:
	$(PY) run_ocr.py --self-test

quick:
	$(PY) run_ocr.py --quick --max-minutes $(MAX_MINUTES) $(RETRY_FLAG)

full:
	$(PY) run_ocr.py --max-minutes $(MAX_MINUTES) $(RETRY_FLAG)

mlx:
	$(PY) run_ocr.py --engines $(ENGINE) --variants gray --backend mlx --max-minutes $(MAX_MINUTES) $(RETRY_FLAG)

status:
	$(PY) run_ocr.py --dry-run

drafts:
	$(PY) make_gt_drafts.py

promote:
	$(PY) make_gt_drafts.py --promote all

report:
	$(PY) evaluate.py

clean-outputs:
	rm -rf gray/* bw/* gray_2x/* overlays/* pages/*.png pages/*.prep.json pages/manifest.json
