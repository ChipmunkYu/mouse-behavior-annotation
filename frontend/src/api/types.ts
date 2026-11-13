/** API DTO 复用生成类型；判断选择等开放 JSON 字段按实际响应窄化。 */
import type { components } from "./generated/schema";

type Schemas = components["schemas"];

export type User = Schemas["UserOut"];
export type LoginResponse = Schemas["LoginResponse"];
export type StreamTicket = Schemas["StreamTicketResponse"];

export type ProjectRole = Schemas["MembershipOut"]["role"];
export type Project = Schemas["ProjectOut"];
export type Membership = Schemas["MembershipOut"];
export type AssigneeDirectoryItem = Schemas["AssigneeDirectoryItem"];
export type Assignee = Schemas["AssigneeOut"];
export type MembershipUpdateInput = Schemas["MembershipUpdate"];
export type Invite = Schemas["InviteOut"];
export type AssignmentStatsItem = Schemas["AssignmentStatsItem"];
export type AssignmentStats = Schemas["AssignmentStatsOut"];
export type BehaviorStatsItem = Schemas["BehaviorStatsItem"];
export type BehaviorStats = Schemas["BehaviorStatsOut"];
export type VideoClaimsInput = Schemas["VideoClaimsRequest"];
export type VideoClaimsResponse = Schemas["VideoClaimsResponse"];

export type ParticipantMode = Schemas["CategoryOut"]["participant_mode"];
export type RoleDefinition = Schemas["RoleDefinitionOut"];
export type RoleDefinitionInput = Schemas["RoleDefinitionIn"];
export type Category = Schemas["CategoryOut"];
export type CategorySchemeCategoryInput = Schemas["CategorySchemeCategoryIn"];
export type CategoryScheme = Schemas["CategorySchemeOut"];
export type CategorySchemePut = Schemas["CategorySchemePut"];
export type CategorySchemeLock = Schemas["CategorySchemeLock"];
export type CategorySchemeAudit = Schemas["CategorySchemeAuditOut"];
export type ProjectCreateInput = Schemas["ProjectCreate"];

export type SubmissionAnnotationSnapshot = Schemas["SubmissionAnnotationSnapshotOut"];
export interface DistancePoint { x: number; y: number }
export interface DistanceCalibration {
  point_a: DistancePoint;
  point_b: DistancePoint;
  distance_cm: number;
  cm_per_pixel: number;
}
export interface DistanceCalibrationInput {
  point_a: DistancePoint;
  point_b: DistancePoint;
  distance_cm: number;
}
export type DataSource = "manual" | "heuristic";
export type SourceScope = DataSource | "all";
export type JudgmentStatus = "pending" | "usable" | "unusable";
export type Video = Schemas["VideoOut"] & { distance_calibration?: DistanceCalibration | null; source?: DataSource; judgment_status?: JudgmentStatus | null };
export type VideoCreateInput = Schemas["VideoCreate"];
export type WorkflowStatus = "draft" | "submitted" | "approved" | "rejected" | "unusable";
export type VideoView = "mine" | "unassigned" | "all";
export interface VideoListParams { view?: VideoView; workflow_status?: string; assignee_membership_id?: number; source?: SourceScope }

export type Annotation = Schemas["AnnotationOut"];
export type AnnotationCreateInput = Schemas["AnnotationCreate"];
export type AnnotationPatchInput = Schemas["AnnotationUpdate"];
export type Review = Schemas["ReviewOut"];
export type ReviewCreateInput = Schemas["ReviewCreate"];
export type BehaviorReviewStatus = Schemas["BehaviorDecisionIn"]["status"];
export type BehaviorReviewDecision = Schemas["BehaviorReviewDecisionOut"];
export type BehaviorReviewAnnotation = Schemas["BehaviorReviewAnnotationOut"];
export type BehaviorReviewCounts = Schemas["BehaviorReviewCountsOut"];
export type BehaviorReviewFeedbackItem = Schemas["BehaviorReviewFeedbackItemOut"];
export type BehaviorReviewState = Schemas["BehaviorReviewStateOut"];
export type BehaviorDecisionInput = Schemas["BehaviorDecisionIn"];
export type ReviewReopenInput = Schemas["BehaviorReopenIn"];
export type ReviewSubmissionContext = Schemas["ReviewSubmissionContextIn"] & Partial<JudgmentVersions>;

export const WORKFLOW_LABELS: Record<string, string> = {
  draft: "草稿", submitted: "待审核", approved: "已通过", rejected: "已退回", unusable: "不可用",
};

export type JobStatus = Schemas["JobOut"]["status"];
export type Job = Schemas["JobOut"];
export type MediaStatus = Schemas["MediaStatusOut"];
export const JOB_LABELS: Record<string, string> = {
  queued: "排队中", running: "处理中", succeeded: "已完成", failed: "生成失败", cancelled: "已取消",
};

export interface ExportEvent {
  annotation_id: number; video_id: string; clip_file: string | null;
  start_time: number; end_time: number; start_frame: number; end_frame: number;
  behavior: string | null; mouse_ids: number[]; detection_import_revision: number;
  identity_revision: number; crop_region: unknown; confidence: string;
  annotator: string | null; reviewer: string | null; review_status: string;
}

export type ImportFileRole = "video" | "tracks" | "metadata" | "annotation";
export type VideoImportBatch = Schemas["VideoImportBatchOut"] & { source?: DataSource; annotation_upload_state?: string };
export type VideoImportCompletion = Schemas["VideoImportCompletionOut"];
export type DetectionImport = Schemas["DetectionImportCurrentOut"];
export type DetectionReplacementPreview = Schemas["DetectionImportReplacementPreviewOut"];
export type DetectionReplacementConfirmed = Schemas["DetectionImportReplacementConfirmedOut"];
export type DetectionReplacementResponse = DetectionReplacementPreview | DetectionReplacementConfirmed;

export interface Keypoint { x_px?: number; y_px?: number; x?: number; y?: number; confidence?: number }
export type DetectionWithTrack = Schemas["DetectionWithTrackOut"];
export type DetectionsResponse = Schemas["DetectionsOut"];
export type CorrectedTrackSummary = Schemas["CorrectedTrackSummaryOut"];
export type CorrectedTracksResponse = Schemas["CorrectedTracksOut"];
export interface CorrectedTracksParams { current_frame?: number; search?: string; page?: number; page_size?: number }
export type IdentityEditCheckRequest = Schemas["IdentityEditCheckRequest"] & Partial<JudgmentVersions>;
export type TrackRoleConflict = Schemas["TrackRoleConflictOut"];
export type IdentityEditCheckResponse = Omit<Schemas["IdentityEditCheckOut"], "proposed_selection"> & { proposed_selection?: Omit<JudgmentSelection, "annotation_id"> | null };
export type IdentityEditCommitRequest = IdentityEditCheckRequest & { confirmed_selection?: JudgmentSelectionInput };
export type IdentityEditResult = Omit<Schemas["IdentityEditResultOut"], "selection"> & { selection?: JudgmentSelection | null };
export type IdentityEdit = Schemas["IdentityEditHistoryOut"];
export type SuppressionCreateRequest = Schemas["SuppressionCreateRequest"];
export type SuppressionResult = Schemas["IdentityEditResultOut"];
export type DetectionSuppression = Schemas["DetectionSuppressionSummaryOut"];
export type CorrectedTracksExport = Schemas["CorrectedTracksExportOut"];

export type ExportRequestInput = Omit<Schemas["ExportRequest"], "source"> & { source?: SourceScope };
export type MissingClip = Schemas["MissingClipOut"];
export type ExportStatus = Schemas["ExportStatusOut"] & { source?: SourceScope; category_ids?: number[] | null };
export const EXPORT_RETENTION_DAYS = 7;

export type ClipItem = Schemas["ClipItem"] & { source?: DataSource };
export type ClipListResponse = Omit<Schemas["ClipPageOut"], "items"> & { items: ClipItem[] };
export type ClipCategoryCount = Schemas["ClipCategoryCount"];
export interface ClipListParams { category_id?: number | null; video_id?: number | null; search?: string | null; page: number; page_size: number; source?: SourceScope }

// 版本上下文供多个请求复用；开放 JSON 选择字段按实际响应窄化。
export interface JudgmentVersions {
  expected_annotation_revision: number;
  base_detection_import_revision: number;
  base_identity_revision: number;
  expected_submission_id: number | null;
  expected_decision_revision: number | null;
}
export interface JudgmentSelection {
  annotation_id: number;
  category_id: number;
  mouse_ids: number[];
  participant_roles: Record<string, number[]>;
}
export type JudgmentSelectionInput = { category_id: number; participant_roles: Record<string, number[]>; mouse_ids?: number[] };
export interface Judgment {
  video_id: number;
  source: "heuristic";
  workflow_status: string;
  judgment_status: JudgmentStatus;
  annotation_revision: number;
  detection_import_revision: number;
  identity_revision: number;
  input: {
    behavior?: string | null;
    canonical_behavior?: string | null;
    mouse_ids?: number[];
    participants?: unknown[];
    actor_id?: number | null;
    target_id?: number | null;
    confidence?: string | null;
    frame_range: { start: number; end: number };
    time_range: { start: number; end: number };
  };
  selection: JudgmentSelection | null;
  submission_id: number | null;
  decision_revision: number;
}
export type JudgmentInput = JudgmentVersions & (
  | ({ judgment_status: "usable" } & JudgmentSelectionInput)
  | { judgment_status: "unusable" }
);
export const DEFAULT_PAGE_SIZE = 20;

export const ROLE_LABELS: Record<string, string> = { owner: "所有者", admin: "管理员", member: "成员" };
export const DEFAULT_FPS = 30;
