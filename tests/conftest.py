"""Test bootstrap: project modules import core/config.py, which hard-requires
BOT_TOKEN and MONGO_URI in the environment — set dummies before anything else
imports the package."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("BOT_TOKEN", "123456:test-token-for-unit-tests")
os.environ.setdefault("MONGO_URI", "mongodb://localhost:27017")
os.environ.setdefault("ADMIN_IDS", "111,222")
