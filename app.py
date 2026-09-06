"""
Alternate entrypoint name for platforms/tools that specifically look for
`app.py`, or for launchers that want a module-level `app` object, e.g.:

    gunicorn -k aiohttp.GunicornWebWorker app:app

`python app.py` behaves exactly like `python main.py` (see main.py for the
RUN_MODE / polling vs webhook logic).
"""

from core.webserver import build_web_app
from main import main

# Built eagerly so `gunicorn app:app` (or any tool that just imports this
# module and looks for `app`) gets a ready-to-serve aiohttp Application.
# Harmless to build even if you end up running in polling mode via
# `python app.py` — it just won't be used in that case.
app = build_web_app()

if __name__ == "__main__":
    main()
