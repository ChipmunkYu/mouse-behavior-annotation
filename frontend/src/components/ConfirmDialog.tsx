/**
 * 可复用的确认对话框 + useConfirm 钩子：
 * - confirm(options) 返回 Promise<boolean>，确认 resolve(true)，取消 / Esc / 点击遮罩 resolve(false)
 * - 取消不会发出任何请求，由调用方决定后续动作
 * - 键盘可达：打开时焦点落在「取消」；Esc 取消；关闭后焦点归还触发元素
 */
import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";

export interface ConfirmOptions {
  title: string;
  message: ReactNode;
  confirmLabel?: string;
  cancelLabel?: string;
  /** 危险操作：确认按钮使用红色（删除 / 退回等不可逆操作） */
  danger?: boolean;
}

/** 返回 [对话框元素, confirm 函数]；confirm 函数可在任意位置 await。 */
export function useConfirm(): [ReactNode, (options: ConfirmOptions) => Promise<boolean>] {
  const [options, setOptions] = useState<ConfirmOptions | null>(null);
  const resolveRef = useRef<((value: boolean) => void) | null>(null);
  const restoreFocusRef = useRef<HTMLElement | null>(null);

  const confirm = useCallback((opts: ConfirmOptions): Promise<boolean> => {
    restoreFocusRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    setOptions(opts);
    return new Promise<boolean>((resolve) => {
      resolveRef.current = resolve;
    });
  }, []);

  const close = useCallback((value: boolean) => {
    resolveRef.current?.(value);
    resolveRef.current = null;
    setOptions(null);
    restoreFocusRef.current?.focus();
  }, []);

  useEffect(() => {
    if (!options) return;
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") {
        e.preventDefault();
        close(false);
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [options, close]);

  const dialog = options ? (
    <div className="modal-overlay" onClick={() => close(false)}>
      <div
        className="modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="confirm-dialog-title"
        aria-describedby="confirm-dialog-desc"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="modal-title" id="confirm-dialog-title">
          {options.title}
        </div>
        <div className="modal-body" id="confirm-dialog-desc">
          {options.message}
        </div>
        <div className="modal-actions">
          <button type="button" className="btn" onClick={() => close(false)} autoFocus>
            {options.cancelLabel ?? "取消"} (Esc)
          </button>
          <button
            type="button"
            className={options.danger ? "btn btn-danger" : "btn btn-primary"}
            onClick={() => close(true)}
          >
            {options.confirmLabel ?? "确认"}
          </button>
        </div>
      </div>
    </div>
  ) : null;

  return [dialog, confirm];
}
