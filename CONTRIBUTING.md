# Contributing

All commits require DCO sign-off: `git commit -s`

## Tests

```bash
python -m pip install -e ".[dev]"
python -m pytest
python -m ruff check .
python -m mypy scripts
```
