"""Swing strategy base classes and signal types."""
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class SignalType(Enum):
    LONG = "long"
    SHORT = "short"
    NONE = "none"


@dataclass
class EntrySignal:
    symbol: str
    signal_type: SignalType
    entry_price: float
    atr: float              # ATR at entry bar — execution uses it to size SL/TP/trail
    timestamp: datetime
    reason: str = ""
    metadata: dict = field(default_factory=dict)
