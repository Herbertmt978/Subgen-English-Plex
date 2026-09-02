"""Shared verified backend unload and allocator-cache release primitives."""

from __future__ import annotations

from typing import Optional


def unload_verified_backend(resident: object) -> None:
    """Unload one resident backend and require an explicit released state."""

    backend = getattr(resident, "model", None)
    unload = getattr(backend, "unload_model", None)
    if not callable(unload):
        raise RuntimeError("Loaded model backend cannot be unloaded")
    unload()
    release_confirmation = getattr(backend, "model_is_loaded", None)
    if type(release_confirmation) is not bool or release_confirmation is not False:
        raise RuntimeError("Loaded model backend did not confirm release")


def release_allocator_caches(
    *,
    gc_module: object,
    torch_module: object,
    device: object,
    cuda_device_index: Optional[int],
    os_module: object,
    ctypes_module: object,
    logger: object = None,
) -> None:
    """Return unused accelerator and native allocator caches to their owners."""

    collect = getattr(gc_module, "collect", None)
    if not callable(collect):
        raise TypeError("gc_module must provide collect")
    collect()

    cuda = getattr(torch_module, "cuda", None)
    cuda_available = getattr(cuda, "is_available", None)
    if (
        isinstance(device, str)
        and device.casefold().startswith("cuda")
        and callable(cuda_available)
        and cuda_available()
    ):
        synchronize = getattr(cuda, "synchronize", None)
        if callable(synchronize):
            synchronize(cuda_device_index)
        empty_cache = getattr(cuda, "empty_cache", None)
        if not callable(empty_cache):
            raise RuntimeError("CUDA allocator does not expose empty_cache")
        empty_cache()
        if callable(synchronize):
            synchronize(cuda_device_index)
        debug = getattr(logger, "debug", None)
        if callable(debug):
            debug("CUDA cache cleared.")

    if getattr(os_module, "name", None) != "nt":
        ctypes_util = getattr(ctypes_module, "util", None)
        find_library = getattr(ctypes_util, "find_library", None)
        cdll = getattr(ctypes_module, "CDLL", None)
        if callable(find_library) and callable(cdll):
            library_name = find_library("c")
            if library_name:
                malloc_trim = getattr(cdll(library_name), "malloc_trim", None)
                if callable(malloc_trim):
                    malloc_trim(0)

    collect()
