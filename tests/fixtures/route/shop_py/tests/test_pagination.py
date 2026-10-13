from app.api.pagination import paginate


def test_first_page():
    assert paginate(list(range(5)), 0, size=2) == [0, 1]
