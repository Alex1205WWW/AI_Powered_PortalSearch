def load_adapters() -> None:
    """Import adapter modules so they register themselves in `registry.ADAPTERS`."""
    import app.sources.imovirtual  # noqa: F401
    import app.sources.remax  # noqa: F401
    import app.sources.idealista_email  # noqa: F401  (email source: polled by runs.execute_email_poll, not swept)
