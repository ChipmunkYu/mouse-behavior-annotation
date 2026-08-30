import { useEffect, useState } from "react";
import { Link, Outlet, useLocation, useParams } from "react-router-dom";
import { useAuth } from "../auth/AuthContext";
import { listProjects } from "../api";
import type { Project } from "../api/types";
import { UploadManagerProvider, useUploadManager } from "../upload/UploadManagerContext";
import UploadTaskTray from "../upload/UploadTaskTray";

/** 项目内二级导航（按项目角色显示合理入口）。 */
interface NavItem {
  label: string;
  to: (pid: number) => string;
  visible: (project: Project) => boolean;
}

const PROJECT_NAV: NavItem[] = [
  { label: "视频库", to: (pid) => `/projects/${pid}/videos`, visible: () => true },
  { label: "片段库", to: (pid) => `/projects/${pid}/clips`, visible: () => true },
  { label: "审核", to: (pid) => `/projects/${pid}/review`, visible: (p) => p.can_review },
  // 批次 6：导出入口放在审核之后，仅 owner / admin 可见（与导出权限一致）
  { label: "导出", to: (pid) => `/projects/${pid}/export`, visible: (p) => p.role === "owner" || p.role === "admin" },
  { label: "项目管理", to: (pid) => `/projects/${pid}/manage`, visible: (p) => p.role === "owner" || p.role === "admin" },
];

/** 登录后页面共用外壳：顶栏 + 项目导航 + 内容区。 */
export default function AppLayout() {
  return <UploadManagerProvider><AppLayoutContent /></UploadManagerProvider>;
}

function AppLayoutContent() {
  const { user, logout } = useAuth();
  const { cancelAllForLogout } = useUploadManager();
  const location = useLocation();
  const { projectId } = useParams<{ projectId: string }>();
  const [project, setProject] = useState<Project | null>(null);
  const [loggingOut, setLoggingOut] = useState(false);

  const pid = projectId ? Number(projectId) : null;

  useEffect(() => {
    let alive = true;
    if (pid == null || !Number.isFinite(pid)) {
      setProject(null);
      return;
    }
    setProject(null);
    listProjects()
      .then((projs) => {
        if (alive) setProject(projs.find((p) => p.id === pid) ?? null);
      })
      .catch(() => {
        /* 导航加载失败不阻塞页面 */
      });
    return () => {
      alive = false;
    };
  }, [pid]);

  const navItems = pid != null && project != null ? PROJECT_NAV.filter((n) => n.visible(project)) : [];

  async function handleLogout() {
    if (loggingOut) return;
    setLoggingOut(true);
    await cancelAllForLogout();
    await logout();
  }

  function isActive(item: NavItem): boolean {
    const path = item.to(pid as number);
    const p = location.pathname;
    if (path.endsWith("/videos") && p.includes("/annotate/")) return true;
    return p.startsWith(path);
  }

  const routeLabel = (): string => {
    const p = location.pathname;
    if (p.includes("/annotate/")) return "行为标注工作台";
    if (p.includes("/review")) return "审核工作台";
    if (p.includes("/export")) return "导出";
    if (p.includes("/clips")) return "片段库";
    if (p.includes("/manage")) return "项目管理";
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
          <UploadTaskTray />
          {user ? (
            <span className="topbar-user">
              <span className="avatar" aria-hidden="true">
                {user.username.slice(0, 1).toUpperCase()}
              </span>
              <span className="topbar-username">{user.username}</span>
              <button type="button" className="btn btn-ghost btn-sm" disabled={loggingOut} onClick={() => void handleLogout()}>
                {loggingOut ? "正在退出…" : "退出登录"}
              </button>
            </span>
          ) : null}
        </div>
      </header>
      {navItems.length > 0 ? (
        <nav className="project-nav" aria-label="项目导航">
          {navItems.map((item) => (
            <Link
              key={item.label}
              to={item.to(pid as number)}
              className={isActive(item) ? "active" : undefined}
            >
              {item.label}
            </Link>
          ))}
        </nav>
      ) : null}
      <main className="main">
        <Outlet />
      </main>
    </div>
  );
}
