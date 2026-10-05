"""Repository entry point; no installation required."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "platform"))
from storyforge.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
