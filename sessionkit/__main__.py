"""Allow ``python3 -m sessionkit`` to run the CLI."""

from sessionkit.cli import main

raise SystemExit(main())
