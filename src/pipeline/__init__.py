"""Novel inference and rendering pipeline."""

__all__ = ["Pipeline"]


def __getattr__(name):
    if name == "Pipeline":
        from .engine import Pipeline

        return Pipeline
    raise AttributeError(name)
