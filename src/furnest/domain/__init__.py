"""Pure domain types: NormalizedListing, Criteria, money, rent periods, geo, addresses,
amenities, US places and the property status state machine.

Nothing here imports from the rest of furnest or does I/O (``places`` reads data bundled
in an installed package); everything else depends on it.
"""

from furnest.domain.address import (
    AddressParser,
    ParsedAddress,
    PassthroughAddressParser,
    normalize_zip,
)
from furnest.domain.amenities import Amenity, AmenityExtractor, AmenityResult, amenity_label
from furnest.domain.criteria import (
    Area,
    Criteria,
    CriteriaChange,
    FilterRule,
    Outline,
    diff_criteria,
)
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
from furnest.domain.owner_type import (
    OwnerType,
    infer_owner_type,
    normalize_owner_type,
    owner_type_label,
)
from furnest.domain.periods import MONTHLY_FACTOR, RentPeriod, parse_period, to_monthly
from furnest.domain.places import Place, find_places, neighborhood_names, resolve_place
from furnest.domain.price import Price, parse_price
from furnest.domain.property_type import PropertyType, normalize_property_type
from furnest.domain.status import (
    TRANSITIONS,
    Actor,
    InvalidTransitionError,
    PropertyStatus,
    allowed_targets,
    check_transition,
    status_rank,
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
    "Outline",
    "OwnerType",
    "ParsedAddress",
    "PassthroughAddressParser",
    "Place",
    "Price",
    "PropertyStatus",
    "PropertyType",
    "RentPeriod",
    "allowed_targets",
    "amenity_label",
    "beds_baths",
    "check_transition",
    "diff_criteria",
    "find_places",
    "get_currency",
    "haversine",
    "infer_owner_type",
    "neighborhood_names",
    "normalize_owner_type",
    "normalize_property_type",
    "normalize_zip",
    "owner_type_label",
    "parse_period",
    "parse_price",
    "register_currency",
    "resolve_place",
    "status_rank",
    "to_monthly",
]
