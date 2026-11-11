Data collection: fetching, cleaning and storing data from the web and APIs reproducibly.
- Prefer official sources and documented APIs; record the URL and retrieval date for every dataset.
- Save raw data untouched (data/raw/), then cleaned data (data/clean/) with a script that reproduces it.
- Use CSV or JSON with clear column names and units; write a short data/README.md describing each file.
- Respect robots.txt and rate limits; cache responses so reruns don't refetch.
