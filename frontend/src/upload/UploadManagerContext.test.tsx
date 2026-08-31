// @vitest-environment jsdom
import { act, type ReactNode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { UNAUTHORIZED_EVENT } from "../auth/events";
import type { EnqueueVideoInput } from "./types";

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const mocks = vi.hoisted(() => ({
  authToken: "session-one" as string | null,
  executeUploadTask: vi.fn((_task, signal: AbortSignal) => new Promise<void>((resolve) => {
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
});
