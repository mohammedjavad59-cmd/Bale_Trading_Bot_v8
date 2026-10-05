from datetime import datetime, timezone
import requests


def _tf_minutes(value):
    s=str(value or "M5").upper().strip()
    if s.startswith("M"):
        return max(1,int(s[1:]))
    if s.startswith("H"):
        return max(1,int(s[1:])*60)
    if s.startswith("D"):
        return 1440
    return 5


def _aggregate(bars, timeframe):
    mins=_tf_minutes(timeframe)
    if mins <= 1:
        return bars
    out=[]; buckets={}
    for x in bars:
        dt=x["dt"]
        total=dt.hour*60+dt.minute
        bucket_total=(total//mins)*mins
        st=dt.replace(hour=bucket_total//60, minute=bucket_total%60, second=0, microsecond=0)
        buckets.setdefault(st,[]).append(x)
    for st, group in sorted(buckets.items()):
        if not group: continue
        out.append({
            "dt":st,
            "open":group[0]["open"],
            "high":max(x["high"] for x in group),
            "low":min(x["low"] for x in group),
            "close":group[-1]["close"],
            "volume":sum(float(x.get("volume",0) or 0) for x in group),
        })
    return out


class SP2LStrategy:
    key="sp2l"

    def __init__(self,cfg,settings,record_signal,bale_send,market_data=None):
        self.cfg=cfg; self.settings=settings; self.record_signal=record_signal; self.bale_send=bale_send; self.market_data=market_data
        self.last_signal_bar=None

    def cfgs(self):
        return self.settings.get("strategies",{}).get(self.key,{})

    def symbol(self):
        return str(self.cfgs().get("symbol","XAU/USD")).strip() or "XAU/USD"

    def timeframe(self):
        return str(self.cfgs().get("timeframe","M5")).upper()

    def fetch(self):
        symbol=self.symbol(); tf=self.timeframe()
        # Always request M1 and build the selected timeframe locally so all
        # configured timeframes use the same market-data source.
        need=max(500,int(self.cfgs().get("lookback_bars",300))+50)
        if self.market_data:
            values=self.market_data.get_bars(symbol,need,"UTC",self.timeframe())
        else:
            params={"symbol":symbol,"interval":"1min","outputsize":min(5000,max(500,need)),"timezone":"UTC","apikey":self.cfg["TWELVEDATA_API_KEY"]}
            r=requests.get("https://api.twelvedata.com/time_series",params=params,timeout=20); r.raise_for_status(); d=r.json()
            if "values" not in d: raise RuntimeError(d.get("message",str(d)))
            values=d["values"]
        bars=[]
        for x in values:
            dt=datetime.strptime(x["datetime"],"%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            bars.append({"dt":dt,"open":float(x["open"]),"high":float(x["high"]),"low":float(x["low"]),"close":float(x["close"]),"volume":float(x.get("volume",0) or 0)})
        return _aggregate(sorted(bars,key=lambda z:z["dt"]),tf)

    @staticmethod
    def body(c): return abs(c["close"]-c["open"])
    @staticmethod
    def rng(c): return c["high"]-c["low"]
    @staticmethod
    def bull(c): return c["close"]>c["open"]
    @staticmethod
    def bear(c): return c["close"]<c["open"]

    def gap_valid(self,base,spike,third,direction):
        mode=str(self.cfgs().get("gap_mode","three_candle")).lower()
        min_gap=float(self.cfgs().get("min_gap_points",0) or 0)*float(self.cfg.get("XAUUSD_POINT",0.01))
        if mode=="body":
            gap=(third["open"]-spike["close"]) if direction==1 else (spike["close"]-third["open"])
        else:
            gap=(third["low"]-spike["high"]) if direction==1 else (spike["low"]-third["high"])
        return gap>=min_gap and gap>0

    def run_once(self):
        s=self.cfgs()
        if not s.get("enabled",False): return
        bars=self.fetch()
        if len(bars)<20: return
        # Work only with completed candles. The newest bar is conservatively
        # treated as forming and excluded from detection.
        closed=bars[:-1]
        if len(closed)<10: return
        min_spike=max(1,int(s.get("min_spike_bars",1) or 1))
        max_spike=max(min_spike,int(s.get("max_spike_bars",3) or 3))
        body_ratio=float(s.get("min_spike_body_ratio",0.65) or 0.65)
        neighbor_mult=float(s.get("min_spike_vs_neighbors",1.50) or 1.50)
        max_wait=max(1,int(s.get("max_bars_after_pattern",20) or 20))
        direction_filter=str(s.get("direction","BOTH")).upper()
        one_per=bool(s.get("one_signal_per_candle",True))

        # Scan newest possible setup backwards. This mirrors the preliminary
        # MT5 EA: one dominant spike candle, followed by a P-Gap candle.
        for third_i in range(len(closed)-2,1,-1):
            third=closed[third_i]; spike=closed[third_i-1]; base=closed[third_i-2]
            body=self.body(spike); rng=self.rng(spike)
            if rng<=0 or body<=0 or body/rng<body_ratio: continue
            nb=(self.body(base)+self.body(third))/2.0
            if body < neighbor_mult*max(nb,1e-12): continue

            direction=0
            if self.bull(spike) and self.bull(third) and third["open"]>spike["high"] and third["close"]>spike["high"] and self.gap_valid(base,spike,third,1):
                direction=1
            elif self.bear(spike) and self.bear(third) and third["open"]<spike["low"] and third["close"]<spike["low"] and self.gap_valid(base,spike,third,-1):
                direction=-1
            if not direction: continue
            if direction_filter=="BUY" and direction!=1: continue
            if direction_filter=="SELL" and direction!=-1: continue

            # Find first valid 2Leg after the P-Gap candle.
            correction_idx=None; previous_idx=None
            end=max(1,third_i-max_wait)
            for i in range(third_i+1,len(closed)):
                corr=closed[i]
                prev=closed[i-1]
                if direction==1 and corr["low"]<=prev["low"]:
                    correction_idx=i; previous_idx=i-1; break
                if direction==-1 and corr["high"]>=prev["high"]:
                    correction_idx=i; previous_idx=i-1; break
                if i-third_i>=max_wait: break
            if correction_idx is None: continue

            prev=closed[previous_idx]
            entry=prev["low"] if direction==1 else prev["high"]
            sl=base["low"] if direction==1 else base["high"]
            if direction==1 and sl>=entry: continue
            if direction==-1 and sl<=entry: continue
            risk=abs(entry-sl)
            if risk<=0: continue

            rr=float(s.get("tp1_r",1.0) or 1.0)
            tp1=entry+risk*rr if direction==1 else entry-risk*rr
            tp2=None
            if bool(s.get("use_tp2",True)):
                tp2r=float(s.get("tp2_r",2.0) or 2.0)
                tp2=entry+risk*tp2r if direction==1 else entry-risk*tp2r
            second_entry=entry+(sl-entry)*float(s.get("second_entry_fraction",0.50) or 0.50) if bool(s.get("use_second_entry_2x",True)) else None

            trigger_bar=closed[correction_idx]
            key=trigger_bar["dt"].isoformat()
            if one_per and key==self.last_signal_bar: return
            self.last_signal_bar=key

            side="BUY" if direction==1 else "SELL"
            text=(f"🧠 استراتژی: SP2L (Spike–2Leg)\n"
                  f"{'🟢' if direction==1 else '🔴'} {self.symbol()} {side} SIGNAL\n\n"
                  f"Timeframe: {self.timeframe()}\n"
                  f"Entry: {entry:.5f}\n"
                  f"SL: {sl:.5f}\n"
                  f"TP1: {tp1:.5f}\n")
            if tp2 is not None: text+=f"TP2: {tp2:.5f}\n"
            if second_entry is not None:
                text += f"2X Entry: {second_entry:.5f}\n"
            text += (f"RR TP1: 1:{rr:g}\n"
                     f"P-Gap: confirmed\n"
                     f"2Leg candle: {trigger_bar['dt'].strftime('%Y-%m-%d %H:%M')} UTC\n\n"
                     f"⚠️ فقط سیگنال؛ معامله خودکار انجام نمی‌شود.")
            extra={"tp1":tp1,"tp2":tp2,"second_entry":second_entry,"timeframe":self.timeframe()}
            self.record_signal(self.key,self.symbol(),side,entry,sl,tp1,text,extra)
            return
