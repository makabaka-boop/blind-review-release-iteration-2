import React, { useState } from "react";
import { api, clearSession, getSessionUser, setSession, ApiError } from "./api.js";
import AdminPage from "./AdminPage.jsx";
import ReviewerPage from "./ReviewerPage.jsx";

function Login({ onLogin }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(e) {
    e.preventDefault();
    setError("");
    setBusy(true);
    try {
      const data = await api("/api/login", {
        method: "POST",
        body: { username, password },
        auth: false,
      });
      setSession(data.token, data.user);
      onLogin(data.user);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "login failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="card login-card" onSubmit={submit}>
      <h2>同行评审系统 · 登录</h2>
      <input
        placeholder="用户名"
        value={username}
        onChange={(e) => setUsername(e.target.value)}
        autoComplete="username"
      />
      <input
        type="password"
        placeholder="密码"
        value={password}
        onChange={(e) => setPassword(e.target.value)}
        autoComplete="current-password"
      />
      {error && <div className="error">{error}</div>}
      <button disabled={busy}>{busy ? "登录中…" : "登录"}</button>
    </form>
  );
}

export default function App() {
  const [user, setUser] = useState(() => getSessionUser());

  function logout() {
    clearSession();
    setUser(null);
  }

  if (!user) return <Login onLogin={setUser} />;

  return (
    <div className="app">
      <div className="topbar">
        <h1>同行评审系统</h1>
        <div className="row">
          <span className="muted">
            {user.username}（{user.role === "admin" ? "管理员" : "评审员"}）
          </span>
          <button className="secondary" onClick={logout}>
            退出
          </button>
        </div>
      </div>
      {user.role === "admin" ? <AdminPage /> : <ReviewerPage />}
    </div>
  );
}
