"""应用配置：全部可被环境变量 / backend/.env 覆盖。"""
from __future__ import annotations

import base64
import binascii
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent  # backend/
DEV_MEDIA_MASTER_SECRET = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"


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
    # 原生视频流媒体票据。安全发布默认关闭，legacy Bearer 默认保留用于回滚。
    media_ticket_enabled: bool = False
    media_legacy_bearer_enabled: bool = True
    media_ticket_ttl_seconds: int = Field(default=7200, ge=1, le=7200)
    # 无 padding 的 canonical base64url；生产值必须解码为至少 32 bytes。
    media_master_secret: str = DEV_MEDIA_MASTER_SECRET
    # 跨层安全标识不是部署配置：Literal 令环境变量也只能提供这些精确值。
    media_ticket_cookie_name: Literal["mouse_media_ticket"] = "mouse_media_ticket"
    media_binding_cookie_name: Literal["mouse_media_binding"] = "mouse_media_binding"
    media_binding_cookie_path: Literal["/api/videos/"] = "/api/videos/"
    media_ticket_audience: Literal["video-stream"] = "video-stream"
    media_binding_audience: Literal["video-stream-binding"] = "video-stream-binding"
    media_ticket_type: Literal["media-ticket"] = "media-ticket"
    media_binding_type: Literal["media-binding"] = "media-binding"
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

    # ---- 播放代理（候选 profile；默认关闭，不改变现有生产行为） ----
    display_proxies_enabled: bool = False
    display_proxy_timeout_seconds: int = Field(default=60 * 60, ge=1)
    display_proxy_max_attempts: int = Field(default=3, ge=1)
    display_proxy_synchronous: bool = False
    # 转码临时文件与最终文件共享此保留线；候选体积估算固定在 worker 代码中。
    display_proxy_disk_reserve_bytes: int = Field(default=1024**3, ge=0)

    # ---- 分类导出（批次 6） ----
    # 导出 ZIP 生成后的保留天数：超过后 `export/download` 拒绝下载（批次 7 清理实体文件）。
    export_retention_days: int = Field(default=7, ge=0)

    # ---- 检测导入限制（v0.6） ----
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
        media_secret = self.media_master_secret.strip()
        if media_secret == DEV_MEDIA_MASTER_SECRET or is_placeholder(media_secret):
            errors.append("MEDIA_MASTER_SECRET must not use a default or placeholder")
        try:
            padding = "=" * (-len(media_secret) % 4)
            decoded_media_secret = base64.b64decode(
                media_secret + padding, altchars=b"-_", validate=True
            )
            canonical = base64.urlsafe_b64encode(decoded_media_secret).rstrip(b"=").decode("ascii")
            if canonical != media_secret:
                raise ValueError("not canonical")
            if len(decoded_media_secret) < 32:
                errors.append("MEDIA_MASTER_SECRET must decode to at least 32 bytes")
        except (ValueError, UnicodeEncodeError, binascii.Error):
            errors.append("MEDIA_MASTER_SECRET must be canonical unpadded base64url")
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
    def display_proxies_dir(self) -> Path:
        return self.data_dir / "display_proxies"

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
