#!/usr/bin/env python3
"""Compatibility launcher for the formal OpenEvo product daemon.

Remote installations should use ``python -m openevo.daemon.product_app``.  The
old path remains executable so existing development automation and imports do
not lose data or behavior during the transition.
"""

from openevo.daemon.product_app import *  # noqa: F403
from openevo.daemon.product_app import main


if __name__ == "__main__":
    raise SystemExit(main())
