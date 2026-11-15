"""HTTP client for one remote ComfyUI server, driven by H3 storyboard packages."""

from .bindings import api_graph, build_graph
from .errors import BindingError, ComfyUIError, ComfyUITimeoutError, WorkflowExecutionError
from .storyboard import StoryboardSegment, load_storyboard_bundle
from .transport import ComfyUIClient, OutputFile

__all__ = [
    "BindingError",
    "ComfyUIClient",
    "ComfyUIError",
    "ComfyUITimeoutError",
    "OutputFile",
    "StoryboardSegment",
    "WorkflowExecutionError",
    "api_graph",
    "build_graph",
    "load_storyboard_bundle",
]
