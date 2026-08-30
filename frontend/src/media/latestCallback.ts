export interface LatestCallback<T extends (...args: never[]) => void> {
  set(callback: T | undefined): void;
  invoke(...args: Parameters<T>): void;
}

/** 保持调用入口稳定，同时让入口始终转发到最新 callback。 */
export function createLatestCallback<T extends (...args: never[]) => void>(initial?: T): LatestCallback<T> {
  let latest = initial;
  return {
    set(callback) {
      latest = callback;
    },
    invoke(...args) {
      latest?.(...args);
    },
  };
}
