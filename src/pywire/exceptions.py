from __future__ import annotations

from typing import Self, override


class PyWireError(Exception):
    """Base class for every error raised by pywire.

    Resolution context is carried as structured data and composed into the
    final text by __str__. Both pieces of context arrive at construction time:
    the frame that raises knows the chain it is standing in, and it receives
    the requester label from its caller as an argument rather than having it
    attached afterwards. The exception is therefore immutable -- its str() does
    not change as it propagates -- and there is exactly one mechanism for
    context instead of two.

    Attributes:
        message: The failure itself, without any context.
        chain: The resolution chain at the point of failure, outermost first.
        requester: "Owner.member" label of whatever asked for the failed
            dependency, or None when nothing did (a direct resolve() call).
    """

    def __init__(
        self,
        message: str,
        *,
        chain: tuple[type, ...] = (),
        requester: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.chain = chain
        self.requester = requester

    def with_context(
        self,
        *,
        chain: tuple[type, ...],
        requester: str | None,
    ) -> Self:
        """Return a copy carrying context this error was raised without.

        Planning (plans.py) is pure inspection and knows nothing about the
        resolution stack, so the errors it raises arrive contextless and would
        otherwise lose the chain entirely. The container re-raises them through
        this method instead of mutating them. Existing context always wins: the
        frame that raised knew more than the frame enriching it.
        """
        return type(self)(
            self.message,
            chain=self.chain or chain,
            requester=self.requester or requester,
        )

    @override
    def __str__(self) -> str:
        parts = [self.message]

        if self.requester is not None:
            parts.append(f"Required by '{self.requester}'.")

        if self.chain:
            names = " -> ".join(entry.__name__ for entry in self.chain)
            parts.append(f"Resolution chain: {names}")

        return " ".join(parts)


class RegistrationError(PyWireError):
    """Raised when a class cannot be registered.

    Narrowly scoped to registration itself -- currently, registering the same
    class twice in one container. Structural defects that make a class
    impossible to build are UnconstructibleComponentError instead, because they
    are detected lazily during resolve(), not at registration.
    """


class UnconstructibleComponentError(PyWireError):
    """Raised when the container cannot build a class at all.

    Its __new__ requires arguments, its __init__ has a parameter nothing can
    supply, or it forbids setting an injected field. A structural property of
    the class itself, independent of what happens to be registered alongside
    it -- which is why it is not a DependencyResolutionError.
    """


class AnnotationResolutionError(PyWireError):
    """Raised when an Autowired[...] annotation cannot be resolved to a type."""


class DependencyResolutionError(PyWireError):
    """Raised when a registered dependency is missing or fails to build."""


class CircularDependencyError(DependencyResolutionError):
    """Raised when a dependency cycle passes through a constructor parameter."""
