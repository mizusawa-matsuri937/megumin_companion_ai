"""Support desktop packaging preflight through ``python -m desktop_client``."""

from desktop_client.entrypoint import main

raise SystemExit(main())
