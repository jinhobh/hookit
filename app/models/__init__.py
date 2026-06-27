"""ORM models package.

Importing this package registers all models with ``Base.metadata``, which is
required for Alembic autogenerate to detect schema changes.
"""

from app.models.api_key import ApiKey, generate_api_key
from app.models.project import Project

__all__ = ["ApiKey", "Project", "generate_api_key"]
