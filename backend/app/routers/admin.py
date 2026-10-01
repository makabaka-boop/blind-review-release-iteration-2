from fastapi import APIRouter, Depends, HTTPException
from psycopg2.extras import RealDictCursor

from ..db import get_conn, put_conn
from ..schemas import (
    AssignmentIn,
    ConflictIn,
    CreateReviewerIn,
    CreateRoundIn,
    ManuscriptsIn,
    ReviewerIdsIn,
)
from ..security import hash_password, require_admin
from ..serializers import serialize_round

router = APIRouter(prefix="/api/admin", dependencies=[Depends(require_admin)])

MAX_MANUSCRIPTS = 20
MAX_REVIEWERS = 10
MAX_ASSIGNMENTS_PER_REVIEWER = 3


def _get_round_locked(cur, round_id: int) -> dict | None:
    cur.execute("SELECT id, name, status FROM rounds WHERE id = %s FOR UPDATE", (round_id,))
    return cur.fetchone()


@router.post("/rounds")
def create_round(body: CreateRoundIn):
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("INSERT INTO rounds (name) VALUES (%s) RETURNING id, name, status", (body.name,))
            row = cur.fetchone()
        conn.commit()
        return {"id": row["id"], "name": row["name"], "status": row["status"]}
    finally:
        put_conn(conn)


@router.get("/rounds")
def list_rounds():
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT r.id, r.name, r.status,
                       (SELECT count(*) FROM manuscripts m WHERE m.round_id = r.id) AS manuscript_count,
                       (SELECT count(*) FROM assignments a WHERE a.round_id = r.id) AS assignment_count,
                       (SELECT count(*) FROM assignments a
                        WHERE a.round_id = r.id AND a.status = 'submitted') AS submitted_count
                FROM rounds r ORDER BY r.id
                """
            )
            rows = cur.fetchall()
        return [dict(r) for r in rows]
    finally:
        put_conn(conn)


@router.get("/rounds/{round_id}")
def get_round(round_id: int):
    conn = get_conn()
    try:
        payload = serialize_round(conn, round_id, full=True)
        if payload is None:
            raise HTTPException(status_code=404, detail="round not found")
        return payload
    finally:
        put_conn(conn)


@router.post("/rounds/{round_id}/manuscripts")
def add_manuscripts(round_id: int, body: ManuscriptsIn):
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            rnd = _get_round_locked(cur, round_id)
            if rnd is None:
                raise HTTPException(status_code=404, detail="round not found")
            if rnd["status"] == "frozen":
                raise HTTPException(status_code=409, detail="round is frozen")
            cur.execute("SELECT count(*) AS c FROM manuscripts WHERE round_id = %s", (round_id,))
            existing = cur.fetchone()["c"]
            if existing + len(body.manuscripts) > MAX_MANUSCRIPTS:
                raise HTTPException(
                    status_code=400,
                    detail=f"a round can contain at most {MAX_MANUSCRIPTS} manuscripts",
                )
            added = []
            for item in body.manuscripts:
                number = existing + len(added) + 1
                cur.execute(
                    "INSERT INTO manuscripts (round_id, number, title, author) "
                    "VALUES (%s, %s, %s, %s) RETURNING id, number",
                    (round_id, number, item.title, item.author),
                )
                row = cur.fetchone()
                added.append({"id": row["id"], "number": row["number"], "title": item.title})
        conn.commit()
        return {"manuscripts": added}
    finally:
        put_conn(conn)


@router.post("/reviewers")
def create_reviewer(body: CreateReviewerIn):
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT 1 FROM users WHERE username = %s", (body.username,))
            if cur.fetchone() is not None:
                raise HTTPException(status_code=400, detail="username already exists")
            cur.execute(
                "INSERT INTO users (username, password, role) VALUES (%s, %s, 'reviewer') "
                "RETURNING id, username",
                (body.username, hash_password(body.password)),
            )
            row = cur.fetchone()
        conn.commit()
        return {"id": row["id"], "username": row["username"], "role": "reviewer"}
    finally:
        put_conn(conn)


@router.get("/reviewers")
def list_reviewers():
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT id, username FROM users WHERE role = 'reviewer' ORDER BY id"
            )
            rows = cur.fetchall()
        return [dict(r) for r in rows]
    finally:
        put_conn(conn)


@router.post("/rounds/{round_id}/reviewers")
def add_reviewers(round_id: int, body: ReviewerIdsIn):
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            rnd = _get_round_locked(cur, round_id)
            if rnd is None:
                raise HTTPException(status_code=404, detail="round not found")
            if rnd["status"] == "frozen":
                raise HTTPException(status_code=409, detail="round is frozen")
            cur.execute("SELECT count(*) AS c FROM round_reviewers WHERE round_id = %s", (round_id,))
            existing = cur.fetchone()["c"]
            cur.execute(
                "SELECT count(*) AS c FROM unnest(%s::int[]) AS uid "
                "JOIN users u ON u.id = uid AND u.role = 'reviewer'",
                (body.reviewer_ids,),
            )
            valid = cur.fetchone()["c"]
            if valid != len(set(body.reviewer_ids)):
                raise HTTPException(status_code=400, detail="one or more reviewer ids do not exist")
            cur.execute(
                "SELECT count(*) AS c FROM unnest(%s::int[]) AS uid "
                "WHERE EXISTS (SELECT 1 FROM round_reviewers rr "
                "              WHERE rr.round_id = %s AND rr.user_id = uid)",
                (body.reviewer_ids, round_id),
            )
            already = cur.fetchone()["c"]
            if existing + len(set(body.reviewer_ids)) - already > MAX_REVIEWERS:
                raise HTTPException(
                    status_code=400,
                    detail=f"a round can have at most {MAX_REVIEWERS} reviewers",
                )
            cur.executemany(
                "INSERT INTO round_reviewers (round_id, user_id) VALUES (%s, %s) "
                "ON CONFLICT DO NOTHING",
                [(round_id, uid) for uid in set(body.reviewer_ids)],
            )
        conn.commit()
        return {"added": sorted(set(body.reviewer_ids))}
    finally:
        put_conn(conn)


def _validate_round_manuscript(cur, round_id: int, manuscript_id: int) -> dict:
    cur.execute(
        "SELECT id, number FROM manuscripts WHERE id = %s AND round_id = %s",
        (manuscript_id, round_id),
    )
    row = cur.fetchone()
    if row is None:
        raise HTTPException(status_code=400, detail="manuscript does not belong to this round")
    return row


def _validate_round_reviewer(cur, round_id: int, reviewer_id: int) -> dict:
    cur.execute(
        """
        SELECT u.id, u.username FROM users u
        JOIN round_reviewers rr ON rr.user_id = u.id
        WHERE u.id = %s AND rr.round_id = %s AND u.role = 'reviewer'
        """,
        (reviewer_id, round_id),
    )
    row = cur.fetchone()
    if row is None:
        raise HTTPException(status_code=400, detail="reviewer is not enrolled in this round")
    return row


@router.post("/rounds/{round_id}/conflicts")
def add_conflict(round_id: int, body: ConflictIn):
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            rnd = _get_round_locked(cur, round_id)
            if rnd is None:
                raise HTTPException(status_code=404, detail="round not found")
            if rnd["status"] == "frozen":
                raise HTTPException(status_code=409, detail="round is frozen")
            manuscript = _validate_round_manuscript(cur, round_id, body.manuscript_id)
            _validate_round_reviewer(cur, round_id, body.reviewer_id)
            cur.execute(
                "SELECT 1 FROM conflicts WHERE round_id = %s AND manuscript_id = %s AND reviewer_id = %s",
                (round_id, body.manuscript_id, body.reviewer_id),
            )
            if cur.fetchone() is not None:
                raise HTTPException(status_code=400, detail="conflict already recorded")
            cur.execute(
                "SELECT 1 FROM assignments WHERE manuscript_id = %s AND reviewer_id = %s",
                (body.manuscript_id, body.reviewer_id),
            )
            if cur.fetchone() is not None:
                raise HTTPException(
                    status_code=400,
                    detail="cannot record conflict: reviewer is already assigned to manuscript "
                    f"#{manuscript['number']}",
                )
            cur.execute(
                "INSERT INTO conflicts (round_id, manuscript_id, reviewer_id) VALUES (%s, %s, %s)",
                (round_id, body.manuscript_id, body.reviewer_id),
            )
        conn.commit()
        return {"ok": True}
    finally:
        put_conn(conn)


@router.delete("/rounds/{round_id}/conflicts/{manuscript_id}/{reviewer_id}")
def remove_conflict(round_id: int, manuscript_id: int, reviewer_id: int):
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            rnd = _get_round_locked(cur, round_id)
            if rnd is None:
                raise HTTPException(status_code=404, detail="round not found")
            if rnd["status"] == "frozen":
                raise HTTPException(status_code=409, detail="round is frozen")
            cur.execute(
                "DELETE FROM conflicts WHERE round_id = %s AND manuscript_id = %s AND reviewer_id = %s",
                (round_id, manuscript_id, reviewer_id),
            )
            if cur.rowcount == 0:
                raise HTTPException(status_code=404, detail="conflict not found")
        conn.commit()
        return {"ok": True}
    finally:
        put_conn(conn)


@router.post("/rounds/{round_id}/assignments")
def add_assignment(round_id: int, body: AssignmentIn):
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            rnd = _get_round_locked(cur, round_id)
            if rnd is None:
                raise HTTPException(status_code=404, detail="round not found")
            if rnd["status"] == "frozen":
                raise HTTPException(status_code=409, detail="round is frozen")
            manuscript = _validate_round_manuscript(cur, round_id, body.manuscript_id)
            reviewer = _validate_round_reviewer(cur, round_id, body.reviewer_id)
            cur.execute(
                "SELECT 1 FROM conflicts WHERE round_id = %s AND manuscript_id = %s AND reviewer_id = %s",
                (round_id, body.manuscript_id, body.reviewer_id),
            )
            if cur.fetchone() is not None:
                raise HTTPException(
                    status_code=400,
                    detail=f"conflict of interest: cannot assign manuscript #{manuscript['number']} "
                    f"to reviewer {reviewer['username']}",
                )
            cur.execute(
                "SELECT count(*) AS c FROM assignments WHERE round_id = %s AND reviewer_id = %s",
                (round_id, body.reviewer_id),
            )
            count = cur.fetchone()["c"]
            if count >= MAX_ASSIGNMENTS_PER_REVIEWER:
                raise HTTPException(
                    status_code=400,
                    detail=f"reviewer {reviewer['username']} already has "
                    f"{MAX_ASSIGNMENTS_PER_REVIEWER} assignments in this round",
                )
            cur.execute(
                "SELECT 1 FROM assignments WHERE manuscript_id = %s AND reviewer_id = %s",
                (body.manuscript_id, body.reviewer_id),
            )
            if cur.fetchone() is not None:
                raise HTTPException(status_code=400, detail="assignment already exists")
            cur.execute(
                "INSERT INTO assignments (round_id, manuscript_id, reviewer_id) "
                "VALUES (%s, %s, %s) RETURNING id",
                (round_id, body.manuscript_id, body.reviewer_id),
            )
            assignment_id = cur.fetchone()["id"]
        conn.commit()
        return {"id": assignment_id, "manuscript_id": body.manuscript_id, "reviewer_id": body.reviewer_id}
    finally:
        put_conn(conn)


@router.delete("/rounds/{round_id}/assignments/{assignment_id}")
def remove_assignment(round_id: int, assignment_id: int):
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            rnd = _get_round_locked(cur, round_id)
            if rnd is None:
                raise HTTPException(status_code=404, detail="round not found")
            if rnd["status"] == "frozen":
                raise HTTPException(status_code=409, detail="round is frozen")
            cur.execute(
                "DELETE FROM assignments WHERE id = %s AND round_id = %s AND status = 'draft'",
                (assignment_id, round_id),
            )
            if cur.rowcount == 0:
                cur.execute(
                    "SELECT 1 FROM assignments WHERE id = %s AND round_id = %s",
                    (assignment_id, round_id),
                )
                if cur.fetchone() is None:
                    raise HTTPException(status_code=404, detail="assignment not found")
                raise HTTPException(status_code=400, detail="cannot remove a submitted assignment")
        conn.commit()
        return {"ok": True}
    finally:
        put_conn(conn)


@router.post("/rounds/{round_id}/freeze")
def freeze_round(round_id: int):
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            rnd = _get_round_locked(cur, round_id)
            if rnd is None:
                raise HTTPException(status_code=404, detail="round not found")
            if rnd["status"] == "frozen":
                raise HTTPException(status_code=409, detail="round is already frozen")
            cur.execute(
                "SELECT count(*) AS total, "
                "count(*) FILTER (WHERE status = 'submitted') AS submitted "
                "FROM assignments WHERE round_id = %s",
                (round_id,),
            )
            stats = cur.fetchone()
            if stats["total"] == 0:
                raise HTTPException(status_code=409, detail="round has no assignments")
            if stats["submitted"] != stats["total"]:
                raise HTTPException(
                    status_code=409,
                    detail=f"not all assignments are submitted "
                    f"({stats['submitted']}/{stats['total']})",
                )
            cur.execute(
                "UPDATE rounds SET status = 'frozen' WHERE id = %s RETURNING status",
                (round_id,),
            )
        conn.commit()
        payload = serialize_round(conn, round_id, full=True)
        return payload
    finally:
        put_conn(conn)
