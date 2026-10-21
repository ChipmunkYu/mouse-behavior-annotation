"""只读定位 Project 2 taxonomy 退役关联视频；不执行归档。"""
from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import sqlite3

RETIRE_REASON = "Project 2 taxonomy migration"


def _after(value, cutoff) -> bool:
    return bool(value and datetime.fromisoformat(value) > datetime.fromisoformat(cutoff))


def report(path: Path) -> dict:
    if not path.is_file():
        raise ValueError("数据库文件不存在")
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        events = [dict(row) for row in db.execute(
            "SELECT c.id,c.retired_at,a.video_id annotation_video_id,s.video_id submission_video_id "
            "FROM clips c LEFT JOIN annotations a ON a.id=c.annotation_id "
            "LEFT JOIN submission_annotations sa ON sa.id=c.submission_annotation_id "
            "LEFT JOIN submissions s ON s.id=sa.submission_id "
            "WHERE c.project_id=2 AND c.retired_reason=? ORDER BY c.id", (RETIRE_REASON,))]
        grouped, unresolved = {}, []
        for event in events:
            ids = {event[key] for key in ("annotation_video_id", "submission_video_id")
                   if event[key] is not None}
            if len(ids) != 1 or not event["retired_at"]:
                unresolved.append(event)
            else:
                grouped.setdefault(ids.pop(), []).append(event)

        videos = []
        for video_id, linked in sorted(grouped.items()):
            video = db.execute(
                "SELECT id,filename,duration,fps,workflow_status FROM videos "
                "WHERE id=? AND project_id=2", (video_id,)).fetchone()
            if video is None:
                unresolved.extend(linked)
                continue
            cutoff = min(event["retired_at"] for event in linked)
            annotations = [dict(row) for row in db.execute(
                "SELECT id,annotator_id,start_frame,end_frame,start_time,end_time,created_at,updated_at "
                "FROM annotations WHERE video_id=? ORDER BY id", (video_id,))]
            later = [{"table": "annotations", "id": row["id"]}
                     for row in annotations if _after(row["created_at"], cutoff)
                     or _after(row["updated_at"], cutoff)]
            for table, time_field in (("submissions", "submitted_at"), ("reviews", "created_at")):
                later.extend({"table": table, "id": row["id"]} for row in db.execute(
                    f"SELECT id,{time_field} FROM {table} WHERE video_id=?", (video_id,))
                    if _after(row[time_field], cutoff))
            active_clips = db.execute(
                "SELECT count(*) FROM clips c LEFT JOIN annotations a ON a.id=c.annotation_id "
                "LEFT JOIN submission_annotations sa ON sa.id=c.submission_annotation_id "
                "LEFT JOIN submissions s ON s.id=sa.submission_id "
                "WHERE coalesce(a.video_id,s.video_id)=? AND c.retired_at IS NULL", (video_id,)).fetchone()[0]
            videos.append({
                **dict(video),
                "classification": "混有后续数据" if later or active_clips else "无法确定",
                "retired_clip_ids": [event["id"] for event in linked],
                "later_evidence": later,
                "nonretired_clip_count": active_clips,
                "annotations": [{key: row[key] for key in (
                    "id", "annotator_id", "start_frame", "end_frame", "start_time", "end_time")}
                    for row in annotations],
            })
        return {
            "status": "dry-run", "readonly": True, "project_id": 2,
            "retirement_reason": RETIRE_REASON, "videos": videos,
            "unresolved_events": unresolved,
            "workload_basis": "被标注视频时长，非人工计时；报告只列现有 duration 和标注区间",
            "limitation": "退役事件只用于定位；无法证明无后续数据的候选仍标记为无法确定，不自动归档",
        }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(report(args.db), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
