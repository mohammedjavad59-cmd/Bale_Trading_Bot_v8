"""Bale Trading Bot V4 strategy plugin: Pro BTB.

Professional Back To Breakeven (Pro BTB)

Core concept:
    Breakout -> return/retest of the broken level -> continuation.

The source material supplied for this strategy defines the six entry styles,
but does not give a fully mechanical formula for detecting every possible
"important level", C/H, or confirmation candle. This plugin therefore uses a
conservative, configurable price-action interpretation:

- a key level is the recent lookback high/low;
- breakout requires a completed candle to close beyond that level;
- C/H for a bullish breakout means the breakout candle close-to-high zone;
  for bearish setups the mirrored close-to-low zone is used;
- retest means price returns to the broken level/entry zone;
- all signals use completed candles only;
- default target is at least 1:2 RR.

The implementation is intentionally configurable so the bot can be tuned
without changing this file if later source material provides more exact
mechanical definitions.
"""

from datetime import datetime, timezone

STRATEGY_KEY = "pro_btb"
STRATEGY_NAME = "Pro BTB"
STRATEGY_DESCRIPTION = (
    "Professional Back To Breakeven؛ استراتژی پرایس‌اکشن بر پایه Breakout، "
    "بازگشت به سطح شکسته‌شده و ادامه روند با ۶ روش ورود استاندارد."
)

STRATEGY_SECTIONS = [
    ("🎯 منطق استراتژی", [
        "شناسایی Breakout معتبر روی یک سطح مهم، انتظار برای Back To Breakeven/Retest و ورود در جهت شکست.",
        "بهترین شرایط: شکست حمایت/مقاومت مهم، هم‌جهتی با روند تایم‌فریم بالاتر و وجود مومنتوم واضح در کندل برگشتی.",
        "مبنای اصلی ورود: بازگشت قیمت به سطح Break یا محدوده C تا H/L و سپس ادامه حرکت.",
    ]),
    ("📌 شش روش ورود", [
        "1) Buy/Sell Limit در محدوده C تا H/L کندل Break.",
        "2) Buy/Sell Stop روی H/L محدوده برگشت؛ یا ورود دستی پس از بسته‌شدن کندل برگشتی.",
        "3) Buy/Sell Market بعد از بسته‌شدن کندل برگشتی.",
        "4) Buy/Sell Limit روی سطح Break.",
        "5) Buy/Sell Stop روی سقف/کف کندل Break.",
        "6) Buy/Sell Market با تأیید کندلی.",
    ]),
    ("🛡 مدیریت معامله", [
        "حد ضرر در سمت مخالف ساختار برگشت/Break قرار می‌گیرد.",
        "حد سود با نسبت ریسک به ریوارد حداقل 1:2 محاسبه می‌شود.",
        "فقط کندل‌های کاملاً بسته‌شده برای تشخیص سیگنال استفاده می‌شوند.",
    ]),
    ("📈 شرایط مناسب", [
        "بازارهای پرنوسان و نقدشونده مانند Forex، Gold، Indices و Crypto.",
        "M5 و M15 به‌عنوان تایم‌فریم‌های پیشنهادی، با امکان استفاده در تایم‌فریم‌های بالاتر.",
        "Pro BTB نسبت به SP2L دیرتر وارد می‌شود و بر Breakout + Retest تمرکز دارد.",
    ]),
]

DEFAULT_SETTINGS = {
    "enabled": True,
    "symbol": "XAU/USD",
    "timeframe": "M5",
    "scan_interval": 10,
    "cooldown": 0,
    "max_signals_per_day": None,
    "one_signal_per_candle": True,
    "direction": "BOTH",
    "entry_mode": "AUTO",
    "lookback_bars": 30,
    "breakout_lookback": 20,
    "breakout_buffer_points": 0,
    "retest_max_bars": 12,
    "retest_tolerance_points": 5,
    "min_breakout_body_ratio": 0.50,
    "require_breakout_close": True,
    "require_retest": True,
    "confirmation_min_body_ratio": 0.45,
    "sl_mode": "RETEST_EXTREME",
    "sl_buffer_points": 0,
    "min_rr": 2.0,
    "tp_r": 2.0,
}


def _dt(bar):
    raw = bar.get("datetime_utc") or bar.get("datetime") or bar.get("dt")
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    if raw is None:
        return None
    text = str(raw).replace("Z", "+00:00")
    try:
        value = datetime.fromisoformat(text)
    except Exception:
        try:
            value = datetime.strptime(str(raw), "%Y-%m-%d %H:%M:%S")
        except Exception:
            return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _tf_minutes(value):
    s = str(value or "M5").upper().strip()
    if s.startswith("M"):
        return max(1, int(s[1:] or 5))
    if s.startswith("H"):
        return max(1, int(s[1:] or 1) * 60)
    if s.startswith("D"):
        return 1440
    return 5


def _aggregate_m1(bars, timeframe):
    """Aggregate M1 bars when the provider does not directly return the TF."""
    mins = _tf_minutes(timeframe)
    if mins <= 1:
        return bars
    buckets = {}
    for x in bars:
        dt = x["dt"]
        total = dt.hour * 60 + dt.minute
        bucket = (total // mins) * mins
        st = dt.replace(hour=bucket // 60, minute=bucket % 60, second=0, microsecond=0)
        buckets.setdefault(st, []).append(x)
    out = []
    for st, group in sorted(buckets.items()):
        if not group:
            continue
        out.append({
            "dt": st,
            "open": group[0]["open"],
            "high": max(x["high"] for x in group),
            "low": min(x["low"] for x in group),
            "close": group[-1]["close"],
            "volume": sum(float(x.get("volume", 0) or 0) for x in group),
        })
    return out


class ProBTBStrategy:
    key = STRATEGY_KEY

    def __init__(self, cfg, settings, record_signal, bale_send, market_data=None):
        self.cfg = cfg
        self.settings = settings
        self.record_signal = record_signal
        self.bale_send = bale_send
        self.market_data = market_data
        self.last_signal_bar = None
        self.last_diagnostic = "هنوز اسکن نشده"

    def cfgs(self):
        return self.settings.get("strategies", {}).get(self.key, {})

    def symbol(self):
        return str(self.cfgs().get("symbol", "XAU/USD")).strip() or "XAU/USD"

    def timeframe(self):
        return str(self.cfgs().get("timeframe", "M5")).upper().strip() or "M5"

    def point(self):
        """Use configured point; otherwise use a sensible symbol fallback."""
        try:
            p = float(self.cfgs().get("point", 0) or 0)
            if p > 0:
                return p
        except Exception:
            pass
        try:
            p = float(self.cfg.get("XAUUSD_POINT", 0) or 0)
            if p > 0:
                return p
        except Exception:
            pass
        return 0.01 if "XAU" in self.symbol().upper() else 0.0001

    @staticmethod
    def body(c):
        return abs(c["close"] - c["open"])

    @staticmethod
    def rng(c):
        return max(0.0, c["high"] - c["low"])

    @staticmethod
    def bull(c):
        return c["close"] > c["open"]

    @staticmethod
    def bear(c):
        return c["close"] < c["open"]

    def _fetch(self):
        s = self.cfgs()
        tf = self.timeframe()
        lookback = max(30, int(s.get("lookback_bars", 30) or 30))
        need = max(lookback + int(s.get("retest_max_bars", 12) or 12) + 20, 80)
        if not self.market_data:
            self.last_diagnostic = "🔴 Market Data Manager در دسترس نیست"
            return []

        # Ask the manager for the configured TF. The V4 manager/provider layer
        # is responsible for provider failover and fresh-data checks.
        values = self.market_data.get_bars(self.symbol(), need, "UTC", tf)
        if not values:
            self.last_diagnostic = "🔴 داده بازار دریافت نشد"
            return []

        bars = []
        for x in values:
            dt = _dt(x)
            try:
                bars.append({
                    "dt": dt,
                    "open": float(x["open"]),
                    "high": float(x["high"]),
                    "low": float(x["low"]),
                    "close": float(x["close"]),
                    "volume": float(x.get("volume", 0) or 0),
                })
            except Exception:
                continue
        bars = [b for b in bars if b["dt"] is not None]
        bars.sort(key=lambda z: z["dt"])
        if not bars:
            self.last_diagnostic = "🔴 داده معتبر بازار وجود ندارد"
            return []
        return _aggregate_m1(bars, tf)

    def _is_completed(self, dt):
        mins = _tf_minutes(self.timeframe())
        now = datetime.now(timezone.utc)
        bucket_minute = (now.minute // mins) * mins
        current_open = now.replace(minute=bucket_minute, second=0, microsecond=0)
        return dt < current_open

    def _breakout_candidates(self, closed):
        """Return the newest valid breakout and its pre-break key level."""
        s = self.cfgs()
        n = max(5, int(s.get("breakout_lookback", 20) or 20))
        if len(closed) < n + 2:
            return []
        buffer = float(s.get("breakout_buffer_points", 0) or 0) * self.point()
        min_body_ratio = float(s.get("min_breakout_body_ratio", 0.50) or 0.50)
        candidates = []

        # Search backwards so the newest actionable breakout wins.
        for i in range(len(closed) - 1, n - 1, -1):
            b = closed[i]
            prior = closed[i - n:i]
            level_high = max(x["high"] for x in prior)
            level_low = min(x["low"] for x in prior)
            r = self.rng(b)
            if r <= 0:
                continue
            body_ratio = self.body(b) / r

            if self.bull(b) and body_ratio >= min_body_ratio and b["close"] > level_high + buffer:
                candidates.append({"index": i, "direction": 1, "level": level_high, "break": b})
                continue
            if self.bear(b) and body_ratio >= min_body_ratio and b["close"] < level_low - buffer:
                candidates.append({"index": i, "direction": -1, "level": level_low, "break": b})
        return candidates

    def _find_retest(self, closed, candidate):
        s = self.cfgs()
        i = candidate["index"]
        direction = candidate["direction"]
        level = candidate["level"]
        max_bars = max(1, int(s.get("retest_max_bars", 12) or 12))
        tol = float(s.get("retest_tolerance_points", 5) or 5) * self.point()
        end = min(len(closed) - 1, i + max_bars)
        if i + 1 > end:
            return None

        for j in range(i + 1, end + 1):
            c = closed[j]
            touches_level = c["low"] <= level + tol if direction == 1 else c["high"] >= level - tol
            if not touches_level:
                continue
            return {"index": j, "candle": c, "level": level}
        return None

    def _entry_zone(self, break_candle, direction):
        # The supplied Pro BTB image labels the bullish zone as C -> H.
        # For a bearish breakout it is mirrored as C -> L.
        if direction == 1:
            return min(break_candle["close"], break_candle["high"]), max(break_candle["close"], break_candle["high"])
        return min(break_candle["low"], break_candle["close"]), max(break_candle["low"], break_candle["close"])

    def _confirmation(self, candle, direction):
        r = self.rng(candle)
        if r <= 0:
            return False
        ratio = self.body(candle) / r
        min_ratio = float(self.cfgs().get("confirmation_min_body_ratio", 0.45) or 0.45)
        return ratio >= min_ratio and (self.bull(candle) if direction == 1 else self.bear(candle))

    def _stop(self, closed, breakout_i, retest_i, direction):
        s = self.cfgs()
        mode = str(s.get("sl_mode", "RETEST_EXTREME")).upper()
        buffer = float(s.get("sl_buffer_points", 0) or 0) * self.point()
        break_candle = closed[breakout_i]
        retest_candle = closed[retest_i]

        if mode == "BREAK_CANDLE":
            sl = break_candle["low"] if direction == 1 else break_candle["high"]
        elif mode == "LEVEL":
            sl = closed[breakout_i - 1]["low"] if direction == 1 else closed[breakout_i - 1]["high"]
        else:
            # Conservative interpretation of the images: stop beyond the
            # retest structure, not inside the entry zone.
            segment = closed[breakout_i:retest_i + 1]
            sl = min(x["low"] for x in segment) if direction == 1 else max(x["high"] for x in segment)

        return sl - buffer if direction == 1 else sl + buffer

    def _make_trade(self, closed, candidate, retest):
        s = self.cfgs()
        direction = candidate["direction"]
        side = "BUY" if direction == 1 else "SELL"
        b = candidate["break"]
        retest_i = retest["index"]
        retest_c = retest["candle"]
        level = candidate["level"]
        mode = str(s.get("entry_mode", "AUTO")).upper().strip()
        if mode == "AUTO":
            mode = "BUY_MARKET_RETEST" if direction == 1 else "SELL_MARKET_RETEST"
        if direction == -1 and mode.startswith("BUY_"):
            mode = mode.replace("BUY_", "SELL_", 1)
        if direction == 1 and mode.startswith("SELL_"):
            mode = mode.replace("SELL_", "BUY_", 1)

        zone_low, zone_high = self._entry_zone(b, direction)
        h_or_l = b["high"] if direction == 1 else b["low"]
        sl = self._stop(closed, candidate["index"], retest_i, direction)

        # The six source methods. For pending orders the reported Entry is the
        # pending trigger; for Market methods it is the actual confirmation/
        # retest candle close used by the signal.
        if mode.endswith("LIMIT_C_H"):
            entry = (zone_low + zone_high) / 2.0
            method_label = "Limit روی محدوده C تا H/L"
        elif mode.endswith("STOP_H"):
            entry = h_or_l
            method_label = "Stop روی H/L"
        elif mode.endswith("MARKET_RETEST"):
            entry = retest_c["close"]
            method_label = "Market بعد از بسته‌شدن کندل برگشتی"
        elif mode.endswith("LIMIT_BREAK"):
            entry = level
            method_label = "Limit روی سطح Break"
        elif mode.endswith("STOP_BREAK_HIGH"):
            entry = h_or_l
            method_label = "Stop روی سقف/کف کندل Break"
        elif mode.endswith("MARKET_CONFIRMATION"):
            if not self._confirmation(retest_c, direction):
                return None, "🟡 Retest پیدا شد ولی کندل تأیید مومنتوم کافی ندارد"
            entry = retest_c["close"]
            method_label = "Market با تأیید کندلی"
        else:
            return None, f"🟠 entry_mode نامعتبر است: {mode}"

        if direction == 1 and sl >= entry:
            return None, "🟡 SL معتبر برای BUY تشکیل نشد"
        if direction == -1 and sl <= entry:
            return None, "🟡 SL معتبر برای SELL تشکیل نشد"

        risk = abs(entry - sl)
        if risk <= 0:
            return None, "🟡 ریسک معامله صفر است"

        min_rr = max(2.0, float(s.get("min_rr", 2.0) or 2.0))
        rr = max(min_rr, float(s.get("tp_r", min_rr) or min_rr))
        tp = entry + risk * rr if direction == 1 else entry - risk * rr

        return {
            "side": side,
            "entry": entry,
            "sl": sl,
            "tp": tp,
            "rr": rr,
            "mode": mode,
            "method_label": method_label,
            "level": level,
            "break_time": b["dt"],
            "retest_time": retest_c["dt"],
            "break_high": b["high"],
            "break_low": b["low"],
        }, None

    def run_once(self):
        s = self.cfgs()
        if not s.get("enabled", True):
            self.last_diagnostic = "⚪ استراتژی غیرفعال است"
            return
        if not self.market_data:
            self.last_diagnostic = "🔴 Market Data Manager در دسترس نیست"
            return

        bars = self._fetch()
        if not bars:
            return

        # Only completed candles participate in the strategy.
        closed = [b for b in bars if self._is_completed(b["dt"])]
        if len(closed) < max(30, int(s.get("breakout_lookback", 20) or 20) + 5):
            self.last_diagnostic = "🟡 داده کافی برای تشخیص Breakout وجود ندارد"
            return

        candidates = self._breakout_candidates(closed)
        if not candidates:
            self.last_diagnostic = "🟡 Breakout معتبر روی سطح مهم پیدا نشد"
            return

        direction_filter = str(s.get("direction", "BOTH")).upper().strip()
        candidate = None
        for c in candidates:
            if direction_filter == "BUY" and c["direction"] != 1:
                continue
            if direction_filter == "SELL" and c["direction"] != -1:
                continue
            candidate = c
            break
        if candidate is None:
            self.last_diagnostic = "🟡 Breakout در جهت انتخاب‌شده وجود ندارد"
            return

        retest = self._find_retest(closed, candidate)
        if not retest:
            self.last_diagnostic = "🟡 Breakout پیدا شد اما هنوز Retest/Back To Breakeven رخ نداده است"
            return

        trade, reason = self._make_trade(closed, candidate, retest)
        if not trade:
            self.last_diagnostic = reason or "🟡 ستاپ ورود کامل نیست"
            return

        trigger_bar = retest["candle"]
        key = trigger_bar["dt"].isoformat()
        if bool(s.get("one_signal_per_candle", True)) and key == self.last_signal_bar:
            self.last_diagnostic = "🟡 این کندل قبلاً بررسی شده است"
            return
        self.last_signal_bar = key

        side = trade["side"]
        icon = "🟢" if side == "BUY" else "🔴"
        symbol = self.symbol()
        text = (
            f"🧠 استراتژی: Pro BTB\n"
            f"{icon} {symbol} {side} SIGNAL\n\n"
            f"روش ورود: {trade['method_label']}\n"
            f"Timeframe: {self.timeframe()}\n"
            f"Entry: {trade['entry']:.5f}\n"
            f"SL: {trade['sl']:.5f}\n"
            f"TP: {trade['tp']:.5f}\n"
            f"RR: 1:{trade['rr']:g}\n"
            f"Break Level: {trade['level']:.5f}\n"
            f"Break: {trade['break_time'].strftime('%Y-%m-%d %H:%M')} UTC\n"
            f"Retest: {trade['retest_time'].strftime('%Y-%m-%d %H:%M')} UTC\n\n"
            f"⚠️ فقط سیگنال؛ معامله خودکار انجام نمی‌شود."
        )
        extra = {
            "timeframe": self.timeframe(),
            "rr": trade["rr"],
            "entry_mode": trade["mode"],
            "break_level": trade["level"],
            "break_time_utc": trade["break_time"].isoformat(),
            "retest_time_utc": trade["retest_time"].isoformat(),
            "break_high": trade["break_high"],
            "break_low": trade["break_low"],
        }
        self.last_diagnostic = "✅ موقعیت Pro BTB پیدا شد"
        self.record_signal(self.key, symbol, side, trade["entry"], trade["sl"], trade["tp"], text, extra)


STRATEGY_CLASS = ProBTBStrategy
