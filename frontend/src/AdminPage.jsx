import React, { useCallback, useEffect, useState } from "react";
import { api, ApiError } from "./api.js";

function useError() {
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const wrap = useCallback(async (fn, okMsg) => {
    setError("");
    try {
      const result = await fn();
      if (okMsg) setNotice(okMsg);
      return result;
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "请求失败");
      throw err;
    }
  }, []);
  return { error, setError, notice, setNotice, wrap };
}

function ReviewerAccounts({ changed }) {
  const [reviewers, setReviewers] = useState([]);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const { error, setError, wrap } = useError();

  const load = useCallback(() => {
    api("/api/admin/reviewers").then(setReviewers).catch(() => {});
  }, []);
  useEffect(load, [load]);

  async function create(e) {
    e.preventDefault();
    setError("");
    try {
      await wrap(() => api("/api/admin/reviewers", { method: "POST", body: { username, password } }));
      setUsername("");
      setPassword("");
      load();
      changed();
    } catch {
      /* shown inline */
    }
  }

  return (
    <div className="card">
      <h2>评审员账号</h2>
      <form className="row" onSubmit={create}>
        <input placeholder="用户名" value={username} onChange={(e) => setUsername(e.target.value)} />
        <input
          type="password"
          placeholder="初始密码"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
        />
        <button>创建评审员</button>
      </form>
      {error && <div className="error">{error}</div>}
      <p className="muted">
        已有评审员：{reviewers.length === 0 ? "无" : reviewers.map((r) => r.username).join("、")}
      </p>
    </div>
  );
}

function RoundManager({ roundId, allReviewers, onChanged }) {
  const [round, setRound] = useState(null);
  const [mssText, setMssText] = useState("");
  const [enrollIds, setEnrollIds] = useState([]);
  const { error, setError, notice, setNotice, wrap } = useError();

  const load = useCallback(() => {
    api(`/api/admin/rounds/${roundId}`).then(setRound).catch(() => {});
  }, [roundId]);
  useEffect(load, [roundId]);

  if (!round) return <div className="card">加载中…</div>;

  const frozen = round.status === "frozen";
  const enrolledIds = new Set(round.reviewers.map((r) => r.user_id));
  const conflictKey = (mId, rId) => `${mId}-${rId}`;
  const conflictSet = new Set(round.conflicts.map((c) => conflictKey(c.manuscript_id, c.reviewer_id)));
  const assignedMap = new Map();
  for (const a of round.assignments) {
    if (!assignedMap.has(a.manuscript_id)) assignedMap.set(a.manuscript_id, []);
    assignedMap.get(a.manuscript_id).push(a);
  }
  const reviewerName = (id) => {
    const r = round.reviewers.find((x) => x.user_id === id) || allReviewers.find((x) => x.id === id);
    return r ? r.username : `#${id}`;
  };

  async function refresh(fn, msg) {
    try {
      await wrap(fn, msg);
      load();
      onChanged();
    } catch {
      /* shown inline */
    }
  }

  async function addManuscripts(e) {
    e.preventDefault();
    const manuscripts = mssText
      .split("\n")
      .map((line) => line.trim())
      .filter(Boolean)
      .map((line) => {
        const [title, author] = line.split("|").map((s) => s.trim());
        return { title, author };
      })
      .filter((m) => m.title && m.author);
    if (manuscripts.length === 0) {
      setError("每行格式：标题 | 作者");
      return;
    }
    await refresh(() => api(`/api/admin/rounds/${roundId}/manuscripts`, {
      method: "POST",
      body: { manuscripts },
    }), "稿件已添加");
    setMssText("");
  }

  function toggleEnroll(id) {
    setEnrollIds((ids) => (ids.includes(id) ? ids.filter((x) => x !== id) : [...ids, id]));
  }

  async function submitEnroll() {
    if (enrollIds.length === 0) return;
    await refresh(() => api(`/api/admin/rounds/${roundId}/reviewers`, {
      method: "POST",
      body: { reviewer_ids: enrollIds },
    }), "评审员已加入本轮");
    setEnrollIds([]);
  }

  async function toggleConflict(manuscriptId, reviewerId) {
    const exists = conflictSet.has(conflictKey(manuscriptId, reviewerId));
    await refresh(
      () =>
        exists
          ? api(`/api/admin/rounds/${roundId}/conflicts/${manuscriptId}/${reviewerId}`, {
              method: "DELETE",
            })
          : api(`/api/admin/rounds/${roundId}/conflicts`, {
              method: "POST",
              body: { manuscript_id: manuscriptId, reviewer_id: reviewerId },
            }),
      exists ? "冲突已移除" : "冲突已登记"
    );
  }

  async function addAssignment(manuscriptId, reviewerId) {
    if (!reviewerId) return;
    await refresh(() => api(`/api/admin/rounds/${roundId}/assignments`, {
      method: "POST",
      body: { manuscript_id: manuscriptId, reviewer_id: Number(reviewerId) },
    }), "分配已创建");
  }

  async function removeAssignment(a) {
    await refresh(() => api(`/api/admin/rounds/${roundId}/assignments/${a.id}`, {
      method: "DELETE",
    }), "分配已删除");
  }

  async function freeze() {
    setNotice("");
    try {
      await wrap(() => api(`/api/admin/rounds/${roundId}/freeze`, { method: "POST" }));
      load();
      onChanged();
    } catch {
      /* shown inline */
    }
  }

  const availableReviewers = allReviewers.filter((r) => !enrolledIds.has(r.id));

  return (
    <div className="card">
      <div className="row" style={{ justifyContent: "space-between" }}>
        <h2 style={{ margin: 0 }}>
          第 {round.id} 轮：{round.name}{" "}
          <span className={`badge ${frozen ? "frozen" : "open"}`}>
            {frozen ? "已冻结（身份已揭示）" : "开放中（匿名）"}
          </span>
        </h2>
        {!frozen && (
          <button
            onClick={freeze}
            disabled={round.submitted_assignments !== round.total_assignments || round.total_assignments === 0}
            title={
              round.total_assignments === 0
                ? "尚无分配"
                : `${round.submitted_assignments}/${round.total_assignments} 已最终提交`
            }
          >
            原子冻结整轮（{round.submitted_assignments}/{round.total_assignments} 已提交）
          </button>
        )}
      </div>
      {error && <div className="error">{error}</div>}
      {notice && <div className="notice">{notice}</div>}

      {!frozen && (
        <form onSubmit={addManuscripts}>
          <p className="muted">
            添加稿件，每行一条，格式「标题 | 作者」（本轮至多 20 份）：
          </p>
          <textarea value={mssText} onChange={(e) => setMssText(e.target.value)} />
          <button>添加稿件</button>
        </form>
      )}

      <h3>评审员名单（至多 10 名）</h3>
      <p>
        {round.reviewers.map((r) => (
          <span key={r.user_id} className="badge draft" style={{ marginRight: 6 }}>
            {r.username}
          </span>
        ))}
      </p>
      {!frozen && availableReviewers.length > 0 && (
        <div className="row">
          {availableReviewers.map((r) => (
            <label key={r.id} className="muted">
              <input
                type="checkbox"
                checked={enrollIds.includes(r.id)}
                onChange={() => toggleEnroll(r.id)}
              />{" "}
              {r.username}
            </label>
          ))}
          <button className="secondary" onClick={submitEnroll} disabled={enrollIds.length === 0}>
            加入本轮
          </button>
        </div>
      )}

      <h3>稿件、冲突与分配</h3>
      <table>
        <thead>
          <tr>
            <th>稿号</th>
            <th>标题</th>
            <th>{frozen ? "作者（已揭示）" : "作者（仅管理员可见）"}</th>
            <th>利益冲突评审员</th>
            <th>已分配评审员</th>
            {!frozen && <th>操作</th>}
          </tr>
        </thead>
        <tbody>
          {round.manuscripts.map((m) => {
            const assigned = assignedMap.get(m.id) || [];
            const assignedIds = new Set(assigned.map((a) => a.reviewer_id));
            return (
              <tr key={m.id}>
                <td>#{m.number}</td>
                <td>{m.title}</td>
                <td>{m.author}</td>
                <td>
                  {round.reviewers.map((r) => (
                    <label key={r.user_id} style={{ marginRight: 8, display: "inline-block" }}>
                      <input
                        type="checkbox"
                        disabled={frozen || assignedIds.has(r.user_id)}
                        checked={conflictSet.has(conflictKey(m.id, r.user_id))}
                        onChange={() => toggleConflict(m.id, r.user_id)}
                      />{" "}
                      {r.username}
                    </label>
                  ))}
                </td>
                <td>
                  {assigned.map((a) => (
                    <span key={a.id} style={{ marginRight: 8 }}>
                      {reviewerName(a.reviewer_id)}
                      <span className={`badge ${a.status}`} style={{ marginLeft: 4 }}>
                        {a.status === "draft" ? `草稿 r${a.revision}` : "已提交"}
                      </span>
                      {!frozen && a.status === "draft" && (
                        <button className="danger" style={{ padding: "1px 8px", marginLeft: 4 }}
                          onClick={() => removeAssignment(a)}>
                          ×
                        </button>
                      )}
                    </span>
                  ))}
                </td>
                {!frozen && (
                  <td>
                    <select
                      defaultValue=""
                      onChange={(e) => addAssignment(m.id, e.target.value)}
                      disabled={
                        assigned.length >= round.reviewers.length
                      }
                    >
                      <option value="">分配给…</option>
                      {round.reviewers
                        .filter((r) => !assignedIds.has(r.user_id))
                        .map((r) => (
                          <option
                            key={r.user_id}
                            value={r.user_id}
                            disabled={conflictSet.has(conflictKey(m.id, r.user_id))}
                          >
                            {r.username}
                            {conflictSet.has(conflictKey(m.id, r.user_id)) ? "（冲突）" : ""}
                          </option>
                        ))}
                    </select>
                  </td>
                )}
              </tr>
            );
          })}
        </tbody>
      </table>
      <p className="muted">
        规则：存在利益冲突的分配无法提交；每名评审员至多 3 份（超限的分配请求会被服务器拒绝）。
      </p>
    </div>
  );
}

export default function AdminPage() {
  const [rounds, setRounds] = useState([]);
  const [allReviewers, setAllReviewers] = useState([]);
  const [selected, setSelected] = useState(null);
  const [newName, setNewName] = useState("");
  const { error, setError, wrap } = useError();

  const load = useCallback(() => {
    api("/api/admin/rounds").then(setRounds).catch(() => {});
    api("/api/admin/reviewers").then(setAllReviewers).catch(() => {});
  }, []);
  useEffect(load, [load]);

  async function createRound(e) {
    e.preventDefault();
    setError("");
    try {
      const rnd = await wrap(() => api("/api/admin/rounds", {
        method: "POST",
        body: { name: newName },
      }));
      setNewName("");
      setSelected(rnd.id);
      load();
    } catch {
      /* shown */
    }
  }

  return (
    <>
      <ReviewerAccounts changed={load} />
      <div className="card">
        <h2>评审轮次</h2>
        <form className="row" onSubmit={createRound}>
          <input
            placeholder="新一轮名称（如：2026 春季）"
            value={newName}
            onChange={(e) => setNewName(e.target.value)}
          />
          <button>创建轮次</button>
        </form>
        {error && <div className="error">{error}</div>}
        <table>
          <thead>
            <tr>
              <th>ID</th>
              <th>名称</th>
              <th>状态</th>
              <th>稿件</th>
              <th>提交进度</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {rounds.map((r) => (
              <tr key={r.id}>
                <td>{r.id}</td>
                <td>{r.name}</td>
                <td>
                  <span className={`badge ${r.status}`}>
                    {r.status === "frozen" ? "已冻结" : "开放中"}
                  </span>
                </td>
                <td>{r.manuscript_count}</td>
                <td>
                  {r.submitted_count}/{r.assignment_count}
                </td>
                <td>
                  <button className="secondary" onClick={() => setSelected(r.id)}>
                    管理
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {selected !== null && (
        <RoundManager roundId={selected} allReviewers={allReviewers} onChanged={load} />
      )}
    </>
  );
}
