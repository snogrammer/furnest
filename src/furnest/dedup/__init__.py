"""Cross-source grouping by address, geo, image pHash and text similarity. Built in P7.

``phash`` fetches listing photos within limits and hashes them; grouping (#50) uses
:func:`~furnest.dedup.phash.matching_pairs` on the stored hashes.
"""
