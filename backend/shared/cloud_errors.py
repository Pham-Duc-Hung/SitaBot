"""Typed errors at the authenticated Sprint 3 service boundary."""
class BackendError(Exception):
    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code, self.status = code, status


def failure(exc):
    return {"status": "ERROR", "error_code": exc.code, "message": str(exc)}
