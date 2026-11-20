"""全 DB/媒体私有测试副本；默认 plan 零 I/O，不迁移、不修改历史 source identity。"""
import argparse
from contextlib import closing
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import time

ROOT = Path('/data/mouse-annotation/candidates/export-cache-20261120-a1')
PROD = Path('/data/mouse-annotation/data')
PROD_ENV = Path('/home/jinghan/.config/mouse-annotation/backend.env')
GIB = 1024**3
MEDIA = ('videos', 'clips', 'thumbnails', 'detection_imports', 'display_proxies')


def require(ok, message):
    if not ok:
        raise RuntimeError(message)


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    mode = p.add_mutually_exclusive_group()
    mode.add_argument('--plan', action='store_true')
    mode.add_argument('--execute', action='store_true')
    p.add_argument('--media-bytes', type=int)
    p.add_argument('--max-single-copy-bytes', type=int)
    p.add_argument('--zip-max-bytes', type=int)
    p.add_argument('--wal-helper', help='已核对的 prepare_b4b9566.py（只复用 WAL 自检 helper）')
    p.add_argument('--python', default='/opt/mouse-annotation/current/backend/.venv/bin/python')
    return p


def budget(db, media, single, zip_max):
    require(all(isinstance(n, int) and n >= 0 for n in (db, media, single, zip_max)), '预算必须为非负整数')
    return db + media + single + zip_max + 2 * GIB + 20 * GIB


def execute(a):
    require(sys.platform == 'linux' and os.getuid() != 0, '仅授权标注服务器非 root 执行')
    require(a.wal_helper is not None, '必须提供已核对的 WAL helper')
    budget(0, a.media_bytes, a.max_single_copy_bytes, a.zip_max_bytes)
    require(ROOT.parent.resolve() == ROOT.parent and ROOT.parent.is_dir(), '候选父目录缺失/重定向')
    require(not ROOT.exists() and not ROOT.is_symlink(), '候选槽必须不存在；失败现场不能覆盖')
    require(PROD.resolve() == PROD, '生产数据路径被重定向')
    require(Path(a.python).is_file(), '只读生产 venv Python 不存在')
    os.umask(0o077)
    ROOT.mkdir(mode=0o700)
    log = (ROOT / 'prepare.log').open('x', buffering=1)

    def mark(text):
        print(text, flush=True)
        print(text, file=log, flush=True)

    def free_gate(required=20 * GIB + 2 * GIB):
        require(shutil.disk_usage(ROOT).free >= required, '空间门禁失败，保留现场')

    def run_copy(args):
        with (ROOT / 'copy.log').open('a') as out:
            proc = subprocess.Popen(args, stdout=out, stderr=subprocess.STDOUT)
            try:
                while proc.poll() is None:
                    free_gate()
                    time.sleep(1)
                require(proc.returncode == 0, 'rsync 失败；见 copy.log')
            finally:
                if proc.poll() is None:
                    proc.terminate()
                    proc.wait()

    try:
        mark('[1/8] CLAIMED')
        data, tmp = ROOT / 'data', ROOT / 'tmp'
        data.mkdir()
        tmp.mkdir()
        os.environ.update(TMPDIR=str(tmp), SQLITE_TMPDIR=str(tmp))
        sys.dont_write_bytecode = True
        spec = importlib.util.spec_from_file_location('reviewed_a2', a.wal_helper)
        helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(helper)
        helper.verify_readonly_shm(tmp)
        helper.require_prod_wal_files(PROD / 'annotation.db')
        mark('[2/8] WAL_READONLY_SHM_VERIFIED')
        db_path = data / 'annotation.db'
        with closing(sqlite3.connect(helper.readonly_shm_uri(PROD / 'annotation.db'), uri=True)) as source:
            require(source.execute('SELECT version_num FROM alembic_version').fetchall() == [('0022',)], '要求 schema0022；不 migrate')
            require(not source.execute("SELECT 1 FROM background_jobs WHERE status NOT IN ('succeeded','failed','cancelled') LIMIT 1").fetchone(), '存在非 terminal 后台任务')
            db_bytes = source.execute('PRAGMA page_count').fetchone()[0] * source.execute('PRAGMA page_size').fetchone()[0]
            free_gate(budget(db_bytes, a.media_bytes, a.max_single_copy_bytes, a.zip_max_bytes))
            with closing(sqlite3.connect(db_path)) as target:
                source.backup(target, pages=8192, progress=lambda *_: free_gate())
        mark('[3/8] DB_BACKUP_COMPLETE_NO_MIGRATION')
        # 不复制任何 DB/WAL、删除 manifest、恢复锁或历史导出。
        actual_bytes = max_file = 0
        for name in MEDIA:
            source_dir = PROD / name
            require(source_dir.is_dir() and source_dir.resolve() == source_dir, '媒体目录缺失/重定向')
            for parent, dirs, files in os.walk(source_dir, followlinks=False):
                for entry in dirs + files:
                    p = Path(parent) / entry
                    require(not p.is_symlink(), '媒体内存在 symlink，拒绝复制')
                    if p.is_file():
                        size = p.stat().st_size
                        actual_bytes += size
                        max_file = max(max_file, size)
            (data / name).mkdir()
        require(actual_bytes <= a.media_bytes and max_file <= a.max_single_copy_bytes, '实际媒体超过输入预算')
        free_gate(budget(0, actual_bytes, a.max_single_copy_bytes, a.zip_max_bytes))
        for name in MEDIA:
            run_copy(['/usr/bin/rsync', '-rt', '--checksum', '--', str(PROD / name) + '/', str(data / name) + '/'])
            # 二次 checksum dry-run：不追赶持续变化的生产媒体，变化则保留现场失败。
            result = subprocess.run(['/usr/bin/rsync', '-rcn', '--delete', '--out-format=%i %n', '--',
                                     str(PROD / name) + '/', str(data / name) + '/'], capture_output=True, text=True)
            require(result.returncode == 0 and not result.stdout.strip(), '媒体复制后校验不同/生产发生变化')
        mark('[4/8] PRIVATE_MEDIA_COPIED_CHECKSUM_VERIFIED')
        with closing(sqlite3.connect(db_path)) as db:
            require(db.execute('PRAGMA quick_check').fetchall() == [('ok',)], '副本 quick_check 失败')
            require(db.execute('PRAGMA foreign_key_check').fetchone() is None, '副本 FK 检查失败')
            trigger_sql = list(db.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' ORDER BY name"))
            require(trigger_sql, '副本触发器缺失')
            # 仅读 DEMO_USERNAME 这一项；不解析生产 secret，不调用 seed。
            selected = ''.join(line for line in PROD_ENV.open() if re.match(r'^\s*(?:export\s+)?DEMO_USERNAME\s*=', line))
            from dotenv import dotenv_values
            username = dotenv_values(stream=io.StringIO(selected), interpolate=False).get('DEMO_USERNAME')
            require(username and '${' not in username and not any(c in username for c in '\r\n\0'), 'DEMO_USERNAME 无效')
            require(db.execute('SELECT count(*) FROM users WHERE username=?', (username,)).fetchone()[0] == 1, '必须保留既有用户')
        env = dict(ENV='production', DATA_DIR=str(data), DATABASE_URL=f'sqlite:///{db_path}',
                   DEMO_USERNAME=username, DEMO_PASSWORD=secrets.token_urlsafe(32),
                   SECRET_KEY=secrets.token_urlsafe(48), MEDIA_MASTER_SECRET=secrets.token_urlsafe(32),
                   CLEANUP_ENABLED='false', CONTEXT_PREWARM_ENABLED='true', CONTEXT_CACHE_MAX_BYTES=str(512 * 1024**2),
                   EXPORT_DISK_RESERVE_BYTES=str(2 * GIB), UPLOAD_DISK_RESERVE_BYTES=str(2 * GIB),
                   DISPLAY_PROXIES_ENABLED='false', MEDIA_TICKET_ENABLED='true', MEDIA_LEGACY_BEARER_ENABLED='true',
                   CORS_ORIGINS='http://127.0.0.1:15180', FFMPEG_PATH='/usr/bin/ffmpeg', FFPROBE_PATH='/usr/bin/ffprobe',
                   TMPDIR=str(tmp), SQLITE_TMPDIR=str(tmp), PYTHONDONTWRITEBYTECODE='1')
        with (ROOT / 'candidate.env.json').open('x') as f:
            json.dump(env, f)
        require((ROOT / 'candidate.env.json').stat().st_mode & 0o777 == 0o600, 'env 必须0600')
        mark('[5/8] INDEPENDENT_ENV_0600_USERS_UNCHANGED')
        wrapper = '''import json, os, pathlib, sys
root = pathlib.Path(__file__).resolve().parent
env = json.loads((root / 'candidate.env.json').read_text())
assert root == pathlib.Path('/data/mouse-annotation/candidates/export-cache-20261120-a1')
assert env['DATABASE_URL'] == 'sqlite:///' + str(root / 'data/annotation.db')
os.environ.clear()
os.environ.update(env, PATH='/usr/bin:/bin', LANG='C.UTF-8')
backend = root / 'backend'
assert backend.is_dir() and not (backend / '.env').exists()
os.chdir(backend)
sys.path.insert(0, str(root / 'deps'))
sys.path.insert(0, str(backend))
if len(sys.argv) > 1:
    assert sys.argv[1] == '--tests'
    import pytest
    raise SystemExit(pytest.main(['-p', 'no:cacheprovider', *sys.argv[2:]]))
import uvicorn
uvicorn.run('app.main:app', host='127.0.0.1', port=18080, workers=1)
'''
        (ROOT / 'boot.py').write_text(wrapper)
        report = dict(schema='0022', media_bytes=actual_bytes, max_file=max_file,
                      triggers=trigger_sql, history_identity_rebound=False,
                      limitation='历史媒体可预览；旧 submission 冻结 inode 不改、不绕过。新短视频导入闭环不能证明生产历史副本导出。历史 ZIP 未复制，可能404。',
                      zip_budget_bytes=a.zip_max_bytes, zip_budget_is_runtime_cap=False)
        (ROOT / 'verification.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
        mark('[6/8] WRAPPER_READY_BACKEND_DIST_MUST_BE_STAGED_SEPARATELY')
        free_gate()
        (ROOT / 'PREPARED.ok').write_text('0022\n')
        mark('[7/8] PREPARED_NO_SERVICES_STARTED')
        mark('[8/8] IDENTITY_LIMITATION_RECORDED')
    except Exception:
        print('FAILED: 保留现场；不自动清理/重试覆盖', file=log)
        raise
    finally:
        log.close()


def main(argv=None):
    a = parser().parse_args(argv)
    if not a.execute:
        print('PLAN_NO_IO: WAL helper自检 → readonly_shm backup0022 → 完整私有媒体rsync/checksum → 独立0600环境；不迁移/启动/改用户或历史identity。')
        print(f'ROOT={ROOT}; ports=18080/15180; 空间=DB+媒体+maxsingle+ZIP预算+2GiB+20GiB；无skip-media。')
        return 0
    try:
        execute(a)
    except Exception as exc:
        print(f'FAILED ({type(exc).__name__}): {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
