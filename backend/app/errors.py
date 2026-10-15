from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

STATUS = {
    "UNAUTHENTICATED": 401, "FORBIDDEN": 403, "NOT_FOUND": 404, "VALIDATION": 422,
    "CONFLICT": 409, "RATE_LIMITED": 429, "UPSTREAM_ERROR": 502, "TIMEOUT": 504,
    "PASSWORD_CHANGE_REQUIRED": 403,
}


class AppError(Exception):
    def __init__(self, code: str, message: str = "", details: dict | None = None):
        self.code, self.message, self.details = code, message or code, details or {}
        self.status = STATUS.get(code, 400)


def _body(code: str, message: str, details: dict | None = None) -> dict:
    return {"error": {"code": code, "message": message, "details": details or {}}}


def install_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(_: Request, e: AppError):
        return JSONResponse(_body(e.code, e.message, e.details), status_code=e.status)

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, e: RequestValidationError):
        errs = [{"loc": [str(p) for p in x["loc"]], "msg": x["msg"]} for x in e.errors()]
        return JSONResponse(_body("VALIDATION", "Invalid request", {"errors": errs}), status_code=422)

    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, e: StarletteHTTPException):
        code = {401: "UNAUTHENTICATED", 403: "FORBIDDEN", 404: "NOT_FOUND"}.get(e.status_code, "ERROR")
        return JSONResponse(_body(code, str(e.detail)), status_code=e.status_code)
