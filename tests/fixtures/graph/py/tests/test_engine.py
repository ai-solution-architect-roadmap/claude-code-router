from app.core.engine import Engine


def test_run():
    assert Engine().run([]) == 0
