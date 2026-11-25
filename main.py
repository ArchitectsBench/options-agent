"""Entry point — routes to the trader CLI.

Usage: .venv/bin/python main.py <command>   (or alias: trader <command>)
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.control.cli import app

if __name__ == "__main__":
    app()
