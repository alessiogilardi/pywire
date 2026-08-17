from __future__ import annotations

import inspect
import sys
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast, get_type_hints

from fastapi import Depends, FastAPI, Request
from fastapi.routing import APIRouter

from .decorators import get_default_container
from .markers import resolve_autowired_type

if TYPE_CHECKING:
    from .container import Container


def _resolve_autowired(target: type) -> Callable[[Request], Any]:
    """Return a Depends(...) resolver that reads the container from
    app.state at request time, falling back to the default container."""

    def resolve(request: Request) -> Any:
        container = getattr(request.app.state, "pywire_container", None)
        return (container or get_default_container()).resolve(target)

    return resolve


def _wire_endpoint(func: Any) -> Any:
    """Rewrite bare Autowired[T] parameters into T = Depends(...) in place."""
    hints = get_type_hints(func, include_extras=True)
    sig = inspect.signature(func)
    module_globals = vars(sys.modules[func.__module__])

    new_params = []
    changed = False
    for name, param in sig.parameters.items():
        target = resolve_autowired_type(hints.get(name), module_globals)
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
        func.__signature__ = sig.replace(parameters=new_params)

    return func


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
        self: APIRouter, path: str, endpoint: Any, **kwargs: Any
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
