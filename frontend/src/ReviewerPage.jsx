import React, { useCallback, useEffect, useState } from "react";
import { api, ApiError } from "./api.js";

function AssignmentEditor({ assignment, manuscript, frozen, onChanged }) {
  const [content, setContent] = useState(assignment.content || "");
  const [revision, setRevision] = useState(assignment.revision);
  const [status, setStatus] = useState(assignment.status);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    setContent(assignment.content || "");
    setRevision(assignment.revision);
    setStatus(assignment.status);
  }, [assignment.content, assignment.revision, assignment.status]);

  async function call(fn, okMsg) {
    setError("");
    setNotice("");
    setBusy(true);
    try {
      const data = await fn();
      setRevision(data.revision);
      setStatus(data.status);
      setNotice(okMsg);
      onChanged();
    } catch (err) {
      if (err instanceof ApiError && err.status === 409 && err.detail?.current_revision !== undefined) {
        setError(`修订号冲突：该评审已在别处被修改（当前修订 r${err.detail.current_revision}），已为您刷新`);
        onChanged();
      } else {
        setError(err instanceof ApiError ? err.message : "请求失败");
      }
    } finally {
      setBusy(false);
    }
  }

  const readOnly = frozen || status === "submitted";

  return (
    <div className="card">
      <div className="row" style={{ justifyContent: "space-between" }}>
        <h2 style={{ margin: 0 }}>
          稿号 #{manuscript?.number ?? "?"}：{manuscript?.title ?? ""}
        </h2>
        <span className={`badge ${status === "submitted" ? "submitted" : "draft"}`}>
          {status === "submitted" ? "已最终提交" : "草稿"} · r{revision}
        </span>
      </div>
      {manuscript?.author && (
        <p className="notice">
          本轮已冻结，作者身份揭示：<strong>{manuscript.author}</strong>
        </p>
      )}
      {error && <div className="error">{error}</div>}
      {notice && <div className="notice">{notice}</div>}
      <textarea
        value={content}
        onChange={(e) => setContent(e.target.value)}
        disabled={readOnly || busy}
        placeholder="在此撰写评审意见…"
      />
      {!readOnly && (
        <div className="row" style={{ marginTop: 8 }}>
          <button
            className="secondary"
            disabled={busy}
            onClick={() =>
              call(
                () =>
                  api(`/api/reviewer/assignments/${assignment.id}/draft`, {
                    method: "PUT",
                    body: { content, expected_revision: revision },
                  }),
                `草稿已保存（修订 r${revision + 1}）`
              )
            }
          >
            保存草稿（按当前修订号）
          </button>
          <button
            className="danger"
            disabled={busy}
            onClick={() =>
              call(
                () =>
                  api(`/api/reviewer/assignments/${assignment.id}/submit`, {
                    method: "POST",
                    body: { content, expected_revision: revision },
                  }),
                "已最终提交，内容不可再修改"
              )
            }
          >
            最终提交
          </button>
          <span className="muted">保存后修订号 +1；并发修改同一修订号时后到的一方会收到 409。</span>
        </div>
      )}
      {frozen && <p className="muted">本轮已冻结，评审内容只读。</p>}
    </div>
  );
}

export default function ReviewerPage() {
  const [rounds, setRounds] = useState([]);
  const [selected, setSelected] = useState(null);
  const [round, setRound] = useState(null);
  const [error, setError] = useState("");

  const loadRounds = useCallback(() => {
    api("/api/reviewer/rounds")
      .then((data) => {
        setRounds(data);
        if (selected === null && data.length > 0) setSelected(data[0].id);
      })
      .catch((err) => setError(err instanceof ApiError ? err.message : "请求失败"));
  }, [selected]);
  useEffect(loadRounds, [loadRounds]);

  const loadRound = useCallback(() => {
    if (selected === null) {
      setRound(null);
      return;
    }
    api(`/api/reviewer/rounds/${selected}`)
      .then(setRound)
      .catch((err) => setError(err instanceof ApiError ? err.message : "请求失败"));
  }, [selected]);
  useEffect(loadRound, [loadRound]);

  const manuscriptById = (id) => (round ? round.manuscripts.find((m) => m.id === id) : null);

  return (
    <>
      {error && <div className="error">{error}</div>}
      <div className="card">
        <h2>我的评审轮次</h2>
        {rounds.length === 0 ? (
          <p className="muted">您尚未被加入任何评审轮次。</p>
        ) : (
          <table>
            <thead>
              <tr>
                <th>ID</th>
                <th>名称</th>
                <th>状态</th>
                <th>我的分配 / 已提交</th>
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
                  <td>
                    {r.assignment_count}/{r.submitted_count}
                  </td>
                  <td>
                    <button className="secondary" onClick={() => setSelected(r.id)}>
                      进入
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {round && (
        <div className="card">
          <h2>
            {round.name}{" "}
            <span className={`badge ${round.status}`}>
              {round.status === "frozen" ? "已冻结 · 作者已揭示" : "开放中 · 稿件匿名"}
            </span>
          </h2>
          <p className="muted">
            本列表仅含匿名稿号与标题；冻结前不返回作者信息。您只能看到分配给自己的评审任务。
          </p>
          <table>
            <thead>
              <tr>
                <th>稿号</th>
                <th>标题</th>
                {round.status === "frozen" && <th>作者（已揭示）</th>}
              </tr>
            </thead>
            <tbody>
              {round.manuscripts.map((m) => (
                <tr key={m.id}>
                  <td>#{m.number}</td>
                  <td>{m.title}</td>
                  {round.status === "frozen" && <td>{m.author}</td>}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {round &&
        round.assignments.map((a) => (
          <AssignmentEditor
            key={a.id}
            assignment={a}
            manuscript={manuscriptById(a.manuscript_id)}
            frozen={round.status === "frozen"}
            onChanged={loadRound}
          />
        ))}
      {round && round.assignments.length === 0 && (
        <div className="card">
          <p className="muted">本轮暂无分配给您的稿件。</p>
        </div>
      )}
    </>
  );
}
