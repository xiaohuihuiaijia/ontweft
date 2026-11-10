"""Serve the actual built app against a fresh, isolated SQLite database for browser checks."""

import os
from pathlib import Path

import uvicorn
from start_local_demo import create_demo_app


def main():
    data = Path(os.environ["ONTOLOGY_PREVIEW_DATA_DIR"]).resolve()
    port = int(os.environ["ONTOLOGY_PREVIEW_PORT"])
    app = create_demo_app(data, port=port, password=os.environ["ONTOLOGY_PREVIEW_PASSWORD"])
    uvicorn.run(app, host="127.0.0.1", port=port, access_log=False)


if __name__ == "__main__":
    main()
