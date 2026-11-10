"""morphoagent: an agentic workflow for bright-field cell morphology analysis.

Sub-packages
------------
:mod:`morphoagent.pipeline`
    The ten-step image-analysis engine (load, enhance-preview, enhance,
    segment-tune, segment, select, review, curate, vampire, stats) and the
    browser review tools. It runs without any agent through the ``morphoagent``
    command line.
:mod:`morphoagent.mcp`
    Model Context Protocol servers that expose pipeline stages as typed tools.

Importing :mod:`morphoagent` is kept free of heavy imports, because
:mod:`morphoagent.pipeline.vampire_runner` is executed by a separate (older)
Python interpreter.
"""

__version__ = "0.1.0"
