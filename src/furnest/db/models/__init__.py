"""ORM models. Importing this package registers every table on ``Base.metadata``."""

from furnest.db.models.amenity_cache import AmenityExtraction
from furnest.db.models.criteria import CriteriaVersion
from furnest.db.models.discord import DiscordBatch, DiscordInteraction
from furnest.db.models.email import Email, EmailDirection, EmailStatus
from furnest.db.models.email_template import EmailTemplate
from furnest.db.models.enrichment import EnrichmentResult
from furnest.db.models.geocode import GeocodeCacheEntry
from furnest.db.models.jobs import Job, JobLock, JobStatus
from furnest.db.models.listing import (
    Listing,
    ListingImage,
    PriceHistory,
    SourceRun,
    SourceRunStatus,
)
from furnest.db.models.property import AuditEvent, ListingSeparation, Note, Property
from furnest.db.models.risk import RiskFlag
from furnest.db.models.settings import Setting
from furnest.db.models.source import Source
from furnest.db.models.worker import WorkerHeartbeat

__all__ = [
    "AmenityExtraction",
    "AuditEvent",
    "CriteriaVersion",
    "DiscordBatch",
    "DiscordInteraction",
    "Email",
    "EmailDirection",
    "EmailStatus",
    "EmailTemplate",
    "EnrichmentResult",
    "GeocodeCacheEntry",
    "Job",
    "JobLock",
    "JobStatus",
    "Listing",
    "ListingImage",
    "ListingSeparation",
    "Note",
    "PriceHistory",
    "Property",
    "RiskFlag",
    "Setting",
    "Source",
    "SourceRun",
    "SourceRunStatus",
    "WorkerHeartbeat",
]
