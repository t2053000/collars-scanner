"""
spreads.py
DEBUG MODE — looser filters, deeper strike coverage, debug counts.

For each ticker, for each of next 10 expirations:
  Bull call: BUY each OTM call strike, SELL next strike up (up to PAIRS_PER_DIR pairs)
  Bear put:  BUY each OTM put strike,  SELL next strike down (up to PAIRS_PER_DIR pairs)
"""

import logging
import math
from collections import Counter
from datetime import datetime

logger = logging.getLogger(__name__)

MAX_EXPIRATIONS  = 10
PAIRS_PER_DIR    = 35
MAX_MID_DEBIT    = 0.10
MAX_WORST_DEBIT  = 0.20
MIN_OI           =
