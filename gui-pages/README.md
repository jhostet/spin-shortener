# A HTTP python component using componentize-py

## Requirements

Dependencies ([`componentize-py`](https://pypi.org/project/componentize-py/) and [`spin-sdk`](https://pypi.org/project/spin-sdk/)) are managed with [`uv`](https://docs.astral.sh/uv/) and pinned in `pyproject.toml`/`uv.lock`.

## Building and Running

```
spin up --build
```

The build command in `spin.toml` runs `uv run componentize-py ...`, which syncs `.venv` from `uv.lock` automatically — no manual install step is needed, even on a fresh clone.

To set up the environment yourself (e.g. for editor/language server support), run `uv sync` from this directory.
