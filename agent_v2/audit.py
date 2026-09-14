"""Local bounded diagnostic log, no API keys, raw prompts or private reasoning."""
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path


def configure():
    logger = logging.getLogger("agent_v2.audit")
    if logger.handlers: return
    folder = Path(__file__).resolve().parents[1] / "logs"
    folder.mkdir(exist_ok=True)
    handler = RotatingFileHandler(folder / "agent_audit.log", maxBytes=2_000_000,
                                 backupCount=3, encoding="utf-8", delay=True)
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
