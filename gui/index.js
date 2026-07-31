api.get("/auth/me").then(({ ok }) => {
  location.replace(ok ? "dashboard.html" : "login.html");
});
