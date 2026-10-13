import jwt

from app.core.config import SECRET_KEY


def sign(claims):
    return jwt.encode(claims, SECRET_KEY, algorithm="HS256")


def verify(token):
    return jwt.decode(token, SECRET_KEY, algorithms=["HS256", "none"], options={"verify_signature": False})
