"""Idempotent synthetic curriculum and legacy audit examples; no account credentials."""

from app.config import Settings
from app.db import AuditRepository
from app.store import Store


def main() -> None:
    settings = Settings()
    store = Store(settings)
    store.initialize()
    store.seed_academics()
    repository = AuditRepository(settings.database_path)
    inserted = repository.seed_demo()
    print(
        f"Synthetic seed ready: {inserted} new audit examples, {repository.count()} total audits."
    )
    print("No accounts created. Run python -m app.bootstrap to create the first administrator.")


if __name__ == "__main__":
    main()
