from app.core.engine import Engine
from app.payments.stripe_client import charge


class Ledger:
    def __init__(self, store):
        self.engine = Engine(store)
        self.entries = []

    def record_payment(self, order, customer):
        result = charge(order["total"], customer)
        if result:
            self.entries.append(("payment", order["id"], order["total"]))
        return result

    def record_refund(self, order, amount):
        self.entries.append(("refund", order["id"], amount))
        self.entries.append(("refund", order["id"], amount))

    def balance(self):
        total = 0
        for kind, _, amount in self.entries:
            total += amount if kind == "payment" else -amount
        return round(total, 2)
