import time

import stripe


def charge(amount, customer, retries=3):
    for attempt in range(retries):
        try:
            return stripe.Charge.create(amount=amount, customer=customer)
        except stripe.error.RateLimitError:
            time.sleep(2 ** attempt)
    return None
