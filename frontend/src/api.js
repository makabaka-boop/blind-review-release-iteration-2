const TOKEN_KEY = "pr_token";
const USER_KEY = "pr_user";

export function getToken() {
  return sessionStorage.getItem(TOKEN_KEY);
}

export function getSessionUser() {
  const raw = sessionStorage.getItem(USER_KEY);
  return raw ? JSON.parse(raw) : null;
}

export function setSession(token, user) {
  sessionStorage.setItem(TOKEN_KEY, token);
  sessionStorage.setItem(USER_KEY, JSON.stringify(user));
}

export function clearSession() {
  sessionStorage.removeItem(TOKEN_KEY);
  sessionStorage.removeItem(USER_KEY);
}

export class ApiError extends Error {
  constructor(status, detail) {
    const message =
      detail && typeof detail === "object" && detail.message
        ? detail.message
        : typeof detail === "string"
          ? detail
          : `request failed (${status})`;
    super(message);
    this.status = status;
    this.detail = detail;
  }
}

export async function api(path, { method = "GET", body, auth = true } = {}) {
  const headers = {};
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (auth) {
    const token = getToken();
    if (token) headers["Authorization"] = `Bearer ${token}`;
  }
  const res = await fetch(path, {
    method,
    headers,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  let data = null;
  const text = await res.text();
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = text;
    }
  }
  if (!res.ok) {
    throw new ApiError(res.status, data && data.detail !== undefined ? data.detail : data);
  }
  return data;
}
