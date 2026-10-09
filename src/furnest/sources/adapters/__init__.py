"""Built-in source adapters, one module or package each. ``discover()`` finds them.

``furnished_finder``, ``zillow`` and ``airbnb`` run through Apify
(``furnest.sources.apify``). ``craigslist`` fetches its pages directly and ``landing``
calls Landing's public API. ``stubs``
holds disabled placeholders for researched sources that aren't built yet
(``furnest.sources.stub``). ``docs/adapters.md`` covers each source's access options
and field mapping.
"""
