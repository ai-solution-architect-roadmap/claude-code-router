from app.core.engine import Engine


def test_run_empty():
    assert Engine(None).run([]) == []
