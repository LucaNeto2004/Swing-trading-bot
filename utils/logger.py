import logging
import os
import sys
from datetime import datetime
from logging.handlers import RotatingFileHandler

_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_LOG_DIR = os.path.join(_BASE_DIR, "logs")
os.makedirs(_LOG_DIR, exist_ok=True)

LIVE_LOG_MAX_BYTES = 50 * 1024 * 1024
LIVE_LOG_BACKUPS = 5

COLORS = {
    "DEBUG":    "\033[36m",
    "INFO":     "\033[92m",
    "WARNING":  "\033[93m",
    "ERROR":    "\033[91m",
    "CRITICAL": "\033[95m",
}
RESET = "\033[0m"
DIM = "\033[2m"


class ColorFormatter(logging.Formatter):
    def format(self, record):
        color = COLORS.get(record.levelname, "")
        record.levelname_color = f"{color}{record.levelname:<8}{RESET}"
        record.asctime_dim = f"{DIM}{self.formatTime(record, self.datefmt)}{RESET}"
        record.name_dim = f"{DIM}{record.name:<12}{RESET}"
        msg = record.getMessage()
        msg = msg.replace("APPROVED", f"\033[92m\033[1mAPPROVED{RESET}")
        msg = msg.replace("REJECTED", f"\033[91m\033[1mREJECTED{RESET}")
        msg = msg.replace("KILL SWITCH", f"\033[91m\033[1mKILL SWITCH{RESET}")
        msg = msg.replace("[PAPER]", f"\033[33m[PAPER]{RESET}")
        msg = msg.replace("[LIVE]", f"\033[91m\033[1m[LIVE]{RESET}")
        # TP tiers — bright green bold (profit)
        msg = msg.replace("TP1", f"\033[92m\033[1mTP1{RESET}")
        msg = msg.replace("TP2", f"\033[92m\033[1mTP2{RESET}")
        msg = msg.replace("TP3", f"\033[92m\033[1mTP3{RESET}")
        # Exit reasons — red for stop, cyan for trail, yellow for time-out
        msg = msg.replace("stop_loss", f"\033[91m\033[1mstop_loss{RESET}")
        msg = msg.replace("trail_stop", f"\033[96m\033[1mtrail_stop{RESET}")
        msg = msg.replace("TRAIL ARMED", f"\033[96m\033[1mTRAIL ARMED{RESET}")
        msg = msg.replace("TRAIL TIGHTEN", f"\033[96mTRAIL TIGHTEN{RESET}")
        msg = msg.replace("max_hold", f"\033[93mmax_hold{RESET}")
        # Skipped / skipping — dim gray
        msg = msg.replace("skipped", f"{DIM}skipped{RESET}")
        msg = msg.replace("skipping", f"{DIM}skipping{RESET}")
        record.msg_colored = msg
        return f"{record.asctime_dim} | {record.levelname_color} | {record.name_dim} | {record.msg_colored}"


def setup_logger(name: str = "bot", level: str = "") -> logging.Logger:
    if not level:
        level = os.environ.get("LOG_LEVEL", "INFO")
    logger = logging.getLogger(name)
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    if not logger.handlers:
        console = logging.StreamHandler(sys.stdout)
        console.setFormatter(ColorFormatter(
            "%(asctime)s | %(levelname)-8s | %(name)-12s | %(message)s",
            datefmt="%H:%M:%S",
        ))
        logger.addHandler(console)
        daily = logging.FileHandler(
            os.path.join(_LOG_DIR, f"bot_{datetime.now().strftime('%Y%m%d')}.log")
        )
        daily.setFormatter(logging.Formatter(
            "%(asctime)s | %(levelname)-8s | %(name)-12s | %(message)s",
        ))
        logger.addHandler(daily)
        live = RotatingFileHandler(
            os.path.join(_LOG_DIR, "bot_live.log"),
            maxBytes=LIVE_LOG_MAX_BYTES,
            backupCount=LIVE_LOG_BACKUPS,
        )
        live.setFormatter(logging.Formatter(
            "%(asctime)s | %(levelname)-8s | %(name)-12s | %(message)s",
            datefmt="%H:%M:%S",
        ))
        logger.addHandler(live)
    return logger
