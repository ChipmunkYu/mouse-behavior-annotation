import { useEffect, useState } from "react";
import { Link, Outlet, useLocation, useParams } from "react-router-dom";
import { useAuth } from "../auth/AuthContext";
import { listProjects, resetDemoData } from "../api";
import type { Project } from "../api/types";
import { DEMO_MODE } from "../demo/mode";
import { useConfirm } from "./ConfirmDialog";

/** 项目内二级导航（按项目角色显示合理入口）。 */
interface NavItem {
  label: string;
  to: (pid: number) => string;
  roles: string[];
}

const PROJECT_NAV: NavItem[] = [
  { label: "视频库", to: (pid) => `/projects/${pid}/videos`, roles: ["owner", "admin", "annotator", "reviewer"] },
  { label: "审核", to: (pid) => `/projects/${pid}/review`, roles: ["owner", "admin", "reviewer"] },
  { label: "片段库", to: (pid) => `/projects/${pid}/clips`, roles: ["owner", "admin", "annotator", "reviewer"] },
  { label: "导出", to: (pid) => `/projects/${pid}/export`, roles: ["owner", "admin"] },
  { label: "项目管理", to: (pid) => `/projects/${pid}/admin`, roles: ["owner", "admin"] },
];

/** 登录后页面共用外壳：顶栏（含演示模式徽标 + 重置入口）+ 项目导航 + 内容区。 */
export default function AppLayout() {
  const { user, logout } = useAuth();
  const location = useLocation();
  const { projectId } = useParams<{ projectId: string }>();
  const [project, setProject] = useState<Project | null>(null);
  const [confirmDialog, confirm] = useConfirm();

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

  const role = project?.role ?? null;
  const navItems = pid != null && role != null ? PROJECT_NAV.filter((n) => n.roles.includes(role)) : [];

  function isActive(item: NavItem): boolean {
    const path = item.to(pid as number);
    const p = location.pathname;
    if (path.endsWith("/videos") && p.includes("/annotate/")) return true;
    return p.startsWith(path);
  }

  async function handleResetDemo() {
    const ok = await confirm({
      title: "重置演示数据？",
      message: (
        <>
          将清除本地演示数据（项目、视频、标注、审核、导出任务等）并恢复初始种子，
          <br />
          页面将自动刷新。此操作仅影响演示模式数据。
        </>
      ),
      confirmLabel: "重置并刷新",
      danger: true,
    });
    if (!ok) return;
    resetDemoData();
    window.location.reload();
  }

  const routeLabel = (): string => {
    const p = location.pathname;
    if (p.includes("/annotate/")) return "标注工作台";
    if (p.includes("/review")) return "审核工作台";
    if (p.includes("/clips")) return "片段库";
    if (p.includes("/export")) return "导出中心";
    if (p.includes("/admin")) return "项目管理";
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
          {DEMO_MODE ? (
            <span className="demo-chip" role="note" title="npm run demo 演示模式：数据为本地模拟，不连接后端">
              <span className="demo-dot" aria-hidden="true" />
              演示模式
              <button type="button" className="btn btn-ghost btn-sm" onClick={() => void handleResetDemo()}>
                重置演示数据
              </button>
            </span>
          ) : null}
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
      {confirmDialog}
    </div>
  );
}
