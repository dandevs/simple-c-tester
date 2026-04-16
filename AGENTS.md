# AGENTS.md

## Project
Python test runner. Entry point: `src/main.py` (run with `python -m src.main` from repo root).

## Structure
- `src/main.py` — CLI with `--parallel N` and `--watch` flags
- `src/models/` — `Test`, `Suite` dataclasses and `TestState` enum

## Tooling
- **Ruff** was used at some point (`.ruff_cache` in `.gitignore`) but no config file exists. Confirm before adding linting.
- No `pyproject.toml`, `requirements.txt`, or test framework configured.
- No tests exist yet.
