import os
import time

import psycopg2
from psycopg2.extras import RealDictCursor
from psycopg2.pool import ThreadedConnectionPool

_pool: ThreadedConnectionPool | None = None

DB_DSN = os.environ.get(
    "DATABASE_URL",
    "postgresql://review:review@db:5432/review",
)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS users (
    id          SERIAL PRIMARY KEY,
    username    TEXT NOT NULL UNIQUE,
    password    TEXT NOT NULL,
    role        TEXT NOT NULL CHECK (role IN ('admin', 'reviewer'))
);

CREATE TABLE IF NOT EXISTS rounds (
    id          SERIAL PRIMARY KEY,
    name        TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'frozen')),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS manuscripts (
    id          SERIAL PRIMARY KEY,
    round_id    INTEGER NOT NULL REFERENCES rounds(id) ON DELETE CASCADE,
    number      INTEGER NOT NULL,
    title       TEXT NOT NULL,
    author      TEXT NOT NULL,
    UNIQUE (round_id, number)
);

CREATE TABLE IF NOT EXISTS round_reviewers (
    round_id    INTEGER NOT NULL REFERENCES rounds(id) ON DELETE CASCADE,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    PRIMARY KEY (round_id, user_id)
);

CREATE TABLE IF NOT EXISTS conflicts (
    round_id    INTEGER NOT NULL REFERENCES rounds(id) ON DELETE CASCADE,
    manuscript_id INTEGER NOT NULL REFERENCES manuscripts(id) ON DELETE CASCADE,
    reviewer_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    PRIMARY KEY (round_id, manuscript_id, reviewer_id)
);

CREATE TABLE IF NOT EXISTS assignments (
    id          SERIAL PRIMARY KEY,
    round_id    INTEGER NOT NULL REFERENCES rounds(id) ON DELETE CASCADE,
    manuscript_id INTEGER NOT NULL REFERENCES manuscripts(id) ON DELETE CASCADE,
    reviewer_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    revision    INTEGER NOT NULL DEFAULT 0,
    status      TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'submitted')),
    content     TEXT NOT NULL DEFAULT '',
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (manuscript_id, reviewer_id)
);
"""


def init_pool() -> None:
    global _pool
    last = None
    for attempt in range(60):
        try:
            _pool = ThreadedConnectionPool(2, 20, dsn=DB_DSN, connect_timeout=5)
            break
        except psycopg2.OperationalError as exc:
            last = exc
            time.sleep(1)
    if _pool is None:
        raise RuntimeError(f"cannot connect to database: {last}")


def get_conn():
    assert _pool is not None, "pool not initialized"
    return _pool.getconn()


def put_conn(conn) -> None:
    assert _pool is not None
    # Discard any aborted/open transaction before the connection is reused.
    conn.rollback()
    _pool.putconn(conn)


def init_schema_and_seed() -> None:
    from .security import hash_password

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(SCHEMA_SQL)
            admin_user = os.environ.get("ADMIN_USER", "admin")
            admin_pass = os.environ.get("ADMIN_PASSWORD", "adminpass")
            cur.execute(
                "INSERT INTO users (username, password, role) VALUES (%s, %s, 'admin') "
                "ON CONFLICT (username) DO NOTHING",
                (admin_user, hash_password(admin_pass)),
            )
        conn.commit()
    finally:
        put_conn(conn)
