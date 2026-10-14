from fastapi.responses import JSONResponse


def operational_error(
    status_code: int, detail: str, code: str, context: dict[str, object]
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"detail": detail, "code": code, "context": context},
    )
