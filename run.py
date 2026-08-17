#!/usr/bin/env python3
"""Start the dashboard from a checkout: `python3 run.py`.

Equivalent to `python -m windmill`, kept because that is the command this
project has always been started with.
"""
from windmill import server

if __name__ == "__main__":
    server.main()
