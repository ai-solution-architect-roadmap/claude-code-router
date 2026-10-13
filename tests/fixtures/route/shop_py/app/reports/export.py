from app.core.engine import Engine
from app.utils.strings import slugify


def export_report(store, rows):
    engine = Engine(store)
    engine.run([{"kind": "report"}])
    lines = []
    for row in rows:
        lines.append(",".join(str(v) for v in row.values()))
    return slugify(rows[0].get("name", "report")) if rows else "", "\n".join(lines)
