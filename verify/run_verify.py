"""One-shot acceptance tests for the peer-review system.

Run via:  docker compose run --rm verify

Covers:
  * role escalation (reviewer -> admin endpoints, admin -> reviewer endpoints,
    missing / forged credentials)
  * round limits (20 manuscripts, 10 reviewers, 3 assignments per reviewer)
  * conflict-of-interest rules (assignment blocked, conflict blocked once
    assigned)
  * author anonymity before freeze across lists, details and error responses
  * concurrent draft saves with optimistic revision numbers
  * concurrent duplicate submit and the submit/freeze race (only legal
    orderings may result)
  * immutable frozen round + revealed author fields after freeze
  * recusal workflow: pending barriers, reject restores the draft, atomic
    approve records the conflict and hands out a fresh blank assignment,
    quota isolation, draft/identity isolation from the replacement,
    freeze blocked while a recusal is pending
  * real-PostgreSQL barrier races around recusal request / submit / save /
    approve / manual assign / freeze / reject
"""

import os
import sys
import threading
import time
import uuid

import requests

BASE = os.environ.get("BASE_URL", "http://localhost:8000").rstrip("/")
ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "adminpass")

PASS = 0
FAILURES = []


def check(name, condition, detail=""):
    global PASS
    if condition:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAILURES.append((name, detail))
        print(f"  FAIL  {name}  {detail}")


class Client:
    def __init__(self, token=None):
        self.s = requests.Session()
        self.token = token

    def req(self, method, path, body=None, expect=None):
        headers = {}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        res = self.s.request(method, BASE + path, json=body, headers=headers, timeout=30)
        if expect is not None and res.status_code != expect:
            raise AssertionError(
                f"{method} {path}: expected {expect}, got {res.status_code}: {res.text[:300]}"
            )
        return res

    def get(self, path, expect=200):
        return self.req("GET", path, expect=expect)

    def post(self, path, body=None, expect=200):
        return self.req("POST", path, body=body, expect=expect)

    def put(self, path, body=None, expect=200):
        return self.req("PUT", path, body=body, expect=expect)

    def delete(self, path, expect=200):
        return self.req("DELETE", path, expect=expect)

    def raw(self, method, path, body=None):
        """Like req but never asserts on the status code (used in race tests)."""
        return self.req(method, path, body=body, expect=None)


def login(username, password):
    res = requests.post(
        BASE + "/api/login", json={"username": username, "password": password}, timeout=10
    )
    assert res.status_code == 200, f"login {username}: {res.status_code} {res.text[:200]}"
    data = res.json()
    return Client(data["token"]), data["user"]


def wait_healthy():
    for _ in range(60):
        try:
            r = requests.get(BASE + "/api/health", timeout=3)
            if r.status_code == 200:
                # health is up, but schema seeding happens during lifespan startup
                return
        except requests.RequestException:
            pass
        time.sleep(1)
    raise RuntimeError("api never became healthy")


def main():
    wait_healthy()
    run = uuid.uuid4().hex[:8]
    secret = f"ZZSECRET{run}"

    admin = Client()
    admin.token = requests.post(
        BASE + "/api/login",
        json={"username": ADMIN_USER, "password": ADMIN_PASSWORD},
        timeout=10,
    ).json()["token"]

    # ---------------------------------------------------------------- health/auth
    print("[1] authentication and role escalation")
    r = requests.get(BASE + "/api/admin/rounds", timeout=10)
    check("no token -> 401", r.status_code == 401, r.text)
    r = requests.get(
        BASE + "/api/admin/rounds",
        headers={"Authorization": "Bearer not.a.token"},
        timeout=10,
    )
    check("forged token -> 401", r.status_code == 401, r.text)
    r = requests.post(
        BASE + "/api/login",
        json={"username": ADMIN_USER, "password": "wrong"},
        timeout=10,
    )
    check("wrong password -> 401", r.status_code == 401, r.text)
    check("login error does not leak internals", "Traceback" not in r.text, r.text)
    check(
        "api responses carry Cache-Control: no-store",
        "no-store" in requests.get(BASE + "/api/health").headers.get("Cache-Control", ""),
    )

    reviewers = {}
    tokens = {}
    for i in range(1, 6):
        name = f"u{i}_{run}"
        res = admin.post("/api/admin/reviewers", {"username": name, "password": "pw"})
        reviewers[f"u{i}"] = res.json()["id"]
        tokens[f"u{i}"], _ = login(name, "pw")
    vtokens = []
    for i in range(1, 12):
        name = f"v{i}_{run}"
        admin.post("/api/admin/reviewers", {"username": name, "password": "pw"})
        c, _ = login(name, "pw")
        vtokens.append(c)

    check("duplicate reviewer username -> 400",
          admin.post("/api/admin/reviewers", {"username": f"u1_{run}", "password": "pw"},
                     expect=400).status_code == 400)

    # ----------------------------------------------------------- main round setup
    print("[2] round A: manuscripts, reviewers, limits and conflicts")
    A = admin.post("/api/admin/rounds", {"name": f"A-{run}"}).json()["id"]
    B = admin.post("/api/admin/rounds", {"name": f"B-{run}"}).json()["id"]

    manuscripts = [
        {"title": f"Alpha {run}", "author": f"{secret}A1"},
        {"title": f"Bravo {run}", "author": f"{secret}A2"},
        {"title": f"Charlie {run}", "author": f"{secret}A3"},
        {"title": f"Delta {run}", "author": f"{secret}A4"},
    ]
    admin.post(f"/api/admin/rounds/{A}/manuscripts", {"manuscripts": manuscripts})
    detail = admin.get(f"/api/admin/rounds/{A}").json()
    m = {row["number"]: row for row in detail["manuscripts"]}
    check("admin sees authors", all("author" in row for row in detail["manuscripts"]))

    admin.post(f"/api/admin/rounds/{A}/reviewers",
               {"reviewer_ids": [reviewers[k] for k in ("u1", "u2", "u3", "u4", "u5")]})
    detail = admin.get(f"/api/admin/rounds/{A}").json()
    check("5 reviewers enrolled", len(detail["reviewers"]) == 5)

    # 20 manuscript cap on a separate round
    C = admin.post("/api/admin/rounds", {"name": f"C-{run}"}).json()["id"]
    twenty = [{"title": f"t{i}", "author": f"a{i}"} for i in range(20)]
    admin.post(f"/api/admin/rounds/{C}/manuscripts", {"manuscripts": twenty})
    r = admin.post(f"/api/admin/rounds/{C}/manuscripts",
                   {"manuscripts": [{"title": "x", "author": "y"}, {"title": "x2", "author": "y2"}]},
                   expect=400)
    check("21st manuscript rejected (max 20)", r.status_code == 400, r.text)

    # 10 reviewer cap on a separate round
    D = admin.post("/api/admin/rounds", {"name": f"D-{run}"}).json()["id"]
    vids = []
    # ids of v* accounts: fetch from listing
    listing = admin.get("/api/admin/reviewers").json()
    id_by_name = {row["username"]: row["id"] for row in listing}
    vids = [id_by_name[f"v{i}_{run}"] for i in range(1, 12)]
    admin.post(f"/api/admin/rounds/{D}/reviewers", {"reviewer_ids": vids[:10]})
    r = admin.post(f"/api/admin/rounds/{D}/reviewers", {"reviewer_ids": [vids[10]]}, expect=400)
    check("11th reviewer rejected (max 10)", r.status_code == 400, r.text)

    # ------------------------------------------------------------ conflict rules
    print("[3] conflicts of interest")
    u1, u2, u3, u4, u5 = (reviewers[k] for k in ("u1", "u2", "u3", "u4", "u5"))
    admin.post(f"/api/admin/rounds/{A}/conflicts",
               {"manuscript_id": m[1]["id"], "reviewer_id": u2})
    r = admin.post(f"/api/admin/rounds/{A}/assignments",
                   {"manuscript_id": m[1]["id"], "reviewer_id": u2}, expect=400)
    check("conflicted assignment rejected", r.status_code == 400 and secret not in r.text, r.text)

    a1 = admin.post(f"/api/admin/rounds/{A}/assignments",
                    {"manuscript_id": m[1]["id"], "reviewer_id": u1}).json()
    a2 = admin.post(f"/api/admin/rounds/{A}/assignments",
                    {"manuscript_id": m[2]["id"], "reviewer_id": u1}).json()
    a3 = admin.post(f"/api/admin/rounds/{A}/assignments",
                    {"manuscript_id": m[3]["id"], "reviewer_id": u1}).json()
    r = admin.post(f"/api/admin/rounds/{A}/assignments",
                   {"manuscript_id": m[4]["id"], "reviewer_id": u1}, expect=400)
    check("4th assignment for one reviewer rejected (max 3)", r.status_code == 400, r.text)

    r = admin.post(f"/api/admin/rounds/{A}/conflicts",
                   {"manuscript_id": m[3]["id"], "reviewer_id": u1}, expect=400)
    check("conflict on existing assignment rejected", r.status_code == 400, r.text)

    a4 = admin.post(f"/api/admin/rounds/{A}/assignments",
                    {"manuscript_id": m[4]["id"], "reviewer_id": u2}).json()
    a5 = admin.post(f"/api/admin/rounds/{A}/assignments",
                    {"manuscript_id": m[4]["id"], "reviewer_id": u3}).json()

    bad_man = {"manuscript_id": 999999, "reviewer_id": u1}
    check("assignment with bogus manuscript -> 400",
          admin.post(f"/api/admin/rounds/{A}/assignments", bad_man, expect=400).status_code == 400)
    check("assignment with unenrolled reviewer -> 400",
          admin.post(f"/api/admin/rounds/{A}/assignments",
                     {"manuscript_id": m[1]["id"], "reviewer_id": vids[0]}, expect=400)
          .status_code == 400)

    # ------------------------------------------- anonymity while round is open
    print("[4] anonymity before freeze (lists / details / errors)")
    u1c = tokens["u1"]
    u2c = tokens["u2"]
    u5c = tokens["u5"]

    # role escalation
    check("reviewer cannot list admin rounds", u1c.get("/api/admin/rounds", expect=403).status_code == 403)
    check("reviewer cannot freeze",
          u1c.post(f"/api/admin/rounds/{A}/freeze", expect=403).status_code == 403)
    check("reviewer cannot create reviewer",
          u1c.post("/api/admin/reviewers", {"username": "x", "password": "y"},
                   expect=403).status_code == 403)
    check("admin cannot use reviewer api", admin.get("/api/reviewer/rounds", expect=403).status_code == 403)

    open_responses = []

    def collect(name, res):
        open_responses.append((name, res))

    collect("reviewer rounds list", u1c.get("/api/reviewer/rounds"))
    collect("round A detail", u1c.get(f"/api/reviewer/rounds/{A}"))
    collect("own assigned manuscript detail",
            u1c.get(f"/api/reviewer/rounds/{A}/manuscripts/{m[1]['id']}"))
    collect("own assignment detail", u1c.get(f"/api/reviewer/assignments/{a1['id']}"))
    # error responses must not leak either
    collect("other reviewer's assignment -> 404",
            u1c.get(f"/api/reviewer/assignments/{a4['id']}", expect=404))
    collect("unassigned manuscript detail -> 404",
            u1c.get(f"/api/reviewer/rounds/{A}/manuscripts/{m[4]['id']}", expect=404))
    # v1 is a reviewer account that is not enrolled in round A
    outsider = vtokens[0]
    collect("unenrolled reviewer round detail -> 404",
            outsider.get(f"/api/reviewer/rounds/{A}", expect=404))
    collect("unknown round -> 404", u1c.get("/api/reviewer/rounds/999999", expect=404))
    collect("unknown assignment -> 404", u1c.get("/api/reviewer/assignments/999999", expect=404))

    leaked = [name for name, res in open_responses if secret in res.text]
    check("no author secret leaks before freeze", not leaked, f"leaked in: {leaked}")

    rd = u1c.get(f"/api/reviewer/rounds/{A}").json()
    check("reviewer sees anonymous numbers", sorted(x["number"] for x in rd["manuscripts"]) == [1, 2, 3, 4])
    check("reviewer sees no author field pre-freeze",
          all("author" not in x for x in rd["manuscripts"]))
    check("reviewer sees only own assignments",
          {a["id"] for a in rd["assignments"]} == {a1["id"], a2["id"], a3["id"]})
    check("reviewer payload has no conflicts/reviewer lists",
          "conflicts" not in rd and "reviewers" not in rd, str(rd.keys()))

    md = u1c.get(f"/api/reviewer/rounds/{A}/manuscripts/{m[1]['id']}").json()
    check("manuscript detail anonymous", "author" not in md and md["number"] == 1, str(md))

    # drafts forbidden for assignments that are not yours
    check("cannot draft someone else's assignment",
          u1c.put(f"/api/reviewer/assignments/{a4['id']}/draft",
                  {"content": "x", "expected_revision": 0}, expect=404).status_code == 404)

    # --------------------------------------------------------- concurrent drafts
    print("[5] concurrent draft saves (optimistic revision)")
    barrier = threading.Barrier(2)
    outcomes = {}
    token_u1 = u1c.token

    def save_draft(worker, content):
        c = Client(token_u1)
        barrier.wait()
        outcomes[worker] = c.raw(
            "PUT",
            f"/api/reviewer/assignments/{a1['id']}/draft",
            {"content": content, "expected_revision": 0},
        )

    t1 = threading.Thread(target=save_draft, args=("w1", "draft-one"))
    t2 = threading.Thread(target=save_draft, args=("w2", "draft-two"))
    t1.start(); t2.start(); t1.join(); t2.join()
    codes = sorted(outcomes[w].status_code for w in outcomes)
    check("concurrent same-revision drafts: one 200 one 409", codes == [200, 409], str(codes))
    conflict_resp = [r for r in outcomes.values() if r.status_code == 409][0]
    check("409 reports current_revision=1",
          conflict_resp.json()["detail"]["current_revision"] == 1, conflict_resp.text)
    winner = [w for w, r in outcomes.items() if r.status_code == 200][0]
    check("winner revision is 1", outcomes[winner].json()["revision"] == 1)
    stored = u1c.get(f"/api/reviewer/assignments/{a1['id']}").json()
    check("stored content matches exactly one writer",
          stored["content"] in ("draft-one", "draft-two"), stored["content"])

    r = u1c.put(f"/api/reviewer/assignments/{a1['id']}/draft",
                {"content": "stale", "expected_revision": 0}, expect=409)
    check("stale revision later still rejected", r.status_code == 409)
    r = u1c.put(f"/api/reviewer/assignments/{a1['id']}/draft",
                {"content": "fresh", "expected_revision": 1})
    check("correct revision advances to 2", r.status_code == 200 and r.json()["revision"] == 2, r.text)

    # submitted assignment cannot be removed by admin; draft can
    u1c.post(f"/api/reviewer/assignments/{a2['id']}/submit",
             {"content": "FINAL-a2", "expected_revision": 0})
    check("admin cannot remove submitted assignment",
          admin.delete(f"/api/admin/rounds/{A}/assignments/{a2['id']}", expect=400).status_code == 400)
    check("admin removes draft assignment",
          admin.delete(f"/api/admin/rounds/{A}/assignments/{a3['id']}").status_code == 200)
    a3 = admin.post(f"/api/admin/rounds/{A}/assignments",
                    {"manuscript_id": m[3]["id"], "reviewer_id": u1}).json()

    # ------------------------------------------------------ duplicate submit race
    print("[6] concurrent duplicate final submit")
    barrier = threading.Barrier(2)
    sub_outcomes = {}
    token_u2 = u2c.token

    def do_submit(worker):
        c = Client(token_u2)
        barrier.wait()
        sub_outcomes[worker] = c.raw(
            "POST",
            f"/api/reviewer/assignments/{a4['id']}/submit",
            {"content": f"FINAL-a4-{worker}", "expected_revision": 0},
        )

    t1 = threading.Thread(target=do_submit, args=("w1",))
    t2 = threading.Thread(target=do_submit, args=("w2",))
    t1.start(); t2.start(); t1.join(); t2.join()
    scodes = sorted(r.status_code for r in sub_outcomes.values())
    check("duplicate submit race: one 200 one 409", scodes == [200, 409], str(scodes))
    a4_state = u2c.get(f"/api/reviewer/assignments/{a4['id']}").json()
    check("submitted review immutable by reviewer",
          a4_state["status"] == "submitted"
          and a4_state["content"] in ("FINAL-a4-w1", "FINAL-a4-w2"),
          str(a4_state))

    # freeze must fail while assignments are still draft
    r = admin.post(f"/api/admin/rounds/{A}/freeze", expect=409)
    check("freeze rejected before all submitted", r.status_code == 409, r.text)

    # submit remaining assignments
    u1c.post(f"/api/reviewer/assignments/{a1['id']}/submit",
             {"content": "FINAL-a1", "expected_revision": 2})
    u1c.post(f"/api/admin/assignments/{a3['id']}/submit", expect=404)  # admin path must not exist
    u1c.post(f"/api/reviewer/assignments/{a3['id']}/submit",
             {"content": "FINAL-a3", "expected_revision": 0})
    u3c = tokens["u3"]
    u3c.post(f"/api/reviewer/assignments/{a5['id']}/submit",
             {"content": "FINAL-a5", "expected_revision": 0})

    # ------------------------------------------------------------- freeze race
    print("[7] final submit vs freeze race on round B")
    admin.post(f"/api/admin/rounds/{B}/manuscripts",
               {"manuscripts": [{"title": f"Bravo-B {run}", "author": f"{secret}B1"}]})
    admin.post(f"/api/admin/rounds/{B}/reviewers", {"reviewer_ids": [u5]})
    bd = admin.get(f"/api/admin/rounds/{B}").json()
    b_ms = bd["manuscripts"][0]["id"]
    b1 = admin.post(f"/api/admin/rounds/{B}/assignments",
                    {"manuscript_id": b_ms, "reviewer_id": u5}).json()

    race = {}
    barrier = threading.Barrier(2)
    token_u5 = u5c.token
    token_admin = admin.token

    def race_submit():
        c = Client(token_u5)
        barrier.wait()
        race["submit"] = c.raw(
            "POST",
            f"/api/reviewer/assignments/{b1['id']}/submit",
            {"content": "FINAL-B", "expected_revision": 0},
        )

    def race_freeze():
        c = Client(token_admin)
        barrier.wait()
        race["freeze1"] = c.raw("POST", f"/api/admin/rounds/{B}/freeze")

    t1 = threading.Thread(target=race_submit)
    t2 = threading.Thread(target=race_freeze)
    t1.start(); t2.start(); t1.join(); t2.join()

    check("submit in race returned 200", race["submit"].status_code == 200, race["submit"].text)
    check("freeze attempt was either 200 (after submit) or 409 (before submit)",
          race["freeze1"].status_code in (200, 409), race["freeze1"].text)
    if race["freeze1"].status_code == 409:
        r = admin.post(f"/api/admin/rounds/{B}/freeze")
        check("retry freeze after racing submit succeeds", r.status_code == 200, r.text)

    for _ in range(30):
        b_state = admin.get(f"/api/admin/rounds/{B}").json()
        if b_state["status"] == "frozen":
            break
        time.sleep(0.2)
    check("round B ends frozen", b_state["status"] == "frozen", str(b_state["status"]))
    b_final = u5c.get(f"/api/reviewer/assignments/{b1['id']}").json()
    check("frozen content equals submitted content",
          b_final["status"] == "submitted" and b_final["content"] == "FINAL-B", str(b_final))

    # ------------------------------------------------------- freeze round A too
    print("[8] freeze round A and immutability")
    fr = admin.post(f"/api/admin/rounds/{A}/freeze")
    check("freeze A succeeds once all submitted", fr.status_code == 200, fr.text)
    check("double freeze -> 409",
          admin.post(f"/api/admin/rounds/{A}/freeze", expect=409).status_code == 409)

    for name, method, path, body in [
        ("reviewer draft after freeze", u1c.put, f"/api/reviewer/assignments/{a1['id']}/draft",
         {"content": "late", "expected_revision": 2}),
        ("reviewer submit after freeze", u1c.post, f"/api/reviewer/assignments/{a1['id']}/submit",
         {"content": "late", "expected_revision": 2}),
        ("admin add manuscript after freeze", admin.post, f"/api/admin/rounds/{A}/manuscripts",
         {"manuscripts": [{"title": "z", "author": "z"}]}),
        ("admin add assignment after freeze", admin.post, f"/api/admin/rounds/{A}/assignments",
         {"manuscript_id": m[1]["id"], "reviewer_id": u4}),
        ("admin add conflict after freeze", admin.post, f"/api/admin/rounds/{A}/conflicts",
         {"manuscript_id": m[2]["id"], "reviewer_id": u4}),
        ("admin delete conflict after freeze", admin.delete,
         f"/api/admin/rounds/{A}/conflicts/{m[1]['id']}/{u2}", None),
    ]:
        r = method(path, body, expect=409) if body is not None else method(path, expect=409)
        check(f"{name} -> 409", r.status_code == 409, r.text[:120])

    final_a1 = u1c.get(f"/api/reviewer/assignments/{a1['id']}").json()
    check("frozen review content unchanged", final_a1["content"] == "FINAL-a1", str(final_a1))

    # ---------------------------------------------------------- reveal after freeze
    print("[9] author reveal after freeze")
    post = u1c.get(f"/api/reviewer/rounds/{A}").json()
    check("authors revealed in reviewer list after freeze",
          all("author" in x and secret in x["author"] for x in post["manuscripts"]),
          str(post["manuscripts"]))
    md1 = u1c.get(f"/api/reviewer/rounds/{A}/manuscripts/{m[1]['id']}").json()
    check("assigned manuscript detail reveals author", md1.get("author") == f"{secret}A1", str(md1))
    md2 = u2c.get(f"/api/reviewer/rounds/{A}/manuscripts/{m[4]['id']}").json()
    check("other reviewer sees their own manuscript author", md2.get("author") == f"{secret}A4", str(md2))
    r = u2c.get(f"/api/reviewer/rounds/{A}/manuscripts/{m[1]['id']}", expect=404)
    check("non-assigned manuscript still 404 after freeze and stays silent",
          r.status_code == 404 and secret not in r.text, r.text)
    revealed = u5c.get(f"/api/reviewer/rounds/{B}").json()
    check("round B author revealed to its reviewer",
          revealed["manuscripts"][0].get("author") == f"{secret}B1", str(revealed))
    # u5 is enrolled in A but has no assignment there: the frozen anonymous list
    # reveals authors to every enrolled reviewer, but someone else's assignment
    # detail is still inaccessible.
    a_list = u5c.get(f"/api/reviewer/rounds/{A}").json()
    check("enrolled reviewer without assignments sees revealed list",
          all("author" in x for x in a_list["manuscripts"]) and a_list["assignments"] == [],
          str(a_list))
    check("assignment of another reviewer stays 404 even after freeze",
          u5c.get(f"/api/reviewer/assignments/{a1['id']}", expect=404).status_code == 404)
    check("unenrolled reviewer cannot see frozen round",
          outsider.get(f"/api/reviewer/rounds/{A}", expect=404).status_code == 404)

    # ============================================================ RECUSAL FLOW
    print("[10] recusal request: eligibility, barriers and reject/restore")
    R = admin.post("/api/admin/rounds", {"name": f"R-{run}"}).json()["id"]
    admin.post(f"/api/admin/rounds/{R}/manuscripts", {
        "manuscripts": [
            {"title": f"Recusal-X {run}", "author": f"{secret}RX"},
            {"title": f"Recusal-Y {run}", "author": f"{secret}RY"},
            {"title": f"Recusal-Z {run}", "author": f"{secret}RZ"},
        ]
    })
    rd = admin.get(f"/api/admin/rounds/{R}").json()
    rm = {row["number"]: row for row in rd["manuscripts"]}
    # fresh reviewers r1..r4 for this round
    rr_ids = {}
    rr_tok = {}
    for i in range(1, 5):
        nm = f"rr{i}_{run}"
        rr_ids[f"r{i}"] = admin.post("/api/admin/reviewers",
                                     {"username": nm, "password": "pw"}).json()["id"]
        rr_tok[f"r{i}"], _ = login(nm, "pw")
    admin.post(f"/api/admin/rounds/{R}/reviewers",
               {"reviewer_ids": [rr_ids[k] for k in ("r1", "r2", "r3", "r4")]})
    r1, r2, r3, r4 = (rr_ids[k] for k in ("r1", "r2", "r3", "r4"))
    r1c, r2c = rr_tok["r1"], rr_tok["r2"]

    x1 = admin.post(f"/api/admin/rounds/{R}/assignments",
                    {"manuscript_id": rm[1]["id"], "reviewer_id": r1}).json()["id"]
    x2 = admin.post(f"/api/admin/rounds/{R}/assignments",
                    {"manuscript_id": rm[2]["id"], "reviewer_id": r1}).json()["id"]
    # give r3 a conflict with manuscript 1 and fill r2's quota to 3
    admin.post(f"/api/admin/rounds/{R}/conflicts",
               {"manuscript_id": rm[1]["id"], "reviewer_id": r3})
    r2_ms = [
        admin.post(f"/api/admin/rounds/{R}/assignments",
                   {"manuscript_id": rm[n]["id"], "reviewer_id": r2}).json()["id"]
        for n in (1, 2, 3)
    ]

    # only the owner can request; empty reason rejected; submitted cannot recuse
    check("other reviewer cannot request recusal (404)",
          r2c.post(f"/api/reviewer/assignments/{x1}/recusal",
                   {"reason": "x"}, expect=404).status_code == 404)
    check("admin token cannot hit reviewer recusal endpoint (403)",
          admin.post(f"/api/reviewer/assignments/{x1}/recusal",
                     {"reason": "x"}, expect=403).status_code == 403)
    check("blank reason rejected",
          r1c.post(f"/api/reviewer/assignments/{x1}/recusal",
                   {"reason": "   "}, expect=400).status_code == 400)

    # a submitted assignment cannot request recusal
    r1c.post(f"/api/reviewer/assignments/{x2}/submit",
             {"content": "FINAL-x2", "expected_revision": 0})
    check("cannot recuse from submitted assignment",
          r1c.post(f"/api/reviewer/assignments/{x2}/recusal",
                   {"reason": "late conflict"}, expect=400).status_code == 400)

    # reviewer saves a draft first, then requests recusal
    r1c.put(f"/api/reviewer/assignments/{x1}/draft",
            {"content": "SECRET-OLD-DRAFT", "expected_revision": 0})
    rec = r1c.post(f"/api/reviewer/assignments/{x1}/recusal",
                   {"reason": "I co-authored related work"}).json()
    check("recusal created pending", rec["status"] == "pending", str(rec))
    rid = rec["id"]

    # while pending: saves and submits are blocked; assignment shows the state
    st = r1c.get(f"/api/reviewer/assignments/{x1}").json()
    check("assignment status recusal_pending", st["status"] == "recusal_pending", str(st["status"]))
    check("pending recusal reason visible to owner",
          st["recusal"] and st["recusal"]["id"] == rid, str(st.get("recusal")))
    check("draft save blocked while pending",
          r1c.put(f"/api/reviewer/assignments/{x1}/draft",
                  {"content": "nope", "expected_revision": 1},
                  expect=409).status_code == 409)
    check("final submit blocked while pending",
          r1c.post(f"/api/reviewer/assignments/{x1}/submit",
                   {"content": "nope", "expected_revision": 1},
                   expect=409).status_code == 409)
    check("duplicate recusal request blocked",
          r1c.post(f"/api/reviewer/assignments/{x1}/recusal",
                   {"reason": "again"}, expect=409).status_code == 409)
    check("admin cannot remove assignment while recusal pending",
          admin.delete(f"/api/admin/rounds/{R}/assignments/{x1}",
                       expect=400).status_code == 400)
    check("freeze blocked while recusal pending",
          admin.post(f"/api/admin/rounds/{R}/freeze",
                     expect=409).status_code == 409)
    admin_rd = admin.get(f"/api/admin/rounds/{R}").json()
    check("admin payload lists one pending recusal",
          len(admin_rd["pending_recusals"]) == 1
          and admin_rd["pending_recusals"][0]["id"] == rid,
          str(admin_rd["pending_recusals"]))
    check("pending assignment still counts toward quota and totals",
          admin_rd["total_assignments"] == 5
          and admin_rd["submitted_assignments"] == 1,
          f"{admin_rd['total_assignments']}/{admin_rd['submitted_assignments']}")
    check("admin round list shows pending count",
          any(r0["id"] == R and r0["pending_recusal_count"] == 1
              for r0 in admin.get("/api/admin/rounds").json()))

    # reject: assignment returns to draft, draft content/revision preserved
    rj = admin.post(f"/api/admin/rounds/{R}/recusals/{rid}/reject", {})
    check("reject succeeds", rj.status_code == 200, rj.text)
    st = r1c.get(f"/api/reviewer/assignments/{x1}").json()
    check("reject restores draft status", st["status"] == "draft", str(st["status"]))
    check("reject preserves old draft content and revision",
          st["content"] == "SECRET-OLD-DRAFT" and st["revision"] == 1, str(st))
    check("recusal entry no longer pending",
          all(p["id"] != rid for p in admin.get(f"/api/admin/rounds/{R}").json()["pending_recusals"]))
    # after reject the reviewer can edit again
    r1c.put(f"/api/reviewer/assignments/{x1}/draft",
            {"content": "SECRET-OLD-DRAFT-2", "expected_revision": 1})
    check("re-resolved recusal cannot be rejected again",
          admin.post(f"/api/admin/rounds/{R}/recusals/{rid}/reject", {},
                     expect=400).status_code == 400)

    # ===================================================== APPROVE / TRANSFER
    print("[11] approve recusal: atomic conflict + replacement + isolation")
    rec2 = r1c.post(f"/api/reviewer/assignments/{x1}/recusal",
                    {"reason": "author is my former PhD advisor"}).json()
    rid2 = rec2["id"]

    # invalid replacement reviewers
    check("reject/approve unknown recusal -> 404",
          admin.post(f"/api/admin/rounds/{R}/recusals/999999/approve",
                     {"replacement_reviewer_id": r4}, expect=404).status_code == 404)
    check("cannot pick the recusing reviewer",
          admin.post(f"/api/admin/rounds/{R}/recusals/{rid2}/approve",
                     {"replacement_reviewer_id": r1}, expect=400).status_code == 400)
    check("conflicted replacement rejected (r3 conflicts ms1)",
          admin.post(f"/api/admin/rounds/{R}/recusals/{rid2}/approve",
                     {"replacement_reviewer_id": r3}, expect=400).status_code == 400)
    check("over-quota replacement rejected (r2 has 3)",
          admin.post(f"/api/admin/rounds/{R}/recusals/{rid2}/approve",
                     {"replacement_reviewer_id": r2}, expect=400).status_code == 400)
    check("unenrolled replacement rejected",
          admin.post(f"/api/admin/rounds/{R}/recusals/{rid2}/approve",
                     {"replacement_reviewer_id": vids[0]}, expect=400).status_code == 400)

    # failed approve must change nothing: still pending, quota unchanged
    mid = admin.get(f"/api/admin/rounds/{R}").json()
    check("failed approve leaves recusal pending",
          any(p["id"] == rid2 for p in mid["pending_recusals"]))
    check("failed approve leaves old assignment live",
          any(a["id"] == x1 and a["status"] == "recusal_pending"
              for a in mid["assignments"]))

    # r1 also holds x2 (submitted) -> before approval r1 has 2 live assignments.
    # approve with the eligible reviewer r4
    appr = admin.post(f"/api/admin/rounds/{R}/recusals/{rid2}/approve",
                      {"replacement_reviewer_id": r4})
    check("approve succeeds", appr.status_code == 200, appr.text)
    appr = appr.json()
    new_id = appr["new_assignment_id"]
    check("new assignment id differs from old", new_id != x1)

    after = admin.get(f"/api/admin/rounds/{R}").json()
    old_row = next(a for a in after["assignments"] if a["id"] == x1)
    new_row = next(a for a in after["assignments"] if a["id"] == new_id)
    check("old assignment marked recused", old_row["status"] == "recused", str(old_row["status"]))
    check("old draft content preserved as history",
          old_row["content"] == "SECRET-OLD-DRAFT-2" and old_row["revision"] == 2, str(old_row))
    check("replacement assignment is blank draft",
          new_row["status"] == "draft" and new_row["content"] == ""
          and new_row["revision"] == 0 and new_row["reviewer_id"] == r4, str(new_row))
    check("conflict relationship recorded (ms1 x r1)",
          any(c["manuscript_id"] == rm[1]["id"] and c["reviewer_id"] == r1
              for c in after["conflicts"]), str(after["conflicts"]))
    check("no pending recusals left", after["pending_recusals"] == [])
    check("totals exclude recused row (5 live, 1 submitted)",
          after["total_assignments"] == 5 and after["submitted_assignments"] == 1,
          f"{after['total_assignments']}/{after['submitted_assignments']}")

    # quota accounting: r1 now has 1 live (x2) + 1 recused history; r4 has 1
    def live_count(payload, rid):
        return sum(1 for a in payload["assignments"]
                   if a["reviewer_id"] == rid and a["status"] != "recused")

    check("recused row does not occupy r1 quota (live=1)", live_count(after, r1) == 1)
    check("replacement occupies r4 quota (live=1)", live_count(after, r4) == 1)
    # r1 can be assigned ms3 now: one slot freed by the recusal
    extra = admin.post(f"/api/admin/rounds/{R}/assignments",
                       {"manuscript_id": rm[3]["id"], "reviewer_id": r1})
    check("recused reviewer can take a new assignment (freed slot)",
          extra.status_code == 200, extra.text)

    # ------------------------------------------- reviewer-facing isolation
    print("[12] draft and author-identity isolation after approved recusal")
    r4c = rr_tok["r4"]
    own = r4c.get(f"/api/reviewer/rounds/{R}").json()
    check("replacement sees the new blank assignment only",
          {a["id"] for a in own["assignments"]} == {new_id},
          str([a["id"] for a in own["assignments"]]))
    new_view = own["assignments"][0]
    check("new assignment is blank for replacement",
          new_view["content"] == "" and new_view["revision"] == 0
          and new_view["status"] == "draft", str(new_view))
    check("old draft content absent from replacement interface",
          "SECRET-OLD-DRAFT" not in str(own))
    check("old assignment id absent from replacement round payload",
          all(a["id"] != x1 for a in own["assignments"])
          and new_id != x1)

    # old reviewer cannot access the recused assignment anymore (it is history)
    check("recused assignment invisible to old reviewer (404)",
          r1c.get(f"/api/reviewer/assignments/{x1}", expect=404).status_code == 404)
    check("recused assignment cannot be drafted by old reviewer (404)",
          r1c.put(f"/api/reviewer/assignments/{x1}/draft",
                  {"content": "x", "expected_revision": 0}, expect=404).status_code == 404)
    # manuscript detail for the recused manuscript is 404 for the old reviewer,
    # and must not reveal the author even via error text
    ms_detail = r1c.get(f"/api/reviewer/rounds/{R}/manuscripts/{rm[1]['id']}", expect=404)
    check("recused manuscript detail 404 and silent for old reviewer",
          ms_detail.status_code == 404 and secret not in ms_detail.text, ms_detail.text)
    # the anonymous manuscript list never carries the author while open
    r1_list = r1c.get(f"/api/reviewer/rounds/{R}").json()
    check("no author leak in old reviewer anonymous list",
          all("author" not in m for m in r1_list["manuscripts"])
          and secret not in str(r1_list))
    check("old reviewer no longer sees recused assignment in list",
          all(a["id"] != x1 for a in r1_list["assignments"]))

    # replacement can work on the fresh assignment and does not inherit revision
    sv = r4c.put(f"/api/reviewer/assignments/{new_id}/draft",
                 {"content": "fresh-r4-opinion", "expected_revision": 0})
    check("replacement saves from revision 0",
          sv.status_code == 200 and sv.json()["revision"] == 1, sv.text)
    check("replacement cannot touch old assignment id",
          r4c.put(f"/api/reviewer/assignments/{x1}/draft",
                  {"content": "x", "expected_revision": 0}, expect=404).status_code == 404)
    # old draft still intact server-side (admin history)
    check("admin history keeps old draft untouched",
          next(a for a in admin.get(f"/api/admin/rounds/{R}").json()["assignments"]
               if a["id"] == x1)["content"] == "SECRET-OLD-DRAFT-2")

    # after freeze, the recused (conflicted) reviewer must still not learn the
    # author of the manuscript they were conflicted with
    # x2 was already submitted in section [10]
    r4c.post(f"/api/reviewer/assignments/{new_id}/submit",
             {"content": "FINAL-new", "expected_revision": 1})
    for aid in [r2_ms[0], r2_ms[1], r2_ms[2]]:
        r2c.post(f"/api/reviewer/assignments/{aid}/submit",
                 {"content": f"FINAL-{aid}", "expected_revision": 0})
    extra_id = extra.json()["id"]
    r1c.post(f"/api/reviewer/assignments/{extra_id}/submit",
             {"content": "FINAL-extra", "expected_revision": 0})
    fr = admin.post(f"/api/admin/rounds/{R}/freeze")
    check("freeze R succeeds once recusal resolved and all submitted",
          fr.status_code == 200, fr.text)
    frozen_list = r1c.get(f"/api/reviewer/rounds/{R}").json()
    ms1_entry = next(m for m in frozen_list["manuscripts"] if m["id"] == rm[1]["id"])
    ms2_entry = next(m for m in frozen_list["manuscripts"] if m["id"] == rm[2]["id"])
    check("recused reviewer does not see conflicted manuscript author after freeze",
          "author" not in ms1_entry, str(ms1_entry))
    check("recused reviewer still sees authors of non-conflicted manuscripts",
          ms2_entry.get("author") == f"{secret}RY", str(ms2_entry))
    check("recused manuscript detail stays 404 after freeze",
          r1c.get(f"/api/reviewer/rounds/{R}/manuscripts/{rm[1]['id']}",
                  expect=404).status_code == 404)
    r4_frozen = r4c.get(f"/api/reviewer/rounds/{R}").json()
    r4_ms1 = next(m for m in r4_frozen["manuscripts"] if m["id"] == rm[1]["id"])
    check("replacement reviewer sees the author after freeze (no conflict)",
          r4_ms1.get("author") == f"{secret}RX", str(r4_ms1))

    # recusal actions after freeze are rejected (the round lock returns 409 first)
    check("approve/reject impossible once frozen",
          admin.post(f"/api/admin/rounds/{R}/recusals/{rid2}/reject", {},
                     expect=409).status_code == 409)

    # ==================================================== CONCURRENCY / RACES
    print("[13] real-PostgreSQL barrier races around recusal")

    # --- race 1: recusal request vs final submit on the same draft ---
    E = admin.post("/api/admin/rounds", {"name": f"E-{run}"}).json()["id"]
    admin.post(f"/api/admin/rounds/{E}/manuscripts",
               {"manuscripts": [{"title": f"Race-E {run}", "author": f"{secret}E1"}]})
    e_ms = admin.get(f"/api/admin/rounds/{E}").json()["manuscripts"][0]["id"]
    admin.post(f"/api/admin/rounds/{E}/reviewers", {"reviewer_ids": [r1, r4]})
    e1 = admin.post(f"/api/admin/rounds/{E}/assignments",
                    {"manuscript_id": e_ms, "reviewer_id": r1}).json()["id"]
    barrier = threading.Barrier(2)
    race_res = {}
    tk = r1c.token

    def race_recusal():
        c = Client(tk)
        barrier.wait()
        race_res["recusal"] = c.raw("POST", f"/api/reviewer/assignments/{e1}/recusal",
                                    {"reason": "race conflict"})

    def race_submit():
        c = Client(tk)
        barrier.wait()
        race_res["submit"] = c.raw("POST", f"/api/reviewer/assignments/{e1}/submit",
                                   {"content": "FINAL-E", "expected_revision": 0})

    t1 = threading.Thread(target=race_recusal)
    t2 = threading.Thread(target=race_submit)
    t1.start(); t2.start(); t1.join(); t2.join()
    codes = sorted(v.status_code for v in race_res.values())
    check("recusal vs submit: exactly one succeeds (200) one loses (409/400)",
          200 in codes and all(c in (200, 400, 409) for c in codes)
          and sum(1 for c in codes if c == 200) == 1, str(codes))
    estate = admin.get(f"/api/admin/rounds/{E}").json()
    check("post-race state is coherent: either submitted or one pending recusal",
          (all(a["status"] == "submitted" for a in estate["assignments"]))
          or (len(estate["pending_recusals"]) == 1),
          str([(a["status"],) for a in estate["assignments"]]) + str(estate["pending_recusals"]))
    # resolve whatever happened so the round is clean
    if estate["pending_recusals"]:
        pid = estate["pending_recusals"][0]["id"]
        admin.post(f"/api/admin/rounds/{E}/recusals/{pid}/reject", {})
        check("race-1 rejected recusal restores draft",
              next(a for a in admin.get(f"/api/admin/rounds/{E}").json()["assignments"]
                   if a["id"] == e1)["status"] == "draft")
    # round is left open; race 2 uses a fresh round

    # --- race 2: freeze vs recusal request (barrier) ---
    # Invariant: a recusal can only be requested on a non-submitted (draft)
    # assignment, and a freeze requires ALL assignments submitted.  Therefore a
    # freeze can never legitimately succeed while a recusal is requestable;
    # the 'pending recusal blocks freeze' check is the explicit guard.  What the
    # round lock must guarantee here is serialization: never a 500, never a
    # deadlock, always one coherent outcome.  Exercise it repeatedly.
    F = admin.post("/api/admin/rounds", {"name": f"F-{run}"}).json()["id"]
    admin.post(f"/api/admin/rounds/{F}/manuscripts",
               {"manuscripts": [
                   {"title": f"Race-F1 {run}", "author": f"{secret}F1"},
                   {"title": f"Race-F2 {run}", "author": f"{secret}F2"},
               ]})
    f_detail = admin.get(f"/api/admin/rounds/{F}").json()
    f_m1, f_m2 = (x["id"] for x in f_detail["manuscripts"])
    admin.post(f"/api/admin/rounds/{F}/reviewers", {"reviewer_ids": [r1, r4]})
    f1 = admin.post(f"/api/admin/rounds/{F}/assignments",
                    {"manuscript_id": f_m1, "reviewer_id": r1}).json()["id"]
    f2 = admin.post(f"/api/admin/rounds/{F}/assignments",
                    {"manuscript_id": f_m2, "reviewer_id": r4}).json()["id"]
    r4c.post(f"/api/reviewer/assignments/{f2}/submit",
             {"content": "FINAL-f2", "expected_revision": 0})

    def freeze_vs_recusal_once(idx):
        barrier = threading.Barrier(2)
        box = {}
        tk1, tka = r1c.token, admin.token

        def do_recusal():
            c = Client(tk1)
            barrier.wait()
            box["recusal"] = c.raw("POST", f"/api/reviewer/assignments/{f1}/recusal",
                                   {"reason": f"barrier {idx}"})

        def do_freeze():
            c = Client(tka)
            barrier.wait()
            box["freeze"] = c.raw("POST", f"/api/admin/rounds/{F}/freeze")

        t1 = threading.Thread(target=do_recusal)
        t2 = threading.Thread(target=do_freeze)
        t1.start(); t2.start(); t1.join(); t2.join()
        return box

    # first barrier run: recusal wins (draft still open); freeze must 409.
    race2 = freeze_vs_recusal_once(0)
    fstate = admin.get(f"/api/admin/rounds/{F}").json()
    check("recusal request succeeds against open draft",
          race2["recusal"].status_code == 200, race2["recusal"].text)
    check("freeze never 500 and is rejected (pending or not-all-submitted)",
          race2["freeze"].status_code == 409, race2["freeze"].text)
    check("round stays open with exactly one pending recusal",
          fstate["status"] == "open" and len(fstate["pending_recusals"]) == 1,
          fstate["status"] + str(fstate["pending_recusals"]))
    # while pending, repeated concurrent freeze attempts are all rejected
    fb = threading.Barrier(3)
    multi = {}
    def multi_freeze(k):
        c = Client(admin.token)
        fb.wait()
        multi[k] = c.raw("POST", f"/api/admin/rounds/{F}/freeze")
    threads = [threading.Thread(target=multi_freeze, args=(i,)) for i in range(3)]
    for t in threads: t.start()
    for t in threads: t.join()
    check("all concurrent freezes rejected while recusal pending",
          all(r.status_code == 409 for r in multi.values()),
          str({k: v.status_code for k, v in multi.items()}))
    # explicit pending-guard message is observable now that all else is submitted
    guard = admin.post(f"/api/admin/rounds/{F}/freeze", expect=409).json()
    check("freeze error mentions pending recusal",
          "recusal" in str(guard["detail"]).lower(), str(guard))

    # resolve: reject -> submit -> freeze succeeds
    fpid = fstate["pending_recusals"][0]["id"]
    admin.post(f"/api/admin/rounds/{F}/recusals/{fpid}/reject", {})
    r1c.post(f"/api/reviewer/assignments/{f1}/submit",
             {"content": "FINAL-f1", "expected_revision": 0})
    check("freeze succeeds after recusal resolved and all submitted",
          admin.post(f"/api/admin/rounds/{F}/freeze").status_code == 200)
    # once frozen, a (late) recusal request on that assignment is rejected by
    # the round lock with 409 -- proving the barrier blocks it freeze-first.
    check("recusal request after freeze -> 409 (frozen)",
          r1c.post(f"/api/reviewer/assignments/{f1}/recusal",
                   {"reason": "late"}, expect=409).status_code == 409)

    # --- race 3: approve vs concurrent manual assignment to the replacement ---
    G = admin.post("/api/admin/rounds", {"name": f"G-{run}"}).json()["id"]
    admin.post(f"/api/admin/rounds/{G}/manuscripts",
               {"manuscripts": [
                   {"title": f"Race-G1 {run}", "author": f"{secret}G1"},
                   {"title": f"Race-G2 {run}", "author": f"{secret}G2"},
                   {"title": f"Race-G3 {run}", "author": f"{secret}G3"},
                   {"title": f"Race-G4 {run}", "author": f"{secret}G4"},
               ]})
    g_detail = admin.get(f"/api/admin/rounds/{G}").json()
    g_ms = {row["number"]: row["id"] for row in g_detail["manuscripts"]}
    admin.post(f"/api/admin/rounds/{G}/reviewers",
               {"reviewer_ids": [r1, r2, r4]})
    g_old = admin.post(f"/api/admin/rounds/{G}/assignments",
                       {"manuscript_id": g_ms[1], "reviewer_id": r1}).json()["id"]
    # fill r4 to exactly 3 with OTHER manuscripts so approve must fail if a
    # concurrent manual assignment grabs the last slot first (or vice versa).
    g_fill = []
    for n in (2, 3):
        g_fill.append(admin.post(f"/api/admin/rounds/{G}/assignments",
                                 {"manuscript_id": g_ms[n], "reviewer_id": r4}).json()["id"])
    g_rec = r1c.post(f"/api/reviewer/assignments/{g_old}/recusal",
                     {"reason": "quota race"}).json()["id"]
    # r4 now has 2/3. Two transactions race: approve (assigns g_ms1 to r4) and
    # manual assign of g_ms4 to r4. Exactly one may win the third slot.
    barrier = threading.Barrier(2)
    race3 = {}
    tka = admin.token

    def race3_approve():
        c = Client(tka)
        barrier.wait()
        race3["approve"] = c.raw("POST", f"/api/admin/rounds/{G}/recusals/{g_rec}/approve",
                                 {"replacement_reviewer_id": r4})

    def race3_manual():
        c = Client(tka)
        barrier.wait()
        race3["manual"] = c.raw("POST", f"/api/admin/rounds/{G}/assignments",
                                {"manuscript_id": g_ms[4], "reviewer_id": r4})

    t1 = threading.Thread(target=race3_approve)
    t2 = threading.Thread(target=race3_manual)
    t1.start(); t2.start(); t1.join(); t2.join()
    gstate = admin.get(f"/api/admin/rounds/{G}").json()
    r4_live = [a for a in gstate["assignments"] if a["reviewer_id"] == r4 and a["status"] != "recused"]
    check("quota race: r4 never exceeds 3 live assignments",
          len(r4_live) == 3, str([(a["id"], a["status"]) for a in r4_live]))
    check("quota race: exactly one of approve/manual succeeded",
          sum(1 for v in race3.values() if v.status_code == 200) == 1
          and all(v.status_code in (200, 400) for v in race3.values()),
          str({k: v.status_code for k, v in race3.items()}))
    if race3["approve"].status_code == 200:
        check("approve won: old row recused, conflict recorded",
              next(a for a in gstate["assignments"] if a["id"] == g_old)["status"] == "recused"
              and any(c["manuscript_id"] == g_ms[1] and c["reviewer_id"] == r1
                      for c in gstate["conflicts"]))
        # manual must have been rejected for quota; r4 can no longer get g_ms4
        check("manual loser reported 400", race3["manual"].status_code == 400)
    else:
        # manual won the 3rd slot; approve failed and must have changed nothing
        check("manual won: approve reported 400 and old assignment still pending",
              race3["approve"].status_code == 400
              and next(a for a in gstate["assignments"] if a["id"] == g_old)["status"] == "recusal_pending"
              and any(p["id"] == g_rec for p in gstate["pending_recusals"])
              and not any(c["manuscript_id"] == g_ms[1] and c["reviewer_id"] == r1
                          for c in gstate["conflicts"]),
              str(race3["approve"].text))

    print()
    print(f"{'='*60}")
    print(f"passed: {PASS}, failed: {len(FAILURES)}")
    if FAILURES:
        for name, detail in FAILURES:
            print(f"  FAIL {name}: {detail}")
        sys.exit(1)
    print("ALL ACCEPTANCE CHECKS PASSED")


if __name__ == "__main__":
    main()
