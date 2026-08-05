import { Navigate, Outlet } from "react-router-dom";
import { useAuth } from "./AuthContext";

/** 未登录时重定向到 /login；登录态由 localStorage 恢复，401 自动登出。 */
export default function ProtectedRoute() {
  const { user } = useAuth();
  if (!user) {
    return <Navigate to="/login" replace />;
  }
  return <Outlet />;
}
