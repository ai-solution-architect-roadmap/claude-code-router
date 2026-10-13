from app.api.pagination import paginate
from app.auth.session import current_user
from app.core.engine import Engine
from app.payments.ledger import Ledger
from app.utils.strings import title_case


def list_orders(request, store):
    user = current_user(request)
    if user is None:
        return {"error": "unauthorized"}
    page = int(request.query.get("page", 0))
    return {"user": title_case(user), "orders": paginate(store.orders(user), page)}


def create_order(request, store):
    engine = Engine(store)
    return engine.run([request.json])


def refund(request, store):
    ledger = Ledger(store)
    ledger.record_refund(request.json["order"], request.json["amount"])
    return {"ok": True}
