"""Cross-source grouping by address, geo, image pHash and text similarity.

``phash`` fetches listing photos within limits and hashes them, ``text`` compares
titles and descriptions, and ``grouping`` scores a pair of listings and decides
whether they are the same unit. The database side is :mod:`furnest.db.grouping`.
"""
