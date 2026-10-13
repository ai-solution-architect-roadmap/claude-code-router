from app.core import Engine
from . import util


def main():
    e = Engine()
    print(util.clamp(5))
    return e.run([1, 2, 3])
