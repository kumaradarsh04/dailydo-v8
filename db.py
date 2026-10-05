"""
DailyDo Postgres storage layer.

The important MVP rule is:

    anonymous visitor -> no database tree
    signed-in user   -> tree belongs to that user

The browser never decides which user owns a tree. The backend resolves
ownership from the authenticated session.
"""

import os
import json

from datetime import date
from urllib.parse import quote_plus

from dotenv import load_dotenv

import psycopg2
import psycopg2.pool


load_dotenv()


def _build_database_url():
    url = os.environ.get("DATABASE_URL", "")

    if url:
        return url

    host = os.environ.get("SUPABASE_DB_HOST", "")
    port = os.environ.get("SUPABASE_DB_PORT", "5432")
    name = os.environ.get("SUPABASE_DB_NAME", "")
    user = os.environ.get("SUPABASE_DB_USER", "")
    password = os.environ.get("SUPABASE_DB_PASSWORD", "")

    if not (host and name and user and password):
        return ""

    return (
        f"postgresql://{user}:{quote_plus(password)}"
        f"@{host}:{port}/{name}?sslmode=require"
    )


DATABASE_URL = _build_database_url()

FREE_ATTEMPTS_LIMIT = 3

_pool = None


def init_pool():
    global _pool

    if not DATABASE_URL:
        return

    if _pool is not None:
        return

    _pool = psycopg2.pool.SimpleConnectionPool(
        1,
        5,
        dsn=DATABASE_URL
    )


def _require_pool():
    if _pool is None:
        raise RuntimeError(
            "Database is not configured (DATABASE_URL missing)"
        )


def init_db():
    """
    Creates the tables and makes sure the tree column exists.

    The ALTER TABLE is intentional: your existing Supabase database may
    already have the users table from the previous MVP.
    """
    if not _pool:
        return

    conn = _pool.getconn()

    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id SERIAL PRIMARY KEY,
                    google_id TEXT NOT NULL UNIQUE,
                    email TEXT UNIQUE NOT NULL,
                    name TEXT,
                    picture TEXT,
                    tree TEXT DEFAULT NULL,
                    created_at TIMESTAMPTZ DEFAULT NOW() NOT NULL,
                    last_login TIMESTAMPTZ DEFAULT NOW() NOT NULL,
                    last_updated TIMESTAMPTZ DEFAULT NOW() NOT NULL
                );
            """)

            # Safe for an existing table that was created before tree existed.
            cur.execute("""
                ALTER TABLE users
                ADD COLUMN IF NOT EXISTS tree TEXT DEFAULT NULL;
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    expires_at TIMESTAMPTZ NOT NULL
                );
            """)

        conn.commit()

    finally:
        _pool.putconn(conn)


def user_exists(google_id):
    _require_pool()

    conn = _pool.getconn()

    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM users WHERE google_id=%s",
                (google_id,)
            )
            return bool(cur.fetchone())

    finally:
        _pool.putconn(conn)


def create_user(google_id, email, name, picture):
    """
    Create the user if new.

    If the user already exists, update profile information and last_login.
    This does NOT overwrite the tree.
    """
    _require_pool()

    conn = _pool.getconn()

    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO users (
                    google_id,
                    email,
                    name,
                    picture,
                    last_login,
                    last_updated
                )
                VALUES (%s, %s, %s, %s, NOW(), NOW())
                ON CONFLICT (google_id)
                DO UPDATE SET
                    email = EXCLUDED.email,
                    name = EXCLUDED.name,
                    picture = EXCLUDED.picture,
                    last_login = NOW(),
                    last_updated = NOW();
            """, (
                google_id,
                email,
                name,
                picture
            ))

        conn.commit()

    finally:
        _pool.putconn(conn)


def get_user_id(google_id):
    _require_pool()

    conn = _pool.getconn()

    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT user_id FROM users WHERE google_id=%s",
                (google_id,)
            )
            row = cur.fetchone()

        return row[0] if row else None

    finally:
        _pool.putconn(conn)


def get_user(user_id):
    _require_pool()

    conn = _pool.getconn()

    try:
        with conn.cursor() as cur:
            # Existing MVP sessions may have user_id stored as TEXT.
            cur.execute(
                """
                SELECT google_id, email, name, picture
                FROM users
                WHERE user_id=%s::integer
                """,
                (str(user_id),)
            )
            return cur.fetchone()

    finally:
        _pool.putconn(conn)


def create_session(session_id, user_id):
    _require_pool()

    conn = _pool.getconn()

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO sessions (
                    id,
                    user_id,
                    expires_at
                )
                VALUES (
                    %s,
                    %s,
                    NOW() + INTERVAL '30 days'
                )
                """,
                (
                    session_id,
                    str(user_id)
                )
            )

        conn.commit()

    finally:
        _pool.putconn(conn)


def get_session(session_id):
    _require_pool()

    conn = _pool.getconn()

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT user_id
                FROM sessions
                WHERE id=%s
                  AND expires_at>NOW()
                """,
                (session_id,)
            )
            row = cur.fetchone()

        return row[0] if row else None

    finally:
        _pool.putconn(conn)


def delete_session(session_id):
    _require_pool()

    conn = _pool.getconn()

    try:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM sessions WHERE id=%s",
                (session_id,)
            )

        conn.commit()

    finally:
        _pool.putconn(conn)


def get_user_tree(user_id):
    """
    Return the user's saved tree.

    Empty/new account -> [].
    """
    _require_pool()

    conn = _pool.getconn()

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT tree
                FROM users
                WHERE user_id=%s::integer
                """,
                (str(user_id),)
            )
            row = cur.fetchone()

        if not row or not row[0]:
            return []

        raw_tree = row[0]

        if isinstance(raw_tree, list):
            return raw_tree

        try:
            parsed = json.loads(raw_tree)
            return parsed if isinstance(parsed, list) else []
        except (TypeError, json.JSONDecodeError):
            return []

    finally:
        _pool.putconn(conn)


def save_user_tree(user_id, tree):
    """
    Replace the user's complete tree.

    For this MVP the whole tree is stored as JSON text in users.tree.
    Later, if DailyDo needs multiple plans/history, this can become a
    separate plans table without changing the frontend contract.
    """
    _require_pool()

    serialized = json.dumps(
        tree,
        ensure_ascii=False,
        separators=(",", ":")
    )

    conn = _pool.getconn()

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE users
                SET tree=%s,
                    last_updated=NOW()
                WHERE user_id=%s::integer
                """,
                (
                    serialized,
                    str(user_id)
                )
            )

            if cur.rowcount != 1:
                raise ValueError("User does not exist")

        conn.commit()

    finally:
        _pool.putconn(conn)


def insert_user_tree(google_id, tree):
    """
    Backwards-compatible helper for any old code that still calls this.
    New code should use save_user_tree(user_id, tree).
    """
    user_id = get_user_id(google_id)

    if user_id is None:
        return

    save_user_tree(user_id, tree)


def get_user_status(user_id):
    """
    Keep this endpoint useful for the current UI/payment work.

    Your current users schema no longer relies on the old email-based
    premium fields, so for now return identity information.
    """
    user = get_user(user_id)

    if not user:
        return None

    return {
        "google_id": user[0],
        "email": user[1],
        "name": user[2],
        "picture": user[3]
    }


# These are retained for compatibility with the previous MVP.
# Anonymous /organize currently does not use them.
def is_allowed_to_organize(email):
    return True


def record_attempt(email):
    # The current users table does not have the old attempts column.
    # Usage/rate limiting should be added as a deliberate account/anonymous
    # rate-limit system rather than silently reintroducing email-based state.
    return True
