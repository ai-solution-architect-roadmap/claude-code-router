import os

SECRET_KEY = os.environ.get("SHOP_SECRET", "dev")
PAGE_SIZE = 20


def get_setting(name, default=None):
    return os.environ.get(name, default)
