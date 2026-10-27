def load_adapters() -> None:
    """Import adapter modules so they register themselves in `registry.ADAPTERS`."""
    import app.sources.imovirtual  # noqa: F401
