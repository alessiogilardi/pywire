from __future__ import annotations

import asyncio
import inspect
import sys
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import TYPE_CHECKING, Any, cast, overload

from fastapi import Depends, FastAPI, Request
from fastapi.routing import APIRouter

from .decorators import get_default_container
from .exceptions import AnnotationResolutionError
from .markers import callable_hints, resolve_autowired_type

if TYPE_CHECKING:
    from .container import Container


def _resolve_autowired[T](target: type[T]) -> Callable[[Request], T]:
    """Return a Depends(...) resolver that reads the container from
    app.state at request time, falling back to the default container."""

    def resolve(request: Request) -> T:
        container = getattr(request.app.state, "pywire_container", None)
        return (container or get_default_container()).resolve(target)

    return resolve


def _resolve_autowired_late(
    annotation: object,
    module_globals: dict[str, Any],
    context: str,
) -> Callable[[Request], Any]:
    """Defer a currently-unresolvable Autowired[T] parameter to request time.

    Mirrors the container's lazy planning: an endpoint may inject a service
    defined further down its own module, and by the time a request arrives that
    module is fully imported, so re-running the resolution succeeds. Raising at
    decoration time instead would reintroduce the decoration-time failure the
    global add_api_route patch exists to eliminate, and would leave endpoints
    eager while components are lazy.

    A genuinely undefined name still fails -- on the first request, naming the
    endpoint. The resolved target is memoised, so the retry happens once rather
    than per request.
    """
    resolved: list[type] = []

    def resolve(request: Request) -> Any:  # noqa: ANN401
        if not resolved:
            resolved.append(
                cast(type, resolve_autowired_type(annotation, module_globals, context))
            )

        container = getattr(request.app.state, "pywire_container", None)

        return (container or get_default_container()).resolve(resolved[0])

    return resolve


def _wire_endpoint(func: Callable[..., object]) -> None:
    """Rewrite bare Autowired[T] parameters into T = Depends(...) in place."""
    if not (inspect.isfunction(func) or inspect.ismethod(func)):
        # The patched add_api_route runs on every route in the process, not
        # just wired ones -- endpoints that aren't plain functions/methods
        # (e.g. a functools.partial, a supported FastAPI pattern) have no
        # __module__/annotations shaped the way get_type_hints expects and
        # simply never carry a bare Autowired[T] parameter to rewrite.
        return

    hints = callable_hints(func)
    sig = inspect.signature(func)
    module_globals = vars(sys.modules[func.__module__])

    new_params = []
    changed = False
    for name, param in sig.parameters.items():
        annotation = hints.get(name)
        context = f"{func.__qualname__}({name})"

        try:
            target = resolve_autowired_type(annotation, module_globals, context)
        except AnnotationResolutionError:
            # The name may simply be defined further down this module; retry on
            # the first request rather than failing this decoration.
            changed = True
            new_params.append(
                param.replace(
                    # Inert: the Depends default means FastAPI never parses this
                    # parameter from the request, so the annotation only has to
                    # be something it accepts. The real type is unknown here by
                    # definition.
                    annotation=object,
                    default=Depends(
                        _resolve_autowired_late(annotation, module_globals, context)
                    ),
                )
            )
            continue

        if target is None:
            new_params.append(param)
            continue

        changed = True
        new_params.append(
            param.replace(
                annotation=target, default=Depends(_resolve_autowired(target))
            )
        )

    if changed:
        # func is narrowed to FunctionType | MethodType by the isfunction/
        # ismethod guard above; neither stub exposes a writable
        # __signature__, even though CPython allows the assignment. Any is
        # the only way to bypass that static restriction for a real dynamic
        # attribute write -- object would still reject the unknown attribute.
        cast(Any, func).__signature__ = sig.replace(parameters=new_params)


def _install_patch() -> None:
    """Patch APIRouter.add_api_route once, so every route on every router
    -- regardless of wire() call order -- has bare Autowired[T] parameters
    rewritten before FastAPI validates them. Guarded by a marker attribute
    on the installed wrapper so re-running this module's body (e.g. via
    importlib.reload) never wraps an already-patched add_api_route again.
    _patched_add_api_route is defined as a closure here, not at module
    level, so a guarded-out call never touches the "original" callable an
    already-installed wrapper depends on.
    """
    if getattr(APIRouter.add_api_route, "__pywire_patched__", False):
        return

    original = APIRouter.add_api_route

    def _patched_add_api_route(
        self: APIRouter,
        path: str,
        endpoint: Callable[..., object],
        # Forwards to FastAPI's own add_api_route, whose kwargs (response_model,
        # status_code, tags, ...) we deliberately don't duplicate/pin here --
        # Any is the correct type for an untyped passthrough wrapper.
        **kwargs: Any,  # noqa: ANN401
    ) -> None:
        _wire_endpoint(endpoint)
        original(self, path, endpoint, **kwargs)

    setattr(_patched_add_api_route, "__pywire_patched__", True)
    APIRouter.add_api_route = cast("Callable[..., None]", _patched_add_api_route)


_install_patch()


def wire(app: FastAPI, *, container: Container | None = None) -> FastAPI:
    """Associate container with app for Autowired[T] route parameter resolution.

    Safe to call at any point relative to route/router decoration -- decorating
    a route with a bare Autowired[T] parameter never fails, on any router,
    whether or not wire() has been called yet. If wire() is never called for
    an app, Autowired[T] parameters resolve against the module-level default
    container (the same one @component uses).

    Raises:
        TypeError: if app is not a FastAPI instance.
    """
    if not isinstance(app, FastAPI):
        got = type(app).__name__
        raise TypeError(f"wire() requires a FastAPI instance, got {got}")
    app.state.pywire_container = container or get_default_container()
    return app


@overload
def pywire_lifespan(app: FastAPI) -> AbstractAsyncContextManager[None]: ...


@overload
def pywire_lifespan(
    *,
    container: Container | None = None,
    close_on_shutdown: bool = True,
) -> Callable[[FastAPI], AbstractAsyncContextManager[None]]: ...


def pywire_lifespan(
    app: FastAPI | None = None,
    *,
    container: Container | None = None,
    close_on_shutdown: bool = True,
) -> (
    AbstractAsyncContextManager[None]
    | Callable[[FastAPI], AbstractAsyncContextManager[None]]
):
    """ASGI lifespan that binds a container to an app and closes it on shutdown.

    Usable bare or called with configuration, the same dual form @component
    uses:

        app = FastAPI(lifespan=pywire_lifespan)
        app = FastAPI(lifespan=pywire_lifespan(container=container))
        app = FastAPI(lifespan=pywire_lifespan(close_on_shutdown=False))

    Startup writes app.state.pywire_container -- everything wire() does --
    so an app using this needs no wire() call. Shutdown calls
    Container.close(), which is what makes a bean's @pre_destroy or
    on_close hook actually run when the service stops.

    Bare, it binds and closes the module-level default container: the same
    one @component registers into, and process-global. In a test suite where
    two apps share it, one app's shutdown tears down beans the other still
    holds -- pass close_on_shutdown=False for the apps that should not own
    that lifetime.
    """
    if app is not None:
        return _run(app, None, True)

    def build(target: FastAPI) -> AbstractAsyncContextManager[None]:
        return _run(target, container, close_on_shutdown)

    return build


@asynccontextmanager
async def _run(
    app: FastAPI,
    container: Container | None,
    close_on_shutdown: bool,
) -> AsyncIterator[None]:
    """Bind at startup, tear down at shutdown.

    close() is synchronous and a teardown hook may block on real I/O
    (draining a pool, joining a thread), so it runs in a worker thread
    rather than on the event loop, which still has the rest of the
    application's shutdown to run. asyncio.to_thread, not anyio: this
    library declares no dependencies and is not going to start with one it
    only gets transitively.
    """
    resolved = container or get_default_container()
    app.state.pywire_container = resolved

    yield

    if close_on_shutdown:
        await asyncio.to_thread(resolved.close)
