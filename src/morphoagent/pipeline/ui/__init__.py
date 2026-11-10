"""Browser helper tools for practising and curating an analysis.

``morphoagent ui build`` writes ONE self-contained HTML file (no network access,
all scripts, styles, images and label maps embedded) for a dataset that has
already been through the pipeline. ``morphoagent ui serve`` serves the same page
from a small standard-library HTTP server with autosave, live enhancement
previews and an optional, vendor-neutral language-model assistant.

Modules
-------
dataset
    Collects the outputs of steps 2, 4 and 6 into one JSON-serialisable dict
    (images as base64 JPEG, label maps as base64 PNG).
build
    Assembles the HTML page from the packaged static files.
server
    ``http.server`` based live server and its JSON API.
llm
    Request construction, pluggable providers, response validation, cache and
    rate limiter of the optional assistant.
"""
