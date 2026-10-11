"""Absolute-path hook entry point that works from any project's cwd."""
from pathlib import Path
import sys

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from bridge.cli import main
    main()
