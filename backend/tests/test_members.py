"""成员、权限与邀请码 API。"""
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import ProjectMembership, Video


def _project(ctx, login_headers):
    headers = login_headers()
    project = ctx.client.post("/api/projects", json={"name": "members"}, headers=headers).json()
    return headers, project


def test_invite_join_is_idempotent_and_reset_invalidates_old(ctx, login_headers):
    owner_headers, project = _project(ctx, login_headers)
    invite = ctx.client.get(f"/api/projects/{project['id']}/invite", headers=owner_headers).json()["invite_code"]
    ctx.create_user("alice")
    alice_headers = login_headers("alice", "pw123")
    first = ctx.client.post("/api/projects/join", json={"invite_code": invite}, headers=alice_headers)
    second = ctx.client.post("/api/projects/join", json={"invite_code": invite}, headers=alice_headers)
    assert first.status_code == second.status_code == 200
    assert first.json()["id"] == second.json()["id"]
    assert first.json()["role"] == "member" and first.json()["can_review"] is False
    reset = ctx.client.post(f"/api/projects/{project['id']}/invite/reset", headers=owner_headers).json()
    assert reset["invite_code"] != invite
    ctx.create_user("bob")
    assert ctx.client.post("/api/projects/join", json={"invite_code": invite},
                           headers=login_headers("bob", "pw123")).status_code == 404


def test_invite_reset_retries_unique_collision(monkeypatch, ctx, login_headers):
    owner_headers, project = _project(ctx, login_headers)
    other = ctx.client.post("/api/projects", json={"name": "other"}, headers=owner_headers).json()
    collision = ctx.client.get(f"/api/projects/{other['id']}/invite", headers=owner_headers).json()["invite_code"]
    values = iter((collision, "unique-reset-code-1234567890"))
    monkeypatch.setattr("app.routers.projects.secrets.token_urlsafe", lambda _size: next(values))
    response = ctx.client.post(f"/api/projects/{project['id']}/invite/reset", headers=owner_headers)
    assert response.status_code == 200
    assert response.json()["invite_code"] == "unique-reset-code-1234567890"


def test_join_recovers_expected_membership_integrity_race(monkeypatch, ctx, login_headers):
    owner_headers, project = _project(ctx, login_headers)
    invite = ctx.client.get(f"/api/projects/{project['id']}/invite", headers=owner_headers).json()["invite_code"]
    user_id = ctx.create_user("race_join")
    headers = login_headers("race_join", "pw123")
    original_commit = Session.commit
    injected = False

    def competing_commit(session):
        nonlocal injected
        pending = next((obj for obj in session.new if isinstance(obj, ProjectMembership)), None)
        if pending is not None and pending.user_id == user_id and not injected:
            injected = True
            with ctx.session_factory() as rival:
                rival.add(ProjectMembership(project_id=project["id"], user_id=user_id, role="member"))
                original_commit(rival)
        return original_commit(session)

    monkeypatch.setattr(Session, "commit", competing_commit)
    response = ctx.client.post("/api/projects/join", json={"invite_code": invite}, headers=headers)
    assert response.status_code == 200
    with ctx.session_factory() as db:
        assert db.query(ProjectMembership).filter_by(project_id=project["id"], user_id=user_id).count() == 1


def test_member_update_owner_protection_and_assignee_blocks_removal(ctx, login_headers):
    headers, project = _project(ctx, login_headers)
    alice_id = ctx.create_user("alice")
    ctx.add_member(project["id"], alice_id)
    members = ctx.client.get(f"/api/projects/{project['id']}/members", headers=headers).json()
    owner = next(m for m in members if m["role"] == "owner")
    alice = next(m for m in members if m["user_id"] == alice_id)
    changed = ctx.client.patch(
        f"/api/projects/{project['id']}/members/{alice['id']}",
        json={"can_review": True}, headers=headers,
    )
    assert changed.status_code == 200 and changed.json()["can_review"] is True
    assert ctx.client.patch(
        f"/api/projects/{project['id']}/members/{owner['id']}",
        json={"role": "member"}, headers=headers,
    ).status_code == 409
    video = ctx.client.post(
        f"/api/projects/{project['id']}/videos",
        json={"filename": "assigned.mp4", "assignee_membership_id": alice["id"]}, headers=headers,
    ).json()
    assert video["assignee"]["username"] == "alice"
    assert ctx.client.delete(
        f"/api/projects/{project['id']}/members/{alice['id']}", headers=headers
    ).status_code == 409


def test_remove_member_fk_race_returns_stable_conflict(monkeypatch, ctx, login_headers):
    headers, project = _project(ctx, login_headers)
    user_id = ctx.create_user("remove_race")
    ctx.add_member(project["id"], user_id)
    with ctx.session_factory() as db:
        membership_id = db.query(ProjectMembership.id).filter_by(user_id=user_id).scalar()
    video = ctx.client.post(f"/api/projects/{project['id']}/videos", json={"filename": "race.mp4"}, headers=headers).json()
    original_commit = Session.commit
    injected = False

    def assign_before_delete(session):
        nonlocal injected
        if not injected and any(isinstance(obj, ProjectMembership) and obj.id == membership_id for obj in session.deleted):
            injected = True
            with ctx.session_factory() as rival:
                rival.get(Video, video["id"]).assignee_membership_id = membership_id
                original_commit(rival)
        return original_commit(session)

    monkeypatch.setattr(Session, "commit", assign_before_delete)
    response = ctx.client.delete(f"/api/projects/{project['id']}/members/{membership_id}", headers=headers)
    assert response.status_code == 409
    assert response.json()["detail"] == "Member is still assigned to videos"


def test_member_management_boundary_and_minimal_assignee_directory(ctx, login_headers):
    owner_headers, project = _project(ctx, login_headers)
    member_id = ctx.create_user("directory_member")
    ctx.add_member(project["id"], member_id)
    member_headers = login_headers("directory_member", "pw123")
    assert ctx.client.get(f"/api/projects/{project['id']}/members", headers=member_headers).status_code == 403
    directory = ctx.client.get(f"/api/projects/{project['id']}/assignees", headers=member_headers)
    assert directory.status_code == 200
    assert all(set(item) == {"membership_id", "username"} for item in directory.json())


def test_nested_assignee_uses_effective_manager_review_permission(ctx, login_headers):
    headers, project = _project(ctx, login_headers)
    response = ctx.client.post(
        f"/api/projects/{project['id']}/videos",
        json={"filename": "owner.mp4", "assignee_membership_id": project["membership_id"]},
        headers=headers,
    )
    assert response.status_code == 201
    assert response.json()["assignee"]["can_review"] is True


def test_owner_review_member_and_plain_member_permissions(ctx, login_headers):
    headers, project = _project(ctx, login_headers)
    reviewer_id = ctx.create_user("review_member"); member_id = ctx.create_user("plain_member")
    ctx.add_member(project["id"], reviewer_id); ctx.add_member(project["id"], member_id)
    members = ctx.client.get(f"/api/projects/{project['id']}/members", headers=headers).json()
    reviewer = next(m for m in members if m["user_id"] == reviewer_id)
    ctx.client.patch(f"/api/projects/{project['id']}/members/{reviewer['id']}",
                     json={"can_review": True}, headers=headers)
    queue = f"/api/projects/{project['id']}/reviews/queue"
    assert ctx.client.get(queue, headers=headers).status_code == 200
    assert ctx.client.get(queue, headers=login_headers("review_member", "pw123")).status_code == 200
    assert ctx.client.get(queue, headers=login_headers("plain_member", "pw123")).status_code == 403
