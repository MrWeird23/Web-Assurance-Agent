import logging
import os

from fastapi import FastAPI

from triage_agent.runtime import build_app
from triage_agent.settings import Settings


def create_runtime_app() -> FastAPI:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    return build_app(Settings.from_mapping(os.environ))
