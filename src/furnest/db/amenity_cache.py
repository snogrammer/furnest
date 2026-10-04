"""SqlAmenityCache: LLM amenity verdicts over the ``amenity_extractions`` table.

Implements :class:`furnest.matching.amenities.llm.AmenityCache`. Keys are content
hashes, so a row never goes stale and is never updated. Two workers that extract the
same listing at once may both try to insert; the second insert is ignored.
"""

from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Session, sessionmaker

from furnest.db.models import AmenityExtraction
from furnest.db.session import session_scope
from furnest.domain import Amenity, AmenityResult


class SqlAmenityCache:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def get(self, key: str) -> AmenityResult | None:
        with session_scope(self._session_factory) as session:
            row = session.get(AmenityExtraction, key)
            if row is None:
                return None
            return AmenityResult(
                present=frozenset(Amenity(tag) for tag in row.present),
                absent=frozenset(Amenity(tag) for tag in row.absent),
            )

    def put(self, key: str, model: str, result: AmenityResult) -> None:
        statement = (
            insert(AmenityExtraction)
            .values(
                content_hash=key,
                model=model,
                present=sorted(result.present),
                absent=sorted(result.absent),
            )
            .on_conflict_do_nothing(index_elements=["content_hash"])
        )
        with session_scope(self._session_factory) as session:
            session.execute(statement)
