#!/usr/bin/env python3
"""NinjaScript hazard linter, no MCP needed: python scripts/nt_lint.py <files|folders|globs> [--rule NT05] [--json] [--list]
Exit 0 = clean, 1 = findings (or unreadable files)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "server"))
from nt8_mcp.lint import main  # noqa: E402

sys.exit(main())
