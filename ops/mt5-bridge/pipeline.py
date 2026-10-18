#!/usr/bin/env python3
"""Thin CLI shim: `python3 pipeline.py <cmd> ...` runs the pipeline package.

See pipeline/__main__.py for commands.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pipeline.__main__ import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
