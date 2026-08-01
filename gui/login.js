// The login page has no nav, so it mounts the shared theme control itself.
renderThemeToggle(document.getElementById("theme-control"));

document.getElementById("login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const username = document.getElementById("username").value;
  const password = document.getElementById("password").value;
  const errorEl = document.getElementById("error");
  errorEl.textContent = "";

  const { ok, data } = await api.post("/auth/login", { username, password });
  if (!ok) {
    errorEl.textContent = friendlyError(data, "Login failed. Please try again.");
    return;
  }
  setCsrfToken(data.csrf_token);
  location.href = "dashboard.html";
});
