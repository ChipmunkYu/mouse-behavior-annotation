from datetime import datetime, timedelta

import pytest

from app import database as db_mod
from app.cleanup import run_retention_cleanup
from app.context_cache import PINNED_KEYS, cache_root
from app.models import BackgroundJob
from app.related_video_jobs import identify_related_video_jobs
from app.video_delete_io import DeletePath, VideoDeleteIO, VideoDeleteIOError
from tests.test_cleanup import _settings, _age


def test_running_material_and_published_zip_survive_retention(tmp_path):
    settings = _settings(tmp_path)
    now = datetime.utcnow()
    paths = [settings.exports_dir / '.export-1.staging',
             settings.exports_dir / '.export-1.tmp.zip',
             settings.videos_dir / ('.submission-media-job-1-' + 'a' * 32 + '.staging'),
             settings.exports_dir / 'export_project_1_1.zip']
    paths[0].mkdir()
    for path in paths[1:]:
        path.write_bytes(b'x')
    for path in paths:
        _age(path, now, 1000)
    with db_mod.SessionLocal() as db:
        job = BackgroundJob(job_type='export', status='running', result_path=paths[-1].name,
                            expires_at=now - timedelta(days=1))
        db.add(job)
        db.commit()
        run_retention_cleanup(db, settings, now=now)
        assert job.result_path == paths[-1].name
    assert all(path.exists() for path in paths)


@pytest.mark.parametrize('status,group', [('running', 'active'), ('succeeded', 'terminal'),
                                          ('new-status', 'unknown')])
def test_prewarm_gate(ctx, status, group):
    project_id = ctx.make_project_with_video()['project']['id']
    payload = {'video_id': 2, 'key': 'a' * 64, 'submission_annotation_id': 4,
               'context': {'version': 1, 'submission_id': 3, 'annotation': {'id': 4}}}
    with ctx.session_factory() as db:
        db.add(BackgroundJob(project_id=project_id, job_type='context_prewarm', status=status,
                             payload=payload))
        db.commit()
        result = identify_related_video_jobs(db, project_id=project_id, video_id=2,
                    submission_ids=[3], submission_annotation_ids=[4])
        assert len(getattr(result, group)) == 1
        job = db.query(BackgroundJob).one()
        job.payload = {}
        db.commit()
        result = identify_related_video_jobs(db, project_id=project_id, video_id=2)
        assert len(result.unknown) == 1


def test_cache_pin_blocks_freeze_and_zero_limit_preserves_pin(tmp_path):
    settings = _settings(tmp_path)
    settings.context_cache_max_bytes = 0
    entry = cache_root(settings) / 'video-2' / ('a' * 64)
    entry.mkdir(parents=True)
    (entry / 'data').write_bytes(b'cached')
    PINNED_KEYS.add(entry.name)
    try:
        with pytest.raises(VideoDeleteIOError, match='pinned'):
            VideoDeleteIO(settings).prepare(2, [DeletePath('context_cache', 'video-2', 'directory')])
        with db_mod.SessionLocal() as db:
            run_retention_cleanup(db, settings)
        assert entry.exists()
    finally:
        PINNED_KEYS.discard(entry.name)
    with db_mod.SessionLocal() as db:
        run_retention_cleanup(db, settings)
    assert not entry.exists()


def test_revsub_parts_are_reclaimed_without_workers(tmp_path):
    settings = _settings(tmp_path)
    now = datetime.utcnow()
    paths = [settings.clips_dir / ('.clip_4_revsub3.' + 'a' * 32 + '.mp4.part'),
             settings.thumbnails_dir / ('.clip_4_revsub3.' + 'a' * 32 + '.jpg.part')]
    for path in paths:
        path.write_bytes(b'part')
        _age(path, now, 1000)
    with db_mod.SessionLocal() as db:
        run_retention_cleanup(db, settings, now=now)
    assert not any(path.exists() for path in paths)


def test_unknown_scratch_owner_is_retained(tmp_path):
    settings = _settings(tmp_path)
    scratch = cache_root(settings) / 'video-2' / ('.job-999-' + 'a' * 32)
    scratch.mkdir(parents=True)
    _age(scratch, datetime.utcnow(), 1000)
    with db_mod.SessionLocal() as db:
        run_retention_cleanup(db, settings)
    assert scratch.exists()


def test_cache_quarantine_recovery(tmp_path):
    settings = _settings(tmp_path)
    io = VideoDeleteIO(settings)
    if not io._descriptor_directory_operations_supported():
        pytest.skip('descriptor-safe directory recovery requires POSIX')
    entry = cache_root(settings) / 'video-2' / ('a' * 64)
    entry.mkdir(parents=True)
    (entry / 'data').write_bytes(b'cached')
    manifest = io.prepare(2, [DeletePath('context_cache', 'video-2', 'directory')])
    io.quarantine(manifest)
    assert not entry.exists()
    assert io.recover(lambda _: True)[0].ok
    assert (entry / 'data').read_bytes() == b'cached'


@pytest.mark.parametrize('status', ['running', 'new-status'])
def test_prewarm_blocks_video_freeze(ctx, tmp_path, status):
    from tests.test_video_delete_db import _base, _freeze
    from app.video_delete_db import VideoDeleteConflictError

    project_id, video_id, actor_id, _ = _base(ctx)
    settings = _settings(tmp_path)
    with ctx.session_factory() as db:
        db.add(BackgroundJob(project_id=project_id, job_type='context_prewarm',
                             status=status, payload={'video_id': video_id}))
        db.commit()
        with pytest.raises(VideoDeleteConflictError):
            _freeze(db, settings, project_id, video_id, actor_id)


def test_terminal_prewarm_freezes_all_cache_copies(ctx, tmp_path):
    from tests.test_video_delete_db import _base, _freeze, _full_graph
    from app.models import Submission

    project_id, video_id, actor_id, category_id = _base(ctx)
    settings = _settings(tmp_path)
    with ctx.session_factory() as db:
        _, annotation_id = _full_graph(db, project_id, video_id, actor_id, category_id)
        submission_id = db.query(Submission).filter_by(video_id=video_id).one().id
        job = BackgroundJob(project_id=project_id, job_type='context_prewarm',
            status='succeeded', payload={'video_id': video_id, 'key': 'a' * 64,
                'submission_annotation_id': annotation_id,
                'context': {'version': 1, 'submission_id': submission_id,
                            'annotation': {'id': annotation_id}}})
        db.add(job)
        db.commit()
        frozen = _freeze(db, settings, project_id, video_id, actor_id)
        assert job.id in frozen.terminal_job_ids
        paths = [p for p in frozen.paths if p.root_kind == 'context_cache']
        assert paths == [DeletePath('context_cache', f'video-{video_id}', 'directory')]
        root = cache_root(settings) / f'video-{video_id}'
        copies = ['a' * 64, 'b' * 64, f'.job-{job.id}-' + 'c' * 32]
        for name in copies:
            (root / name).mkdir(parents=True)
            (root / name / 'data').write_bytes(b'cached')
        io = VideoDeleteIO(settings)
        manifest = io.prepare(video_id, paths)
        io.quarantine(manifest)
        assert not root.exists()
        target = io._quarantine_path(manifest.operation_id, manifest.entries[0].quarantine_key)
        assert all((target / name / 'data').read_bytes() == b'cached' for name in copies)


def test_unknown_prewarm_preserves_cache_during_retention(tmp_path):
    settings = _settings(tmp_path)
    settings.context_cache_max_bytes = 0
    entry = cache_root(settings) / 'video-2' / ('a' * 64)
    entry.mkdir(parents=True)
    (entry / 'data').write_bytes(b'cached')
    _age(entry, datetime.utcnow(), 1000)
    with db_mod.SessionLocal() as db:
        db.add(BackgroundJob(job_type='context_prewarm', status='new-status', payload={}))
        db.commit()
        run_retention_cleanup(db, settings)
    assert (entry / 'data').read_bytes() == b'cached'


@pytest.mark.parametrize('damage', [None, 'version', 'owner', 'annotation', 'key', 'result_path'])
def test_enqueued_v2_prewarm_delete_freeze(ctx, tmp_path, damage):
    from app.context_cache import enqueue_prewarm
    from app.models import Clip, DetectionImport, SubmissionAnnotation, Video, VideoImportBatch
    from app.video_delete_db import VideoDeleteConflictError
    from tests.test_video_delete_db import _base, _freeze, _full_graph

    project_id, video_id, actor_id, category_id = _base(ctx)
    settings = _settings(tmp_path).model_copy(update={'context_prewarm_enabled': True})
    with ctx.session_factory() as db:
        other = Video(project_id=project_id, filename='other.mp4', storage_path='other.mp4',
                      workflow_status='draft')
        db.add(other)
        db.flush()
        other_id = other.id
        jobs = []
        owners = []
        for owner_video_id in (video_id, other_id):
            _, annotation_id = _full_graph(db, project_id, owner_video_id, actor_id,
                                           category_id, source_storage_key=f'{owner_video_id}.mp4')
            # The graph helper uses fixed filenames; keep physical ownership disjoint.
            if owner_video_id == other_id:
                for model, fields in ((DetectionImport, ('tracks_path', 'metadata_path')),
                                      (VideoImportBatch, ('video_path', 'tracks_path', 'metadata_path')),
                                      (Clip, ('clip_path', 'thumbnail_path'))):
                    owner_field = 'created_video_id' if model is VideoImportBatch else 'video_id'
                    rows = (db.query(model).filter_by(**{owner_field: other_id}).all()
                            if model is not Clip else db.query(Clip).filter(
                                Clip.submission_annotation_id == annotation_id).all())
                    for row in rows:
                        for field in fields:
                            setattr(row, field, 'other-' + getattr(row, field))
                for row in db.query(Clip).filter(Clip.annotation_id.is_not(None)).all():
                    if row.annotation.video_id == other_id:
                        row.clip_path, row.thumbnail_path = 'other-live.mp4', 'other-live.jpg'
            annotation = db.get(SubmissionAnnotation, annotation_id)
            clip = db.query(Clip).filter_by(submission_annotation_id=annotation_id).one()
            ids = enqueue_prewarm(db, settings, [(annotation, annotation.submission, clip)])
            assert len(ids) == 1
            job = db.get(BackgroundJob, ids[0])
            assert job.payload['context']['version'] == 2
            job.status = 'succeeded' if owner_video_id == video_id else 'running'
            jobs.append(job)
            owners.append((annotation.submission_id, annotation.id))
        db.commit()
        result = identify_related_video_jobs(db, project_id=project_id, video_id=video_id,
                    submission_ids=[owners[0][0]], submission_annotation_ids=[owners[0][1]])
        assert [job.id for job in result.terminal if job.job_type == 'context_prewarm'] == [jobs[0].id]
        assert not result.active and not result.unknown
        if damage:
            # Damage the other video's descriptor: malformed same-project jobs fail closed.
            job = jobs[1]
            payload = {**job.payload, 'context': {**job.payload['context']}}
            if damage == 'version':
                payload['context']['version'] = 999
            elif damage == 'owner':
                payload['context']['submission_id'] = owners[0][0]
            elif damage == 'annotation':
                payload['context']['annotation'] = {'id': owners[0][1]}
            elif damage == 'key':
                payload['key'] = 'bad'
            else:
                job.result_path = 'unexpected.zip'
            job.payload = payload
            db.commit()
            with pytest.raises(VideoDeleteConflictError):
                _freeze(db, settings, project_id, video_id, actor_id)
        else:
            frozen = _freeze(db, settings, project_id, video_id, actor_id)
            assert jobs[0].id in frozen.terminal_job_ids
            assert jobs[1].id not in frozen.terminal_job_ids
