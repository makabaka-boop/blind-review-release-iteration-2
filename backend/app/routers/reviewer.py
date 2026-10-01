from fastapi import APIRouter, Body, Depends, HTTPException
from psycopg2.extras import RealDictCursor

from ..db import get_conn, put_conn
from ..schemas import DraftIn
from ..security import require_reviewer
from ..serializers import serialize_round

router = APIRouter(prefix="/api/reviewer", dependencies=[Depends(require_reviewer)])


def _lock_enrolled_round(cur, round_id: int, reviewer_id: int) -> dict:
    """Lock the round row and verify the reviewer is enrolled (404 otherwise)."""
    cur.execute(
        """
        SELECT r.id, r.name, r.status FROM rounds r
        JOIN round_reviewers rr ON rr.round_id = r.id AND rr.user_id = %s
        WHERE r.id = %s
        FOR UPDATE OF r
        """,
        (reviewer_id, round_id),
    )
    rnd = cur.fetchone()
    if rnd is None:
        raise HTTPException(status_code=404, detail="round not found")
    return rnd


def _get_assignment(cur, assignment_id: int, reviewer_id: int, for_update: bool = False) -> dict | None:
    lock = " FOR UPDATE OF a" if for_update else ""
    cur.execute(
        f"""
        SELECT a.*, m.number AS manuscript_number, r.status AS round_status
        FROM assignments a
        JOIN manuscripts m ON m.id = a.manuscript_id
        JOIN rounds r ON r.id = a.round_id
        WHERE a.id = %s AND a.reviewer_id = %s
        {lock}
        """,
        (assignment_id, reviewer_id),
    )
    return cur.fetchone()


@router.get("/rounds")
def list_my_rounds(user: dict = Depends(require_reviewer)):
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT r.id, r.name, r.status,
                       count(a.*) AS assignment_count,
                       count(a.*) FILTER (WHERE a.status = 'submitted') AS submitted_count
                FROM rounds r
                JOIN round_reviewers rr ON rr.round_id = r.id AND rr.user_id = %s
                LEFT JOIN assignments a ON a.round_id = r.id AND a.reviewer_id = rr.user_id
                GROUP BY r.id, r.name, r.status
                ORDER BY r.id
                """,
                (user["id"],),
            )
            rows = cur.fetchall()
        return [dict(r) for r in rows]
    finally:
        put_conn(conn)


@router.get("/rounds/{round_id}")
def get_my_round(round_id: int, user: dict = Depends(require_reviewer)):
    conn = get_conn()
    try:
        payload = serialize_round(conn, round_id, reviewer_id=user["id"])
        if payload is None:
            raise HTTPException(status_code=404, detail="round not found")
        return payload
    finally:
        put_conn(conn)


@router.get("/rounds/{round_id}/manuscripts/{manuscript_id}")
def get_manuscript(round_id: int, manuscript_id: int, user: dict = Depends(require_reviewer)):
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT 1 FROM round_reviewers WHERE round_id = %s AND user_id = %s",
                (round_id, user["id"]),
            )
            if cur.fetchone() is None:
                raise HTTPException(status_code=404, detail="manuscript not found")
            # Reviewers may only read manuscripts actually assigned to them.
            cur.execute(
                """
                SELECT m.id, m.number, m.title, m.author, r.status AS round_status
                FROM manuscripts m
                JOIN rounds r ON r.id = m.round_id
                JOIN assignments a ON a.manuscript_id = m.id AND a.reviewer_id = %s
                WHERE m.id = %s AND m.round_id = %s
                """,
                (user["id"], manuscript_id, round_id),
            )
            row = cur.fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="manuscript not found")
            result = {"id": row["id"], "number": row["number"], "title": row["title"]}
            if row["round_status"] == "frozen":
                result["author"] = row["author"]
            return result
    finally:
        put_conn(conn)


@router.get("/assignments/{assignment_id}")
def get_assignment(assignment_id: int, user: dict = Depends(require_reviewer)):
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            assignment = _get_assignment(cur, assignment_id, user["id"])
            if assignment is None:
                raise HTTPException(status_code=404, detail="assignment not found")
            return {
                "id": assignment["id"],
                "round_id": assignment["round_id"],
                "manuscript_id": assignment["manuscript_id"],
                "manuscript_number": assignment["manuscript_number"],
                "revision": assignment["revision"],
                "status": assignment["status"],
                "content": assignment["content"],
                "updated_at": assignment["updated_at"],
                "round_status": assignment["round_status"],
            }
    finally:
        put_conn(conn)


def _lock_for_write(cur, assignment_id: int, reviewer_id: int) -> tuple[dict, dict]:
    """Acquire write locks in the global order (round first, then assignment).

    Locks round row (also verifies enrollment via JOIN) and then the assignment
    row.  Keeping this order identical across reviewer and admin endpoints
    prevents lock-order deadlocks.
    """
    assignment = _get_assignment(cur, assignment_id, reviewer_id)
    if assignment is None:
        raise HTTPException(status_code=404, detail="assignment not found")
    rnd = _lock_enrolled_round(cur, assignment["round_id"], reviewer_id)
    locked = _get_assignment(cur, assignment_id, reviewer_id, for_update=True)
    return locked, rnd


@router.put("/assignments/{assignment_id}/draft")
def save_draft(assignment_id: int, body: DraftIn, user: dict = Depends(require_reviewer)):
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            assignment, rnd = _lock_for_write(cur, assignment_id, user["id"])
            if rnd["status"] == "frozen":
                raise HTTPException(status_code=409, detail="round is frozen")
            if assignment["status"] == "submitted":
                raise HTTPException(status_code=409, detail="review already submitted")
            if assignment["revision"] != body.expected_revision:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "message": "revision conflict",
                        "current_revision": assignment["revision"],
                    },
                )
            cur.execute(
                """
                UPDATE assignments
                SET content = %s, revision = revision + 1, updated_at = now()
                WHERE id = %s RETURNING revision, status, updated_at
                """,
                (body.content, assignment_id),
            )
            updated = cur.fetchone()
        conn.commit()
        return {
            "id": assignment_id,
            "revision": updated["revision"],
            "status": updated["status"],
            "updated_at": updated["updated_at"],
        }
    finally:
        put_conn(conn)


@router.post("/assignments/{assignment_id}/submit")
def submit_final(
    assignment_id: int,
    body: DraftIn | None = Body(default=None),
    user: dict = Depends(require_reviewer),
):
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            assignment, rnd = _lock_for_write(cur, assignment_id, user["id"])
            if rnd["status"] == "frozen":
                raise HTTPException(status_code=409, detail="round is frozen")
            if assignment["status"] == "submitted":
                raise HTTPException(status_code=409, detail="review already submitted")
            content = assignment["content"]
            revision = assignment["revision"]
            if body is not None:
                if assignment["revision"] != body.expected_revision:
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "message": "revision conflict",
                            "current_revision": assignment["revision"],
                        },
                    )
                content = body.content
                revision = assignment["revision"] + 1
            cur.execute(
                """
                UPDATE assignments
                SET content = %s, revision = %s, status = 'submitted', updated_at = now()
                WHERE id = %s RETURNING revision, status, updated_at
                """,
                (content, revision, assignment_id),
            )
            updated = cur.fetchone()
        conn.commit()
        return {
            "id": assignment_id,
            "revision": updated["revision"],
            "status": updated["status"],
            "updated_at": updated["updated_at"],
        }
    finally:
        put_conn(conn)
