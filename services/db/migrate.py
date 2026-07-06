from __future__ import annotations

import os
from pathlib import Path

from alembic import command
from alembic.config import Config


def run_migrations(database_url: str) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    alembic_ini = repo_root / "alembic.ini"
    config = Config(str(alembic_ini))
    config.set_main_option("script_location", str(repo_root / "alembic"))
    config.set_main_option("sqlalchemy.url", database_url)
    previous_database_url = os.environ.get("FURNACE_DATABASE_URL")
    os.environ["FURNACE_DATABASE_URL"] = database_url
    try:
        command.upgrade(config, "head")
    finally:
        if previous_database_url is None:
            os.environ.pop("FURNACE_DATABASE_URL", None)
        else:
            os.environ["FURNACE_DATABASE_URL"] = previous_database_url
