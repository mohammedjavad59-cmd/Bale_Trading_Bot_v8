"""Bale Trading Bot V4 plugin: تست"""
from datetime import datetime, timezone

STRATEGY_KEY = "test"
STRATEGY_NAME = "تست"
STRATEGY_DESCRIPTION = "آزمون سیگنال روی طلا؛ جهت هر کندل بسته‌شده M1 و RR برابر 1:2."
STRATEGY_SECTIONS = [
    ("🎯 منطق ورود", [
        "نماد XAU/USD و تایم‌فریم M1.",
        "فقط آخرین کندل کاملاً بسته‌شده بررسی می‌شود.",
        "کندل مثبت (Close > Open) → BUY؛ کندل منفی (Close < Open) → SELL؛ دوجی → بدون سیگنال.",
    ]),
    ("🛡 مدیریت معامله", [
        "Entry = Close کندل.",
        "BUY: SL روی Low کندل؛ SELL: SL روی High کندل.",
        "TP = 2R و یک سیگنال برای هر کندل.",
    ]),
]
DEFAULT_SETTINGS = {
    "enabled": True, "symbol": "XAU/USD", "timeframe": "M1",
    "scan_interval": 10, "cooldown": 0, "max_signals_per_day": None,
    "one_signal_per_candle": True,
}

def _dt(bar):
    raw = bar.get("datetime_utc") or bar.get("datetime") or bar.get("dt")
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    if raw is None: return None
    text = str(raw).replace("Z", "+00:00")
    try: dt = datetime.fromisoformat(text)
    except Exception: dt = datetime.strptime(str(raw), "%Y-%m-%d %H:%M:%S")
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)

class TestStrategy:
    key = STRATEGY_KEY
    def __init__(self, cfg, settings, record_signal, bale_send, market_data=None):
        self.cfg=cfg; self.settings=settings; self.record_signal=record_signal; self.bale_send=bale_send; self.market_data=market_data; self.last_signal_bar=None; self.last_diagnostic="هنوز اسکن نشده"
    def cfgs(self): return self.settings.get("strategies", {}).get(self.key, {})
    def run_once(self):
        s=self.cfgs()
        if not s.get("enabled", True): self.last_diagnostic="⚪ استراتژی غیرفعال است"; return
        if str(s.get("timeframe","M1")).upper() != "M1": self.last_diagnostic="🟠 تایم‌فریم تست باید M1 باشد"; return
        if not self.market_data: self.last_diagnostic="🔴 Market Data Manager در دسترس نیست"; return
        bars=self.market_data.get_bars(str(s.get("symbol","XAU/USD")), 20, "UTC", "M1")
        if not bars: self.last_diagnostic="🔴 داده بازار دریافت نشد"; return
        now=datetime.now(timezone.utc).replace(second=0,microsecond=0)
        closed=[]
        for b in bars:
            dt=_dt(b)
            if dt and dt < now: closed.append((dt,b))
        if not closed: self.last_diagnostic="🟡 هنوز کندل M1 بسته‌شده در داده نیست"; return
        dt,c=closed[-1]
        if self.last_signal_bar == dt: self.last_diagnostic="🟡 این کندل قبلاً بررسی شده است"; return
        self.last_signal_bar=dt
        o=float(c["open"]); h=float(c["high"]); l=float(c["low"]); close=float(c["close"])
        if close == o: self.last_diagnostic="🟡 کندل دوجی است؛ ستاپی وجود ندارد"; return
        symbol=str(s.get("symbol","XAU/USD"))
        if close > o:
            direction="BUY"; entry=close; sl=l; risk=entry-sl; tp=entry+2*risk
        else:
            direction="SELL"; entry=close; sl=h; risk=sl-entry; tp=entry-2*risk
        if risk <= 0: self.last_diagnostic="🟠 ریسک کندل معتبر نیست"; return
        self.last_diagnostic="✅ موقعیت ورود پیدا شد"
        text=(f"🧠 استراتژی: تست\n{'🟢' if direction=='BUY' else '🔴'} {symbol} {direction} SIGNAL\n\n"
              f"Timeframe: M1\nEntry: {entry:.5f}\nSL: {sl:.5f}\nTP: {tp:.5f}\nRR: 1:2\n"
              f"Candle: {dt.strftime('%Y-%m-%d %H:%M')} UTC\n\n⚠️ فقط سیگنال؛ معامله خودکار انجام نمی‌شود.")
        self.record_signal(self.key,symbol,direction,entry,sl,tp,text,{"timeframe":"M1","rr":2.0})

STRATEGY_CLASS = TestStrategy
