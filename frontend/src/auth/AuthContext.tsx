import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import type { User } from "../api/types";
import { login as loginApi } from "../api";
import { clearAuth, getUser, getToken, setAuth } from "./storage";
import { UNAUTHORIZED_EVENT } from "./events";
import { coordinateLogout, LOGOUT_WARNING } from "./logoutCoordinator";

interface AuthContextValue {
  user: User | null;
  token: string | null;
  login: (username: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
  logoutWarning: string | null;
  clearLogoutWarning: () => void;
}

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(() => getUser());
  const [token, setToken] = useState<string | null>(() => getToken());
  const [logoutWarning, setLogoutWarning] = useState<string | null>(null);

  const finishLogout = useCallback(async (): Promise<void> => {
    const result = await coordinateLogout();
    setLogoutWarning(result.accepted ? null : LOGOUT_WARNING);
  }, []);

  const logout = useCallback(async (): Promise<void> => {
    // 先阻止当前会话继续操作，再发送独立的服务端清理请求。
    clearAuth();
    setUser(null);
    setToken(null);
    await finishLogout();
  }, [finishLogout]);

  // 任意 API 返回 401（token 过期/被吊销）时自动登出
  useEffect(() => {
    const onUnauthorized = (): void => {
      clearAuth();
      setUser(null);
      setToken(null);
      void finishLogout();
    };
    window.addEventListener(UNAUTHORIZED_EVENT, onUnauthorized);
    return () => window.removeEventListener(UNAUTHORIZED_EVENT, onUnauthorized);
  }, [finishLogout]);

  const login = useCallback(async (username: string, password: string) => {
    await coordinateLogout.waitForInFlight();
    const res = await loginApi(username, password);
    setAuth(res.access_token, res.user);
    setToken(res.access_token);
    setUser(res.user);
    setLogoutWarning(null);
  }, []);

  const clearLogoutWarning = useCallback(() => setLogoutWarning(null), []);

  const value = useMemo(
    () => ({ user, token, login, logout, logoutWarning, clearLogoutWarning }),
    [user, token, login, logout, logoutWarning, clearLogoutWarning]
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth 必须在 AuthProvider 内使用");
  return ctx;
}
