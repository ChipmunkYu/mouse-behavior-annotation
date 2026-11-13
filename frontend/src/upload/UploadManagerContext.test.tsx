// @vitest-environment jsdom
import { act, type ReactNode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { UNAUTHORIZED_EVENT } from "../auth/events";
import type { EnqueueVideoInput, UploadTask } from "./types";
import type { TaskExecutionCallbacks } from "./uploadTasks";

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const mocks = vi.hoisted(() => ({
  authToken: "session-one" as string | null,
  executeUploadTask: vi.fn((_task: UploadTask, signal: AbortSignal, _callbacks: TaskExecutionCallbacks) => new Promise<void>((resolve) => {
    signal.addEventListener("abort", () => resolve(), { once: true });
  })),
}));

vi.mock("../auth/AuthContext", () => ({
  useAuth: () => ({ token: mocks.authToken }),
}));

vi.mock("./uploadTasks", async (importOriginal) => {
  const original = await importOriginal<typeof import("./uploadTasks")>();
  return { ...original, executeUploadTask: mocks.executeUploadTask };
});

import { UploadManagerProvider, useUploadManager } from "./UploadManagerContext";

type Manager = ReturnType<typeof useUploadManager>;
let manager: Manager;

function Probe() {
  manager = useUploadManager();
  return null;
}

function renderProvider(root: Root, children: ReactNode = <Probe />) {
  root.render(<UploadManagerProvider>{children}</UploadManagerProvider>);
}

const videoInput: EnqueueVideoInput = {
  projectId: 1,
  projectName: "project",
  assigneeMembershipId: null,
  file: new File(["video"], "mouse.mp4", { type: "video/mp4" }),
};

describe("UploadManagerProvider session lifecycle", () => {
  beforeEach(() => {
    document.body.innerHTML = '<div id="root"></div>';
    mocks.authToken = "session-one";
    mocks.executeUploadTask.mockClear();
  });

  it("clears an unauthorized session and executes only a new session's task", async () => {
    const root = createRoot(document.getElementById("root")!);
    await act(async () => renderProvider(root));
    await act(async () => manager.enqueueVideo(videoInput));
    expect(mocks.executeUploadTask).toHaveBeenCalledTimes(1);
    const oldTaskId = mocks.executeUploadTask.mock.calls[0][0].taskId;

    await act(async () => {
      window.dispatchEvent(new Event(UNAUTHORIZED_EVENT));
      mocks.authToken = null;
      renderProvider(root);
    });
    expect(manager.tasks).toEqual([]);

    await act(async () => {
      mocks.authToken = "session-two";
      renderProvider(root);
    });
    await act(async () => manager.enqueueVideo(videoInput));

    expect(mocks.executeUploadTask).toHaveBeenCalledTimes(2);
    expect(mocks.executeUploadTask.mock.calls[1][0].taskId).not.toBe(oldTaskId);
    expect(manager.tasks[0]?.status).toBe("running");
    await act(async () => root.unmount());
  });

  it("does not execute a queued task after the provider is unmounted", async () => {
    const root = createRoot(document.getElementById("root")!);
    await act(async () => renderProvider(root));

    await act(async () => {
      manager.enqueueVideo(videoInput);
      root.unmount();
    });

    expect(mocks.executeUploadTask).not.toHaveBeenCalled();
  });

  it("四文件复用调度：失败不阻塞后项，同名带序号且第四槽/source完整传播", async () => {
    const root = createRoot(document.getElementById("root")!);
    mocks.executeUploadTask
      .mockImplementationOnce(async (_task, _signal, callbacks) => { callbacks.failed("坏文件，清理待重试"); })
      .mockImplementationOnce(async (_task, _signal, callbacks) => { callbacks.succeeded(); });
    await act(async () => renderProvider(root));
    const input = { projectId: 1, projectName: "project", assigneeMembershipId: null, source: "heuristic" as const, displayName: "same-event", files: { video: videoInput.file, tracks: new File(["[]"], "tracks.json"), metadata: new File(["{}"], "metadata.json"), annotation: new File(["{}"], "annotation.json") } };
    await act(async () => { manager.enqueueBatch(input); manager.enqueueBatch(input); });
    expect(manager.tasks.map((task) => task.status)).toEqual(["failed", "success"]);
    expect(manager.tasks.map((task) => task.name)).toEqual(["same-event · #1", "same-event · #2"]);
    expect(mocks.executeUploadTask.mock.calls[0][0].source).toBe("heuristic");
    expect(mocks.executeUploadTask.mock.calls[0][0].slots?.annotation?.file.name).toBe("annotation.json");
    expect(manager.tasks[0].error).toContain("清理待重试");
    await act(async () => root.unmount());
  });
});
