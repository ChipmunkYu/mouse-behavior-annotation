import { Link, Outlet, useLocation } from "react-router-dom";
import { useAuth } from "../auth/AuthContext";

/** 登录后页面共用外壳：顶部导航 + 内容区。 */
export default function AppLayout() {
  const { user, logout } = useAuth();
  const location = useLocation();

  const routeLabel = (): string => {
    const p = location.pathname;
    if (p.includes("/annotate/")) return "标注工作台";
    if (p.includes("/videos")) return "视频库";
    if (p.includes("/projects")) return "项目";
    return "";
  };

  return (
    <div className="app">
      <header className="topbar">
        <div className="topbar-left">
          <Link to="/projects" className="brand" title="返回项目列表">
            行为标注平台
          </Link>
          {routeLabel() ? <span className="topbar-route">{routeLabel()}</span> : null}
        </div>
        <div className="topbar-right">
          {user ? (
            <span className="topbar-user">
              <span className="avatar" aria-hidden="true">
                {user.username.slice(0, 1).toUpperCase()}
              </span>
              <span className="topbar-username">{user.username}</span>
              <button type="button" className="btn btn-ghost btn-sm" onClick={logout}>
                退出登录
              </button>
            </span>
          ) : null}
        </div>
      </header>
      <main className="main">
        <Outlet />
      </main>
    </div>
  );
}
