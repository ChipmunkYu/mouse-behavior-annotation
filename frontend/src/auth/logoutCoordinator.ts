import { API_BASE } from "../api/client";

export const LOGOUT_WARNING = "服务端注销未完成，请重新登录后重试。";

export type LogoutResult = { accepted: true } | { accepted: false };
export type LogoutFetch = (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>;
export type LogoutCoordinator = {
  (): Promise<LogoutResult>;
  waitForInFlight: () => Promise<void>;
};

/** 创建可注入 fetch 的单飞协调器；它刻意不依赖 apiFetch/401 处理链。 */
export function createLogoutCoordinator(
  fetcher: LogoutFetch = fetch,
  apiBase: string = API_BASE
): LogoutCoordinator {
  let inFlight: Promise<LogoutResult> | null = null;

  const logout = (() => {
    if (inFlight) return inFlight;
    inFlight = (async (): Promise<LogoutResult> => {
      try {
        const response = await fetcher(`${apiBase}/auth/logout`, {
          method: "POST",
          credentials: "same-origin",
        });
        return response.ok ? { accepted: true } : { accepted: false };
      } catch {
        return { accepted: false };
      }
    })();
    inFlight.finally(() => {
      inFlight = null;
    });
    return inFlight;
  }) as LogoutCoordinator;

  logout.waitForInFlight = async (): Promise<void> => {
    const current = inFlight;
    if (current) await current;
  };

  return logout;
}

export const coordinateLogout = createLogoutCoordinator();
