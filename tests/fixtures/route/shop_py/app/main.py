from app.api import handlers
from app.core.engine import Engine

ROUTES = {
    "GET /orders": handlers.list_orders,
    "POST /orders": handlers.create_order,
    "POST /refunds": handlers.refund,
}


def build_engine(store):
    return Engine(store)
