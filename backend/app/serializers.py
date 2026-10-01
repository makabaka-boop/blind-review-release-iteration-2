from psycopg2.extras import RealDictCursor


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

        cur.execute(
            "SELECT id, number, title, author FROM manuscripts "
            "WHERE round_id = %s ORDER BY number",
            (round_id,),
        )
        manuscripts = []
        for row in cur.fetchall():
            m = {"id": row["id"], "number": row["number"], "title": row["title"]}
            if full or frozen:
                m["author"] = row["author"]
            manuscripts.append(m)
        payload["manuscripts"] = manuscripts

        if reviewer_id is not None:
            cur.execute(
                "SELECT 1 FROM round_reviewers WHERE round_id = %s AND user_id = %s",
                (round_id, reviewer_id),
            )
            if cur.fetchone() is None:
                return None
            cur.execute(
                """
                SELECT a.id, a.manuscript_id, m.number AS manuscript_number,
                       a.revision, a.status, a.content, a.updated_at
                FROM assignments a JOIN manuscripts m ON m.id = a.manuscript_id
                WHERE a.round_id = %s AND a.reviewer_id = %s
                ORDER BY m.number
                """,
                (round_id, reviewer_id),
            )
            payload["assignments"] = [dict(row) for row in cur.fetchall()]
        else:
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
                       a.revision, a.status, a.content, a.updated_at
                FROM assignments a
                JOIN manuscripts m ON m.id = a.manuscript_id
                JOIN users u ON u.id = a.reviewer_id
                WHERE a.round_id = %s
                ORDER BY m.number, u.username
                """,
                (round_id,),
            )
            payload["assignments"] = [dict(row) for row in cur.fetchall()]
            payload["total_assignments"] = len(payload["assignments"])
            payload["submitted_assignments"] = sum(
                1 for a in payload["assignments"] if a["status"] == "submitted"
            )

    return payload
