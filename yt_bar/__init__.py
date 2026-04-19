__all__ = ["YTBar", "main"]


def __getattr__(name):
    if name in __all__:
        from .app import YTBar, main

        return {"YTBar": YTBar, "main": main}[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
