# Furnace services

This package contains the Raven control-plane skeleton.

## Run locally

```bash
uvicorn services.main:app --reload
```

## Test

```bash
pytest
```

## Configuration

Copy `.env.example` to `.env` and adjust the `FURNACE_*` settings as needed.
