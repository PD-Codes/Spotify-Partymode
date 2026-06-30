"""Console entry point: `python -m spotify_partymode` or `spotify-partymode`."""

from __future__ import annotations

import uvicorn

from .config import bootstrap


def main() -> None:
    uvicorn.run(
        "spotify_partymode.main:app",
        host=bootstrap.host,
        port=bootstrap.port,
        reload=False,
    )


if __name__ == "__main__":
    main()
