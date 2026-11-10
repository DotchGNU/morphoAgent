"""Bright-field (phase-contrast) cell morphology pipeline.

The package is split so that :mod:`morphoagent.pipeline.vampire_runner` can be
executed by a separate (older) Python interpreter that only has numpy, pandas
and Pillow plus the VAMPIRE dependencies. Importing
:mod:`morphoagent.pipeline` itself is therefore kept free of heavy imports.
"""

from .. import __version__

__all__ = ["__version__"]
