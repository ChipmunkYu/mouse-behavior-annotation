import type { DataSource } from "../api/types";

export type BatchFiles = Record<"video" | "tracks" | "metadata", File> & { annotation?: File };
export type BatchSlots = Record<"video" | "tracks" | "metadata", UploadSlotState> & { annotation?: UploadSlotState };

export type UploadTaskKind = "video" | "batch";
export type UploadTaskStatus = "queued" | "running" | "success" | "failed" | "cancelling";
export type UploadTaskPhase = "queued" | "video" | "tracks" | "metadata" | "annotation" | "completing" | "success" | "failed" | "cleanup";

export interface UploadSlotState {
  file: File;
  percent: number;
}

export interface UploadTask {
  taskId: string;
  kind: UploadTaskKind;
  source?: DataSource;
  projectId: number;
  projectName: string;
  assigneeMembershipId: number | null;
  name: string;
  status: UploadTaskStatus;
  phase: UploadTaskPhase;
  progress: number;
  error: string | null;
  batchId: number | null;
  batchCleaned?: boolean;
  videoFile?: File;
  slots?: BatchSlots;
}

export interface EnqueueVideoInput {
  projectId: number;
  projectName: string;
  assigneeMembershipId: number | null;
  file: File;
}

export interface EnqueueBatchInput {
  source?: DataSource;
  displayName?: string;
  projectId: number;
  projectName: string;
  assigneeMembershipId: number | null;
  files: BatchFiles;
}
