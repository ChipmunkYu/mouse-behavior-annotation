from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from threading import Barrier

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app import project_write_gate as gate_module
from app.models import Annotation, BehaviorCategory, CategorySchemeAudit, Project
from app.participant_roles import ParticipantRoleError, canonicalize_participant_roles
from tests.conftest import auth_headers


def _project(ctx):
    headers = auth_headers(ctx.client)
    response = ctx.client.post("/api/projects", json={"name": "scheme"}, headers=headers)
    assert response.status_code == 201
    return headers, response.json()


def _payload(expected_version=0):
    return {"expected_version": expected_version, "categories": [{
        "name": "追逐", "group": "社交行为", "sort_order": 0,
        "participant_mode": "role_based", "role_definitions": [
            {"name": "追逐者", "min_count": 1, "max_count": 1, "role_sort_order": 0},
            {"name": "被追逐者", "min_count": 1, "max_count": None, "role_sort_order": 1},
        ],
    }]}


def test_owner_replace_lock_audit_and_runtime_read(ctx):
    headers, project = _project(ctx)
    pid = project["id"]
    assert ctx.client.get(f"/api/projects/{pid}/categories", headers=headers).status_code == 409
    replaced = ctx.client.put(f"/api/projects/{pid}/category-scheme", json=_payload(), headers=headers)
    assert replaced.status_code == 200, replaced.text
    category = replaced.json()["categories"][0]
    assert category["mouse_count_min"] == 2 and category["mouse_count_max"] is None
    assert all(role["key"].startswith("role_") for role in category["role_definitions"])
    locked = ctx.client.post(
        f"/api/projects/{pid}/category-scheme/lock", json={"expected_version": 1}, headers=headers
    )
    assert locked.status_code == 200, locked.text
    first_time = locked.json()["category_scheme_locked_at"]
    again = ctx.client.post(
        f"/api/projects/{pid}/category-scheme/lock", json={"expected_version": 0}, headers=headers
    )
    assert again.status_code == 200
    assert again.json()["category_scheme_locked_at"] == first_time
    assert again.json()["category_scheme_version"] == 1
    assert ctx.client.get(f"/api/projects/{pid}/categories", headers=headers).status_code == 200
    audit = ctx.client.get(f"/api/projects/{pid}/category-scheme/audit", headers=headers)
    assert [row["action"] for row in audit.json()] == ["replace", "replace", "lock"]
    lock_audit = audit.json()[-1]
    assert lock_audit["before_json"]["category_scheme_locked_at"] is None
    assert lock_audit["before_json"]["category_scheme_locked_by"] is None
    assert lock_audit["after_json"]["category_scheme_locked_at"] == first_time
    assert lock_audit["after_json"]["category_scheme_locked_by"] is not None
    import hashlib
    import json
    encoded = json.dumps(
        lock_audit["after_json"], ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    assert lock_audit["scheme_hash"] == hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def test_empty_category_scheme_cannot_be_locked(ctx):
    headers, project = _project(ctx)
    emptied = ctx.client.put(
        f"/api/projects/{project['id']}/category-scheme",
        json={"expected_version": 0, "categories": []}, headers=headers,
    )
    assert emptied.status_code == 200
    response = ctx.client.post(
        f"/api/projects/{project['id']}/category-scheme/lock",
        json={"expected_version": 1}, headers=headers,
    )
    assert response.status_code == 422
    assert response.json()["detail"] == "Category scheme must contain at least one category"


@pytest.mark.parametrize("orders", [[1], [0, 0], [0, 2], [False]])
def test_category_sort_order_must_be_canonical(ctx, orders):
    headers, project = _project(ctx)
    categories = [
        {"name": f"类别{index}", "group": "测试", "sort_order": order}
        for index, order in enumerate(orders)
    ]
    response = ctx.client.put(
        f"/api/projects/{project['id']}/category-scheme",
        json={"expected_version": 0, "categories": categories}, headers=headers,
    )
    assert response.status_code == 422


def test_existing_role_order_is_append_only(ctx):
    headers, project = _project(ctx)
    pid = project["id"]
    category = ctx.client.put(
        f"/api/projects/{pid}/category-scheme", json=_payload(), headers=headers
    ).json()["categories"][0]
    roles = category["role_definitions"]

    def put(definitions, expected_version=1):
        body = _payload(expected_version)
        body["categories"][0]["id"] = category["id"]
        body["categories"][0]["role_definitions"] = definitions
        return ctx.client.put(f"/api/projects/{pid}/category-scheme", json=body, headers=headers)

    reordered = [dict(roles[1], role_sort_order=0), dict(roles[0], role_sort_order=1)]
    assert put(reordered).status_code == 422
    inserted = [
        dict(roles[0], role_sort_order=0),
        {"name": "新角色", "min_count": 1, "max_count": 1, "role_sort_order": 1},
        dict(roles[1], role_sort_order=2),
    ]
    assert put(inserted).status_code == 422
    delete_then_append = [
        dict(roles[1], role_sort_order=0),
        {"name": "新角色", "min_count": 1, "max_count": 1, "role_sort_order": 1},
    ]
    accepted = put(delete_then_append)
    assert accepted.status_code == 200, accepted.text
    accepted_roles = accepted.json()["categories"][0]["role_definitions"]
    assert accepted_roles[0]["key"] == roles[1]["key"]
    assert accepted_roles[1]["key"] not in {role["key"] for role in roles}


def test_participant_role_max_is_error_but_min_is_incomplete():
    definitions = [{"key": "role_" + "1" * 32, "min_count": 2, "max_count": 3}]
    assert canonicalize_participant_roles(definitions, {definitions[0]["key"]: [1]})[2] == "needs_participants"
    with pytest.raises(ParticipantRoleError, match="exceeds max_count"):
        canonicalize_participant_roles(definitions, {definitions[0]["key"]: [1, 2, 3, 4]})


def test_scheme_is_active_owner_only(ctx):
    owner_headers, project = _project(ctx)
    pid = project["id"]
    for username, role in (("scheme-admin", "admin"), ("scheme-member", "member")):
        user_id = ctx.create_user(username)
        ctx.add_member(pid, user_id, role)
        headers = auth_headers(ctx.client, username, "pw123")
        assert ctx.client.get(f"/api/projects/{pid}/category-scheme", headers=headers).status_code == 403
        assert ctx.client.put(
            f"/api/projects/{pid}/category-scheme",
            json={"expected_version": 0, "categories": []}, headers=headers,
        ).status_code == 403
        assert ctx.client.post(
            f"/api/projects/{pid}/category-scheme/lock",
            json={"expected_version": 0}, headers=headers,
        ).status_code == 403
        assert ctx.client.get(f"/api/projects/{pid}/category-scheme/audit", headers=headers).status_code == 403
    assert ctx.client.get(f"/api/projects/{pid}/category-scheme", headers=owner_headers).status_code == 200


def test_role_keys_preserved_and_forgery_rejected(ctx):
    headers, project = _project(ctx)
    pid = project["id"]
    first = ctx.client.put(f"/api/projects/{pid}/category-scheme", json=_payload(), headers=headers).json()
    category = first["categories"][0]
    body = _payload(1)
    body["categories"][0]["id"] = category["id"]
    body["categories"][0]["role_definitions"] = category["role_definitions"]
    kept = ctx.client.put(f"/api/projects/{pid}/category-scheme", json=body, headers=headers)
    assert kept.status_code == 200, kept.text
    body["expected_version"] = 2
    body["categories"][0]["role_definitions"][0]["key"] = "role_" + "0" * 32
    assert ctx.client.put(f"/api/projects/{pid}/category-scheme", json=body, headers=headers).status_code == 422


def test_role_keys_are_project_unique_on_put_and_lock_revalidation(ctx):
    headers, project = _project(ctx)
    pid = project["id"]
    body = _payload()
    body["categories"].append({
        "name": "跟随", "group": "社交行为", "sort_order": 1,
        "participant_mode": "role_based", "role_definitions": [
            {"name": "跟随者", "min_count": 1, "max_count": 1, "role_sort_order": 0},
        ],
    })
    response = ctx.client.put(f"/api/projects/{pid}/category-scheme", json=body, headers=headers)
    assert response.status_code == 200, response.text
    keys = [
        role["key"]
        for category in response.json()["categories"]
        for role in category["role_definitions"]
    ]
    assert len(keys) == len(set(keys))

    duplicate_key = keys[0]
    with ctx.session_factory() as db:
        categories = db.query(BehaviorCategory).filter_by(project_id=pid).order_by(
            BehaviorCategory.sort_order
        ).all()
        second_roles = list(categories[1].role_definitions)
        second_roles[0] = {**second_roles[0], "key": duplicate_key}
        db.execute(
            text("UPDATE behavior_categories SET role_definitions=:roles WHERE id=:id"),
            {"roles": __import__("json").dumps(second_roles), "id": categories[1].id},
        )
        db.commit()
    locked = ctx.client.post(
        f"/api/projects/{pid}/category-scheme/lock",
        json={"expected_version": 1}, headers=headers,
    )
    assert locked.status_code == 422
    with ctx.session_factory() as db:
        assert db.get(Project, pid).category_scheme_locked_at is None


def test_database_lock_and_audit_barriers(ctx):
    headers, project = _project(ctx)
    pid = project["id"]
    category_id = ctx.configure_and_lock_minimal_scheme(pid, headers)[0]["id"]
    video_id = ctx.client.post(
        f"/api/projects/{pid}/videos", json={"filename": "barriers.mp4"}, headers=headers
    ).json()["id"]
    other_headers, other_project = _project(ctx)
    other_category_id = ctx.configure_and_lock_minimal_scheme(
        other_project["id"], other_headers
    )[0]["id"]
    with ctx.session_factory() as db:
        project_row = db.get(Project, pid)
        project_row.category_scheme_locked_at = None
        project_row.category_scheme_locked_by = None
        with _integrity_failure(db):
            db.commit()
        project_row = db.get(Project, pid)
        project_row.category_scheme_version += 1
        with _integrity_failure(db):
            db.commit()
        project_row = db.get(Project, pid)
        project_row.category_scheme_locked_at = datetime(2000, 1, 1)
        with _integrity_failure(db):
            db.commit()
        invalid_locker = ctx.create_user("invalid-scheme-locker")
        project_row = db.get(Project, pid)
        project_row.category_scheme_locked_by = invalid_locker
        with _integrity_failure(db):
            db.commit()
        db.add(BehaviorCategory(project_id=pid, name="旁路", group="测试"))
        with _integrity_failure(db):
            db.commit()
        category = db.get(BehaviorCategory, category_id)
        category.name = "旁路改名"
        with _integrity_failure(db):
            db.commit()
        category = db.get(BehaviorCategory, category_id)
        db.delete(category)
        with _integrity_failure(db):
            db.commit()
        audit = db.query(CategorySchemeAudit).first()
        audit.action = "lock"
        with _integrity_failure(db):
            db.commit()
        audit = db.query(CategorySchemeAudit).first()
        db.delete(audit)
        with _integrity_failure(db):
            db.commit()

        # Locking permits ordinary same-project Annotation business writes.
        annotation = Annotation(
            video_id=video_id, annotator_id=db.get(Project, pid).created_by,
            category_id=category_id, start_time=0, end_time=1,
            start_frame=0, end_frame=1,
        )
        db.add(annotation)
        db.commit()
        annotation.end_time = 2
        annotation.end_frame = 2
        db.commit()
        annotation.category_id = other_category_id
        with _integrity_failure(db):
            db.commit()
        annotation = db.get(Annotation, annotation.id)
        db.delete(annotation)
        db.commit()
        assert db.get(Annotation, annotation.id) is None


@pytest.mark.parametrize("corruption", [
    "UPDATE behavior_categories SET name=' 坏名称 ' WHERE project_id=:pid",
    "UPDATE behavior_categories SET \"group\"=' ' WHERE project_id=:pid",
    "UPDATE behavior_categories SET sort_order=1 WHERE project_id=:pid",
    "UPDATE behavior_categories SET role_definitions='[{\"name\":\"坏角色\"}]' WHERE project_id=:pid",
    "UPDATE behavior_categories SET participant_mode='role_based', "
    "role_definitions=json_array(json_object('key','role_11111111111111111111111111111111',"
    "'name','角色','min_count',1,'max_count',1,'role_sort_order',0)), "
    "mouse_count_min=2, mouse_count_max=2 WHERE project_id=:pid",
])
def test_lock_revalidates_raw_sql_corruption(ctx, corruption):
    headers, project = _project(ctx)
    pid = project["id"]
    assert ctx.client.put(
        f"/api/projects/{pid}/category-scheme", json={"expected_version": 0, "categories": [{
            "name": "合法类别", "group": "测试", "sort_order": 0,
        }]}, headers=headers,
    ).status_code == 200
    with ctx.session_factory() as db:
        db.execute(text(corruption), {"pid": pid})
        db.commit()
    response = ctx.client.post(
        f"/api/projects/{pid}/category-scheme/lock",
        json={"expected_version": 1}, headers=headers,
    )
    assert response.status_code == 422
    with ctx.session_factory() as db:
        assert db.get(Project, pid).category_scheme_locked_at is None


def test_write_gate_http_errors_rollback_and_allow_retry(ctx):
    headers, project = _project(ctx)
    pid = project["id"]
    assert ctx.client.put(
        f"/api/projects/{pid}/category-scheme", json=_payload(1), headers=headers
    ).status_code == 409
    assert ctx.client.put(
        f"/api/projects/{pid}/category-scheme", json=_payload(0), headers=headers
    ).status_code == 200
    assert ctx.client.post(
        f"/api/projects/{pid}/category-scheme/lock", json={"expected_version": 0}, headers=headers
    ).status_code == 409
    assert ctx.client.post(
        f"/api/projects/{pid}/category-scheme/lock", json={"expected_version": 1}, headers=headers
    ).status_code == 200


def test_raw_annotation_requires_lock_and_same_project(ctx):
    headers, unlocked = _project(ctx)
    unlocked_pid = unlocked["id"]
    replaced = ctx.client.put(
        f"/api/projects/{unlocked_pid}/category-scheme",
        json={"expected_version": 0, "categories": [{
            "name": "未锁定", "group": "测试", "sort_order": 0,
            "participant_mode": "unordered", "role_definitions": [], "mouse_count_min": 1,
        }]}, headers=headers,
    ).json()
    unlocked_category_id = replaced["categories"][0]["id"]
    unlocked_video = ctx.client.post(
        f"/api/projects/{unlocked_pid}/videos", json={"filename": "unlocked.mp4"}, headers=headers
    ).json()
    with ctx.session_factory() as db:
        with _integrity_failure(db):
            db.execute(text(
                "INSERT INTO annotations(video_id,annotator_id,category_id,start_time,end_time,start_frame,"
                "end_frame,confidence,review_status,mouse_ids,mouse_id_status,detection_import_revision,"
                "identity_revision,participant_roles,participant_status,created_at,updated_at) VALUES "
                "(:video,:actor,:category,0,1,0,1,'certain','pending','[]','needs_mouse_ids',0,0,'{}','valid',"
                "CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"
            ), {"video": unlocked_video["id"], "actor": unlocked["category_scheme_locked_by"] or 1,
                "category": unlocked_category_id})
            db.commit()

    first_headers, first = _project(ctx)
    first_category = ctx.configure_and_lock_minimal_scheme(first["id"], first_headers)[0]
    first_video = ctx.client.post(
        f"/api/projects/{first['id']}/videos", json={"filename": "first.mp4"}, headers=first_headers
    ).json()
    second_headers, second = _project(ctx)
    second_category = ctx.configure_and_lock_minimal_scheme(second["id"], second_headers)[0]
    with ctx.session_factory() as db:
        actor = db.get(Project, first["id"]).created_by
        with _integrity_failure(db):
            db.execute(text(
                "INSERT INTO annotations(video_id,annotator_id,category_id,start_time,end_time,start_frame,"
                "end_frame,confidence,review_status,mouse_ids,mouse_id_status,detection_import_revision,"
                "identity_revision,participant_roles,participant_status,created_at,updated_at) VALUES "
                "(:video,:actor,:category,0,1,0,1,'certain','pending','[]','needs_mouse_ids',0,0,'{}','valid',"
                "CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"
            ), {"video": first_video["id"], "actor": actor, "category": second_category["id"]})
            db.commit()


class _integrity_failure:
    def __init__(self, db):
        self.db = db

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, _tb):
        assert exc_type is not None and issubclass(exc_type, IntegrityError)
        self.db.rollback()
        return True


def _race(left, right):
    barrier = Barrier(2)
    def run(call):
        barrier.wait()
        return call()
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(run, call) for call in (left, right)]
        return [future.result(timeout=60) for future in futures]


def _synchronize_gate(monkeypatch):
    barrier = Barrier(2)
    monkeypatch.setattr(gate_module, "_before_project_lock", lambda: barrier.wait(timeout=30))


def test_put_vs_put_real_connection_race(ctx, monkeypatch):
    headers, project = _project(ctx)
    pid = project["id"]
    _synchronize_gate(monkeypatch)
    with TestClient(ctx.client.app) as left, TestClient(ctx.client.app) as right:
        responses = _race(
            lambda: left.put(f"/api/projects/{pid}/category-scheme", json=_payload(), headers=headers),
            lambda: right.put(f"/api/projects/{pid}/category-scheme", json=_payload(), headers=headers),
        )
    assert sorted(response.status_code for response in responses) == [200, 409]


def test_put_vs_lock_real_connection_race(ctx, monkeypatch):
    headers, project = _project(ctx)
    pid = project["id"]
    assert ctx.client.put(f"/api/projects/{pid}/category-scheme", json=_payload(), headers=headers).status_code == 200
    update = _payload(1)
    update["categories"][0]["name"] = "追逐更新"
    current = ctx.client.get(f"/api/projects/{pid}/category-scheme", headers=headers).json()["categories"][0]
    update["categories"][0]["id"] = current["id"]
    update["categories"][0]["role_definitions"] = current["role_definitions"]
    _synchronize_gate(monkeypatch)
    with TestClient(ctx.client.app) as left, TestClient(ctx.client.app) as right:
        responses = _race(
            lambda: left.put(f"/api/projects/{pid}/category-scheme", json=update, headers=headers),
            lambda: right.post(
                f"/api/projects/{pid}/category-scheme/lock", json={"expected_version": 1}, headers=headers
            ),
        )
    assert sorted(response.status_code for response in responses) == [200, 409]


def test_lock_vs_lock_real_connection_race_is_idempotent(ctx, monkeypatch):
    headers, project = _project(ctx)
    pid = project["id"]
    assert ctx.client.put(f"/api/projects/{pid}/category-scheme", json=_payload(), headers=headers).status_code == 200
    _synchronize_gate(monkeypatch)
    with TestClient(ctx.client.app) as left, TestClient(ctx.client.app) as right:
        responses = _race(
            lambda: left.post(f"/api/projects/{pid}/category-scheme/lock", json={"expected_version": 1}, headers=headers),
            lambda: right.post(f"/api/projects/{pid}/category-scheme/lock", json={"expected_version": 1}, headers=headers),
        )
    assert [response.status_code for response in responses] == [200, 200]
    assert len({response.json()["category_scheme_locked_at"] for response in responses}) == 1
