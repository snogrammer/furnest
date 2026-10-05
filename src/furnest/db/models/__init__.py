"""ORM models. Importing this package registers every table on ``Base.metadata``."""

from furnest.db.models.amenity_cache import AmenityExtraction
from furnest.db.models.criteria import CriteriaVersion
from furnest.db.models.jobs import Job, JobLock, JobStatus
from furnest.db.models.listing import (
    Listing,
    ListingImage,
    PriceHistory,
    SourceRun,
    SourceRunStatus,
)
from furnest.db.models.property import AuditEvent, Property
from furnest.db.models.settings import Setting
from furnest.db.models.source import Source
from furnest.db.models.worker import WorkerHeartbeat

__all__ = [
    "AmenityExtraction",
    "AuditEvent",
    "CriteriaVersion",
    "Job",
    "JobLock",
    "JobStatus",
    "Listing",
    "ListingImage",
    "PriceHistory",
    "Property",
    "Setting",
    "Source",
    "SourceRun",
    "SourceRunStatus",
    "WorkerHeartbeat",
]
