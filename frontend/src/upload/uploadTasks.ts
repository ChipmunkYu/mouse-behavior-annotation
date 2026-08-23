import { completeImportBatch, createImportBatch, getImportBatch, uploadBatchFile, uploadVideo } from "../api";
import { ApiError } from "../api/client";
import type { ImportFileRole, VideoImportBatch } from "../api/types";
import type { UploadTask } from "./types";

const BATCH_ORDER: ImportFileRole[] = ["tracks", "metadata", "video"];

export interface TaskExecutionCallbacks {
  update: (change: (task: UploadTask) => UploadTask) => void;
  batchCreated: (batchId: number) => void;
  succeeded: () => void;
  failed: (message: string) => void;
  isCurrent: () => boolean;
}

export function batchSucceeded(batch: VideoImportBatch): boolean {
  return batch.status === "ready" || batch.status === "video_only";
}

function batchStatusError(batch: VideoImportBatch): string {
  if (batch.status === "failed") return "批次校验失败，请重试或取消任务。";
  return `批次尚未完成（${batch.status}），请稍后重试。`;
}

function messageOf(error: unknown): string {
  return error instanceof Error ? error.message : "上传失败，请重试";
}

export async function executeUploadTask(task: UploadTask, signal: AbortSignal, callbacks: TaskExecutionCallbacks): Promise<void> {
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
      const created = await createImportBatch(task.projectId, signal);
      callbacks.batchCreated(created.id);
      let uploadedBytes = 0;
      const totalBytes = BATCH_ORDER.reduce((sum, role) => sum + task.slots![role].file.size, 0);

      for (const role of BATCH_ORDER) {
        const slot = task.slots[role];
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
      let completed: VideoImportBatch;
      try {
        completed = await completeImportBatch(task.projectId, created.id, task.assigneeMembershipId, signal);
        if (batchSucceeded(completed)) {
          if (callbacks.isCurrent()) callbacks.succeeded();
          return;
        }
      } catch (error) {
        if (!(error instanceof ApiError) || error.status !== 0) throw error;
      }

      completed = await getImportBatch(task.projectId, created.id, signal);
      if (!batchSucceeded(completed)) throw new Error(batchStatusError(completed));
    }

    if (callbacks.isCurrent()) callbacks.succeeded();
  } catch (error) {
    if (signal.aborted || !callbacks.isCurrent()) return;
    callbacks.failed(messageOf(error));
  }
}
