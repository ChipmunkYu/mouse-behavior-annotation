import { useState, type FormEvent } from "react";
import { Navigate, useNavigate } from "react-router-dom";
import { useAuth } from "../auth/AuthContext";
import { ApiError } from "../api/client";

export default function LoginPage() {
  const { user, login } = useAuth();
  const navigate = useNavigate();

  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [fieldError, setFieldError] = useState<{ username?: string; password?: string }>({});
  const [submitting, setSubmitting] = useState(false);

  if (user) {
    return <Navigate to="/projects" replace />;
  }

  function validate(): boolean {
    const fe: { username?: string; password?: string } = {};
    if (!username.trim()) fe.username = "请输入用户名";
    if (!password) fe.password = "请输入密码";
    setFieldError(fe);
    return Object.keys(fe).length === 0;
  }

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    if (!validate()) return;

    setSubmitting(true);
    try {
      await login(username.trim(), password);
      navigate("/projects", { replace: true });
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        setError("用户名或密码错误，请重试");
      } else if (err instanceof ApiError) {
        setError(err.message);
      } else {
        setError("网络错误，请确认后端服务已启动");
      }
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="login-page">
      <div className="card login-card">
        <div className="card-body">
          <div className="login-brand">
            <h1>行为标注平台</h1>
            <p>多目标行为事件标注 · 登录</p>
          </div>

          <div className="demo-hint">
            <span>开发账号</span>
            <code>demo</code>
            <span>/</span>
            <code>demo123</code>
          </div>

          <form onSubmit={handleSubmit} noValidate>
            <div className="field">
              <label htmlFor="login-username">用户名</label>
              <input
                id="login-username"
                className="input"
                type="text"
                value={username}
                autoComplete="username"
                placeholder="请输入用户名"
                onChange={(e) => setUsername(e.target.value)}
              />
              {fieldError.username ? <span className="form-error">{fieldError.username}</span> : null}
            </div>

            <div className="field">
              <label htmlFor="login-password">密码</label>
              <input
                id="login-password"
                className="input"
                type="password"
                value={password}
                autoComplete="current-password"
                placeholder="请输入密码"
                onChange={(e) => setPassword(e.target.value)}
              />
              {fieldError.password ? <span className="form-error">{fieldError.password}</span> : null}
            </div>

            <div className="form-error" role="alert">
              {error ?? ""}
            </div>

            <button type="submit" className="btn btn-primary" style={{ width: "100%" }} disabled={submitting}>
              {submitting ? "登录中…" : "登录"}
            </button>
          </form>
        </div>
      </div>
    </div>
  );
}
