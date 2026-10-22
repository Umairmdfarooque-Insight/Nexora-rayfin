"""Request and response shapes for the local FastAPI app.

Moved to `app/fabric/schemas.py` (T-102 FR-1): this module just re-exports it,
so the local development server keeps working unchanged.
"""

from app.fabric.schemas import *  # noqa: F401,F403
from app.fabric.schemas import Guid  # noqa: F401  (not exported by *)
