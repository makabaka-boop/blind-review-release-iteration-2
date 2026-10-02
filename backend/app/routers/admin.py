from fastapi import APIRouter, Depends, HTTPException
from psycopg2.extras import RealDictCursor

from ..db import get_conn, put_conn
from ..schemas import (
    AssignmentIn,
    ConflictIn,
    CreateReviewerIn,
    CreateRoundIn,
    ManuscriptsIn,
    RecusalDecisionIn,
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
                       (SELECT count(*) FROM assignments a
                        WHERE a.round_id = r.id AND a.status <> 'recused') AS assignment_count,
                       (SELECT count(*) FROM assignments a
                        WHERE a.round_id = r.id AND a.status = 'submitted') AS submitted_count,
                       (SELECT count(*) FROM assignments a
                        WHERE a.round_id = r.id AND a.status = 'recusal_pending') AS recusal_pending_count
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
                "SELECT 1 FROM assignments "
                "WHERE manuscript_id = %s AND reviewer_id = %s AND status <> 'recused'",
                (body.manuscript_id, body.reviewer_id),
            )
            if cur.fetchone() is not None:
                raise HTTPException(
                    status_code=400,
                    detail="cannot record conflict: reviewer is currently assigned to manuscript "
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
                "SELECT count(*) AS c FROM assignments "
                "WHERE round_id = %s AND reviewer_id = %s AND status <> 'recused'",
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
                "SELECT status FROM assignments WHERE manuscript_id = %s AND reviewer_id = %s",
                (body.manuscript_id, body.reviewer_id),
            )
            existing = cur.fetchone()
            if existing is not None:
                if existing["status"] == "recused":
                    raise HTTPException(
                        status_code=400,
                        detail="reviewer previously recused from this manuscript",
                    )
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
                "SELECT status FROM assignments WHERE id = %s AND round_id = %s",
                (assignment_id, round_id),
            )
            row = cur.fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="assignment not found")
            if row["status"] == "submitted":
                raise HTTPException(status_code=400, detail="cannot remove a submitted assignment")
            if row["status"] == "recusal_pending":
                raise HTTPException(
                    status_code=400,
                    detail="cannot remove an assignment with a pending recusal request",
                )
            if row["status"] == "recused":
                raise HTTPException(
                    status_code=400, detail="cannot remove a recused assignment (history is retained)"
                )
            cur.execute(
                "DELETE FROM assignments WHERE id = %s AND round_id = %s AND status = 'draft'",
                (assignment_id, round_id),
            )
            if cur.rowcount == 0:
                raise HTTPException(status_code=400, detail="cannot remove this assignment")
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
                "SELECT count(*) AS c FROM assignments "
                "WHERE round_id = %s AND status = 'recusal_pending'",
                (round_id,),
            )
            pending_recusals = cur.fetchone()["c"]
            if pending_recusals:
                raise HTTPException(
                    status_code=409,
                    detail=f"cannot freeze: {pending_recusals} recusal request(s) pending",
                )
            cur.execute(
                "SELECT count(*) AS total, "
                "count(*) FILTER (WHERE status = 'submitted') AS submitted "
                "FROM assignments WHERE round_id = %s AND status <> 'recused'",
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


def _lock_pending_recusal(cur, round_id: int, recusal_id: int) -> dict:
    """Lock the round then fetch the pending recusal (and its assignment).

    Every mutating endpoint takes the round lock first, so a pending recusal is
    serialized against draft saves, submits, freezes and reassignments.
    """
    rnd = _get_round_locked(cur, round_id)
    if rnd is None:
        raise HTTPException(status_code=404, detail="round not found")
    if rnd["status"] == "frozen":
        raise HTTPException(status_code=409, detail="round is frozen")
    cur.execute(
        """
        SELECT rc.id, rc.assignment_id, rc.reason, rc.status,
               a.manuscript_id, a.reviewer_id, a.round_id
        FROM recusals rc
        JOIN assignments a ON a.id = rc.assignment_id
        WHERE rc.id = %s AND rc.status = 'pending'
        FOR UPDATE OF rc
        """,
        (recusal_id,),
    )
    row = cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="pending recusal not found")
    if row["round_id"] != round_id:
        # Do not leak that the recusal exists in another round.
        raise HTTPException(status_code=404, detail="pending recusal not found")
    return row


@router.post("/recusals/{recusal_id}/reject")
def reject_recusal(recusal_id: int):
    """Deny the request: assignment becomes a live, editable draft again with
    its former content and revision untouched."""
    # The round id is discovered from the recusal itself; the round lock is
    # taken inside _lock_pending_recusal before any row is read for update.
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT assignment_id FROM recusals WHERE id = %s", (recusal_id,))
            ref = cur.fetchone()
            if ref is None:
                raise HTTPException(status_code=404, detail="pending recusal not found")
            cur.execute("SELECT round_id FROM assignments WHERE id = %s", (ref["assignment_id"],))
            assignment_ref = cur.fetchone()
            if assignment_ref is None:
                raise HTTPException(status_code=404, detail="pending recusal not found")
            round_id = assignment_ref["round_id"]

            recusal = _lock_pending_recusal(cur, round_id, recusal_id)
            cur.execute(
                "UPDATE recusals SET status = 'rejected', decided_at = now() "
                "WHERE id = %s",
                (recusal_id,),
            )
            cur.execute(
                "UPDATE assignments SET status = 'draft', updated_at = now() "
                "WHERE id = %s RETURNING status",
                (recusal["assignment_id"],),
            )
            status = cur.fetchone()["status"]
        conn.commit()
        return {"id": recusal_id, "status": "rejected", "assignment_status": status}
    finally:
        put_conn(conn)


@router.post("/recusals/{recusal_id}/approve")
def approve_recusal(recusal_id: int, body: RecusalDecisionIn):
    """Approve recusal, record the conflict, and hand the manuscript to a
    replacement reviewer — all in one transaction.

    Any validation failure rolls the whole transaction back: the old
    assignment stays recusal_pending with its draft, and no slot is consumed.
    """
    if body.replacement_reviewer_id is None:
        raise HTTPException(status_code=400, detail="replacement_reviewer_id is required")
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT assignment_id FROM recusals WHERE id = %s", (recusal_id,))
            ref = cur.fetchone()
            if ref is None:
                raise HTTPException(status_code=404, detail="pending recusal not found")
            cur.execute("SELECT round_id FROM assignments WHERE id = %s", (ref["assignment_id"],))
            assignment_ref = cur.fetchone()
            if assignment_ref is None:
                raise HTTPException(status_code=404, detail="pending recusal not found")
            round_id = assignment_ref["round_id"]

            recusal = _lock_pending_recusal(cur, round_id, recusal_id)
            old_assignment_id = recusal["assignment_id"]
            manuscript_id = recusal["manuscript_id"]
            old_reviewer_id = recusal["reviewer_id"]
            new_reviewer_id = body.replacement_reviewer_id

            if new_reviewer_id == old_reviewer_id:
                raise HTTPException(
                    status_code=400,
                    detail="replacement reviewer must be different from the recusing reviewer",
                )
            manuscript = _validate_round_manuscript(cur, round_id, manuscript_id)
            reviewer = _validate_round_reviewer(cur, round_id, new_reviewer_id)
            cur.execute(
                "SELECT 1 FROM conflicts "
                "WHERE round_id = %s AND manuscript_id = %s AND reviewer_id = %s",
                (round_id, manuscript_id, new_reviewer_id),
            )
            if cur.fetchone() is not None:
                raise HTTPException(
                    status_code=400,
                    detail=f"conflict of interest: reviewer {reviewer['username']} is conflicted "
                    f"with manuscript #{manuscript['number']}",
                )
            # Live slots only: the recusing reviewer's pending assignment still
            # occupies one slot until this transaction commits, but the
            # replacement's count is what must stay under the cap.
            cur.execute(
                "SELECT count(*) AS c FROM assignments "
                "WHERE round_id = %s AND reviewer_id = %s AND status <> 'recused'",
                (round_id, new_reviewer_id),
            )
            if cur.fetchone()["c"] >= MAX_ASSIGNMENTS_PER_REVIEWER:
                raise HTTPException(
                    status_code=400,
                    detail=f"reviewer {reviewer['username']} already has "
                    f"{MAX_ASSIGNMENTS_PER_REVIEWER} assignments in this round",
                )
            cur.execute(
                "SELECT 1 FROM assignments WHERE manuscript_id = %s AND reviewer_id = %s",
                (manuscript_id, new_reviewer_id),
            )
            if cur.fetchone() is not None:
                raise HTTPException(
                    status_code=400, detail="replacement reviewer already has this manuscript"
                )

            # 1) close the old assignment as non-submittable history
            cur.execute(
                "UPDATE assignments SET status = 'recused', updated_at = now() "
                "WHERE id = %s RETURNING id",
                (old_assignment_id,),
            )
            # 2) record the conflict that backs the recusal
            cur.execute(
                "INSERT INTO conflicts (round_id, manuscript_id, reviewer_id) "
                "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                (round_id, manuscript_id, old_reviewer_id),
            )
            # 3) hand a brand-new blank assignment to the replacement reviewer
            cur.execute(
                "INSERT INTO assignments (round_id, manuscript_id, reviewer_id) "
                "VALUES (%s, %s, %s) RETURNING id",
                (round_id, manuscript_id, new_reviewer_id),
            )
            new_assignment_id = cur.fetchone()["id"]
            # 4) close the recusal and link the replacement
            cur.execute(
                "UPDATE recusals SET status = 'approved', decided_at = now(), "
                "replacement_assignment_id = %s WHERE id = %s",
                (new_assignment_id, recusal_id),
            )
        conn.commit()
        return {
            "id": recusal_id,
            "status": "approved",
            "old_assignment_id": old_assignment_id,
            "old_assignment_status": "recused",
            "replacement_assignment_id": new_assignment_id,
            "replacement_reviewer_id": new_reviewer_id,
            "conflict_recorded": True,
        }
    finally:
        put_conn(conn)
