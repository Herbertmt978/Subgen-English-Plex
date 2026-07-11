# Contributing

Small, focused fixes are welcome. Please open an issue first for changes that alter subtitle naming, deletion behaviour, API compatibility, or deployment defaults.

## Local checks

Use Python 3.10 or newer.

```bash
python -m pip install -r requirements-test.txt
python -m pytest -q
python -m py_compile subgen_override.py language_code.py monitor_subgen_failures.py repair_subgen_failures.py
docker compose -f docker-compose.yml config --quiet
docker compose -f docker-compose.gpu.yml config --quiet
```

Tests mock the large machine-learning dependencies, so a GPU is not required to run the suite.

## Pull requests

- Keep unrelated formatting and dependency upgrades out of the change.
- Add regression coverage for behavioural fixes.
- Do not commit `.env`, `monitor.env`, tokens, media names, subtitle text, or private paths.
- Update the README or configuration guide when a default or operator-visible behaviour changes.
