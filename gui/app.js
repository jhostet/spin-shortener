const API_BASE = "/api";

function getCsrfToken() {
  return sessionStorage.getItem("csrf_token");
}

function setCsrfToken(token) {
  if (token) {
    sessionStorage.setItem("csrf_token", token);
  } else {
    sessionStorage.removeItem("csrf_token");
  }
}

async function apiFetch(path, options = {}) {
  const method = (options.method || "GET").toUpperCase();
  const headers = Object.assign({}, options.headers);

  if (["POST", "PATCH", "PUT", "DELETE"].includes(method)) {
    const csrf = getCsrfToken();
    if (csrf) headers["X-CSRF-Token"] = csrf;
  }
  if (options.body && !headers["Content-Type"]) {
    headers["Content-Type"] = "application/json";
  }

  const response = await fetch(API_BASE + path, {
    ...options,
    headers,
    credentials: "same-origin",
  });

  if (response.status === 401 && !path.startsWith("/auth/login") && !location.pathname.endsWith("login.html")) {
    setCsrfToken(null);
    location.href = "login.html";
  }

  return response;
}

async function apiCall(path, options) {
  const res = await apiFetch(path, options);
  let data = null;
  try {
    data = await res.json();
  } catch {
    // no body
  }
  return { ok: res.ok, status: res.status, data };
}

const api = {
  get: (path) => apiCall(path),
  post: (path, body) => apiCall(path, { method: "POST", body: body !== undefined ? JSON.stringify(body) : undefined }),
  delete: (path) => apiCall(path, { method: "DELETE" }),
};

function escapeHtml(value) {
  const div = document.createElement("div");
  div.textContent = value;
  return div.innerHTML;
}
