import type { ReactNode } from "react";

/** 中性状态徽标（不使用类别色，类别色仅用于区分行为）。 */
const BADGE_TONE: Record<string, string> = {
  ok: "badge badge-ok",
  warn: "badge badge-warn",
  danger: "badge badge-danger",
  muted: "badge badge-muted",
  active: "badge badge-ok",
  ready: "badge badge-ok",
  metadata: "badge badge-muted",
  uploading: "badge badge-warn",
  uploaded: "badge badge-ok",
  needs_transcode: "badge badge-warn",
  error: "badge badge-danger",
  archived: "badge badge-muted",
};

/** 常见媒体、导入与行为标注审核枚举值 → 中文显示。 */
const STATUS_LABELS: Record<string, string> = {
  ok: "正常",
  active: "进行中",
  ready: "就绪",
  metadata: "仅元数据",
  uploading: "上传中",
  uploaded: "已上传",
  needs_transcode: "待转码",
  error: "异常",
  archived: "已归档",
  certain: "确定",
  uncertain: "不确定",
  occluded: "被遮挡",
  owner: "所有者",
  admin: "管理员",
  member: "成员",
};

/** 行为标注审核状态独立于视频工作流。 */
const ANNOTATION_REVIEW_STATUS_LABELS: Record<string, string> = {
  pending: "待审核",
  approved: "已通过",
  rejected: "已退回",
};

/** 视频工作流状态单独维护，避免混用两套业务语义。 */
const WORKFLOW_STATUS_LABELS: Record<string, string> = {
  draft: "草稿",
  submitted: "待审核",
  approved: "审核通过",
  rejected: "已驳回",
};

export function workflowStatusLabel(value: string): string {
  return WORKFLOW_STATUS_LABELS[value] ?? "未知状态";
}

export function statusLabel(value: string): string {
  return ANNOTATION_REVIEW_STATUS_LABELS[value] ?? STATUS_LABELS[value] ?? "未知状态";
}

export function StatusBadge({ value, tone }: { value: string; tone?: string }) {
  const cls = tone ? (BADGE_TONE[tone] ?? "badge badge-muted") : (BADGE_TONE[value] ?? "badge badge-muted");
  return <span className={cls}>{statusLabel(value)}</span>;
}

/** 视频审核工作流徽标：状态 + 行为标注版本。 */
export function WorkflowBadge({ value, revision }: { value: string; revision?: number | null }) {
  return (
    <span className="workflow-badge" data-status={value ?? "draft"} title={`行为标注版本 v${revision ?? 1}`}>
      <span className="workflow-dot" aria-hidden="true" />
      {workflowStatusLabel(value)}
      {revision != null ? <span className="workflow-rev mono">行为标注版本 v{revision}</span> : null}
    </span>
  );
}

export function EmptyState({
  title,
  hint,
  compact,
}: {
  title: string;
  hint?: string;
  compact?: boolean;
}) {
  return (
    <div className={compact ? "empty empty-compact" : "empty"}>
      <div className="empty-icon" aria-hidden="true">◌</div>
      <div className="empty-title">{title}</div>
      {hint ? <div className="empty-hint">{hint}</div> : null}
    </div>
  );
}

export function Loading({ text = "加载中…" }: { text?: string }) {
  return (
    <div className="loading">
      <span className="spinner" aria-hidden="true" />
      <span>{text}</span>
    </div>
  );
}

export function ErrorBox({ message }: { message: string }) {
  return <div className="error-box" role="alert">⚠ {message}</div>;
}

export function Card({
  title,
  extra,
  children,
  className,
}: {
  title?: ReactNode;
  extra?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <div className={className ? `card ${className}` : "card"}>
      {title != null || extra != null ? (
        <div className="card-header">
          <div className="card-title">{title}</div>
          {extra ? <div className="card-extra">{extra}</div> : null}
        </div>
      ) : null}
      <div className="card-body">{children}</div>
    </div>
  );
}
