import logging
import logging.handlers

from .config import ROOT

LOG_DIR = ROOT / "logs"
FMT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def setup(name):
    """Log to console and logs/<name>.log (10 MB x 5 files)."""
    LOG_DIR.mkdir(exist_ok=True)
    fh = logging.handlers.RotatingFileHandler(LOG_DIR / f"{name}.log", maxBytes=10 * 2**20, backupCount=5,
                                              encoding="utf-8")
    sh = logging.StreamHandler()
    for h in (fh, sh):
        h.setFormatter(logging.Formatter(FMT))
    logging.basicConfig(level=logging.INFO, handlers=[fh, sh], force=True)
    return fh
