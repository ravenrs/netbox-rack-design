"""Resolve a ``naming_script`` / ``distribution_script`` config value to a callable.

Two forms are accepted:

``package.module.function``
    A plain dotted path, imported with Django's ``import_string``. It must be
    importable from NetBox's Python path (an installed package, or a file in a
    local ``SCRIPTS_ROOT`` referenced as ``scripts.<module>.<function>``).

``scriptmodule:<module>.<function>``
    A module kept in NetBox's own script storage -- ``STORAGES["scripts"]``,
    which may be the local ``SCRIPTS_ROOT`` or a remote backend such as S3 --
    i.e. a file you add under Customization -> Scripts. The code is read
    through that storage, so it works when the file is not on the local disk.

A dotted path never contains ``:``, so the two forms cannot be confused.

Storage modules are cached in-process. The file is re-read at most every
:data:`RECHECK_SECONDS`, and the module is re-executed only when its content
changed, so an edited script takes effect within that window without a
restart and without a storage round-trip on every name preview.
"""

import hashlib
import sys
import threading
import time
import types

from django.core.files.storage import storages
from django.utils.module_loading import import_string

__all__ = ("SCRIPTMODULE_PREFIX", "RECHECK_SECONDS", "resolve_callable", "clear_cache")

SCRIPTMODULE_PREFIX = "scriptmodule:"

#: How long a loaded storage module is trusted before its file is re-read.
RECHECK_SECONDS = 10

# Loaded storage modules register under this namespace in ``sys.modules``, so
# a script named like a core app ("circuits") never shadows that app.
_MODULE_NAMESPACE = "netbox_rack_design_scriptmodule"

# file name -> (sha256 of the source, module, monotonic time of the last check)
_cache = {}
_lock = threading.Lock()


def clear_cache():
    """Forget every loaded storage module (tests use this)."""
    with _lock:
        _cache.clear()


def resolve_callable(path, config_key):
    """Return the callable *path* names; ``ValueError`` if it can't be resolved.

    *config_key* (``"naming_script"`` / ``"distribution_script"``) only labels
    the error message, which the caller shows or logs.
    """
    if path.startswith(SCRIPTMODULE_PREFIX):
        fn = _resolve_storage(path[len(SCRIPTMODULE_PREFIX):], config_key)
    else:
        try:
            fn = import_string(path)
        except ImportError as exc:
            raise ValueError(f"Could not import {config_key} '{path}': {exc}") from exc
    if not callable(fn):
        raise ValueError(f"{config_key} '{path}' is not callable.")
    return fn


def _resolve_storage(ref, config_key):
    module_name, _, attr = ref.rpartition(".")
    if not module_name or not attr:
        raise ValueError(
            f"{config_key} '{SCRIPTMODULE_PREFIX}{ref}' must name a module and a "
            f"function, e.g. '{SCRIPTMODULE_PREFIX}device_naming.build_name'."
        )
    module = _load_storage_module(module_name, config_key)
    try:
        return getattr(module, attr)
    except AttributeError:
        raise ValueError(
            f"{config_key}: script module '{module_name}' has no '{attr}'."
        ) from None


def _load_storage_module(module_name, config_key):
    filename = module_name.replace(".", "/") + ".py"
    now = time.monotonic()
    with _lock:
        cached = _cache.get(filename)
        if cached and now - cached[2] < RECHECK_SECONDS:
            return cached[1]

    try:
        with storages["scripts"].open(filename, "rb") as fh:
            source = fh.read()
    except (FileNotFoundError, OSError) as exc:
        raise ValueError(
            f"Could not read {config_key} module '{filename}' from NetBox's script "
            f"storage: {exc}"
        ) from exc

    digest = hashlib.sha256(source).hexdigest()
    with _lock:
        cached = _cache.get(filename)
        if cached and cached[0] == digest:
            _cache[filename] = (digest, cached[1], now)
            return cached[1]

    qualname = f"{_MODULE_NAMESPACE}.{module_name}"
    module = types.ModuleType(qualname)
    module.__file__ = filename
    # Registered before exec so dataclasses / typing can find the module.
    sys.modules[qualname] = module
    try:
        exec(compile(source, filename, "exec"), module.__dict__)
    except Exception:
        sys.modules.pop(qualname, None)
        raise
    with _lock:
        _cache[filename] = (digest, module, now)
    return module
