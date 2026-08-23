import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { cancelImportBatch, getImportBatch } from "../api";
import { ApiError } from "../api/client";
import { UNAUTHORIZED_EVENT } from "../auth/events";
import { batchSucceeded, executeUploadTask } from "./uploadTasks";
import type { EnqueueBatchInput, EnqueueVideoInput, UploadTask } from "./types";

const MAX_CONCURRENT_TASKS = 2;

interface UploadManagerValue {
  tasks: UploadTask[];
  enqueueVideo: (input: EnqueueVideoInput) => void;
  enqueueBatch: (input: EnqueueBatchInput) => void;
  retry: (taskId: string) => Promise<void>;
  cancel: (taskId: string) => Promise<void>;
  confirm: (taskId: string) => void;
  confirmAllSuccess: () => void;
  cancelAllForLogout: () => Promise<void>;
  projectSuccessVersion: (projectId: number) => number;
}

const UploadManagerContext = createContext<UploadManagerValue | null>(null);
const makeTaskId = () => globalThis.crypto?.randomUUID?.() ?? `upload-${Date.now()}-${Math.random().toString(36).slice(2)}`;

function resetForRetry(task: UploadTask): UploadTask {
  const slots = task.slots && Object.fromEntries(
    Object.entries(task.slots).map(([role, slot]) => [role, { ...slot, percent: 0 }]),
  ) as UploadTask["slots"];
  return { ...task, slots, batchId: null, status: "queued", phase: "queued", progress: 0, error: null };
}

function ignoreMissingBatch(error: unknown): void {
  if (!(error instanceof ApiError) || error.status !== 404) throw error;
}

export function UploadManagerProvider({ children }: { children: ReactNode }) {
  const [tasks, setTasks] = useState<UploadTask[]>([]);
  const [versions, setVersions] = useState<Record<number, number>>({});
  const tasksRef = useRef<UploadTask[]>([]);
  const activeRuns = useRef(new Map<string, AbortController>());
  const stopping = useRef(false);

  const changeTasks = useCallback((change: (tasks: UploadTask[]) => UploadTask[]) => {
    setTasks((current) => {
      const next = change(current);
      tasksRef.current = next;
      return next;
    });
  }, []);

  const updateTask = useCallback((id: string, change: (task: UploadTask) => UploadTask) => {
    changeTasks((current) => current.map((task) => task.taskId === id ? change(task) : task));
  }, [changeTasks]);

  const markSuccess = useCallback((id: string, projectId: number) => {
    updateTask(id, (task) => ({ ...task, status: "success", phase: "success", progress: 100, error: null, batchId: null }));
    setVersions((current) => ({ ...current, [projectId]: (current[projectId] ?? 0) + 1 }));
  }, [updateTask]);

  const enqueueVideo = useCallback((input: EnqueueVideoInput) => {
    changeTasks((current) => [...current, {
      taskId: makeTaskId(), kind: "video", projectId: input.projectId, projectName: input.projectName,
      assigneeMembershipId: input.assigneeMembershipId, name: input.file.name, videoFile: input.file,
      status: "queued", phase: "queued", progress: 0, error: null, batchId: null,
    }]);
  }, [changeTasks]);

  const enqueueBatch = useCallback((input: EnqueueBatchInput) => {
    changeTasks((current) => [...current, {
      taskId: makeTaskId(), kind: "batch", projectId: input.projectId, projectName: input.projectName,
      assigneeMembershipId: input.assigneeMembershipId, name: input.files.video.name,
      slots: {
        tracks: { file: input.files.tracks, percent: 0 },
        metadata: { file: input.files.metadata, percent: 0 },
        video: { file: input.files.video, percent: 0 },
      },
      status: "queued", phase: "queued", progress: 0, error: null, batchId: null,
    }]);
  }, [changeTasks]);

  useEffect(() => {
    if (stopping.current) return;
    const available = MAX_CONCURRENT_TASKS - activeRuns.current.size;
    if (available <= 0) return;

    tasks
      .filter((task) => task.status === "queued" && !activeRuns.current.has(task.taskId))
      .slice(0, available)
      .forEach((queuedTask) => {
        const id = queuedTask.taskId;
        const controller = new AbortController();
        activeRuns.current.set(id, controller);
        const startedTask = { ...queuedTask, status: "running" as const, error: null };
        updateTask(id, () => startedTask);

        const isCurrent = () => activeRuns.current.get(id) === controller
          && tasksRef.current.some((task) => task.taskId === id && task.status === "running");

        void executeUploadTask(startedTask, controller.signal, {
          isCurrent,
          update: (change) => { if (isCurrent()) updateTask(id, change); },
          batchCreated: (batchId) => { if (isCurrent()) updateTask(id, (task) => ({ ...task, batchId })); },
          succeeded: () => markSuccess(id, queuedTask.projectId),
          failed: (error) => updateTask(id, (task) => ({ ...task, status: "failed", phase: "failed", error })),
        }).finally(() => {
          if (activeRuns.current.get(id) !== controller) return;
          activeRuns.current.delete(id);
          if (!stopping.current) changeTasks((current) => [...current]);
        });
      });
  }, [tasks, changeTasks, markSuccess, updateTask]);

  const removeTask = useCallback((id: string) => {
    changeTasks((current) => current.filter((task) => task.taskId !== id));
  }, [changeTasks]);

  const cancel = useCallback(async (id: string) => {
    const task = tasksRef.current.find((item) => item.taskId === id);
    if (!task || task.status === "success" || task.status === "cancelling") return;

    activeRuns.current.get(id)?.abort();
    if (task.kind !== "batch" || task.batchId == null) {
      removeTask(id);
      return;
    }

    updateTask(id, (current) => ({ ...current, status: "cancelling", phase: "cleanup", error: null }));
    try {
      await cancelImportBatch(task.projectId, task.batchId).catch(ignoreMissingBatch);
      removeTask(id);
    } catch (error) {
      updateTask(id, (current) => ({
        ...current, status: "failed", phase: "failed",
        error: error instanceof Error ? `批次删除失败：${error.message}` : "批次删除失败，请重试",
      }));
    }
  }, [removeTask, updateTask]);

  const retry = useCallback(async (id: string) => {
    const task = tasksRef.current.find((item) => item.taskId === id);
    if (!task || task.status !== "failed") return;
    if (task.kind !== "batch" || task.batchId == null) {
      updateTask(id, resetForRetry);
      return;
    }

    updateTask(id, (current) => ({ ...current, status: "cancelling", phase: "cleanup", error: null }));
    try {
      const batch = await getImportBatch(task.projectId, task.batchId);
      if (batchSucceeded(batch)) {
        markSuccess(id, task.projectId);
        return;
      }
      await cancelImportBatch(task.projectId, task.batchId).catch(ignoreMissingBatch);
      updateTask(id, resetForRetry);
    } catch (error) {
      if (error instanceof ApiError && error.status === 404) {
        updateTask(id, resetForRetry);
        return;
      }
      updateTask(id, (current) => ({
        ...current, status: "failed", phase: "failed",
        error: error instanceof Error ? error.message : "批次状态查询失败，请重试",
      }));
    }
  }, [markSuccess, updateTask]);

  const confirm = useCallback((id: string) => {
    changeTasks((current) => current.filter((task) => task.taskId !== id || task.status !== "success"));
  }, [changeTasks]);

  const confirmAllSuccess = useCallback(() => {
    changeTasks((current) => current.filter((task) => task.status !== "success"));
  }, [changeTasks]);

  const stopLocally = useCallback(() => {
    stopping.current = true;
    activeRuns.current.forEach((controller) => controller.abort());
    activeRuns.current.clear();
    tasksRef.current = [];
    setTasks([]);
  }, []);

  const cancelAllForLogout = useCallback(async () => {
    stopping.current = true;
    activeRuns.current.forEach((controller) => controller.abort());
    const batches = tasksRef.current.filter((task) => task.kind === "batch" && task.status !== "success" && task.batchId != null);
    await Promise.allSettled(batches.map((task) => cancelImportBatch(task.projectId, task.batchId!).catch(ignoreMissingBatch)));
    stopLocally();
  }, [stopLocally]);

  useEffect(() => {
    window.addEventListener(UNAUTHORIZED_EVENT, stopLocally);
    return () => window.removeEventListener(UNAUTHORIZED_EVENT, stopLocally);
  }, [stopLocally]);

  useEffect(() => () => {
    stopping.current = true;
    activeRuns.current.forEach((controller) => controller.abort());
  }, []);

  const value = useMemo<UploadManagerValue>(() => ({
    tasks, enqueueVideo, enqueueBatch, retry, cancel, confirm, confirmAllSuccess, cancelAllForLogout,
    projectSuccessVersion: (projectId) => versions[projectId] ?? 0,
  }), [tasks, enqueueVideo, enqueueBatch, retry, cancel, confirm, confirmAllSuccess, cancelAllForLogout, versions]);

  return <UploadManagerContext.Provider value={value}>{children}</UploadManagerContext.Provider>;
}

export function useUploadManager(): UploadManagerValue {
  const context = useContext(UploadManagerContext);
  if (!context) throw new Error("useUploadManager 必须在 UploadManagerProvider 内使用");
  return context;
}

export function useProjectUploadVersion(projectId: number): number {
  return useUploadManager().projectSuccessVersion(projectId);
}
