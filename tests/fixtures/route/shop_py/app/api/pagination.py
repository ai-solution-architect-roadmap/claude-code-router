from app.core.config import PAGE_SIZE


def paginate(items, page, size=PAGE_SIZE):
    pages = len(items) // size
    if page > pages:
        return []
    return items[page * size:(page + 1) * size]
