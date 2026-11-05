"""API errors: every failure is returned as JSON {"error": <message>, "code": <machine-readable code>}."""

import logging

from flask import jsonify
from werkzeug.exceptions import HTTPException

log = logging.getLogger(__name__)


class ApiError(Exception):
    def __init__(self, status, code, message, **extra):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.extra = extra


def bad_request(code, message, **extra):
    return ApiError(400, code, message, **extra)


def unauthorized(code="unauthorized", message="Authentication required"):
    return ApiError(401, code, message)


def not_found(message="Not found"):
    return ApiError(404, "not_found", message)


def conflict(code, message):
    return ApiError(409, code, message)


_HTTP_CODES = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    413: "payload_too_large",
    415: "unsupported_media_type",
    429: "rate_limited",
}


def register_error_handlers(app):
    @app.errorhandler(ApiError)
    def handle_api_error(error):
        response = jsonify(error=error.message, code=error.code, **error.extra)
        if "retry_after" in error.extra:
            response.headers["Retry-After"] = str(error.extra["retry_after"])
        return response, error.status

    @app.errorhandler(HTTPException)
    def handle_http_error(error):
        response = error.get_response()  # keeps headers such as Allow / Retry-After
        response.data = jsonify(
            error=error.description if error.code == 429 else error.name,
            code=_HTTP_CODES.get(error.code, "http_error"),
        ).get_data()
        response.content_type = "application/json"
        return response

    @app.errorhandler(Exception)
    def handle_unexpected_error(error):
        # Details go to the server log only; clients never see stack traces or SQL.
        log.exception("Unhandled error")
        return jsonify(error="Internal server error", code="internal_error"), 500
