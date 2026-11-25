from __future__ import annotations

from typing import Any


class BridgeError(Exception):
    """An expected, user-visible bridge error.

    Messages are deliberately short and must never contain credential material.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details:
            result["details"] = self.details
        return result


class RunNotCancellable(BridgeError):
    def __init__(self, run_id: str, status: str) -> None:
        super().__init__(
            "run_not_cancellable",
            f"Run {run_id} is already {status} and cannot be cancelled.",
            details={"run_id": run_id, "status": status},
        )
