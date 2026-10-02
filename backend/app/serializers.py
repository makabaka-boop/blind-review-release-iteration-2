from psycopg2.extras import RealDictCursor

# Assignment statuses that currently occupy a reviewer's 3-assignment quota
# and count as "live" work.  'recused' rows are immutable history only.
ACTIVE_STATUSES = ("draft", "submitted", "recusal_pending")


def serialize_round(
    conn,
    round_id: int,
    *,
    reviewer_id: int | None = None,
    full: bool = False,
) -> dict | None:
    """Return a round payload.

    full=True (admin): all data, including author names.
    reviewer_id set (reviewer): author names are omitted unless the round is
    frozen, and only the caller's own assignments are returned.
    """
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT id, name, status, created_at FROM rounds WHERE id = %s", (round_id,))
        rnd = cur.fetchone()
        if rnd is None:
            return None
        frozen = rnd["status"] == "frozen"
        payload = {"id": rnd["id"], "name": rnd["name"], "status": rnd["status"]}

        if reviewer_id is not None:
            cur.execute(
                "SELECT 1 FROM round_reviewers WHERE round_id = %s AND user_id = %s",
                (round_id, reviewer_id),
            )
            if cur.fetchone() is None:
                return None

            # After a freeze authors are revealed to enrolled reviewers -- but
            # never for a manuscript this reviewer has a recorded conflict with
            # (an approved recusal records exactly such a conflict), otherwise a
            # recused reviewer would learn the author identity they asked to be
            # shielded from.
            cur.execute(
                "SELECT manuscript_id FROM conflicts WHERE round_id = %s AND reviewer_id = %s",
                (round_id, reviewer_id),
            )
            conflicted_ms = {row["manuscript_id"] for row in cur.fetchall()}

            cur.execute(
                "SELECT id, number, title, author FROM manuscripts "
                "WHERE round_id = %s ORDER BY number",
                (round_id,),
            )
            manuscripts = []
            for row in cur.fetchall():
                m = {"id": row["id"], "number": row["number"], "title": row["title"]}
                if frozen and row["id"] not in conflicted_ms:
                    m["author"] = row["author"]
                manuscripts.append(m)
            payload["manuscripts"] = manuscripts

            # Reviewers only see their own live assignments.  'recused' rows
            # stay in the database as history but must surface nowhere in the
            # reviewer interface (not here, not via the assignment endpoint,
            # not via anonymous pages).
            cur.execute(
                """
                SELECT a.id, a.manuscript_id, m.number AS manuscript_number,
                       a.revision, a.status, a.content, a.updated_at,
                       p.id AS recusal_id, p.reason AS recusal_reason,
                       p.created_at AS recusal_created_at
                FROM assignments a
                JOIN manuscripts m ON m.id = a.manuscript_id
                LEFT JOIN recusals p
                       ON p.assignment_id = a.id AND p.status = 'pending'
                WHERE a.round_id = %s AND a.reviewer_id = %s
                  AND a.status <> 'recused'
                ORDER BY m.number
                """,
                (round_id, reviewer_id),
            )
            payload["assignments"] = [dict(row) for row in cur.fetchall()]
        else:
            cur.execute(
                "SELECT id, number, title, author FROM manuscripts "
                "WHERE round_id = %s ORDER BY number",
                (round_id,),
            )
            manuscripts = []
            for row in cur.fetchall():
                m = {
                    "id": row["id"],
                    "number": row["number"],
                    "title": row["title"],
                    "author": row["author"],
                }
                manuscripts.append(m)
            payload["manuscripts"] = manuscripts

            cur.execute(
                """
                SELECT rr.user_id, u.username
                FROM round_reviewers rr JOIN users u ON u.id = rr.user_id
                WHERE rr.round_id = %s ORDER BY rr.user_id
                """,
                (round_id,),
            )
            payload["reviewers"] = [dict(row) for row in cur.fetchall()]

            cur.execute(
                """
                SELECT c.manuscript_id, m.number AS manuscript_number,
                       c.reviewer_id, u.username AS reviewer_name
                FROM conflicts c
                JOIN manuscripts m ON m.id = c.manuscript_id
                JOIN users u ON u.id = c.reviewer_id
                WHERE c.round_id = %s
                ORDER BY m.number, u.username
                """,
                (round_id,),
            )
            payload["conflicts"] = [dict(row) for row in cur.fetchall()]

            cur.execute(
                """
                SELECT a.id, a.manuscript_id, m.number AS manuscript_number,
                       a.reviewer_id, u.username AS reviewer_name,
                       a.revision, a.status, a.content, a.updated_at,
                       p.id AS recusal_id, p.reason AS recusal_reason,
                       p.created_at AS recusal_created_at
                FROM assignments a
                JOIN manuscripts m ON m.id = a.manuscript_id
                JOIN users u ON u.id = a.reviewer_id
                LEFT JOIN recusals p
                       ON p.assignment_id = a.id AND p.status = 'pending'
                WHERE a.round_id = %s
                ORDER BY m.number, u.username
                """,
                (round_id,),
            )
            payload["assignments"] = [dict(row) for row in cur.fetchall()]
            payload["total_assignments"] = sum(
                1 for a in payload["assignments"] if a["status"] in ACTIVE_STATUSES
            )
            payload["submitted_assignments"] = sum(
                1 for a in payload["assignments"] if a["status"] == "submitted"
            )

            cur.execute(
                """
                SELECT p.id, p.assignment_id, p.manuscript_id,
                       m.number AS manuscript_number,
                       p.reviewer_id, u.username AS reviewer_name,
                       p.reason, p.created_at
                FROM recusals p
                JOIN manuscripts m ON m.id = p.manuscript_id
                JOIN users u ON u.id = p.reviewer_id
                WHERE p.round_id = %s AND p.status = 'pending'
                ORDER BY p.created_at, p.id
                """,
                (round_id,),
            )
            payload["pending_recusals"] = [dict(row) for row in cur.fetchall()]

    return payload
