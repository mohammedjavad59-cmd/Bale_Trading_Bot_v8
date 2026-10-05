"""Template for strategy plugins generated for Bale Trading Bot V2.
Send this file back to the bot from the owner account after replacing the
strategy logic. Do not change the required metadata names.
"""
from datetime import datetime, timezone

STRATEGY_KEY = "example_strategy"
STRATEGY_NAME = "Example Strategy"
STRATEGY_DESCRIPTION = "توضیح کوتاه استراتژی."
STRATEGY_SECTIONS = [
    ("🎯 منطق", ["توضیح منطق ورود و خروج."]),
    ("🛡 مدیریت معامله", ["توضیح Entry / SL / TP."]),
]
DEFAULT_SETTINGS = {
    "enabled": True,
    "symbol": "XAU/USD",
    "timeframe": "M5",
    "scan_interval": 20,
    "cooldown": 0,
    "max_signals_per_day": None,
    "one_signal_per_candle": True,
}

class ExampleStrategy:
    key = STRATEGY_KEY

    def __init__(self, cfg, settings, record_signal, bale_send, market_data=None):
        self.cfg = cfg
        self.settings = settings
        self.record_signal = record_signal
        self.bale_send = bale_send
        self.market_data = market_data
        self.last_signal_bar = None
        # Optional diagnostic text shown by Bale > 🩺 عیب‌یابی.
        self.last_diagnostic = "هنوز اسکن نشده"

    def cfgs(self):
        return self.settings.get("strategies", {}).get(self.key, {})

    def run_once(self):
        """Detect ONE completed setup and call record_signal(...).

        Required call:
        record_signal(self.key, symbol, direction, entry, sl, tp, text, extra)
        """
        # Before every return, plugins should set:
        # self.last_diagnostic = "🟡 ستاپی پیدا نشد: ..."
        # On a valid setup set it to "✅ موقعیت ورود پیدا شد".
        # The main bot also reports exceptions separately as technical errors.
        return

STRATEGY_CLASS = ExampleStrategy
