/** 跨模块的认证失效事件：任何 API 返回 401 时广播，AuthProvider 监听后清除登录态。 */
export const UNAUTHORIZED_EVENT = "mba:unauthorized";

export function emitUnauthorized(): void {
  window.dispatchEvent(new Event(UNAUTHORIZED_EVENT));
}
