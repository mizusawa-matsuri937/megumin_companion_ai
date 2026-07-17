"""Support ``python -m app`` without importing or starting runtime resources first."""

from app.cli import main

raise SystemExit(main())
