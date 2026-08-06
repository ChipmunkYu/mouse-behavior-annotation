"""应用配置：全部可被环境变量 / backend/.env 覆盖。"""
from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent  # backend/


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    env: str = "development"
    # 数据目录（数据库、上传视频、导出片段默认都在其下）
    data_dir: Path = BASE_DIR / "data"
    database_url: str | None = None
    # 仅开发用途；生产环境必须通过环境变量覆盖
    secret_key: str = "dev-only-insecure-secret-change-me"
    access_token_expire_minutes: int = 60 * 24 * 7
    # demo 账号（仅开发）
    demo_username: str = "demo"
    demo_password: str = "demo123"
    # 允许的前端来源，逗号分隔
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"
    # 视频上传：磁盘安全保留空间（字节）。写入前/每块写入前检查 videos_dir 可用空间，
    # 不足（可用空间将低于该保留值）时返回 507。用于避免大视频上传耗尽数据盘。
    upload_disk_reserve_bytes: int = 1024**3
    # 视频上传：分块流式写入的块大小（字节），应用层不设文件大小上限。
    upload_chunk_size: int = 1024 * 1024

    @property
    def videos_dir(self) -> Path:
        return self.data_dir / "videos"

    @property
    def exports_dir(self) -> Path:
        return self.data_dir / "exports"

    @property
    def clips_dir(self) -> Path:
        return self.data_dir / "clips"

    @property
    def thumbnails_dir(self) -> Path:
        return self.data_dir / "thumbnails"

    @property
    def cleanup_log(self) -> Path:
        """清理异常 JSONL 日志：实体文件删除失败 / 越界路径等可观测记录。"""
        return self.data_dir / "cleanup-issues.log"

    @property
    def resolved_database_url(self) -> str:
        if self.database_url:
            return self.database_url
        return f"sqlite:///{(self.data_dir / 'annotation.db').as_posix()}"

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


def get_settings() -> Settings:
    return Settings()
