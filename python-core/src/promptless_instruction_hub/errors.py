"""Domain exceptions for Instruction Hub operations."""


class InstructionHubError(Exception):
    """Base exception for user-correctable Instruction Hub failures."""


class BuildCheckFailedError(InstructionHubError):
    """Raised when generated files differ from freshly compiled output."""
