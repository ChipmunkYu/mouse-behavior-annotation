"""集成流程：以真实 HTTP 链路复现前端主流程。

demo 登录 → 项目列表 → 创建项目（12 类别）→ 视频列表/创建 → 标注
列表/创建/PATCH/DELETE → 导出。同时核对前端依赖的契约：
- POST 创建返回 201、PATCH 返回 200、DELETE 返回 204（空响应体）
- 未认证访问一律 401
- 类别实际返回 12 条（PowerShell smoke 曾因数组包装误报 Count=1，此处用 TestClient 可靠核对）
- 视频流无 storage_path 时 404（前端据此显示"无视频文件"）
"""
from __future__ import annotations

EXPECTED_FIELDS = {
    "video_id",
    "start_time",
    "end_time",
    "start_frame",
    "end_frame",
    "behavior",
    "crop_region",
    "confidence",
    "annotator",
    "reviewer",
    "review_status",
}


def test_main_flow_end_to_end(ctx, login_headers):
    client = ctx.client
    headers = login_headers()

    # 1. 健康检查
    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}

    # 2. 未认证访问 → 401（前端据此清除登录态）
    assert client.get("/api/projects").status_code == 401
    assert client.get("/api/projects/1/categories").status_code == 401

    # 3. 登录后项目列表为空
    resp = client.get("/api/projects", headers=headers)
    assert resp.status_code == 200
    assert resp.json() == []

    # 4. 创建项目 → 201，创建者为 owner
    resp = client.post(
        "/api/projects",
        json={"name": "集成流程项目", "description": "主流程验证"},
        headers=headers,
    )
    assert resp.status_code == 201
    project = resp.json()
    assert project["role"] == "owner"
    assert project["status"] == "active"
    pid = project["id"]

    # 5. 12 个行为类别（可靠核对，非 PS 包装误报）
    resp = client.get(f"/api/projects/{pid}/categories", headers=headers)
    assert resp.status_code == 200
    categories = resp.json()
    assert len(categories) == 12
    assert [c["sort_order"] for c in categories] == list(range(12))
    assert all(c["is_active"] for c in categories)
    assert all(c["color"] for c in categories)

    # 6. 视频列表为空 → 创建 Mock 元数据 → 201
    resp = client.get(f"/api/projects/{pid}/videos", headers=headers)
    assert resp.status_code == 200
    assert resp.json() == []
    resp = client.post(
        f"/api/projects/{pid}/videos",
        json={"filename": "integ.mp4", "duration": 30.0, "fps": 25.0, "status": "metadata"},
        headers=headers,
    )
    assert resp.status_code == 201
    video = resp.json()
    assert video["duration"] == 30.0
    vid = video["id"]

    # 7. 无 storage_path → stream 404（前端显示"无视频文件"）
    assert client.get(f"/api/videos/{vid}/stream", headers=headers).status_code == 404

    # 8. 创建标注 → 201
    base = f"/api/projects/{pid}/videos/{vid}/annotations"
    cat = categories[0]
    resp = client.post(
        base,
        json={
            "category_id": cat["id"],
            "start_time": 1.0,
            "end_time": 3.0,
            "start_frame": 25,
            "end_frame": 75,
            "confidence": "certain",
            "review_status": "pending",
        },
        headers=headers,
    )
    assert resp.status_code == 201
    ann = resp.json()
    assert ann["annotator"] == "demo"
    assert ann["category_name"] == cat["name"]
    assert ann["review_status"] == "pending"
    ann_id = ann["id"]

    # 9. 列表含该标注
    items = client.get(base, headers=headers).json()
    assert len(items) == 1
    assert items[0]["id"] == ann_id

    # 10. PATCH → 200，end_time/end_frame 更新
    resp = client.patch(
        f"{base}/{ann_id}",
        json={"end_time": 5.0, "end_frame": 125},
        headers=headers,
    )
    assert resp.status_code == 200
    assert resp.json()["end_time"] == 5.0
    assert resp.json()["end_frame"] == 125

    # 11. 导出 → 统一事件格式
    resp = client.get(f"{base}/export", headers=headers)
    assert resp.status_code == 200
    events = resp.json()
    assert len(events) == 1
    assert set(events[0].keys()) == EXPECTED_FIELDS
    assert events[0]["behavior"] == cat["name"]
    assert events[0]["video_id"] == f"video_{vid}"

    # 12. DELETE → 204 空响应体（前端 handleResponse 按 204 处理）
    resp = client.delete(f"{base}/{ann_id}", headers=headers)
    assert resp.status_code == 204
    assert resp.content == b""

    # 13. 删除后列表为空
    assert client.get(base, headers=headers).json() == []
