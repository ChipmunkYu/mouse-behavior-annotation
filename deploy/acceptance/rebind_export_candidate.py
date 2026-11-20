"""仅私有导出验收副本的历史身份重绑定；默认 plan 不访问文件系统。"""
import argparse
from contextlib import closing
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import socket
import sqlite3
import sys

ROOT = Path('/data/mouse-annotation/candidates/export-cache-20261120-a1')
PROD = Path('/data/mouse-annotation/data')
FIELDS = ('source_file_size', 'source_mtime_ns', 'source_device', 'source_inode')
DIGEST_TABLES = frozenset({
    'submissions', 'background_jobs', 'detection_snapshots', 'annotations', 'reviews',
    'submission_annotations', 'behavior_review_decisions', 'behavior_review_reopens',
    'feedback_marks', 'category_scheme_audits',
})
PATH_COLUMNS = {
    'videos': ('storage_path', 'display_path'),
    'clips': ('clip_path', 'thumbnail_path'),
    'background_jobs': ('result_path',),
    'video_import_batches': ('annotation_path', 'video_path', 'tracks_path', 'metadata_path'),
    'detection_imports': ('tracks_path', 'metadata_path'),
}


def require(ok, message):
    if not ok:
        raise RuntimeError(message)


def _private(path, root, *, file=True):
    require(path.is_relative_to(root) and path.resolve() == path, '路径越界或 symlink')
    if file:
        require(path.is_file() and path.stat().st_nlink == 1, '要求独立普通文件，禁止 hardlink')
    return path


def _key(value):
    require(isinstance(value, str) and value and '\\' not in value and ':' not in value
            and not value.startswith('/') and str(PurePosixPath(value)) == value
            and all(p not in ('.', '..', '') for p in value.split('/')), '非法 source_storage_key')
    return value


def _snapshot(db):
    """全表 count；小型权威表按主键流式摘要，排除四个获准身份列。

    raw/state 等其余表不重读内容；依靠 snapshot 摘要、受限 UPDATE 及精确
    trigger SQL 恢复约束。无主键的旧 schema/fixture 使用 rowid 稳定排序。
    """
    result = {}
    for (table,) in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
        q = '"' + table.replace('"', '""') + '"'
        result[table] = {'rows': db.execute(f'SELECT count(*) FROM {q}').fetchone()[0]}
        if table not in DIGEST_TABLES:
            continue
        info = list(db.execute(f'PRAGMA table_info({q})'))
        columns = [r[1] for r in info]
        selected = [c for c in columns if table != 'submissions' or c not in FIELDS]
        names = ','.join('"' + c.replace('"', '""') + '"' for c in selected)
        keys = [r[1] for r in sorted(info, key=lambda r: r[5]) if r[5]]
        order = ','.join('"' + c.replace('"', '""') + '"' for c in keys) or 'rowid'
        digest = hashlib.sha256()
        for row in db.execute(f'SELECT {names} FROM {q} ORDER BY {order}'):
            payload = repr(tuple(row)).encode()
            digest.update(len(payload).to_bytes(8, 'big'))
            digest.update(payload)
        result[table]['sha256'] = digest.hexdigest()
    return result


def _rebind(root, hash_file_handle):
    """私有参数入口供临时 fixture 使用；CLI 不允许覆盖固定 root。"""
    root = Path(root)
    require(root.is_absolute() and root.resolve() == root and not root.is_relative_to(PROD), '非法 candidate root')
    _private(root / 'PREPARED.ok', root)
    _private(root / 'backend/app/file_identity.py', root)
    require(not (root / 'candidateapp.pid').exists() and not (root / 'candidateapp.pid').is_symlink(),
            'candidateapp.pid 存在；拒绝运行')
    with socket.socket() as sock:
        require(sock.connect_ex(('127.0.0.1', 18080)) != 0, 'candidate 18080 已监听')
    data = _private(root / 'data', root, file=False)
    path = _private(data / 'annotation.db', root)
    for suffix in ('-wal', '-shm', '-journal'):
        sidecar = Path(str(path) + suffix)
        if sidecar.exists() or sidecar.is_symlink():
            _private(sidecar, root)
    # 私有媒体树禁止任何链接，不访问生产源文件。
    for name in ('videos', 'clips', 'thumbnails', 'detection_imports', 'display_proxies'):
        directory = data / name
        if directory.exists() or directory.is_symlink():
            _private(directory, root, file=False)
            for parent, dirs, files in os.walk(directory, followlinks=False):
                for entry in dirs:
                    _private(Path(parent) / entry, root, file=False)
                for entry in files:
                    _private(Path(parent) / entry, root)
    with closing(sqlite3.connect(f'{path.as_uri()}?mode=rw', uri=True, isolation_level=None)) as db:
        db.row_factory = sqlite3.Row
        baseline = _snapshot(db)
        triggers = [tuple(r) for r in db.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' ORDER BY name")]
        sql = dict(triggers).get('trg_submission_frozen')
        require(sql, '冻结 trigger 缺失')
        for table, columns in PATH_COLUMNS.items():
            present = {r[1] for r in db.execute(f'PRAGMA table_info("{table}")')}
            for column in set(columns) & present:
                for (value,) in db.execute(f'SELECT "{column}" FROM "{table}" WHERE "{column}" IS NOT NULL'):
                    require(not (str(value) == str(PROD) or str(value).startswith(str(PROD) + '/')), '明确路径列指向生产，拒绝修改')
        rows = [dict(r) for r in db.execute('SELECT * FROM submissions ORDER BY id')]
        mapping, hashed = [], {}
        for row in rows:
            key = _key(row['source_storage_key'])
            media = _private(data / 'videos' / key, root)
            if key not in hashed:
                digest, identity = hash_file_handle(media)
                hashed[key] = (digest, identity)
            digest, identity = hashed[key]
            require(row['source_video_sha256'] == digest and row['source_file_size'] == identity.size,
                    '历史预期 SHA/size 不匹配；零更新')
            new = dict(zip(FIELDS, (identity.size, identity.mtime_ns, identity.device, identity.inode)))
            mapping.append(dict(id=row['id'], source_storage_key=key, sha256=digest,
                                old={k: row[k] for k in FIELDS}, new=new))
        report = dict(hash_gate='passed', mapping=mapping, logical_digests=baseline,
                      digest_scope=dict(
                          method='streaming primary-key order; rowid fallback; length-prefixed repr rows',
                          excluded_submission_columns=list(FIELDS),
                          count_only_tables=[t for t, value in baseline.items() if 'sha256' not in value],
                          limitation='Large immutable raw/state payloads are not rehashed; all table counts checked. '
                          'Snapshot digests, four-column-only UPDATE and exact trigger SQL restoration constrain this transaction.'),
                      historical_jobs='unchanged; including 1973, no retry', committed=False)
        report_path = root / 'rebind-export-report.json'
        require(not report_path.exists() and not report_path.is_symlink(), '报告已存在，保留现场')
        with report_path.open('x') as out:
            json.dump(report, out, ensure_ascii=False, indent=2)
        try:
            db.execute('BEGIN IMMEDIATE')
            require(_snapshot(db) == baseline and [dict(r) for r in db.execute('SELECT * FROM submissions ORDER BY id')] == rows,
                    'hash 后数据库变化')
            require([tuple(r) for r in db.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' ORDER BY name")] == triggers,
                    'trigger 发生变化')
            db.execute('DROP TRIGGER trg_submission_frozen')
            for item in mapping:
                cursor = db.execute('UPDATE submissions SET source_file_size=?,source_mtime_ns=?,source_device=?,source_inode=? WHERE id=?',
                                    (*item['new'].values(), item['id']))
                require(cursor.rowcount == 1, '更新行数异常')
            db.execute(sql)
            require([tuple(r) for r in db.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' ORDER BY name")] == triggers,
                    'trigger SQL 未精确恢复')
            if rows:
                db.execute('SAVEPOINT frozen_probe')
                try:
                    db.execute("UPDATE submissions SET source_video_sha256=source_video_sha256 || 'x' WHERE id=?", (rows[0]['id'],))
                except sqlite3.IntegrityError as exc:
                    require('submission immutable' in str(exc), '不是冻结 trigger 拒绝')
                else:
                    raise RuntimeError('冻结 trigger 未阻止修改')
                finally:
                    db.execute('ROLLBACK TO frozen_probe')
                    db.execute('RELEASE frozen_probe')
            require(_snapshot(db) == baseline, '逻辑摘要/行数/jobs refs 变化')
            actual = [dict(r) for r in db.execute('SELECT * FROM submissions ORDER BY id')]
            expected = [dict(row, **item['new']) for row, item in zip(rows, mapping)]
            require(actual == expected, '身份更新结果异常')
            db.execute('COMMIT')
        except BaseException:
            if db.in_transaction:
                db.execute('ROLLBACK')
            raise
        report['committed'] = True
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
        return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--plan', action='store_true')
    args = parser.parse_args(argv)
    if not args.execute:
        print(f'PLAN_NO_IO: {ROOT}; SHA+size gate → candidate-only identity transaction; jobs/refs/SHA/storage keys unchanged')
        return 0
    require(sys.platform == 'linux' and os.getuid() != 0, '仅 Linux 非 root')
    _private(ROOT / 'backend/app/file_identity.py', ROOT)
    sys.dont_write_bytecode = True
    spec = importlib.util.spec_from_file_location('candidate_file_identity', ROOT / 'backend/app/file_identity.py')
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    _rebind(ROOT, module.hash_file_handle)
    print('COMMITTED: candidate-only identity; 旧 job 1973 不改 refs、不重试；新导出冻结新 identity，缓存自然使用新 key。')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
