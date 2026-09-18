class SandboxError(RuntimeError):
    """Base class for sandbox execution failures."""


class SandboxUnavailableError(SandboxError):
    """Raised when the configured sandbox backend cannot be reached."""


class SandboxConfigurationError(SandboxError):
    """Raised when a request cannot be represented safely."""
