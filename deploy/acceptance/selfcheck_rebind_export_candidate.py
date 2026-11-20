"""stdlib 临时 fixture；运行 python -B deploy/acceptance/selfcheck_rebind_export_candidate.py。"""
from contextlib import closing
import importlib.util
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile

import rebind_export_candidate as target


def main():
    with closing(sqlite3.connect(':memory:')) as db:
        db.executescript('''
            CREATE TABLE raw_detections(id INTEGER PRIMARY KEY, payload BLOB);
            INSERT INTO raw_detections VALUES(1, zeroblob(1048576));
            CREATE TABLE background_jobs(id INTEGER PRIMARY KEY, refs TEXT);
            INSERT INTO background_jobs VALUES(2, 'old refs'), (1, 'first');
            CREATE TABLE detection_snapshots(id INTEGER PRIMARY KEY, digest TEXT);
            INSERT INTO detection_snapshots VALUES(1, 'snapshot digest');
        ''')
        # Reject raw payload reads while allowing count(*).
        db.set_authorizer(lambda action, table, column, *_:
                          sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_READ
                          and table == 'raw_detections' and column == 'payload'
                          else sqlite3.SQLITE_OK)
        baseline = target._snapshot(db)
        assert baseline['raw_detections'] == {'rows': 1}
        db.execute("UPDATE background_jobs SET refs='changed' WHERE id=2")
        assert target._snapshot(db) != baseline
        db.execute("UPDATE background_jobs SET refs='old refs' WHERE id=2")
        assert target._snapshot(db) == baseline
        db.execute("UPDATE detection_snapshots SET digest='changed'")
        assert target._snapshot(db) != baseline
        db.execute('INSERT INTO raw_detections VALUES(2, NULL)')
        assert target._snapshot(db)['raw_detections']['rows'] == 2
    source = Path(__file__).resolve().parents[2] / 'backend/app/file_identity.py'
    spec = importlib.util.spec_from_file_location('fixture_identity', source)
    identity = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = identity
    spec.loader.exec_module(identity)
    for case in ('success', 'sha', 'size', 'path', 'production_path', 'rollback', 'hardlink'):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / 'backend/app').mkdir(parents=True)
            shutil.copyfile(source, root / 'backend/app/file_identity.py')
            (root / 'PREPARED.ok').touch()
            (root / 'data/videos').mkdir(parents=True)
            media = root / 'data/videos/a.mp4'
            media.write_bytes(b'fixture')
            digest, stat = identity.hash_file_handle(media)
            path = root / 'data/annotation.db'
            with closing(sqlite3.connect(path)) as db:
                db.executescript('''
                    CREATE TABLE submissions(id INTEGER PRIMARY KEY, source_storage_key TEXT,
                      source_video_sha256 TEXT, source_file_size INTEGER, source_mtime_ns INTEGER,
                      source_device INTEGER, source_inode INTEGER);
                    CREATE TABLE background_jobs(id INTEGER, refs TEXT);
                    CREATE TABLE clips(clip_path TEXT);
                    INSERT INTO background_jobs VALUES(1973,'historical refs unchanged');
                    CREATE TRIGGER trg_submission_frozen BEFORE UPDATE ON submissions
                    BEGIN SELECT RAISE(ABORT,'submission immutable'); END;
                ''')
                db.execute('INSERT INTO submissions VALUES(1,?,?,?,?,?,?)',
                           ('../a.mp4' if case == 'path' else 'a.mp4',
                            '0' * 64 if case == 'sha' else digest,
                            stat.size + (case == 'size'), 1, 2, 3))
                if case == 'production_path':
                    db.execute('INSERT INTO clips VALUES(?)', (str(target.PROD) + '/clips/old.mp4',))
                if case == 'rollback':
                    db.execute("CREATE TRIGGER extra_guard BEFORE UPDATE ON submissions BEGIN SELECT RAISE(ABORT,'test rollback'); END")
                db.commit()
                before = list(db.iterdump())
            if case == 'hardlink':
                (root / 'linked.db').hardlink_to(path)
            try:
                report = target._rebind(root, identity.hash_file_handle)
            except (RuntimeError, sqlite3.IntegrityError):
                assert case != 'success', case
                with closing(sqlite3.connect(path)) as db:
                    assert list(db.iterdump()) == before, case
            else:
                assert case == 'success' and report['committed'], case
                with closing(sqlite3.connect(path)) as db:
                    assert db.execute('SELECT source_inode FROM submissions').fetchone()[0] == stat.inode
                    assert db.execute('SELECT refs FROM background_jobs WHERE id=1973').fetchone()[0] == 'historical refs unchanged'
                    try:
                        db.execute("UPDATE submissions SET source_video_sha256='bad'")
                    except sqlite3.IntegrityError:
                        pass
                    else:
                        raise AssertionError('trigger not restored')
    print('PASS: identity rebind; SHA/path/link zero changes; transaction rollback restores exact triggers')


if __name__ == '__main__':
    main()
