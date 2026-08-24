#!/usr/bin/env python3
"""Compatibility launcher for the formal OpenEvo Web Layer."""

from openevo.web_gateway.product_app import *  # noqa: F403
from openevo.web_gateway.product_app import main


if __name__ == "__main__":
    raise SystemExit(main())
