import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

HERE = Path(__file__).parent.resolve()
ERROR_LOG = HERE / 'error.log'


def setup_logging(name: str = 'musicconvert'):
    """Configure a root logger that writes to a rotating file and stdout.

    Importing this module and calling `setup_logging()` ensures that any
    code running in the process (including when started by systemd) will
    have consistent logging to `error.log` and to the process stdout.
    """
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)

    if not logger.handlers:
        # Rotating file handler
        fh = RotatingFileHandler(str(ERROR_LOG), maxBytes=5 * 1024 * 1024, backupCount=5)
        fh.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(name)s: %(message)s'))
        logger.addHandler(fh)

        # Stream handler (stdout) so systemd/journalctl sees logs immediately
        sh = logging.StreamHandler(stream=sys.stdout)
        sh.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(name)s: %(message)s'))
        logger.addHandler(sh)

    # Make uncaught exceptions get logged
    def _excepthook(exc_type, exc, tb):
        logger.exception('Uncaught exception', exc_info=(exc_type, exc, tb))
        # Fall back to default behavior
        sys.__excepthook__(exc_type, exc, tb)

    sys.excepthook = _excepthook
    return logger
