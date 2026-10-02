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
  * reviewer recusal flow: pending gate, reject restores the draft, approve
    records the conflict and hands a blank assignment to a replacement;
    slot/conflict/authorization checks, draft isolation, and barrier races
    (recusal vs submit/freeze/approval) against real PostgreSQL
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

    # ------------------------------------------------------------- recusals
    print("[10] recusal requests: authorization, gating and reject")
    E = admin.post("/api/admin/rounds", {"name": f"E-{run}"}).json()["id"]
    emss = admin.post(f"/api/admin/rounds/{E}/manuscripts", {"manuscripts": [
        {"title": f"Recusal-1 {run}", "author": f"{secret}E1"},
        {"title": f"Recusal-2 {run}", "author": f"{secret}E2"},
        {"title": f"Recusal-3 {run}", "author": f"{secret}E3"},
        {"title": f"Recusal-4 {run}", "author": f"{secret}E4"},
    ]}).json()["manuscripts"]
    e1m, e2m, e3m, e4m = (x["id"] for x in emss)
    admin.post(f"/api/admin/rounds/{E}/reviewers",
               {"reviewer_ids": [u1, u2, u3, u4]})
    # reviewer cannot raise recusal on someone else's assignment (404)
    e_a2 = admin.post(f"/api/admin/rounds/{E}/assignments",
                      {"manuscript_id": e2m, "reviewer_id": u2}).json()["id"]
    check("cannot recuse another reviewer's assignment",
          u1c.post(f"/api/reviewer/assignments/{e_a2}/recusal",
                   {"reason": "x"}, expect=404).status_code == 404)
    check("admin cannot request recusal",
          admin.post(f"/api/reviewer/assignments/{e_a2}/recusal",
                     {"reason": "x"}, expect=403).status_code == 403)
    check("blank reason rejected",
          u2c.post(f"/api/reviewer/assignments/{e_a2}/recusal",
                   {"reason": "   "}, expect=422).status_code == 422)
    check("recusal of unknown assignment -> 404",
          u2c.post("/api/reviewer/assignments/999999/recusal",
                   {"reason": "x"}, expect=404).status_code == 404)

    e_a1 = admin.post(f"/api/admin/rounds/{E}/assignments",
                      {"manuscript_id": e1m, "reviewer_id": u1}).json()["id"]
    u1c.put(f"/api/reviewer/assignments/{e_a1}/draft",
            {"content": "U1-SECRET-DRAFT", "expected_revision": 0})
    # submit first, then confirm recusal is no longer possible
    u1c.post(f"/api/reviewer/assignments/{e_a1}/submit",
             {"content": "U1-FINAL-E1", "expected_revision": 1})
    check("cannot recuse a submitted assignment",
          u1c.post(f"/api/reviewer/assignments/{e_a1}/recusal",
                   {"reason": "late COI"}, expect=400).status_code == 400)

    # the real pending flow happens on u2's draft assignment; save a
    # distinctive draft first so we can later prove the history keeps it while
    # the replacement gets nothing.
    r = u2c.put(f"/api/reviewer/assignments/{e_a2}/draft",
                {"content": "U2-SECRET-RECUSED-DRAFT", "expected_revision": 0})
    check("u2 draft saved at revision 1", r.status_code == 200 and r.json()["revision"] == 1, r.text)
    rr = u2c.post(f"/api/reviewer/assignments/{e_a2}/recusal",
                  {"reason": "co-authored with E2 author"}).json()
    recusal_id = rr["id"]
    check("recusal request returns pending status", rr["status"] == "recusal_pending", str(rr))
    check("duplicate recusal request -> 409",
          u2c.post(f"/api/reviewer/assignments/{e_a2}/recusal",
                   {"reason": "again"}, expect=409).status_code == 409)
    # while pending: saves and final submit are suspended
    check("draft save blocked while pending",
          u2c.put(f"/api/reviewer/assignments/{e_a2}/draft",
                  {"content": "x", "expected_revision": 0}, expect=409).status_code == 409)
    check("final submit blocked while pending",
          u2c.post(f"/api/reviewer/assignments/{e_a2}/submit",
                   {"content": "x", "expected_revision": 0}, expect=409).status_code == 409)
    # admin cannot delete an assignment with pending recusal
    check("admin cannot remove pending-recusal assignment",
          admin.delete(f"/api/admin/rounds/{E}/assignments/{e_a2}", expect=400).status_code == 400)
    # round cannot freeze with a pending recusal
    fr = admin.post(f"/api/admin/rounds/{E}/freeze", expect=409)
    check("freeze blocked while recusal pending", fr.status_code == 409, fr.text)
    ed = admin.get(f"/api/admin/rounds/{E}").json()
    check("admin detail exposes one pending recusal", ed["recusal_pending_count"] == 1, str(ed.get("recusal_pending_count")))
    pend = [a for a in ed["assignments"] if a["status"] == "recusal_pending"]
    check("pending row carries reason and reviewer",
          len(pend) == 1 and pend[0]["recusal_reason"] == "co-authored with E2 author"
          and pend[0]["reviewer_id"] == u2, str(pend))
    lst = admin.get("/api/admin/rounds").json()
    erow = [r for r in lst if r["id"] == E][0]
    check("round list shows pending recusal count", erow["recusal_pending_count"] == 1, str(erow))
    # reviewer payload shows the pending assignment with its reason, still no author
    rd = u2c.get(f"/api/reviewer/rounds/{E}").json()
    own = [a for a in rd["assignments"] if a["id"] == e_a2][0]
    check("reviewer sees own pending assignment",
          own["status"] == "recusal_pending" and own["recusal_reason"] == "co-authored with E2 author",
          str(own))
    check("no author secret leaks while recusal pending", secret not in u2c.get(f"/api/reviewer/rounds/{E}").text)

    print("[11] admin reject: draft restored unchanged")
    r = admin.post(f"/api/admin/recusals/{recusal_id}/reject")
    check("reject succeeds", r.status_code == 200, r.text)
    check("reject returns assignment to draft",
          r.json()["assignment_status"] == "draft", r.text)
    # only admin may decide
    check("reviewer cannot reject recusal",
          u1c.post(f"/api/admin/recusals/{recusal_id}/reject", expect=403).status_code == 403)
    # double reject / unknown recusal
    check("rejecting an already decided recusal -> 404",
          admin.post(f"/api/admin/recusals/{recusal_id}/reject", expect=404).status_code == 404)
    check("unknown recusal reject -> 404",
          admin.post("/api/admin/recusals/999999/reject", expect=404).status_code == 404)
    after = u2c.get(f"/api/reviewer/assignments/{e_a2}").json()
    check("draft editable again after reject", after["status"] == "draft", str(after))
    check("reject restores original draft content and revision",
          after["content"] == "U2-SECRET-RECUSED-DRAFT" and after["revision"] == 1, str(after))
    check("rejected reason is cleared from reviewer payload",
          after.get("recusal_reason") is None, str(after))
    after_round = u2c.get(f"/api/reviewer/rounds/{E}").json()
    after_own = [a for a in after_round["assignments"] if a["id"] == e_a2][0]
    check("rejected reason cleared in round payload too",
          after_own.get("recusal_reason") is None, str(after_own))
    # raise again: same row goes back to pending (then approved below)
    rr = u2c.post(f"/api/reviewer/assignments/{e_a2}/recusal",
                  {"reason": "still conflicted"}).json()
    recusal_id = rr["id"]

    print("[12] admin approve: validation failures keep slots and assignment intact")
    # fill u3's 3 slots in round E (e2 is pending for u2, not u3)
    a_e3 = admin.post(f"/api/admin/rounds/{E}/assignments",
                      {"manuscript_id": e3m, "reviewer_id": u3}).json()["id"]
    a_e4 = admin.post(f"/api/admin/rounds/{E}/assignments",
                      {"manuscript_id": e4m, "reviewer_id": u3}).json()["id"]
    third_mss = [x for x in (e1m, e2m, e3m, e4m) if x not in (e3m, e4m)][0]
    a_e5 = admin.post(f"/api/admin/rounds/{E}/assignments",
                      {"manuscript_id": third_mss, "reviewer_id": u3}).json()["id"]
    check("approve without body -> 422",
          admin.post(f"/api/admin/recusals/{recusal_id}/approve", expect=422).status_code == 422)
    check("approve with self as replacement -> 400",
          admin.post(f"/api/admin/recusals/{recusal_id}/approve",
                     {"replacement_reviewer_id": u2}, expect=400).status_code == 400)
    check("approve with unenrolled replacement -> 400",
          admin.post(f"/api/admin/recusals/{recusal_id}/approve",
                     {"replacement_reviewer_id": vids[0]}, expect=400).status_code == 400)
    check("approve with full (3-slot) replacement -> 400",
          admin.post(f"/api/admin/recusals/{recusal_id}/approve",
                     {"replacement_reviewer_id": u3}, expect=400).status_code == 400)
    # record a conflict for u4 on manuscript 2, then refuse u4 as replacement
    admin.post(f"/api/admin/rounds/{E}/conflicts",
               {"manuscript_id": e2m, "reviewer_id": u4})
    r = admin.post(f"/api/admin/recusals/{recusal_id}/approve",
                   {"replacement_reviewer_id": u4}, expect=400)
    check("approve with conflicted replacement -> 400", r.status_code == 400, r.text)
    check("conflict rejection message stays anonymous", secret not in r.text, r.text)
    # failed approvals change nothing
    midstate = admin.get(f"/api/admin/rounds/{E}").json()
    still_pending = [a for a in midstate["assignments"] if a["id"] == e_a2][0]
    check("assignment still pending after failed approvals",
          still_pending["status"] == "recusal_pending", str(still_pending))
    check("no new assignment / no slot consumed after failures",
          midstate["recusal_pending_count"] == 1
          and len([a for a in midstate["assignments"] if a["reviewer_id"] == u3
                   and a["status"] != "recused"]) == 3,
          str([(a["reviewer_id"], a["status"]) for a in midstate["assignments"]]))

    print("[13] approve success: blank replacement, history kept, isolation")
    # u1 has one submitted assignment in E and a free slot; approve to u1
    r = admin.post(f"/api/admin/recusals/{recusal_id}/approve",
                   {"replacement_reviewer_id": u1})
    check("approve with eligible replacement -> 200", r.status_code == 200, r.text)
    appr = r.json()
    new_id = appr["replacement_assignment_id"]
    check("approval response links new blank assignment",
          appr["old_assignment_id"] == e_a2 and appr["conflict_recorded"] is True
          and new_id != e_a2, str(appr))
    ed = admin.get(f"/api/admin/rounds/{E}").json()
    check("no pending recusals remain", ed["recusal_pending_count"] == 0, str(ed["recusal_pending_count"]))
    old_row = [a for a in ed["assignments"] if a["id"] == e_a2][0]
    new_row = [a for a in ed["assignments"] if a["id"] == new_id][0]
    check("old assignment kept as recused history",
          old_row["status"] == "recused" and old_row["content"] == "U2-SECRET-RECUSED-DRAFT" and
          old_row["recusal_status"] == "approved"
          and old_row["replacement_assignment_id"] == new_id
          and old_row["replacement_reviewer_name"] == f"u1_{run}",
          str({k: old_row[k] for k in ("status", "content", "recusal_status")}))
    check("replacement assignment is blank",
          new_row["status"] == "draft" and new_row["revision"] == 0 and new_row["content"] == "",
          str(new_row))
    check("conflict registered for recusing reviewer",
          (e2m, u2) in {(c["manuscript_id"], c["reviewer_id"]) for c in ed["conflicts"]},
          str(ed["conflicts"]))
    # slot accounting: old row stopped occupying, new occupies one of u1's slots
    check("recused history does not occupy a slot",
          len([a for a in ed["assignments"] if a["reviewer_id"] == u2
               and a["status"] != "recused"]) == 0, str(ed["assignments"]))
    check("u1 live slot count is 2 (old submitted + new)",
          len([a for a in ed["assignments"] if a["reviewer_id"] == u1
               and a["status"] != "recused"]) == 2, str(ed["assignments"]))

    # isolation: former reviewer (u2) loses every trace
    check("former reviewer GET old assignment -> 404",
          u2c.get(f"/api/reviewer/assignments/{e_a2}", expect=404).status_code == 404)
    check("former reviewer cannot save to old assignment",
          u2c.put(f"/api/reviewer/assignments/{e_a2}/draft",
                  {"content": "late", "expected_revision": 0}, expect=404).status_code == 404)
    check("former reviewer cannot submit old assignment",
          u2c.post(f"/api/reviewer/assignments/{e_a2}/submit",
                   {"content": "late", "expected_revision": 0}, expect=404).status_code == 404)
    check("former reviewer cannot recuse old assignment again",
          u2c.post(f"/api/reviewer/assignments/{e_a2}/recusal",
                   {"reason": "x"}, expect=404).status_code == 404)
    u2round = u2c.get(f"/api/reviewer/rounds/{E}").json()
    check("old assignment absent from former reviewer round payload",
          all(a["id"] != e_a2 for a in u2round["assignments"])
          and u2round["assignments"] == [], str(u2round["assignments"]))
    check("former reviewer manuscript detail -> 404",
          u2c.get(f"/api/reviewer/rounds/{E}/manuscripts/{e2m}", expect=404).status_code == 404)
    # isolation: replacement reviewer (u1) sees a clean blank slate, no draft,
    # no author secret while the round is open
    na = u1c.get(f"/api/reviewer/assignments/{new_id}").json()
    check("replacement sees blank new assignment",
          na["status"] == "draft" and na["revision"] == 0 and na["content"] == ""
          and na["recusal_reason"] is None, str(na))
    u1text = u1c.get(f"/api/reviewer/rounds/{E}").text
    check("old draft text never reaches replacement API",
          "U2-SECRET-RECUSED-DRAFT" not in u1text and "u2_" not in u1text, u1text[:300])
    check("author hidden from replacement while open", secret not in u1text)
    check("replacement manuscript detail anonymous",
          "author" not in u1c.get(f"/api/reviewer/rounds/{E}/manuscripts/{e2m}").json())
    # history is immutable: cannot delete, cannot submit, cannot conflict-clean
    check("admin cannot delete recused history",
          admin.delete(f"/api/admin/rounds/{E}/assignments/{e_a2}", expect=400).status_code == 400)
    check("re-assigning manuscript to recused reviewer blocked by conflict",
          admin.post(f"/api/admin/rounds/{E}/assignments",
                     {"manuscript_id": e2m, "reviewer_id": u2}, expect=400).status_code == 400)
    check("duplicate replacement assignment rejected",
          admin.post(f"/api/admin/rounds/{E}/assignments",
                     {"manuscript_id": e2m, "reviewer_id": u1}, expect=400).status_code == 400)
    check("approving an already decided recusal -> 404",
          admin.post(f"/api/admin/recusals/{recusal_id}/approve",
                     {"replacement_reviewer_id": u4}, expect=404).status_code == 404)

    print("[14] recusal barrier races against submit / freeze / approval")
    # race A: recusal vs concurrent submit on one draft assignment (round F).
    # Whichever takes the round lock first wins; the loser must fail; exactly
    # one outcome occurs, never a mixed/inconsistent state.
    F = admin.post("/api/admin/rounds", {"name": f"F-{run}"}).json()["id"]
    fm = admin.post(f"/api/admin/rounds/{F}/manuscripts",
                    {"manuscripts": [{"title": f"RaceF {run}", "author": f"{secret}F1"}]}).json()["manuscripts"][0]["id"]
    admin.post(f"/api/admin/rounds/{F}/reviewers", {"reviewer_ids": [u2]})
    f_a = admin.post(f"/api/admin/rounds/{F}/assignments",
                     {"manuscript_id": fm, "reviewer_id": u2}).json()["id"]
    race = {}
    barrier = threading.Barrier(2)

    def race_recusal():
        c = Client(token_u2)
        barrier.wait()
        race["recusal"] = c.raw(
            "POST", f"/api/reviewer/assignments/{f_a}/recusal", {"reason": "race"})

    def race_submit():
        c = Client(token_u2)
        barrier.wait()
        race["submit"] = c.raw(
            "POST", f"/api/reviewer/assignments/{f_a}/submit",
            {"content": "FINAL-F", "expected_revision": 0})

    t1 = threading.Thread(target=race_recusal)
    t2 = threading.Thread(target=race_submit)
    t1.start(); t2.start(); t1.join(); t2.join()
    fstate = u2c.get(f"/api/reviewer/assignments/{f_a}").json()
    check("recusal vs submit: exactly one of 200/200 winner and one 4xx",
          (race["recusal"].status_code, race["submit"].status_code) in
          ((200, 409), (400, 200)),
          str({k: v.status_code for k, v in race.items()}))
    check("final state matches the winning request",
          (race["recusal"].status_code == 200 and fstate["status"] == "recusal_pending") or
          (race["submit"].status_code == 200 and fstate["status"] == "submitted"
           and fstate["content"] == "FINAL-F"),
          fstate.get("status", ""))

    # race B: approve-recusal (with a full replacement) vs freeze on round G.
    # Both take the round lock first; if approval wins it fails (slot cap) and
    # freeze still sees the pending recusal -> 409; if freeze wins it 409s on
    # the pending recusal and approval then fails on slots. Either way nothing
    # changes and no frozen-but-pending state is possible.
    G = admin.post("/api/admin/rounds", {"name": f"G-{run}"}).json()["id"]
    gm = admin.post(f"/api/admin/rounds/{G}/manuscripts",
                    {"manuscripts": [
                        {"title": f"RaceG1 {run}", "author": f"{secret}G1"},
                        {"title": f"RaceG2 {run}", "author": f"{secret}G2"},
                        {"title": f"RaceG3 {run}", "author": f"{secret}G3"},
                        {"title": f"RaceG4 {run}", "author": f"{secret}G4"},
                    ]}).json()["manuscripts"]
    # u4 enrolled now (needed as an approval candidate later); u3 receives
    # manuscripts 2-4, i.e. all 3 live slots in round G.
    admin.post(f"/api/admin/rounds/{G}/reviewers", {"reviewer_ids": [u2, u3, u4]})
    g_a = admin.post(f"/api/admin/rounds/{G}/assignments",
                     {"manuscript_id": gm[0]["id"], "reviewer_id": u2}).json()["id"]
    g_ids = []
    for mm in gm[1:]:
        g_ids.append(admin.post(f"/api/admin/rounds/{G}/assignments",
                                {"manuscript_id": mm["id"], "reviewer_id": u3}).json()["id"])
    grc = u2c.post(f"/api/reviewer/assignments/{g_a}/recusal", {"reason": "race"}).json()["id"]
    race = {}
    barrier = threading.Barrier(2)

    def race_approve():
        c = Client(token_admin)
        barrier.wait()
        race["approve"] = c.raw(
            "POST", f"/api/admin/recusals/{grc}/approve",
            {"replacement_reviewer_id": u3})

    def race_freeze():
        c = Client(token_admin)
        barrier.wait()
        race["freeze"] = c.raw("POST", f"/api/admin/rounds/{G}/freeze")

    t1 = threading.Thread(target=race_approve)
    t2 = threading.Thread(target=race_freeze)
    t1.start(); t2.start(); t1.join(); t2.join()
    check("approve-vs-freeze: approval fails on slot cap",
          race["approve"].status_code == 400, race["approve"].text[:200])
    check("approve-vs-freeze: freeze fails while pending",
          race["freeze"].status_code == 409, race["freeze"].text[:200])
    gstate = admin.get(f"/api/admin/rounds/{G}").json()
    check("round G stays open with recusal still pending",
          gstate["status"] == "open" and gstate["recusal_pending_count"] == 1
          and len(gstate["assignments"]) == 4,
          str((gstate["status"], gstate["recusal_pending_count"], len(gstate["assignments"]))))

    # race C: two concurrent approvals of the same pending recusal by two
    # eligible candidates (u4 and the freshly enrolled u5). Only one can win
    # the recusal row; the loser gets 404 and leaves no second replacement.
    admin.post(f"/api/admin/rounds/{G}/reviewers", {"reviewer_ids": [u5]})
    race = {}
    barrier = threading.Barrier(2)

    def race_approve_one(worker, reviewer):
        c = Client(token_admin)
        barrier.wait()
        race[worker] = c.raw(
            "POST", f"/api/admin/recusals/{grc}/approve",
            {"replacement_reviewer_id": reviewer})

    t1 = threading.Thread(target=race_approve_one, args=("w1", u4))
    t2 = threading.Thread(target=race_approve_one, args=("w2", u5))
    t1.start(); t2.start(); t1.join(); t2.join()
    codes = sorted(v.status_code for v in race.values())
    check("concurrent approvals: one 200 one 404", codes == [200, 404], str(codes))
    winner_id = [v for v in race.values() if v.status_code == 200][0].json()["replacement_assignment_id"]
    gstate = admin.get(f"/api/admin/rounds/{G}").json()
    check("exactly one replacement assignment and one recused row exist",
          len(gstate["assignments"]) == 5
          and len([a for a in gstate["assignments"] if a["status"] == "recused"]) == 1
          and len([a for a in gstate["assignments"]
                   if a["manuscript_id"] == gm[0]["id"] and a["status"] != "recused"]) == 1,
          str([(a["id"], a["reviewer_name"], a["status"]) for a in gstate["assignments"]]))
    check("surviving replacement assignment is the 200 response one",
          any(a["id"] == winner_id for a in gstate["assignments"]), str(winner_id))
    check("conflict row for old reviewer exists exactly once",
          len([c for c in gstate["conflicts"]
               if c["manuscript_id"] == gm[0]["id"] and c["reviewer_id"] == u2]) == 1,
          str(gstate["conflicts"]))

    # end-to-end: the new replacement can finish and the round then freezes;
    # after freeze the former reviewer still cannot see the manuscript.
    gnew = [a for a in gstate["assignments"] if a["manuscript_id"] == gm[0]["id"]
            and a["status"] != "recused"][0]
    new_owner = gnew["reviewer_id"]
    new_client = {u4: tokens["u4"], u5: tokens["u5"]}[new_owner]
    new_client.post(f"/api/reviewer/assignments/{gnew['id']}/submit",
                    {"content": "FINAL-G1", "expected_revision": 0})
    u3c.post(f"/api/reviewer/assignments/{g_ids[0]}/submit",
             {"content": "FINAL-G2", "expected_revision": 0})
    u3c.post(f"/api/reviewer/assignments/{g_ids[1]}/submit",
             {"content": "FINAL-G3", "expected_revision": 0})
    g4_submit = u3c.post(f"/api/reviewer/assignments/{g_ids[2]}/submit",
                         {"content": "FINAL-G4", "expected_revision": 0})
    check("u3 submits all three round-G reviews", g4_submit.status_code == 200, g4_submit.text)
    fr = admin.post(f"/api/admin/rounds/{G}/freeze")
    check("freeze G succeeds once replacement finishes", fr.status_code == 200, fr.text[:200])
    check("recused history still 404 to former reviewer after freeze",
          u2c.get(f"/api/reviewer/assignments/{g_a}", expect=404).status_code == 404)
    check("former reviewer still barred from manuscript detail after freeze",
          u2c.get(f"/api/reviewer/rounds/{G}/manuscripts/{gm[0]['id']}", expect=404).status_code == 404)
    gfrozen = admin.get(f"/api/admin/rounds/{G}").json()
    check("recused row preserved in frozen round",
          any(a["id"] == g_a and a["status"] == "recused" for a in gfrozen["assignments"]),
          str([(a["id"], a["status"]) for a in gfrozen["assignments"]]))
    check("recusal reject blocked after freeze",
          admin.post(f"/api/admin/recusals/{grc}/reject", expect=409).status_code == 409)
    check("recusal approve blocked after freeze",
          admin.post(f"/api/admin/recusals/{grc}/approve",
                     {"replacement_reviewer_id": u4}, expect=409).status_code == 409)
    check("new recusal on frozen round rejected",
          new_client.post(f"/api/reviewer/assignments/{gnew['id']}/recusal",
                          {"reason": "x"}, expect=409).status_code == 409)

    print("[15] concurrent assignment slot race behind the round lock")
    H2 = admin.post("/api/admin/rounds", {"name": f"H2-{run}"}).json()["id"]
    h2m = admin.post(f"/api/admin/rounds/{H2}/manuscripts", {"manuscripts": [
        {"title": "h2a", "author": f"{secret}H2a"},
        {"title": "h2b", "author": f"{secret}H2b"},
        {"title": "h2c", "author": f"{secret}H2c"},
        {"title": "h2d", "author": f"{secret}H2d"},
    ]}).json()["manuscripts"]
    admin.post(f"/api/admin/rounds/{H2}/reviewers", {"reviewer_ids": [u2]})
    # one pre-existing assignment leaves exactly two of the three slots free
    admin.post(f"/api/admin/rounds/{H2}/assignments",
               {"manuscript_id": h2m[0]["id"], "reviewer_id": u2})
    slot_outcomes = {}
    barrier = threading.Barrier(3)

    def race_assign(worker, mid):
        c = Client(token_admin)
        barrier.wait()
        slot_outcomes[worker] = c.raw("POST", f"/api/admin/rounds/{H2}/assignments",
                                      {"manuscript_id": mid, "reviewer_id": u2})

    threads = [threading.Thread(target=race_assign, args=(f"w{i}", h2m[i + 1]["id"])) for i in range(3)]
    for t in threads: t.start()
    for t in threads: t.join()
    scodes = sorted(r.status_code for r in slot_outcomes.values())
    check("3 concurrent assignments for 2 remaining slots: two 200 one 400",
          scodes == [200, 200, 400], str(scodes))
    h2state = admin.get(f"/api/admin/rounds/{H2}").json()
    check("exactly 3 live assignments for reviewer after race",
          len([a for a in h2state["assignments"] if a["reviewer_id"] == u2
               and a["status"] != "recused"]) == 3,
          str([(a["id"], a["status"]) for a in h2state["assignments"]]))
    # rejected request created no assignment: total live rows == 3
    check("no extra row from the losing request",
          h2state["total_assignments"] == 3, str(h2state["total_assignments"]))

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
