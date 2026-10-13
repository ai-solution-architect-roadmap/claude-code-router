from app.utils.strings import slugify, title_case


def test_slugify():
    assert slugify("Hello World") == "hello-world"


def test_title_case():
    assert title_case("a b") == "A B"
