from app.display_proxy_processor import DISPLAY_PROXY_PROFILE_VERSION
from app.models import BackgroundJob
from app.related_video_jobs import RelatedJobRef, identify_related_video_jobs


def _add(db, *, job_type="media", status="succeeded", project_id=None, payload=None,
         result_path=None, dedupe_key=None, run_token=None):
    job = BackgroundJob(job_type=job_type, status=status, project_id=project_id,
                        payload=payload, result_path=result_path, dedupe_key=dedupe_key,
                        run_token=run_token)
    db.add(job)
    db.flush()
    return job.id


def _export_payload(project_id=7, pairs=((101, 201),)):
    refs = [{
        "submission_id": submission_id,
        "submission_annotation_id": annotation_id,
        "snapshot_id": 301 + index,
        "source_media_revision": 1,
        "source_sha256": "a" * 64,
        "source_file_size": 1234,
        "source_mtime_ns": 5678,
        "source_device": 9,
        "source_inode": 10,
        "raw_digest": "b" * 64,
        "state_digest": "c" * 64,
        "metadata_digest": "d" * 64,
        "opaque_token": f"{index + 1:032x}",
    } for index, (submission_id, annotation_id) in enumerate(pairs)]
    return {
        "contract_version": 1,
        "project_id": project_id,
        "category_ids": [11],
        "category_directories": {"11": "grooming"},
        "category_tokens": {"11": "e" * 32},
        "submission_ids": sorted({item[0] for item in pairs}),
        "submission_annotation_ids": [item[1] for item in pairs],
        "refs": refs,
    }


def test_identifies_both_media_generations_and_splits_statuses(ctx):
    project_id = ctx.make_project_with_video()["project"]["id"]
    with ctx.session_factory() as db:
        legacy = _add(db, status="queued", project_id=project_id,
                      payload={"video_id": 41, "project_id": project_id, "revision": 2})
        submission = _add(db, status="failed", project_id=project_id, result_path="failed.part",
                           payload={"submission_id": 101,
                                    "submission_annotation_ids": [201, 202]})
        running = _add(db, status="running", project_id=project_id,
                       payload={"submission_id": 101,
                                "submission_annotation_ids": [201]})
        db.commit()
        result = identify_related_video_jobs(
            db, project_id=project_id, video_id=41, submission_ids=[101],
            submission_annotation_ids=[201, 202])

    assert [ref.id for ref in result.active] == [legacy, running]
    assert [ref.id for ref in result.terminal] == [submission]
    assert result.unknown == ()
    assert result.terminal[0] == RelatedJobRef(
        submission, "media", "failed", project_id, "failed.part")


def test_display_proxy_job_is_visible_to_delete_guard(ctx):
    info = ctx.make_project_with_video()
    project_id, video_id = info["project"]["id"], info["video"]["id"]
    payload = {"video_id": video_id, "project_id": project_id,
               "source_sha256": "a" * 64, "profile_version": DISPLAY_PROXY_PROFILE_VERSION}
    with ctx.session_factory() as db:
        job_id = _add(db, job_type="display_proxy", status="running", project_id=project_id,
                      payload=payload, run_token="a" * 32,
                      dedupe_key=(f"display-proxy:video:{video_id}:source:{'a' * 64}:"
                                  f"profile:{DISPLAY_PROXY_PROFILE_VERSION}"))
        db.commit()
        result = identify_related_video_jobs(db, project_id=project_id, video_id=video_id)
    assert [job.id for job in result.active] == [job_id]


def test_legacy_display_proxy_profiles_have_fail_closed_delete_semantics(ctx):
    info = ctx.make_project_with_video()
    project_id, video_id = info["project"]["id"], info["video"]["id"]

    def payload(profile):
        return {"video_id": video_id, "project_id": project_id,
                "source_sha256": "a" * 64, "profile_version": profile}

    with ctx.session_factory() as db:
        terminal_id = _add(
            db, job_type="display_proxy", status="failed", project_id=project_id,
            payload=payload("candidate-720p-h264-crf28-g30-sar1"),
        )
        active_id = _add(
            db, job_type="display_proxy", status="queued", project_id=project_id,
            payload=payload("candidate-720p-h264-crf28-g30-sar1"),
        )
        unknown_id = _add(
            db, job_type="display_proxy", status="failed", project_id=project_id,
            payload=payload("candidate-720p-h264-crf28-g30-sar1-almost"),
        )
        db.commit()
        result = identify_related_video_jobs(db, project_id=project_id, video_id=video_id)

    assert [job.id for job in result.terminal] == [terminal_id]
    assert [job.id for job in result.active] == [active_id]
    assert [job.id for job in result.unknown] == [unknown_id]


def test_display_proxy_payload_with_persisted_storage_path_is_unknown(ctx):
    info = ctx.make_project_with_video()
    project_id, video_id = info["project"]["id"], info["video"]["id"]
    payload = {"video_id": video_id, "project_id": project_id,
               "source_sha256": "a" * 64, "storage_path": "must-not-persist.mp4",
               "profile_version": DISPLAY_PROXY_PROFILE_VERSION}
    with ctx.session_factory() as db:
        job_id = _add(db, job_type="display_proxy", status="queued",
                      project_id=project_id, payload=payload)
        db.commit()
        result = identify_related_video_jobs(db, project_id=project_id, video_id=video_id)
    assert [job.id for job in result.unknown] == [job_id]


def test_export_with_any_target_reference_is_related(ctx):
    project_id = ctx.make_project_with_video()["project"]["id"]
    payload = _export_payload(project_id, ((999, 901), (101, 201)))
    with ctx.session_factory() as db:
        job_id = _add(db, job_type="export", status="succeeded", project_id=project_id, payload=payload,
                      result_path="project.zip")
        db.commit()
        result = identify_related_video_jobs(
            db, project_id=project_id, video_id=41, submission_ids=[101],
            submission_annotation_ids=[201])
    assert [ref.id for ref in result.terminal] == [job_id]


def test_unrelated_jobs_in_same_project_are_not_selected(ctx):
    project_id = ctx.make_project_with_video()["project"]["id"]
    with ctx.session_factory() as db:
        _add(db, project_id=project_id,
             payload={"video_id": 42, "project_id": project_id, "revision": 1})
        _add(db, job_type="export", project_id=project_id,
             payload=_export_payload(project_id, ((999, 998),)))
        _add(db, job_type="cleanup", payload={"project_id": 7})
        _add(db, job_type="export", payload=None)
        db.commit()
        result = identify_related_video_jobs(
            db, project_id=project_id, video_id=41, submission_ids=[101],
            submission_annotation_ids=[201])
    assert result.active == result.terminal == result.unknown == ()


def test_target_marker_on_media_or_export_from_other_project_is_unknown(ctx):
    target_project_id = ctx.make_project_with_video()["project"]["id"]
    other_project_id = ctx.make_project_with_video()["project"]["id"]
    with ctx.session_factory() as db:
        media = _add(db, project_id=other_project_id,
                     payload={"video_id": 41, "project_id": target_project_id, "revision": 1})
        export = _add(db, job_type="export", project_id=other_project_id,
                      payload=_export_payload(other_project_id, ((101, 201),)))
        db.commit()
        result = identify_related_video_jobs(
            db, project_id=target_project_id, video_id=41, submission_ids=[101],
            submission_annotation_ids=[201])
    assert [ref.id for ref in result.unknown] == [media, export]
    assert result.active == result.terminal == ()


def test_legacy_media_payload_project_mismatch_is_unknown(ctx):
    project_id = ctx.make_project_with_video()["project"]["id"]
    other_project_id = ctx.make_project_with_video()["project"]["id"]
    with ctx.session_factory() as db:
        job_id = _add(db, project_id=project_id,
                      payload={"video_id": 41, "project_id": other_project_id, "revision": 1})
        db.commit()
        result = identify_related_video_jobs(db, project_id=project_id, video_id=41)
    assert [ref.id for ref in result.unknown] == [job_id]


def test_contract_v1_export_without_project_id_is_unknown(ctx):
    project_id = ctx.make_project_with_video()["project"]["id"]
    payload = _export_payload(project_id)
    payload.pop("project_id")
    with ctx.session_factory() as db:
        job_id = _add(db, job_type="export", project_id=project_id, payload=payload)
        db.commit()
        result = identify_related_video_jobs(
            db, project_id=project_id, video_id=41, submission_ids=[101])
    assert [ref.id for ref in result.unknown] == [job_id]


def test_contract_v1_export_without_worker_ref_field_is_unknown(ctx):
    project_id = ctx.make_project_with_video()["project"]["id"]
    payload = _export_payload(project_id)
    payload["refs"][0].pop("source_inode")
    with ctx.session_factory() as db:
        job_id = _add(db, job_type="export", project_id=project_id, payload=payload)
        db.commit()
        result = identify_related_video_jobs(
            db, project_id=project_id, video_id=41, submission_annotation_ids=[201])
    assert [ref.id for ref in result.unknown] == [job_id]


def test_complete_contract_v1_export_is_classified_normally(ctx):
    project_id = ctx.make_project_with_video()["project"]["id"]
    with ctx.session_factory() as db:
        job_id = _add(db, job_type="export", status="queued", project_id=project_id,
                      payload=_export_payload(project_id))
        db.commit()
        result = identify_related_video_jobs(
            db, project_id=project_id, video_id=41, submission_ids=[101],
            submission_annotation_ids=[201])
    assert [ref.id for ref in result.active] == [job_id]
    assert result.terminal == result.unknown == ()


def test_target_markers_in_malformed_payloads_fail_closed(ctx):
    malformed = [
        ("media", {"video_id": 41}),
        ("media", {"video_id": "41", "project_id": 7, "revision": 1}),
        ("media", {"submission_id": 101, "submission_annotation_ids": "bad"}),
        ("export", {"contract_version": 1, "submission_ids": [101], "refs": []}),
        ("mystery", {"submission_annotation_ids": [201]}),
    ]
    with ctx.session_factory() as db:
        ids = [_add(db, job_type=kind, status="queued", payload=payload)
               for kind, payload in malformed]
        db.commit()
        result = identify_related_video_jobs(
            db, project_id=7, video_id=41, submission_ids=[101],
            submission_annotation_ids=[201])
    assert [ref.id for ref in result.unknown] == ids
    assert result.active == result.terminal == ()


def test_unknown_status_is_unknown_even_with_valid_payload(ctx):
    project_id = ctx.make_project_with_video()["project"]["id"]
    with ctx.session_factory() as db:
        job_id = _add(db, status="completed", project_id=project_id,
                      payload={"video_id": 41, "project_id": project_id, "revision": 1})
        db.commit()
        result = identify_related_video_jobs(db, project_id=project_id, video_id=41)
    assert [ref.id for ref in result.unknown] == [job_id]


def test_legacy_export_and_refs_argument_are_supported(ctx):
    project_id = ctx.make_project_with_video()["project"]["id"]
    with ctx.session_factory() as db:
        by_video = _add(db, job_type="export", status="cancelled", project_id=project_id,
                        payload={"annotation_ids": [888],
                                 "video_revisions": {"41": 3, "42": 1}})
        by_annotation = _add(db, job_type="export", status="failed", project_id=project_id,
                             payload={"annotation_ids": [201],
                                      "video_revisions": {"42": 1}})
        db.commit()
        result = identify_related_video_jobs(
            db, project_id=project_id, video_id=41, annotation_ids=[201],
            refs=[{"submission_id": 101, "submission_annotation_id": 201}])
    assert [ref.id for ref in result.terminal] == [by_video, by_annotation]


def test_invalid_frozen_identifiers_are_rejected(ctx):
    with ctx.session_factory() as db:
        for kwargs in ({"project_id": 7, "video_id": True},
                       {"project_id": 0, "video_id": 1},
                       {"project_id": 7, "video_id": 1, "submission_ids": [False]},
                       {"project_id": 7, "video_id": 1,
                        "submission_annotation_ids": [0]},
                       {"project_id": 7, "video_id": 1, "annotation_ids": [0]}):
            try:
                identify_related_video_jobs(db, **kwargs)
            except ValueError:
                pass
            else:
                raise AssertionError("invalid frozen identifier was accepted")


def test_missing_payload_with_target_dedupe_key_is_unknown(ctx):
    with ctx.session_factory() as db:
        job_id = _add(db, status="queued", payload=None,
                      dedupe_key="media:video:41:rev:3")
        db.commit()
        result = identify_related_video_jobs(db, project_id=7, video_id=41)
    assert [ref.id for ref in result.unknown] == [job_id]


def test_live_and_frozen_annotation_ids_are_not_mixed(ctx):
    project_id = ctx.make_project_with_video()["project"]["id"]
    with ctx.session_factory() as db:
        live = _add(db, job_type="export", project_id=project_id, payload={
            "annotation_ids": [201], "video_revisions": {"42": 1},
        })
        frozen_only = _add(db, job_type="export", project_id=project_id, payload={
            "annotation_ids": [901], "video_revisions": {"42": 1},
        })
        db.commit()
        result = identify_related_video_jobs(
            db, project_id=project_id, video_id=41, annotation_ids=[201],
            submission_annotation_ids=[901])
    assert [ref.id for ref in result.terminal] == [live]
    assert frozen_only not in [ref.id for ref in result.terminal]


def test_malformed_same_project_media_and_export_block_but_cleanup_does_not(ctx):
    project_id = ctx.make_project_with_video()["project"]["id"]
    with ctx.session_factory() as db:
        media = _add(db, job_type="media", project_id=project_id,
                     payload={"unexpected": True})
        export = _add(db, job_type="export", project_id=project_id, payload=None)
        _add(db, job_type="cleanup", project_id=project_id, payload=None)
        db.commit()
        result = identify_related_video_jobs(db, project_id=project_id, video_id=41)
    assert [ref.id for ref in result.unknown] == [media, export]
    assert result.active == result.terminal == ()


def test_valid_explicitly_unrelated_same_project_jobs_do_not_block(ctx):
    project_id = ctx.make_project_with_video()["project"]["id"]
    with ctx.session_factory() as db:
        _add(db, job_type="media", project_id=project_id,
             payload={"video_id": 42, "project_id": project_id, "revision": 1})
        _add(db, job_type="export", project_id=project_id,
             payload=_export_payload(project_id, ((999, 998),)))
        db.commit()
        result = identify_related_video_jobs(
            db, project_id=project_id, video_id=41, annotation_ids=[201],
            submission_ids=[101], submission_annotation_ids=[901])
    assert result.active == result.terminal == result.unknown == ()
