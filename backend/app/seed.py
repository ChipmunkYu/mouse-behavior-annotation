"""种子数据：北医 12 类初始化 + demo 开发账号。"""
from __future__ import annotations

from sqlalchemy.orm import Session

from .auth import hash_password
from .config import Settings
from .models import BehaviorCategory, User

# 北医 12 类（需求文档 §2.4）：方向性社交行为退化为无向标签。
# 仅供受控 demo seed 建立演示方案；普通项目创建必须使用请求携带的完整方案。
INITIAL_CATEGORIES: list[tuple[str, list[str]]] = [
    ("个体行为", ["奔跑", "行走", "静止"]),
    ("社交行为", ["一起", "接近", "追逐", "回避", "攻击行为", "鼻头接触", "鼻尾接触"]),
    ("群体行为", ["扎堆行为", "孤立行为"]),
]

CATEGORY_COLORS: list[str] = [
    "#E6194B",
    "#3CB44B",
    "#FFE119",
    "#4363D8",
    "#F58231",
    "#911EB4",
    "#46F0F0",
    "#F032E6",
    "#BCF60C",
    "#FABEBE",
    "#008080",
    "#E6BEFF",
]

# 参与小鼠数量范围（需求文档 §2.4）：None 表示无固定上限
CATEGORY_MOUSE_COUNTS: dict[str, tuple[int, int | None]] = {
    "奔跑": (1, 1),
    "行走": (1, 1),
    "静止": (1, 1),
    "一起": (2, 2),
    "接近": (2, 2),
    "追逐": (2, 2),
    "回避": (2, 2),
    "攻击行为": (2, 2),
    "鼻头接触": (2, 2),
    "鼻尾接触": (2, 2),
    "扎堆行为": (2, None),
    "孤立行为": (1, 1),
}


def init_project_categories(db: Session, project_id: int) -> list[BehaviorCategory]:
    """为项目初始化 12 个行为类别（未 commit，由调用方统一提交）。"""
    categories: list[BehaviorCategory] = []
    for order, (group, names) in enumerate(INITIAL_CATEGORIES):
        for name in names:
            mouse_count_min, mouse_count_max = CATEGORY_MOUSE_COUNTS[name]
            cat = BehaviorCategory(
                project_id=project_id,
                name=name,
                group=group,
                color=CATEGORY_COLORS[len(categories) % len(CATEGORY_COLORS)],
                sort_order=len(categories),
                is_active=True,
                mouse_count_min=mouse_count_min,
                mouse_count_max=mouse_count_max,
            )
            db.add(cat)
            categories.append(cat)
    return categories


def ensure_demo_user(db: Session, settings: Settings) -> User:
    """确保 demo 账号存在（仅开发用途，密码可被环境变量覆盖）。"""
    user = db.query(User).filter(User.username == settings.demo_username).first()
    if user is None:
        user = User(
            username=settings.demo_username,
            password_hash=hash_password(settings.demo_password),
        )
        db.add(user)
        db.commit()
        db.refresh(user)
    return user
