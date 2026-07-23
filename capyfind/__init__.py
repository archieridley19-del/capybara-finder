"""capyfind -- a micro-niche gap finder that is built to say no.

The tool's most important job is rejection. A tool that says "yes" easily is
worthless; one that says "no" with evidence, cheaply and fast, is valuable.
"""

__version__ = "0.1.0"

from .discover import discover
from .models import Run, Score
from .providers import build_providers
from .store import Store
from .verify import verify

__all__ = ["discover", "verify", "build_providers", "Store", "Run", "Score", "__version__"]
