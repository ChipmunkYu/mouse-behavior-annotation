/**
 * 演示模式种子数据（fixtures）：
 * 1 个项目（owner = demo 当前用户）、北医 12 类、
 * 覆盖 draft / submitted / approved / rejected 的多个视频、
 * 跨视频标注、审核历史、审核通过片段、一个进行中/一个完成的导出任务。
 * 所有数据均为本地模拟，不连接后端、不执行真实 ffmpeg。
 */
import type { Annotation, Category, Project, Review, User, Video } from "../api/types";
import type { ExportTask, ProjectMember, TaskAssignment } from "./types";

export interface DemoState {
  version: number;
  users: User[];
  projects: Project[];
  categories: Category[];
  videos: Video[];
  annotations: Annotation[];
  reviews: Review[];
  members: ProjectMember[];
  assignments: TaskAssignment[];
  exports: ExportTask[];
  nextIds: {
    annotation: number;
    review: number;
    project: number;
    clip: number;
    exportTask: number;
    member: number;
  };
}

export const DEMO_STATE_VERSION = 1;

/** 相对当前时间的 ISO 时间戳（days 天前 + hours 小时前 + minutes 分钟前）。 */
function isoAgo(days: number, hours = 0, minutes = 0): string {
  const d = Date.now() - ((days * 24 + hours) * 60 + minutes) * 60000;
  return new Date(d).toISOString();
}

function isoLater(days: number): string {
  return new Date(Date.now() + days * 86400000).toISOString();
}

/** 北医 12 类（与 backend/app/seed.py 一致，演示模式独立维护副本）。 */
const CATEGORY_COLORS = [
  "#E6194B",
  "#3CB44B",
  "#FFE119",
  "#4363D8",
  "#F58231",
  "#911EB4",
  "#46F0F0",
  "#F032E6",
  "#BCF60C",
  "#FABEBE",
  "#008080",
  "#E6BEFF",
];

export function buildSeed(): DemoState {
  const users: User[] = [
    { id: 1, username: "demo", created_at: isoAgo(30) },
    { id: 2, username: "alice", created_at: isoAgo(28) },
    { id: 3, username: "bob", created_at: isoAgo(26) },
    { id: 4, username: "carol", created_at: isoAgo(25) },
    { id: 5, username: "dave", created_at: isoAgo(24) },
  ];

  const projects: Project[] = [
    {
      id: 1,
      name: "顶视群养小鼠社会行为标注",
      description: "北医 12 类行为标注：个体 / 社交 / 群体行为；顶视视角多小鼠视频。",
      status: "active",
      created_at: isoAgo(21),
      role: "owner",
    },
  ];

  const groupNames: [string, string[]][] = [
    ["个体行为", ["奔跑", "行走", "静止"]],
    ["社交行为", ["一起", "接近", "追逐", "回避", "攻击行为", "鼻头接触", "鼻尾接触"]],
    ["群体行为", ["扎堆行为", "孤立行为"]],
  ];
  const categories: Category[] = [];
  let catId = 0;
  for (const [group, names] of groupNames) {
    for (const name of names) {
      catId += 1;
      categories.push({
        id: catId,
        project_id: 1,
        name,
        group,
        color: CATEGORY_COLORS[catId - 1],
        sort_order: catId - 1,
        is_active: true,
      });
    }
  }

  const baseVideo = {
    project_id: 1,
    fps: 30,
    width: 1920,
    height: 1080,
    status: "ready",
    annotation_revision: 1,
    approved_by: null,
    storage_path: null,
  };
  const videos: Video[] = [
    {
      ...baseVideo,
      id: 1,
      filename: "session_01_topview_baseline.mp4",
      duration: 120,
      storage_path: "data/videos/session_01_topview_baseline.mp4",
      workflow_status: "draft",
      submitted_at: null,
      approved_at: null,
      created_at: isoAgo(20),
    },
    {
      ...baseVideo,
      id: 2,
      filename: "session_02_group_rearing.mp4",
      duration: 180,
      storage_path: "data/videos/session_02_group_rearing.mp4",
      workflow_status: "submitted",
      submitted_at: isoAgo(0, 5, 22),
      approved_at: null,
      created_at: isoAgo(18),
    },
    {
      ...baseVideo,
      id: 3,
      filename: "session_03_cage_resident.mp4",
      duration: 240,
      storage_path: "data/videos/session_03_cage_resident.mp4",
      workflow_status: "approved",
      annotation_revision: 2,
      submitted_at: isoAgo(2, 3),
      approved_at: isoAgo(1, 2),
      approved_by: 4,
      created_at: isoAgo(16),
    },
    {
      ...baseVideo,
      id: 4,
      filename: "session_04_night_cycle.mp4",
      duration: 300,
      storage_path: "data/videos/session_04_night_cycle.mp4",
      workflow_status: "rejected",
      submitted_at: isoAgo(3, 6),
      approved_at: null,
      created_at: isoAgo(14),
    },
    {
      ...baseVideo,
      id: 5,
      filename: "session_05_feeding_arena.mp4",
      duration: 200,
      storage_path: "data/videos/session_05_feeding_arena.mp4",
      workflow_status: "approved",
      submitted_at: isoAgo(4, 1),
      approved_at: isoAgo(3, 5),
      approved_by: 4,
      created_at: isoAgo(12),
    },
    {
      ...baseVideo,
      id: 6,
      filename: "session_06_social_probe.mp4",
      duration: 150,
      storage_path: "data/videos/session_06_social_probe.mp4",
      workflow_status: "draft",
      submitted_at: null,
      approved_at: null,
      created_at: isoAgo(9),
    },
  ];

  // (video_id, category_id, start, end, annotator_id, review_status)
  type AnnSeed = [number, number, number, number, number, string];
  const annSeeds: AnnSeed[] = [
    // v2 submitted：6 条
    [2, 4, 10.2, 16.8, 2, "pending"],
    [2, 6, 22.4, 28.1, 2, "pending"],
    [2, 5, 33.0, 35.6, 2, "pending"],
    [2, 8, 41.2, 45.0, 3, "pending"],
    [2, 1, 60.3, 66.9, 3, "pending"],
    [2, 7, 88.1, 92.4, 3, "pending"],
    // v3 approved rev2：8 条
    [3, 9, 12.5, 18.2, 2, "approved"],
    [3, 6, 19.8, 24.5, 2, "approved"],
    [3, 4, 30.1, 36.7, 2, "approved"],
    [3, 11, 44.0, 55.2, 3, "approved"],
    [3, 8, 66.3, 70.1, 3, "approved"],
    [3, 5, 80.4, 83.9, 3, "approved"],
    [3, 2, 95.0, 101.6, 2, "approved"],
    [3, 3, 120.2, 130.8, 2, "approved"],
    // v4 rejected rev1：5 条（审核意见指出第 3 条误判）
    [4, 6, 15.0, 20.6, 2, "pending"],
    [4, 9, 21.4, 25.2, 2, "pending"],
    [4, 5, 40.5, 44.1, 2, "pending"],
    [4, 1, 66.0, 71.3, 3, "pending"],
    [4, 4, 90.2, 96.0, 3, "pending"],
    // v5 approved rev1：6 条
    [5, 11, 14.2, 26.8, 3, "approved"],
    [5, 2, 35.0, 41.5, 3, "approved"],
    [5, 8, 52.3, 56.0, 2, "approved"],
    [5, 6, 70.5, 76.2, 2, "approved"],
    [5, 4, 88.0, 94.6, 2, "approved"],
    [5, 12, 110.4, 118.9, 3, "approved"],
    // v6 draft：3 条（进行中）
    [6, 5, 9.6, 13.4, 2, "pending"],
    [6, 7, 28.0, 32.1, 2, "pending"],
    [6, 3, 60.5, 68.2, 3, "pending"],
  ];
  const categoryName = new Map(categories.map((c) => [c.id, c.name]));
  const userName = new Map(users.map((u) => [u.id, u.username]));
  const annotations: Annotation[] = annSeeds.map(([video_id, category_id, start_time, end_time, annotator_id, review_status], i) => {
    const start_frame = Math.round(start_time * 30);
    const end_frame = Math.round(end_time * 30);
    return {
      id: i + 1,
      video_id,
      annotator_id,
      category_id,
      start_time,
      end_time,
      start_frame,
      end_frame,
      confidence: "certain",
      review_status,
      crop_region: null,
      created_at: isoAgo(6, i % 9),
      updated_at: isoAgo(6, i % 9),
      annotator: userName.get(annotator_id) ?? null,
      category_name: categoryName.get(category_id) ?? null,
    };
  });

  const reviews: Review[] = [
    {
      id: 1,
      project_id: 1,
      video_id: 5,
      reviewer_id: 4,
      result: "approved",
      comment: "标注准确，片段可直接用于行为数据库。注意第 2 条终点略提前 0.3s，不影响使用。",
      annotation_revision: 1,
      created_at: isoAgo(3, 5),
      reviewer: "carol",
    },
    {
      id: 2,
      project_id: 1,
      video_id: 4,
      reviewer_id: 4,
      result: "rejected",
      comment: "第 3 条（40.5–44.1s）类别疑似误判：该段为「追逐」而非「接近」，请重新校准后再次提交。",
      annotation_revision: 1,
      created_at: isoAgo(2, 20),
      reviewer: "carol",
    },
    {
      id: 3,
      project_id: 1,
      video_id: 3,
      reviewer_id: 4,
      result: "approved",
      comment: "标注整体准确，起点/终点贴合行为起止。",
      annotation_revision: 1,
      created_at: isoAgo(2, 3),
      reviewer: "carol",
    },
    {
      id: 4,
      project_id: 1,
      video_id: 3,
      reviewer_id: 4,
      result: "approved",
      comment: "复核通过，可进入片段库与导出。",
      annotation_revision: 2,
      created_at: isoAgo(1, 2),
      reviewer: "carol",
    },
  ];

  const members: ProjectMember[] = [
    { id: 1, username: "demo", role: "owner", is_self: true, joined_at: isoAgo(21) },
    { id: 2, username: "alice", role: "annotator", is_self: false, joined_at: isoAgo(20) },
    { id: 3, username: "bob", role: "annotator", is_self: false, joined_at: isoAgo(19) },
    { id: 4, username: "carol", role: "reviewer", is_self: false, joined_at: isoAgo(18) },
    { id: 5, username: "dave", role: "admin", is_self: false, joined_at: isoAgo(15) },
  ];

  const assignments: TaskAssignment[] = [
    { video_id: 1, video_filename: "session_01_topview_baseline.mp4", video_workflow: "draft", annotator_id: 3, annotator_name: "bob", status: "assigned" },
    { video_id: 2, video_filename: "session_02_group_rearing.mp4", video_workflow: "submitted", annotator_id: 2, annotator_name: "alice", status: "assigned" },
    { video_id: 3, video_filename: "session_03_cage_resident.mp4", video_workflow: "approved", annotator_id: 3, annotator_name: "bob", status: "done" },
    { video_id: 4, video_filename: "session_04_night_cycle.mp4", video_workflow: "rejected", annotator_id: 2, annotator_name: "alice", status: "in_progress" },
    { video_id: 6, video_filename: "session_06_social_probe.mp4", video_workflow: "draft", annotator_id: 2, annotator_name: "alice", status: "in_progress" },
  ];

  const exports: ExportTask[] = [
    {
      id: 1,
      name: "全部已通过片段 · 完整导出",
      scope: { category_id: null, approved_only: true },
      status: "completed",
      progress: 100,
      clip_count: 14,
      created_at: isoAgo(2, 0, 12),
      expires_at: isoLater(5),
      started_at: isoAgo(2, 0, 12),
      completed_at: isoAgo(1, 23, 32),
      error: null,
    },
    {
      id: 2,
      name: "已通过片段 · 按类别（攻击行为）",
      scope: { category_id: 9, approved_only: true },
      status: "running",
      progress: 40,
      clip_count: 1,
      created_at: isoAgo(0, 0, 41),
      expires_at: isoLater(7),
      started_at: isoAgo(0, 0, 41),
      completed_at: null,
      error: null,
    },
  ];

  return {
    version: DEMO_STATE_VERSION,
    users,
    projects,
    categories,
    videos,
    annotations,
    reviews,
    members,
    assignments,
    exports,
    nextIds: {
      annotation: annotations.length + 1,
      review: reviews.length + 1,
      project: 2,
      clip: 1,
      exportTask: exports.length + 1,
      member: members.length + 1,
    },
  };
}
