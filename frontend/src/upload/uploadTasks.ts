import { cancelImportBatch, completeImportBatch, createImportBatch, getImportBatch, uploadBatchFile, uploadVideo } from "../api";
import { ApiError, apiErrorDetail, operationalErrorDetail } from "../api/client";
import type { DataSource, ImportFileRole, VideoImportBatch, VideoImportCompletion } from "../api/types";
import type { UploadTask } from "./types";

const BATCH_ORDER: ImportFileRole[] = ["tracks", "metadata", "video"];

export interface TaskExecutionCallbacks {
  update: (change: (task: UploadTask) => UploadTask) => void;
  batchCreated: (batchId: number) => void;
  succeeded: () => void;
  failed: (message: string) => void;
  isCurrent: () => boolean;
}

type ImportBatchResult = VideoImportBatch | VideoImportCompletion;

export function batchSucceeded(batch: ImportBatchResult, source: DataSource | undefined = batch.source): boolean {
  return batch.status === "ready" || (source !== "heuristic" && batch.status === "video_only");
}

function batchStatusError(batch: ImportBatchResult): string {
  if (batch.status === "failed") {
    return validationErrorMessage(batch.validation_errors) || "批次校验失败，请检查所选文件；若文件无误，请联系管理员检查导入环境。";
  }
  return `批次尚未完成（${batch.status}），请稍后重试。`;
}

function safeText(value: string): string {
  return value.replace(/Validation failed[.;]?\s*(?:cancel this batch and retry|Fix the files and create a new batch to retry)\.?/gi, "批次校验失败")
    .replace(/(?:[A-Za-z]:[\\/]|\\\\|\/)[^\s"'<>，；）)]+/g, "[路径已隐藏]");
}

/** Only public diagnostic fields are shown; never dump input/context or server paths. */
function validationMessages(value: unknown, depth = 0): string[] {
  if (depth > 8 || value == null) return [];
  if (typeof value === "string") return value.trim() ? [safeText(value)] : [];
  if (Array.isArray(value)) return value.flatMap((item) => validationMessages(item, depth + 1));
  if (typeof value !== "object") return [];
  const row = value as Record<string, unknown>;
  const code = typeof row.code === "string" && /^[\w-]+$/.test(row.code) ? row.code : "";
  const message = typeof row.message === "string" ? safeText(row.message) : typeof row.msg === "string" ? safeText(row.msg) : "";
  const path = row.path ?? row.field ?? row.loc;
  const field = Array.isArray(path) && path.every((part) => typeof part === "string" || typeof part === "number") ? path.join(".") : path;
  const location = typeof field === "string" && /^[\w.\[\]-]+$/.test(field) ? field : "";
  const own = [code, message, location ? `字段 ${location}` : ""].filter(Boolean).join(" · ");
  return [...(own ? [own] : []), ...validationMessages(row.validation_errors ?? row.detail, depth + 1)];
}

function validationErrorMessage(value: unknown): string {
  const rows = validationMessages(value);
  if (!rows.length) return "";
  const detail = rows.join("；");
  const action = /dependenc|environment|ffmpeg|ffprobe|numpy/i.test(detail)
    ? "；服务器运行依赖不可用，请联系管理员修复环境后重试，无需反复修改文件。"
    : "；请按上述字段检查文件；若文件无误，请联系管理员。";
  return `导入校验失败：${detail}${action}`;
}

export function uploadErrorMessage(error: unknown): string {
  if (error instanceof ApiError && [0, 401].includes(error.status)) return error.message;
  const raw = apiErrorDetail(error) ?? operationalErrorDetail(error);
  const detail = error instanceof ApiError && error.code ? { code: error.code, validation_errors: raw } : raw;
  return validationErrorMessage(detail)
    || (detail && typeof detail === "object" ? "上传失败，未收到可展示的错误详情，请联系管理员。" : "")
    || (error instanceof Error ? safeText(error.message) : "上传失败，请重试");
}

export async function executeUploadTask(task: UploadTask, signal: AbortSignal, callbacks: TaskExecutionCallbacks): Promise<void> {
  let batchId: number | null = null;
  try {
    if (task.kind === "video") {
      if (!task.videoFile) throw new Error("上传文件不存在");
      callbacks.update((current) => ({ ...current, phase: "video" }));
      await uploadVideo(task.projectId, task.videoFile, {
        signal,
        assigneeMembershipId: task.assigneeMembershipId,
        onProgress: (progress) => callbacks.update((current) => ({ ...current, progress: progress.percent })),
      });
    } else {
      if (!task.slots) throw new Error("批次文件不完整");
      const order: ImportFileRole[] = task.source === "heuristic" ? ["tracks", "metadata", "annotation", "video"] : BATCH_ORDER;
      if (order.some((role) => !task.slots?.[role])) throw new Error("批次文件不完整");
      const created = await createImportBatch(task.projectId, signal, task.source);
      batchId = created.id;
      callbacks.batchCreated(created.id);
      let uploadedBytes = 0;
      const totalBytes = order.reduce((sum, role) => sum + task.slots![role]!.file.size, 0);

      for (const role of order) {
        const slot = task.slots[role]!;
        callbacks.update((current) => ({ ...current, phase: role }));
        await uploadBatchFile(task.projectId, created.id, role, slot.file, {
          signal,
          onProgress: (progress) => callbacks.update((current) => {
            if (!current.slots) return current;
            const progressTotal = totalBytes ? Math.round(((uploadedBytes + Math.min(progress.loaded, slot.file.size)) / totalBytes) * 100) : 0;
            return {
              ...current,
              progress: Math.min(100, progressTotal),
              slots: { ...current.slots, [role]: { ...current.slots[role], percent: progress.percent } },
            };
          }),
        });
        uploadedBytes += slot.file.size;
        callbacks.update((current) => current.slots ? {
          ...current,
          progress: totalBytes ? Math.round((uploadedBytes / totalBytes) * 100) : 0,
          slots: { ...current.slots, [role]: { ...current.slots[role], percent: 100 } },
        } : current);
      }

      callbacks.update((current) => ({ ...current, phase: "completing", progress: 100 }));
      let completed: ImportBatchResult;
      try {
        completed = await completeImportBatch(task.projectId, created.id, task.assigneeMembershipId, signal);
        if (batchSucceeded(completed, task.source)) {
          if (callbacks.isCurrent()) callbacks.succeeded();
          return;
        }
        if (completed.status === "failed") throw new Error(batchStatusError(completed));
      } catch (error) {
        if (!(error instanceof ApiError) || error.status !== 0) throw error;
      }

      completed = await getImportBatch(task.projectId, created.id, signal);
      if (!batchSucceeded(completed, task.source)) throw new Error(batchStatusError(completed));
    }

    if (callbacks.isCurrent()) callbacks.succeeded();
  } catch (error) {
    if (signal.aborted || !callbacks.isCurrent()) return;
    let message = uploadErrorMessage(error);
    if (batchId != null) {
      try {
        // 不确定 complete 结果先核实；绝不清理已成功或仍在处理的批次。
        const batch = await getImportBatch(task.projectId, batchId, signal);
        if (batchSucceeded(batch, task.source)) { if (callbacks.isCurrent()) callbacks.succeeded(); return; }
        if (batch.status === "processing") message += "；服务端仍在处理，保留批次待重查";
        else if (batch.status === "video_only") message += "；启发式批次不允许退化为仅视频，已发布批次不清理，请联系管理员";
        else {
          await cancelImportBatch(task.projectId, batchId, signal);
          callbacks.update((current) => ({ ...current, batchId: null, batchCleaned: true }));
        }
      } catch (cleanupError) {
        if (cleanupError instanceof ApiError && cleanupError.status === 404) callbacks.update((current) => ({ ...current, batchId: null, batchCleaned: true }));
        else message += `；清理待重试：${uploadErrorMessage(cleanupError)}`;
      }
    }
    if (!signal.aborted && callbacks.isCurrent()) callbacks.failed(message);
  }
}
