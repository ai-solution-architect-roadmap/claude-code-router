from app.auth.tokens import verify


def current_user(request):
    token = request.headers.get("Authorization", "").removeprefix("Bearer ")
    if not token:
        return None
    claims = verify(token)
    if claims.get("exp") is None:
        return None
    return claims.get("sub")
