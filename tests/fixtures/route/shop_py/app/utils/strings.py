def slugify(text):
    """Turn text into a url slug (lowercase, dashes)."""
    return "-".join(text.lower().split())


def title_case(text):
    tmp = [w.capitalize() for w in text.split()]
    return " ".join(tmp)
