"""Pure domain types: NormalizedListing, Criteria, money, rent periods, geo, addresses,
amenities and the property status state machine.

Nothing here imports from the rest of furnest or does I/O; everything else depends on it.
"""

from furnest.domain.address import (
    AddressParser,
    ParsedAddress,
    PassthroughAddressParser,
    normalize_zip,
)
from furnest.domain.amenities import Amenity, AmenityExtractor, AmenityResult, amenity_label
from furnest.domain.criteria import Area, Criteria, CriteriaChange, FilterRule, diff_criteria
from furnest.domain.geo import EARTH_RADIUS, DistanceUnit, haversine
from furnest.domain.listing import Address, Contact, NormalizedListing, beds_baths
from furnest.domain.money import (
    DEFAULT_CURRENCY,
    USD,
    Currency,
    Money,
    get_currency,
    register_currency,
)
from furnest.domain.periods import MONTHLY_FACTOR, RentPeriod, parse_period, to_monthly
from furnest.domain.price import Price, parse_price
from furnest.domain.property_type import PropertyType, normalize_property_type
from furnest.domain.status import (
    TRANSITIONS,
    Actor,
    InvalidTransitionError,
    PropertyStatus,
    allowed_targets,
    check_transition,
)

__all__ = [
    "DEFAULT_CURRENCY",
    "EARTH_RADIUS",
    "MONTHLY_FACTOR",
    "TRANSITIONS",
    "USD",
    "Actor",
    "Address",
    "AddressParser",
    "Amenity",
    "AmenityExtractor",
    "AmenityResult",
    "Area",
    "Contact",
    "Criteria",
    "CriteriaChange",
    "Currency",
    "DistanceUnit",
    "FilterRule",
    "InvalidTransitionError",
    "Money",
    "NormalizedListing",
    "ParsedAddress",
    "PassthroughAddressParser",
    "Price",
    "PropertyStatus",
    "PropertyType",
    "RentPeriod",
    "allowed_targets",
    "amenity_label",
    "beds_baths",
    "check_transition",
    "diff_criteria",
    "get_currency",
    "haversine",
    "normalize_property_type",
    "normalize_zip",
    "parse_period",
    "parse_price",
    "register_currency",
    "to_monthly",
]
