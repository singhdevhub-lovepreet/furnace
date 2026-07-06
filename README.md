# Raven

Raven is a hosted iOS development harness: it is designed to run user tasks on macOS with Xcode and the iOS Simulator, closing the loop from build to run to screenshot/video proof. The architecture and implementation plan live in [docs/architecture.md](docs/architecture.md) and [docs/implementation-lld.md](docs/implementation-lld.md).

## Phase-0 Spike

This spike must run on **macOS** because it depends on Xcode, the iOS Simulator, and `xcrun simctl`.

### Prerequisites

- Xcode installed and selected
- `xcodes` available for managing Xcode versions
- XcodeGen installed via Homebrew:
  ```bash
  brew install xcodegen
  ```

### Run

Default SampleApp flow:

```bash
./spike/run_spike.sh
```

Run against a cloned repository instead of the built-in SampleApp:

```bash
./spike/run_spike.sh --repo <git-url>
```

## Database & migrations

For local Postgres development, start the bundled database container:

```bash
docker compose up -d
```

Then point the app at Postgres and run migrations:

```bash
export FURNACE_DATABASE_URL=postgresql+asyncpg://furnace:furnace@localhost:5432/furnace
export FURNACE_RUN_MIGRATIONS_ON_STARTUP=true
export FURNACE_AUTO_CREATE_SCHEMA=false
alembic upgrade head
```

The test suite uses SQLite by default, so you do not need Postgres for the standard local test run.
