"""Application exception hierarchy.

Every failure the application raises deliberately derives from `AppError`, so
the API layer can translate it into a predictable response while anything else
surfaces as a genuine 500.
"""

from __future__ import annotations

from typing import Any


class AppError(Exception):
    """Base class for all expected application failures."""

    status_code: int = 500
    error_code: str = "internal_error"
    message: str = "An unexpected error occurred."

    def __init__(self, message: str | None = None, **details: Any) -> None:
        self.message = message or self.message
        self.details = details
        super().__init__(self.message)

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"error": self.error_code, "message": self.message}
        if self.details:
            payload["details"] = self.details
        return payload


class ConfigurationError(AppError):
    error_code = "configuration_error"
    message = "The service is not configured correctly."


class NotFoundError(AppError):
    status_code = 404
    error_code = "not_found"
    message = "Resource not found."


class ValidationError(AppError):
    status_code = 422
    error_code = "validation_error"
    message = "The supplied data is invalid."


# --- Integration failures --------------------------------------------------


class IntegrationError(AppError):
    status_code = 502
    error_code = "integration_error"
    message = "An upstream service failed."


class WhatsAppError(IntegrationError):
    error_code = "whatsapp_error"
    message = "Could not reach the WhatsApp Cloud API."


class SignatureVerificationError(AppError):
    status_code = 403
    error_code = "invalid_signature"
    message = "Webhook signature verification failed."


class LLMError(IntegrationError):
    error_code = "llm_error"
    message = "The language model could not answer right now."


class CRMError(IntegrationError):
    error_code = "crm_error"
    message = "Could not sync the lead to the CRM."


class KnowledgeBaseError(AppError):
    error_code = "knowledge_base_error"
    message = "The knowledge base could not be loaded."
