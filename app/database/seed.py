"""Explicit, repeatable synthetic data seed: python -m app.database.seed."""

import argparse
from pathlib import Path

from sqlalchemy import Engine, text
from sqlmodel import Session, select

from app.config import Settings
from app.database.db import create_db_engine, init_db
from app.database.models import (
    DepartmentResource,
    ResourceCategory,
    ResourceRoom,
    ResourceSeedState,
)

CATEGORIES = ("Компьютерлік техника", "Көрсету жабдықтары", "Кеңсе техникасы")
ROOMS = ("Зертхана 201", "Дәрісхана 202", "Кафедра 203")
RESOURCES = (
    ("Оқу компьютері", "DEMO-PC-001", 0, 0, 12, 280_000.0),
    ("Мультимедиа проекторы", "DEMO-PRJ-001", 1, 1, 1, 190_000.0),
    ("Лазерлік принтер", "DEMO-PRN-001", 2, 2, 2, 85_000.0),
    ("Оқытушы ноутбугі", "DEMO-NB-001", 0, 2, 1, 350_000.0),
)


def seed_resources(engine: Engine) -> dict[str, int]:
    """Seed each database once; retain later edits and physical deletions."""
    init_db(engine)
    added = {"categories": 0, "rooms": 0, "resources": 0}
    with Session(engine) as session:
        # Serialize concurrent seed invocations without replacing existing rows.
        session.execute(text("BEGIN IMMEDIATE"))
        if session.get(ResourceSeedState, "department_resources_v1") is not None:
            return added
        categories: list[ResourceCategory] = []
        rooms: list[ResourceRoom] = []
        for name in CATEGORIES:
            category = session.exec(
                select(ResourceCategory).where(ResourceCategory.name == name)
            ).first()
            if category is None:
                category = ResourceCategory(name=name)
                session.add(category)
                added["categories"] += 1
            categories.append(category)
        for name in ROOMS:
            room = session.exec(select(ResourceRoom).where(ResourceRoom.name == name)).first()
            if room is None:
                room = ResourceRoom(name=name)
                session.add(room)
                added["rooms"] += 1
            rooms.append(room)
        session.flush()
        for name, code, category_index, room_index, quantity, cost in RESOURCES:
            existing = session.exec(
                select(DepartmentResource).where(DepartmentResource.inventory_code == code)
            ).first()
            if existing is None:
                category_id = categories[category_index].id
                room_id = rooms[room_index].id
                assert category_id is not None and room_id is not None
                session.add(
                    DepartmentResource(
                        name=name,
                        inventory_code=code,
                        category=categories[category_index],
                        room=rooms[room_index],
                        category_id=category_id,
                        room_id=room_id,
                        quantity=quantity,
                        unit_cost=cost,
                    )
                )
                added["resources"] += 1
        session.add(ResourceSeedState(name="department_resources_v1"))
        session.commit()
    return added


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, help="Use an isolated demonstration database")
    parser.add_argument("--echo", action="store_true", help="Show generated CREATE and INSERT SQL")
    args = parser.parse_args()
    engine = create_db_engine(args.database or Settings().database_path, echo=args.echo)
    try:
        print(seed_resources(engine))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
