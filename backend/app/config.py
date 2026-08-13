"""应用配置：全部可被环境变量 / backend/.env 覆盖。"""
from __future__ import annotations

from pathlib import Path

from pydantic import Field, model_validator
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

    # ---- 媒体处理（批次 4）：精确片段重编码与缩略图 ----
    # ffmpeg/ffprobe 可执行文件：默认取 PATH 上的命令名；本机无 ffmpeg 的环境
    # （或测试）通过 FFMPEG_PATH / FFPROBE_PATH 注入可替换执行器。
    ffmpeg_path: str = "ffmpeg"
    ffprobe_path: str = "ffprobe"
    # 重编码质量与速度（libx264）：CRF 越小质量越高、体积越大。
    media_crf: int = 23
    media_preset: str = "veryfast"
    # 单条媒体命令超时（秒），超时视为失败并清理半成品。
    media_timeout_seconds: int = 600
    # 片段是否映射音频（默认不映射；开启则 -map 0:a:0? 可选音频 + aac 编码）。
    media_map_audio: bool = False
    # 任务重试上限：重启恢复时 running 任务被重排/判失败 attempts 的阈值。
    media_max_attempts: int = 3
    # 测试用：true 时媒体 worker 在请求线程内同步执行（配合可替换执行器，
    # 不要求系统 ffmpeg，测试可确定性驱动任务流程）。
    media_synchronous: bool = False

    # ---- 分类导出（批次 6） ----
    # 导出 ZIP 生成后的保留天数：超过后 `export/download` 拒绝下载（批次 7 清理实体文件）。
    export_retention_days: int = Field(default=7, ge=0)

    # ---- 检测导入限制（v0.6） ----
    detection_import_max_file_bytes: int = 200 * 1024 * 1024  # 200 MB
    detection_import_max_frames: int = 100000
    detection_import_max_detections_per_frame: int = 100
    detection_import_max_errors: int = 100

    # ---- 生命周期清理（批次 7） ----
    cleanup_enabled: bool = True
    cleanup_interval_seconds: int = Field(default=60 * 60, ge=1)
    temp_retention_hours: int = Field(default=24, ge=0)
    job_retention_days: int = Field(default=30, ge=0)

    @model_validator(mode="after")
    def validate_production_credentials(self) -> "Settings":
        """生产环境拒绝开发默认值、模板占位值和强度不足的凭据。"""
        if self.env.strip().lower() != "production":
            return self

        secret = self.secret_key.strip()
        password = self.demo_password.strip()
        username = self.demo_username.strip()

        def is_placeholder(value: str) -> bool:
            normalized = value.upper().replace("-", "_")
            compact = "".join(character for character in normalized if character.isalnum())
            return "CHANGEME" in compact or "PLACEHOLDER" in compact

        errors: list[str] = []
        if secret == "dev-only-insecure-secret-change-me":
            errors.append("SECRET_KEY must not use the development default")
        if is_placeholder(secret):
            errors.append("SECRET_KEY must not contain a template placeholder")
        if len(secret) < 32:
            errors.append("SECRET_KEY must be at least 32 characters")
        if username.lower() == "demo":
            errors.append("DEMO_USERNAME must not be demo")
        if is_placeholder(username):
            errors.append("DEMO_USERNAME must not contain a template placeholder")
        if password.lower() == "demo123":
            errors.append("DEMO_PASSWORD must not be demo123")
        if is_placeholder(password):
            errors.append("DEMO_PASSWORD must not contain a template placeholder")
        if len(password) < 12:
            errors.append("DEMO_PASSWORD must be at least 12 characters")
        if errors:
            raise ValueError("invalid production credentials: " + "; ".join(errors))
        return self

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
    def import_batches_dir(self) -> Path:
        """三文件（video/tracks/metadata）上传批次暂存目录（v0.6 检测导入）。"""
        return self.data_dir / "import_batches"

    @property
    def detection_imports_dir(self) -> Path:
        """检测导入（tracks.jsonl / metadata.json）持久化目录（v0.6 检测导入）。"""
        return self.data_dir / "detection_imports"

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
