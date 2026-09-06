"""
Forces polling mode regardless of RUN_MODE/PORT env vars — handy for quick
local testing without touching your .env.

    python run_polling.py
"""

import os

os.environ["RUN_MODE"] = "polling"

from main import main

if __name__ == "__main__":
    main()
