"""私有副本单条真实历史资产冷热验收；默认 plan 零 I/O，不启动服务。"""
import argparse
import hashlib
import json
import logging
import os
from pathlib import Path
import shutil
import signal
import socket
import sys
import time
import zipfile
from contextlib import contextmanager
from unittest.mock import patch

ROOT = Path('/data/mouse-annotation/candidates/export-cache-20261120-a1')


class CheckFailure(RuntimeError):
    pass


def require(ok, message):
    if not ok:
        raise CheckFailure(message)


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    mode = p.add_mutually_exclusive_group()
    mode.add_argument('--execute', action='store_true')
    mode.add_argument('--plan', action='store_true')
    p.add_argument('--selfcheck', action='store_true')
    return p


def narrow(payload, annotation_id, submission_id, category_id):
    """保留 enqueue 的原始嵌套 context/digests；不重算或绕过冻结校验。"""
    result = json.loads(json.dumps(payload))
    refs = [r for r in result['refs'] if r['submission_annotation_id'] == annotation_id]
    require(len(refs) == 1 and refs[0]['submission_id'] == submission_id, 'single ref mismatch')
    result.update(submission_ids=[submission_id], submission_annotation_ids=[annotation_id],
                  category_ids=[category_id], refs=refs)
    for name in ('category_directories', 'category_tokens'):
        result[name] = {str(category_id): result[name][str(category_id)]}
    return result


def private(path, file=True):
    require(path.is_relative_to(ROOT) and path.resolve() == path, 'candidate path/link guard')
    require(path.is_file() and path.stat().st_nlink == 1 if file else path.is_dir(),
            'candidate independent file/directory required')
    return path


def private_data():
    # 只检查业务 data（含 DB sidecars、媒体、缓存和 exports），不扫描 deps/测试 sandbox。
    data = private(ROOT / 'data', False)

    def walk_error(error):
        raise error

    for parent, dirs, files in os.walk(data, followlinks=False, onerror=walk_error):
        for name in dirs:
            private(Path(parent) / name, False)
        for name in files:
            private(Path(parent) / name)


@contextmanager
def deadline():
    def expired(*_):
        raise TimeoutError('test exceeded 600 seconds')
    old = signal.signal(signal.SIGALRM, expired)
    signal.alarm(600)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)


def execute(report):
    require(sys.platform == 'linux' and os.getuid() != 0, 'Linux non-root only')
    private(ROOT, False)
    for name in ('PREPARED.ok', 'rebind-export-report.json', 'candidate.env.json', 'data/annotation.db'):
        private(ROOT / name)
    for name in ('__init__', 'config', 'export_jobs', 'context_cache', 'media', 'models',
                 'submission_media_plan', 'file_identity'):
        private(ROOT / 'backend/app' / f'{name}.py')
    require(json.loads((ROOT / 'rebind-export-report.json').read_text()).get('committed') is True,
            'identity rebind not committed')
    require(not (ROOT / 'candidateapp.pid').exists(), 'candidate must be stopped')
    with socket.socket() as sock:
        require(sock.connect_ex(('127.0.0.1', 18080)) != 0, 'candidate port listening')
    private_data()
    env = json.loads((ROOT / 'candidate.env.json').read_text())
    url = 'sqlite:///' + str(ROOT / 'data/annotation.db')
    require(env.get('DATABASE_URL') == url and env.get('DATA_DIR') == str(ROOT / 'data'),
            'candidate env database/data guard')
    require(not (ROOT / 'backend/.env').exists(), 'backend .env forbidden')
    os.environ.clear()
    os.environ.update(env, PATH='/usr/bin:/bin', LANG='C.UTF-8')
    sys.dont_write_bytecode = True
    sys.path[:0] = [str(ROOT / 'backend'), str(ROOT / 'deps')]
    logging.disable(logging.CRITICAL)  # 不将运行库异常中的配置或路径透传 stdout。
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from app.config import Settings
    from app import export_jobs as exports
    from app import context_cache as cache
    from app.media import FfmpegMediaProcessor
    from app.models import BackgroundJob, Project
    from app.submission_media_plan import build_submission_media_plan

    settings = Settings(_env_file=None)
    require(settings.resolved_database_url == url and settings.data_dir == ROOT / 'data', 'settings guard')
    settings.media_synchronous = True
    settings.media_timeout_seconds = 600
    settings.context_prewarm_enabled = False
    settings.ffmpeg_path, settings.ffprobe_path = '/usr/bin/ffmpeg', '/usr/bin/ffprobe'
    # 使用实例正常缓存键；已有命中则拒绝冒充 cold，不删除缓存。
    settings.media_preset = 'veryfast'
    require(settings.context_cache_max_bytes > 0, 'cache disabled')
    settings.exports_dir.mkdir(exist_ok=True)
    engine = create_engine(url)
    factory = sessionmaker(bind=engine)
    processor = FfmpegMediaProcessor(timeout_seconds=600,
        ffmpeg_path=settings.ffmpeg_path, ffprobe_path=settings.ffprobe_path,
        crf=settings.media_crf, preset=settings.media_preset, map_audio=settings.media_map_audio)
    worker = exports.ExportWorker(processor, factory, settings)
    with factory() as db:
        history = {j.id: {c.name: getattr(j, c.name) for c in j.__table__.columns}
                   for j in db.query(BackgroundJob).all()}
        require(all(j['status'] in ('succeeded', 'failed', 'cancelled') for j in history.values()),
                'nonterminal historical jobs; do not recover/retry')
        rows = sorted(exports.asset_rows(db, 2, None, source='manual'),
                      key=lambda r: (r[1].source_file_size, r[0].end_frame-r[0].start_frame, r[0].id))
        require(rows, 'no published manual Project 2 assets')
        a, s, clip = rows[0]
        aid, sid, category = a.id, s.id, a.category_id
        context = cache.frozen_context(db, a, s, clip)
        require(cache.lookup(settings, s.video_id, cache.cache_key(settings, context)) is None,
                'selected context already cached; cold would not be cold (no deletion)')
        start, end = context['range']
        snap = s.detection_snapshot
        plan = build_submission_media_plan(start_time=start/snap.fps, end_time=(end+1)/snap.fps,
            start_frame=start, end_frame=end, fps=snap.fps, frame_count=snap.frame_count,
            width=snap.width, height=snap.height, crop_region=a.crop_region)
        expected = dict(fps=snap.fps, width=plan.output_width, height=plan.output_height,
                        frame_count=end-start+1)
        report['selected'] = dict(annotation_id=aid, submission_id=sid, clip_id=clip.id,
            source_file_size=s.source_file_size, annotation_range=[a.start_frame, a.end_frame],
            context_range=[start, end], crop=plan.crop, expected=expected)

    def run(label, fault=False):
        with factory() as db:
            job = exports.enqueue_export_job(db, db.get(Project, 2), [category], source='manual')
            require(job is not None and job.status == 'queued' and job.id not in history,
                    'new queued job required')
            jid = job.id
            report['tests'].append(dict(label=label, job_id=jid))
            job.payload = narrow(job.payload, aid, sid, category)
            require(len(worker._frozen_rows(db, job, require_ready=False)) == 1, 'frozen authority')
            db.commit()
        counts = dict(render=0, tracks=0, stagecopy=0)
        timings = dict(render=0.0, tracks=0.0, stagecopy=0.0)

        def instrument(name, fn):
            def wrapped(*args, **kwargs):
                counts[name] += 1
                t = time.monotonic()
                try:
                    return fn(*args, **kwargs)
                finally:
                    timings[name] += time.monotonic()-t
            return wrapped

        reserve = settings.export_disk_reserve_bytes
        if fault:
            settings.export_disk_reserve_bytes = shutil.disk_usage(ROOT).free + 1024**3
        t = time.monotonic()
        try:
            with deadline(), patch.object(processor, 'render_clip', instrument('render', processor.render_clip)), \
                 patch.object(worker, '_write_tracks_range', instrument('tracks', worker._write_tracks_range)), \
                 patch.object(exports, 'stage_submission_input', instrument('stagecopy', exports.stage_submission_input)):
                worker._run_job(jid)  # 精确领取本次 job，绝不 start/wake/drain/recover。
        finally:
            settings.export_disk_reserve_bytes = reserve
            report['tests'][-1].update(counts=counts, timings=timings, seconds=time.monotonic()-t)
        with factory() as db:
            job = db.get(BackgroundJob, jid)
            report['tests'][-1]['status'] = job.status
            if fault:
                require(job.status == 'failed' and (job.error or '').startswith('storage_insufficient:')
                        and not job.result_path, 'expected safe storage_insufficient without result_path')
                require(not list(settings.exports_dir.glob(f'*{jid}*.zip')), 'fault left ZIP')
                report['tests'][-1]['error_code'] = 'storage_insufficient'
                return None
            require(job.status == 'succeeded' and job.result_path, 'export failed; inspect candidate job/artifacts')
            path = private(settings.exports_dir / job.result_path)
        members = {}
        with deadline(), zipfile.ZipFile(path) as z:
            require(z.testzip() is None and len(z.infolist()) == 4, 'four member CRC gate')
            for info in z.infolist():
                content = z.read(info)
                members[info.filename] = dict(size=info.file_size, crc=info.CRC,
                                              sha256=hashlib.sha256(content).hexdigest())
            metadata = json.loads(z.read(next(n for n in members if n.endswith('/metadata.json'))))
            require(metadata['source']['clip']['start_frame'] == start and
                    metadata['source']['clip']['end_frame'] == end, 'context range mismatch')
            crop = plan.crop or (0, 0, snap.width, snap.height)
            require(metadata['source']['crop_region'] == dict(zip(('x', 'y', 'w', 'h'), crop)), 'crop mismatch')
            probe_path = ROOT / f'{label}-{jid}-clip.mp4'
            with probe_path.open('xb') as out:
                out.write(z.read(next(n for n in members if n.endswith('/clip.mp4'))))
            probe = processor.probe_clip(str(probe_path), expected=expected)
        report['tests'][-1].update(artifact=str(path), zip_size=path.stat().st_size,
            zip_sha256=hashlib.sha256(path.read_bytes()).hexdigest(), members=members, ffprobe=probe)
        if label == 'cold':
            require(counts['tracks'] == 1 and counts['stagecopy'] == 1, 'cold not exercised')
        if label == 'warm':
            require(not any(counts.values()), 'warm must render/track/stagecopy zero')
        return members

    cold = run('cold')
    warm = run('warm')
    require(cold == warm, 'cold/warm member content differs')
    run('storage-fault', True)
    # Leave the browser-facing candidate on a successful job, not the injected failure.
    # The space fault can legitimately evict the cache; rebuild if needed.
    run('ready')
    with factory() as db:
        require(all({c.name: getattr(db.get(BackgroundJob, jid), c.name) for c in BackgroundJob.__table__.columns} == old
                    for jid, old in history.items()), 'historical jobs changed (including 1973)')
    worker.shutdown()
    engine.dispose()


def selfcheck():
    assert not parser().parse_args([]).execute
    assert parser().parse_args(['--execute']).execute
    p = dict(refs=[dict(submission_annotation_id=3, submission_id=2, context={'range': [0, 9]}),
                   dict(submission_annotation_id=4, submission_id=5)],
             category_directories={'7': 'x'}, category_tokens={'7': 't'})
    q = narrow(p, 3, 2, 7)
    assert len(p['refs']) == 2 and q['refs'][0] == p['refs'][0]
    assert q['submission_annotation_ids'] == [3] and q['submission_ids'] == [2]
    try:
        narrow(p, 3, 999, 7)
    except RuntimeError:
        pass
    else:
        raise AssertionError('mismatched owner accepted')
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp).resolve()
        (root / 'data').mkdir()
        target = root / 'data/annotation.db'
        target.write_text('fixture')
        (root / 'tests-tmp').mkdir()
        (root / 'deps').mkdir()
        with patch.dict(globals(), ROOT=root):
            # Hardlinks are available without Windows symlink privileges.
            os.link(target, root / 'tests-tmp/sandbox-link')
            os.link(target, root / 'deps/sandbox-link')
            # The data file itself must remain independent.
            target.unlink()
            target.write_text('fixture')
            private_data()
            os.link(target, root / 'data/annotation.db-wal')
            try:
                private_data()
            except CheckFailure:
                pass
            else:
                raise AssertionError('business data hardlink accepted')
            (root / 'data/annotation.db-wal').unlink()
            if sys.platform == 'linux':
                (root / 'tests-tmp/symlink').symlink_to(target)
                (root / 'deps/symlink').symlink_to(target)
                private_data()
                (root / 'data/exports').symlink_to(root / 'tests-tmp', target_is_directory=True)
                try:
                    private_data()
                except CheckFailure:
                    pass
                else:
                    raise AssertionError('business data symlink accepted')
    print('SELF_CHECK_OK: parser, frozen payload and private data scope; no candidate I/O')


def main(argv=None):
    args = parser().parse_args(argv)
    if args.selfcheck:
        selfcheck()
        return 0
    if not args.execute:
        print('PLAN_NO_IO: candidate Project 2 manual single real asset cold/warm + storage fault; not full710/public API acceptance')
        return 0
    report = dict(scope='内部受控单条真实历史资产验证；非全710、非公共API全量验收', tests=[], passed=False)
    destination = ROOT / 'real-export-check.json'
    try:
        private(ROOT, False)
        require(not destination.exists() and not destination.is_symlink(), 'report exists; preserve artifacts')
        with destination.open('x') as out:
            json.dump(report, out, ensure_ascii=False)
    except Exception:
        print('FAILED: report guard; preserve existing artifacts')
        return 1
    try:
        execute(report)
        report['passed'] = True
    except BaseException as exc:
        # 不回显 exception 文本（第三方 ValidationError 可能含 secret）。
        report['failure_type'] = type(exc).__name__
        report['failure'] = '验收失败；保留候选 jobs、报告及工件，不自动重试。'
        if isinstance(exc, CheckFailure):
            report['check_failure'] = str(exc)
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    print(json.dumps(report, ensure_ascii=False, default=str))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
