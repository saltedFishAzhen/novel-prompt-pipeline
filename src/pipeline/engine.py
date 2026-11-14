"""Application entry point for the six-stage production pipeline."""

from .shuohao.engine import ProductionPipeline as Pipeline

__all__ = ["Pipeline"]
