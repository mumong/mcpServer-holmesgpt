"""AIOps observability collection primitives for MCP tools."""

import os as _os
import sys as _sys

# cluster_scope lives next to this package and is imported by its top-level name.
_SERVERS_DIR = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _SERVERS_DIR not in _sys.path:
    _sys.path.append(_SERVERS_DIR)
