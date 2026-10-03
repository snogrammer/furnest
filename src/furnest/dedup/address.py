"""US address parsing with ``usaddress``: the :class:`AddressParser` for ``country == "US"``.

``usaddress`` tags each token of a free-text address (house number, street name,
unit, city, state, ZIP…) with a CRF model. Sources often put the whole address in
``line1``, so the parser joins every line the source gave, tags the result, and
normalizes each component through the tables in :mod:`furnest.domain.address`.

Components the source gave as separate fields (city, region, postal code,
neighborhood) win over tagged ones: a structured field is more trustworthy than a
guess from free text. Parsing never raises. Ambiguous input (a repeated label such
as two street names, an intersection, or text usaddress can't classify) or a tag
result with neither a street nor a city falls back to
:class:`PassthroughAddressParser`, which marks the result ``parsed=False``.
"""

from __future__ import annotations

import usaddress

from furnest.domain.address import (
    ParsedAddress,
    PassthroughAddressParser,
    clean,
    normalize_directional,
    normalize_region,
    normalize_suffix,
    normalize_unit,
    split_zip,
)
from furnest.domain.listing import Address

# usaddress also returns "Intersection" and "Ambiguous"; neither names one door.
_ADDRESS_KINDS = frozenset({"Street Address", "PO Box"})


class UsAddressParser:
    def __init__(self) -> None:
        self._fallback = PassthroughAddressParser()

    def parse(self, address: Address) -> ParsedAddress:
        if address.country != "US":
            return self._fallback.parse(address)
        text = ", ".join(
            part
            for part in (
                address.line1,
                address.line2,
                address.city,
                address.region,
                address.postal_code,
            )
            if part
        )
        if not text:
            return self._fallback.parse(address)
        try:
            tags, kind = usaddress.tag(text)
        # The bundled stub omits the exception class, though the module defines it.
        except usaddress.RepeatedLabelError:  # type: ignore[attr-defined]
            return self._fallback.parse(address)
        if kind not in _ADDRESS_KINDS:
            return self._fallback.parse(address)

        street = " ".join(
            p
            for p in (
                normalize_directional(tags.get("StreetNamePreDirectional")),
                clean(tags.get("StreetNamePreType")),
                clean(tags.get("StreetName")),
                normalize_suffix(tags.get("StreetNamePostType")),
                normalize_directional(tags.get("StreetNamePostDirectional")),
            )
            if p
        )
        po_box = clean(tags.get("USPSBoxID"))
        city = clean(address.city) or clean(tags.get("PlaceName"))
        if not (street or po_box or city):
            return self._fallback.parse(address)

        postal_code, ext = split_zip(address.postal_code or tags.get("ZipCode"))
        return ParsedAddress(
            house_number=clean(tags.get("AddressNumber")),
            street=street or None,
            unit=normalize_unit(tags.get("OccupancyType"), tags.get("OccupancyIdentifier")),
            po_box=po_box,
            city=city,
            region=normalize_region(address.region or tags.get("StateName")),
            postal_code=postal_code,
            postal_code_ext=ext,
            country="US",
            neighborhood=clean(address.neighborhood),
            parsed=True,
        )
