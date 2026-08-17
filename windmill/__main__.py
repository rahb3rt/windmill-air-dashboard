"""Entry point: `python -m windmill`.

The server module does the work; this exists so the package has one obvious way
in, which is also what the container's CMD points at.
"""
from . import server

if __name__ == "__main__":
    server.main()
