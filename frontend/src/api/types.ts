/** API DTO 以 openapi-typescript 生成结果为唯一契约来源。 */
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
export type Video = Schemas["VideoOut"];
export type VideoCreateInput = Schemas["VideoCreate"];
export type WorkflowStatus = "draft" | "submitted" | "approved" | "rejected";
export type VideoView = "mine" | "unassigned" | "all";
export interface VideoListParams { view?: VideoView; workflow_status?: string; assignee_membership_id?: number }

export type Annotation = Schemas["AnnotationOut"];
export type AnnotationCreateInput = Schemas["AnnotationCreate"];
export type AnnotationPatchInput = Schemas["AnnotationUpdate"];
export type Review = Schemas["ReviewOut"];
export type ReviewCreateInput = Schemas["ReviewCreate"];

export const WORKFLOW_LABELS: Record<string, string> = {
  draft: "草稿", submitted: "待审核", approved: "已通过", rejected: "已退回",
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

export type ImportFileRole = "video" | "tracks" | "metadata";
export type VideoImportBatch = Schemas["VideoImportBatchOut"];
export type VideoImportCompletion = Schemas["VideoImportCompletionOut"];
export type DetectionImport = Schemas["DetectionImportCurrentOut"];
export type DetectionReplacementPreview = Schemas["DetectionImportReplacementPreviewOut"];
export type DetectionReplacementConfirmed = Schemas["DetectionImportReplacementConfirmedOut"];
export type DetectionReplacementResponse = DetectionReplacementPreview | DetectionReplacementConfirmed;

export interface Keypoint { x_px?: number; y_px?: number; x?: number; y?: number; confidence?: number }
export interface DetectionWithTrack {
  detection_id: number; frame_index: number; raw_track_id: number; display_track_id: number;
  box_xyxy_px: number[] | null; keypoints: Keypoint[] | null; confidence?: number | null;
  import_revision: number; identity_revision: number;
}
export interface DetectionsResponse { detections: DetectionWithTrack[]; total: number }
export interface CorrectedTrackSummary {
  display_track_id: number; first_frame: number | null; last_frame: number | null;
  detection_count: number; visible_in_current_frame: boolean | null;
}
export interface CorrectedTracksResponse { items: CorrectedTrackSummary[]; total: number; page: number; page_size: number; pages: number }
export interface CorrectedTracksParams { current_frame?: number; search?: string; page?: number; page_size?: number }
export type IdentityEditCheckRequest = Schemas["IdentityEditCheckRequest"];
export interface TrackRoleConflict {
  annotation_id: number; start_time: number; end_time: number; start_frame: number; end_frame: number;
  role_key: string; role_name: string | null; track_id: number;
}
export interface IdentityEditCheckResponse {
  operation: "split" | "merge"; old_display_track_id?: number; new_display_track_id?: number;
  split_frame?: number; detections_before?: number; detections_after?: number;
  retained_display_track_id?: number; merged_display_track_ids?: number[];
  affected_detection_count?: number; affected_annotation_count: number; conflict_frames?: number[];
  message?: string; conflicts?: TrackRoleConflict[];
}
export type IdentityEditCommitRequest = Schemas["IdentityEditCommitRequest"];
export interface IdentityEditResult {
  edit_id?: number; identity_revision: number; new_display_track_id?: number;
  retained_display_track_id?: number; affected_detection_count?: number;
  affected_annotation_count?: number; needs_mouse_ids_annotation_ids?: number[];
}
export interface IdentityEdit {
  id: number; video_id: number; detection_import_id: number; operation: string;
  base_identity_revision: number; result_identity_revision: number; params: Record<string, unknown> | null;
  affected_detections: unknown[] | null; affected_annotations: unknown[] | null;
  operator_id: number | null; created_at: string; reverted_edit_id: number | null;
}
export type SuppressionCreateRequest = Schemas["SuppressionCreateRequest"];
export interface SuppressionResult { suppression_id?: number; identity_revision: number; frozen_detection_count?: number; freed_detection_count?: number; affected_track_ids?: number[] }
export interface DetectionSuppression { id: number; scope: string; result_identity_revision: number; created_at: string; frozen_detection_count: number }
export interface CorrectedTracksExport { tracks_corrected: string[]; manifest: Record<string, unknown> }

export type ExportRequestInput = Schemas["ExportRequest"];
export type MissingClip = Schemas["MissingClipOut"];
export type ExportStatus = Schemas["ExportStatusOut"];
export const EXPORT_RETENTION_DAYS = 7;

export type ClipItem = Schemas["ClipItem"];
export type ClipListResponse = Schemas["ClipPageOut"];
export type ClipCategoryCount = Schemas["ClipCategoryCount"];
export interface ClipListParams { category_id?: number | null; video_id?: number | null; search?: string | null; page: number; page_size: number }
export const DEFAULT_PAGE_SIZE = 20;

export const ROLE_LABELS: Record<string, string> = { owner: "所有者", admin: "管理员", member: "成员" };
export const DEFAULT_FPS = 30;
