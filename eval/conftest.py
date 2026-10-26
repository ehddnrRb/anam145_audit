import sys
from pathlib import Path

_here = str(Path(__file__).resolve().parent)
if _here not in sys.path:
    sys.path.insert(0, _here)

# bare `pytest` must never import code from cloned target repos or the scabench submodule
collect_ignore = ["datasets", "runs"]
