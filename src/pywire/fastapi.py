from __future__ import annotations

import functools
import inspect
import sys
from typing import TYPE_CHECKING, Any, cast, get_type_hints

from fastapi import APIRouter, Depends, FastAPI
from fastapi.routing import APIRoute

from .decorators import get_default_container
from .markers import resolve_autowired_type

if TYPE_CHECKING:
    from .container import Container


def _wire_endpoint(func: Any, container: Container) -> Any:
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
        resolver = functools.partial(container.resolve, target)
        new_params.append(param.replace(annotation=target, default=Depends(resolver)))

    if changed:
        func.__signature__ = sig.replace(parameters=new_params)

    return func


class _WiredRoute(APIRoute):
    """APIRoute that resolves bare Autowired[T] endpoint parameters via pywire.

    Internal implementation detail of wire() — not part of the public API.
    """

    def __init__(
        self,
        path: str,
        endpoint: Any,
        *,
        container: Container | None = None,
        **kwargs: Any,
    ) -> None:
        endpoint = _wire_endpoint(endpoint, container or get_default_container())
        super().__init__(path, endpoint, **kwargs)


def wire(
    target: FastAPI | APIRouter, *, container: Container | None = None
) -> FastAPI | APIRouter:
    """Enable Autowired[T] bare parameters on every route added to target from now on.

    target is typically a FastAPI() app (which exposes its routing surface via
    app.router) or a plain APIRouter(). Call this once, before defining routes;
    it only affects routes registered afterward.
    """
    router = target.router if isinstance(target, FastAPI) else target
    router.route_class = cast(
        "type[APIRoute]", functools.partial(_WiredRoute, container=container)
    )
    return target
