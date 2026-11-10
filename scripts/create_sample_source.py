"""Create explicit synthetic source data for UI integration; never overwrite it."""

import sqlite3
from pathlib import Path

path = Path(__file__).resolve().parents[1] / "var/imports/ui-inventory.db"
path.parent.mkdir(parents=True, exist_ok=True)
if not path.exists():
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE inventory (id INTEGER PRIMARY KEY, product TEXT NOT NULL, quantity INTEGER NOT NULL)"
        )
        connection.executemany(
            "INSERT INTO inventory VALUES (?, ?, ?)", [(1, "UI联调：阀门", 80), (2, "UI联调：轴承", 20)]
        )
print(path)
