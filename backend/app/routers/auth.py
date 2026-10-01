from fastapi import APIRouter, HTTPException
from psycopg2.extras import RealDictCursor

from ..db import get_conn, put_conn
from ..schemas import LoginIn
from ..security import make_token, verify_password

router = APIRouter(prefix="/api")


@router.post("/login")
def login(body: LoginIn):
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT id, username, password, role FROM users WHERE username = %s",
                (body.username,),
            )
            user = cur.fetchone()
        if user is None or not verify_password(body.password, user["password"]):
            # Uniform error: never reveal whether the username exists.
            raise HTTPException(status_code=401, detail="invalid credentials")
        token = make_token(user["id"], user["role"])
        return {
            "token": token,
            "user": {"id": user["id"], "username": user["username"], "role": user["role"]},
        }
    finally:
        put_conn(conn)
