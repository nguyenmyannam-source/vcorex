"""
Custom Exceptions for VCOREX Institutional Trading Bot.
Standardizing error handling across all modules.
"""

from typing import Any, Dict, Optional


class VCOREXError(Exception):
    """Base exception for all VCOREX errors."""

    def __init__(self, message: str, context: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.context = context or {}


# --- Exchange Exceptions ---
class ExchangeError(VCOREXError):
    """Base exception for exchange-related errors."""

    pass


class OKXAPIError(ExchangeError):
    """Errors returned by OKX API."""

    pass


class CircuitBrokenError(ExchangeError):
    """Raised when the Circuit Breaker is OPEN and blocks new REST requests to protect capital."""

    pass


class AuthenticationError(ExchangeError):
    """Errors related to API Key/Secret/Passphrase."""

    pass


class InsufficientBalanceError(ExchangeError):
    """Errors when balance is not enough to open position."""

    pass


class RateLimitError(ExchangeError):
    """API Rate limit exceeded."""

    pass


# --- Position Exceptions ---
class PositionError(VCOREXError):
    """Base exception for position-related errors."""

    pass


class PositionNotFoundError(PositionError):
    """When a position ID is not found in internal tracking."""

    pass


class GhostPositionError(PositionError):
    """When an exchange position has no corresponding internal entry."""

    pass


# --- Risk Exceptions ---
class RiskError(VCOREXError):
    """Base exception for risk management errors."""

    pass


class RiskLimitExceededError(RiskError):
    """When a trade violates risk parameters."""

    pass


# --- Infrastructure Exceptions ---
class DatabaseError(VCOREXError):
    """Errors related to database operations."""

    pass


class ConfigurationError(VCOREXError):
    """Errors in settings or environment variables."""

    pass


# --- Dependency Injection Exceptions ---
class DIError(VCOREXError):
    """Base exception for dependency injection failures."""

    pass


class DependencyResolutionError(DIError):
    """Failed to resolve a registered dependency."""

    pass


class DependencyCreationError(DIError):
    """Failed to instantiate a dependency."""

    pass


# --- Telegram Exceptions ---
class TelegramError(VCOREXError):
    """Base exception for Telegram-related errors."""

    pass


class TelegramSendError(TelegramError):
    """Failed to send message to Telegram."""

    pass


class TelegramRenderError(TelegramError):
    """Failed to render Telegram message."""

    pass


class TelegramFloodControlError(TelegramError):
    """Telegram flood control (RetryAfter) triggered."""

    pass


# --- Order/Trade Exceptions ---
class OrderError(VCOREXError):
    """Base exception for order-related errors."""

    pass


class OrderPlacementError(OrderError):
    """Failed to place order on exchange."""

    pass


class OrderCancellationError(OrderError):
    """Failed to cancel order."""

    pass


# --- Reconciliation Exceptions ---
class ReconciliationError(VCOREXError):
    """Errors during position reconciliation."""

    pass


class DataMismatchError(ReconciliationError):
    """Mismatch between local and exchange data."""

    pass


# --- Persistence Exceptions ---
class PersistenceError(VCOREXError):
    """Errors during data persistence."""

    pass


class TransactionError(PersistenceError):
    """Database transaction failed."""

    pass


# --- Strategy Exceptions ---
class StrategyError(VCOREXError):
    """Base exception for strategy-related errors."""

    pass


class SignalError(StrategyError):
    """Error processing trading signal."""

    pass


class StrategyExecutionError(StrategyError):
    """Strategy execution failed."""

    pass


# --- Validation Exceptions ---
class ValidationError(VCOREXError):
    """Base exception for validation errors."""

    pass


class InvalidSignalError(ValidationError):
    """Invalid trading signal."""

    pass


class InvalidParameterError(ValidationError):
    """Invalid parameter provided."""

    pass
