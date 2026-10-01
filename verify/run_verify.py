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
