import type { ImportFileRole } from "../api/types";

export type UploadTaskKind = "video" | "batch";
export type UploadTaskStatus = "queued" | "running" | "success" | "failed" | "cancelling";
export type UploadTaskPhase = "queued" | "video" | "tracks" | "metadata" | "completing" | "success" | "failed" | "cleanup";

export interface UploadSlotState {
  file: File;
  percent: number;
}

export interface UploadTask {
  taskId: string;
  kind: UploadTaskKind;
  projectId: number;
  projectName: string;
  assigneeMembershipId: number | null;
  name: string;
  status: UploadTaskStatus;
  phase: UploadTaskPhase;
  progress: number;
  error: string | null;
  batchId: number | null;
  videoFile?: File;
  slots?: Record<ImportFileRole, UploadSlotState>;
}

export interface EnqueueVideoInput {
  projectId: number;
  projectName: string;
  assigneeMembershipId: number | null;
  file: File;
}

export interface EnqueueBatchInput {
  projectId: number;
  projectName: string;
  assigneeMembershipId: number | null;
  files: Record<ImportFileRole, File>;
}
