"""Pure FastAPI surface assembly shared by runtime and schema export."""
from fastapi import FastAPI

from .routers import (annotations, auth, categories, category_scheme, clips,
                      detection_imports, exports, health, identity_edits, media,
                      projects, reviews, suppressions, videos)

ROUTERS = (
    health.router, auth.router, projects.router, category_scheme.router,
    categories.router, videos.router, annotations.router, reviews.router,
    clips.router, exports.router, media.router, detection_imports.router,
    identity_edits.router, suppressions.router,
)


def register_routers(app: FastAPI) -> FastAPI:
    for router in ROUTERS:
        app.include_router(router)
    return app


def create_schema_app() -> FastAPI:
    """Build OpenAPI without settings, filesystem, database, seed, or workers."""
    return register_routers(FastAPI(title="Behavior Annotation Backend", version="0.1.0"))
