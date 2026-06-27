"""ORM models package.

Importing this package registers all models with ``Base.metadata``, which is
required for Alembic autogenerate to detect schema changes.
"""

from app.models.api_key import ApiKey, generate_api_key
from app.models.endpoint import Endpoint, EndpointStatus
from app.models.project import Project

__all__ = ["ApiKey", "Endpoint", "EndpointStatus", "Project", "generate_api_key"]
