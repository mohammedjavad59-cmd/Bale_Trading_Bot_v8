import json, threading, time, random, os
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
import requests


class MarketDataRateLimit(Exception):
    pass


class MarketDataUnavailable(Exception):
    pass


class MarketDataCache:
    """Central market-data manager.

    Provider order, enabled flags and cooldowns are controlled by SETTINGS['providers'].
    Secrets are read only from environment variables / runtime CFG and are never persisted.
    """
    TF_MAP = {"M1":"1min","M5":"5min","M15":"15min","M30":"30min","H1":"1h","H2":"2h","H4":"4h","D1":"1day"}
    ALLTICK_TYPES = {"M1":1,"M5":2,"M15":3,"M30":4,"H1":5,"H2":6,"H4":7,"D1":8}
    DEFAULT_PROVIDERS = {
        "oanda":{"name":"OANDA","env_key":"OANDA_API_TOKEN","enabled":True,"priority":1},
        "twelvedata":{"name":"Twelve Data","env_key":"TWELVEDATA_API_KEY","enabled":True,"priority":2},
        "alltick":{"name":"AllTick","env_key":"ALLTICK_API_TOKEN","enabled":True,"priority":3},
        "tradermade":{"name":"TraderMade","env_key":"TRADERMADE_API_KEY","enabled":True,"priority":4},
        "finnhub":{"name":"Finnhub","env_key":"FINNHUB_API_KEY","enabled":True,"priority":5},
    }

    def __init__(self, cfg, settings=None):
        self.cfg = cfg
        self.settings = settings if settings is not None else {}
        self.lock = threading.RLock()
        self.bars_cache = {}
        self.quote_cache = {}
        self.inflight = {}
        self.provider_state = {}
        self.request_count = 0
        self.cache_hits = 0
        self.cache_misses = 0
        self.cooldowns = {}
        self.session = requests.Session()
        self._ensure_provider_settings()

    def _ensure_provider_settings(self):
        providers = self.settings.setdefault("providers", {})
        for key, default in self.DEFAULT_PROVIDERS.items():
            cur = providers.setdefault(key, dict(default))
            for k,v in default.items(): cur.setdefault(k,v)
            self.provider_state.setdefault(key, {"status":"unknown","last_success":None,"last_error":None,"errors":0,"rate_limits":0,"requests":0})

    def provider_items(self):
        self._ensure_provider_settings()
        p=self.settings["providers"]
        return sorted(p.items(), key=lambda kv:int(kv[1].get("priority",999)))

    def provider_status(self):
        out=[]
        now=time.monotonic()
        for key,cfg in self.provider_items():
            env=cfg.get("env_key")
            configured=bool(os.getenv(env) or self.cfg.get(env)) if env else False
            st=dict(self.provider_state.get(key,{}))
            cd=max(0,int(self.cooldowns.get(key,0)-now))
            st.update({"key":key,"name":cfg.get("name",key),"enabled":bool(cfg.get("enabled",True)),"priority":cfg.get("priority"),"configured":configured,"cooldown_seconds":cd})
            out.append(st)
        return out

    @staticmethod
    def _canonical(symbol):
        s=str(symbol or "").strip().upper().replace(" ","")
        aliases={"XAUUSD":"XAU/USD","GOLD":"XAU/USD","GBPJPY":"GBP/JPY","EURUSD":"EUR/USD","USDJPY":"USD/JPY","GBPUSD":"GBP/USD"}
        return aliases.get(s,s)

    @staticmethod
    def _pair(symbol):
        s=MarketDataCache._canonical(symbol).replace("/","")
        return s[:3],s[3:]

    def _mapped_symbol(self, provider, symbol):
        s=self._canonical(symbol)
        custom=((self.settings.get("providers",{}).get(provider,{}) or {}).get("symbol_map") or {})
        if s in custom: return str(custom[s])
        maps={
            "oanda":{"XAU/USD":"XAU_USD","GBP/JPY":"GBP_JPY","EUR/USD":"EUR_USD","USD/JPY":"USD_JPY","GBP/USD":"GBP_USD"},
            "twelvedata":{"XAU/USD":"XAU/USD","GBP/JPY":"GBP/JPY","EUR/USD":"EUR/USD","USD/JPY":"USD/JPY","GBP/USD":"GBP/USD"},
            "alltick":{"XAU/USD":"GOLD","GBP/JPY":"GBPJPY","EUR/USD":"EURUSD","USD/JPY":"USDJPY","GBP/USD":"GBPUSD"},
            "tradermade":{"XAU/USD":"XAUUSD","GBP/JPY":"GBPJPY","EUR/USD":"EURUSD","USD/JPY":"USDJPY","GBP/USD":"GBPUSD"},
            "finnhub":{"XAU/USD":"OANDA:XAU_USD","GBP/JPY":"OANDA:GBP_JPY","EUR/USD":"OANDA:EUR_USD","USD/JPY":"OANDA:USD_JPY","GBP/USD":"OANDA:GBP_USD"},
        }
        return maps.get(provider,{}).get(s,s.replace("/",""))

    def _secret(self, key):
        env=self.settings.get("providers",{}).get(key,{}).get("env_key","")
        return str(os.getenv(env) or self.cfg.get(env) or "").strip()

    def _ttl(self, timeframe):
        tf=str(timeframe).upper()
        return {"M1":55,"M5":240,"M15":720,"M30":1400,"H1":3000}.get(tf,3000)

    def _fresh(self, item, timeframe):
        return item and time.monotonic()-item[0] < self._ttl(timeframe) and item[1]

    def _set_success(self,p):
        st=self.provider_state[p]; st["status"]="healthy"; st["last_success"]=datetime.now(timezone.utc).isoformat(); st["last_error"]=None; st["requests"]+=1

    @staticmethod
    def _sanitize_error(exc):
        text=str(exc)
        # Never allow API credentials to leak through provider errors/logs.
        import re
        text=re.sub(r"([?&](?:token|apikey|api_key|key)=)[^&\s]+", r"\1[REDACTED]", text, flags=re.I)
        text=re.sub(r"(Bearer\s+)[^\s]+", r"\1[REDACTED]", text, flags=re.I)
        return text

    def _set_error(self,p,e,rate=False):
        st=self.provider_state[p]; st["status"]="rate_limited" if rate else "error"; st["last_error"]=self._sanitize_error(e); st["errors"]+=1; st["requests"]+=1
        if rate: st["rate_limits"]+=1

    def _request(self, provider, method, url, **kwargs):
        with self.lock: self.request_count += 1
        r=self.session.request(method,url,timeout=20,**kwargs)
        if r.status_code==429:
            retry=60
            try: retry=max(10,int(r.headers.get("Retry-After","60")))
            except Exception: pass
            self.cooldowns[provider]=time.monotonic()+min(retry,600)+random.uniform(0,3)
            self._set_error(provider,f"HTTP 429; cooldown={retry}s",True)
            raise MarketDataRateLimit(f"{provider}: HTTP 429")
        if r.status_code in (401,403,404):
            # Auth/permission/unsupported-endpoint failures should trigger failover,
            # but the provider should not be hammered again on every scan.
            cooldown={401:900,403:1800,404:1800}[r.status_code]
            self.cooldowns[provider]=time.monotonic()+cooldown+random.uniform(0,5)
            msg=f"HTTP {r.status_code}; provider temporarily skipped for {cooldown}s"
            self._set_error(provider,msg,False)
            raise MarketDataUnavailable(f"{provider}: HTTP {r.status_code}")
        try:
            r.raise_for_status()
        except Exception as exc:
            safe=self._sanitize_error(exc)
            raise MarketDataUnavailable(safe) from exc
        return r

    def _normalize(self, values, timezone_name):
        out=[]
        for x in values or []:
            try:
                raw=x.get("datetime_utc") or x.get("datetime") or x.get("time") or x.get("t")
                if raw is None: continue
                if isinstance(raw,(int,float)):
                    ts=float(raw); ts=ts/1000 if ts>10_000_000_000 else ts; dt=datetime.fromtimestamp(ts,timezone.utc)
                else:
                    text=str(raw).replace("Z","+00:00")
                    try: dt=datetime.fromisoformat(text)
                    except Exception: dt=datetime.strptime(str(raw),"%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                    if dt.tzinfo is None: dt=dt.replace(tzinfo=timezone.utc)
                    dt=dt.astimezone(timezone.utc)
                def num(*ks):
                    for k in ks:
                        if k in x and x[k] not in (None,""): return float(x[k])
                    raise ValueError
                out.append({"datetime_utc":dt.isoformat(),"datetime":dt.strftime("%Y-%m-%d %H:%M:%S"),"open":num("open","o"),"high":num("high","h"),"low":num("low","l"),"close":num("close","c"),"volume":float(x.get("volume",x.get("v",0)) or 0)})
            except Exception: continue
        out.sort(key=lambda z:z["datetime_utc"])
        if timezone_name and timezone_name.upper()!="UTC":
            try:
                z=ZoneInfo(timezone_name)
                for x in out:
                    dt=datetime.fromisoformat(x["datetime_utc"]).astimezone(z); x["datetime"]=dt.strftime("%Y-%m-%d %H:%M:%S")
            except Exception: pass
        return out

    def _oanda(self,symbol,outputsize,timeframe,timezone_name):
        token=self._secret("oanda")
        if not token: raise MarketDataUnavailable("OANDA_API_TOKEN تنظیم نشده است")
        env=str(self.cfg.get("OANDA_ENVIRONMENT") or os.getenv("OANDA_ENVIRONMENT") or "practice").lower()
        host="https://api-fxpractice.oanda.com" if env!="live" else "https://api-fxtrade.oanda.com"
        gran={"M1":"M1","M5":"M5","M15":"M15","M30":"M30","H1":"H1","H2":"H2","H4":"H4","D1":"D"}.get(str(timeframe).upper(),"M1")
        url=f"{host}/v3/instruments/{self._mapped_symbol('oanda',symbol)}/candles"
        r=self._request("oanda","GET",url,headers={"Authorization":f"Bearer {token}","Accept-Datetime-Format":"RFC3339"},params={"granularity":gran,"count":min(max(int(outputsize),2),5000),"price":"M"})
        d=r.json(); vals=[]
        for c in d.get("candles",[]):
            if not c.get("complete",True): continue
            mid=c.get("mid") or {}; vals.append({"datetime_utc":c.get("time"),"open":mid.get("o"),"high":mid.get("h"),"low":mid.get("l"),"close":mid.get("c"),"volume":c.get("volume",0)})
        vals=self._normalize(vals,timezone_name)
        if not vals: raise MarketDataUnavailable("OANDA داده کامل برنگرداند")
        return vals

    def _twelve(self,symbol,outputsize,timeframe,timezone_name):
        key=self._secret("twelvedata")
        if not key: raise MarketDataUnavailable("TWELVEDATA_API_KEY تنظیم نشده است")
        interval=self.TF_MAP.get(str(timeframe).upper(),"1min")
        r=self._request("twelvedata","GET","https://api.twelvedata.com/time_series",params={"symbol":self._mapped_symbol("twelvedata",symbol),"interval":interval,"outputsize":min(max(int(outputsize),2),5000),"timezone":timezone_name,"apikey":key})
        d=r.json()
        if "values" not in d: raise MarketDataUnavailable(f"Twelve Data: {d.get('message',str(d))}")
        return self._normalize(list(reversed(d["values"])),timezone_name)

    def _alltick(self,symbol,outputsize,timeframe,timezone_name):
        token=self._secret("alltick")
        if not token: raise MarketDataUnavailable("ALLTICK_API_TOKEN تنظیم نشده است")
        q={"trace":f"bot_{int(time.time()*1000)}","data":{"code":self._mapped_symbol("alltick",symbol),"kline_type":self.ALLTICK_TYPES.get(str(timeframe).upper(),1),"kline_timestamp_end":0,"query_kline_num":min(max(int(outputsize),2),500),"adjust_type":0}}
        url=self.cfg.get("ALLTICK_KLINE_URL","https://quote.alltick.co/quote-b-api/kline")
        r=self._request("alltick","GET",url,params={"token":token,"query":json.dumps(q,separators=(",",":"))})
        d=r.json(); vals=(d.get("data") or {}).get("kline_list") or (d.get("data") or {}).get("list") or []
        if d.get("ret") not in (None,200): raise MarketDataUnavailable(f"AllTick: {d.get('msg','request failed')}")
        return self._normalize(vals,timezone_name)

    def _tradermade(self,symbol,outputsize,timeframe,timezone_name):
        key=self._secret("tradermade")
        if not key: raise MarketDataUnavailable("TRADERMADE_API_KEY تنظیم نشده است")
        base,quote=self._pair(symbol); interval={"M1":"minute","M5":"minute","M15":"minute","M30":"minute","H1":"hour"}.get(str(timeframe).upper(),"minute")
        # TraderMade's timeseries endpoint accepts start/end; request a bounded recent window.
        minutes=max(60,int(outputsize)*({"M1":1,"M5":5,"M15":15,"M30":30,"H1":60}.get(str(timeframe).upper(),1)))
        end=datetime.now(timezone.utc); start=end-timedelta(minutes=minutes+10)
        fmt="%Y-%m-%d-%H:%M"
        r=self._request("tradermade","GET","https://marketdata.tradermade.com/api/v1/timeseries",params={"currency":f"{base}{quote}","start_date":start.strftime(fmt),"end_date":end.strftime(fmt),"interval":interval,"format":"json","api_key":key})
        d=r.json(); vals=d.get("quotes") or d.get("data") or []
        return self._normalize(vals,timezone_name)

    def _finnhub(self,symbol,outputsize,timeframe,timezone_name):
        # Finnhub's endpoint used here is specifically /forex/candle. Do not send
        # index/CFD-style symbols such as US300/USD to it as fake FX pairs.
        canonical=self._canonical(symbol)
        base,quote=self._pair(canonical)
        if len(base)!=3 or len(quote)!=3:
            raise MarketDataUnavailable(f"Finnhub skipped: {canonical} is not an FX-style symbol")
        key=self._secret("finnhub")
        if not key: raise MarketDataUnavailable("FINNHUB_API_KEY تنظیم نشده است")
        resolution={"M1":"1","M5":"5","M15":"15","M30":"30","H1":"60","D1":"D"}.get(str(timeframe).upper(),"1")
        end=int(time.time()); span=max(3600,int(outputsize)*int(resolution)*60) if resolution.isdigit() else 86400*max(2,int(outputsize))
        # Put the secret in a header instead of the query string so HTTP errors
        # cannot accidentally expose it through the request URL.
        r=self._request("finnhub","GET","https://finnhub.io/api/v1/forex/candle",headers={"X-Finnhub-Token":key},params={"symbol":self._mapped_symbol("finnhub",canonical),"resolution":resolution,"from":end-span,"to":end})
        d=r.json()
        if d.get("s") not in ("ok",None): raise MarketDataUnavailable(f"Finnhub: {d.get('s')}")
        vals=[]
        for i,ts in enumerate(d.get("t",[])):
            vals.append({"datetime_utc":ts,"open":d.get("o",[])[i],"high":d.get("h",[])[i],"low":d.get("l",[])[i],"close":d.get("c",[])[i],"volume":(d.get("v",[])[i] if i<len(d.get("v",[])) else 0)})
        return self._normalize(vals,timezone_name)

    def _fetch(self,p,symbol,outputsize,timeframe,timezone_name):
        return {"oanda":self._oanda,"twelvedata":self._twelve,"alltick":self._alltick,"tradermade":self._tradermade,"finnhub":self._finnhub}[p](symbol,outputsize,timeframe,timezone_name)

    def get_bars(self,symbol="XAU/USD",outputsize=500,timezone_name="UTC",timeframe="M1"):
        symbol=self._canonical(symbol); tf=str(timeframe).upper(); key=(symbol,timezone_name,tf); now=time.monotonic(); need=int(outputsize)
        with self.lock:
            item=self.bars_cache.get(key)
            # A cache hit is valid only when it contains at least as many bars
            # as the caller requested. A small diagnostic probe must never
            # satisfy a later strategy request for a larger history.
            if self._fresh(item,tf) and len(item[1]) >= need:
                self.cache_hits+=1; return item[1][-need:]
            self.cache_misses+=1
            event=self.inflight.get(key)
            if event is None: event=threading.Event(); self.inflight[key]=event; owner=True
            else: owner=False
        if not owner:
            event.wait(25)
            with self.lock:
                item=self.bars_cache.get(key)
                if self._fresh(item,tf) and len(item[1]) >= need:
                    self.cache_hits+=1; return item[1][-need:]
            raise MarketDataUnavailable("دریافت داده هم‌زمان شکست خورد")
        errors=[]
        try:
            for p,pcfg in self.provider_items():
                if not pcfg.get("enabled",True): continue
                if time.monotonic()<self.cooldowns.get(p,0): continue
                try:
                    values=self._fetch(p,symbol,max(need,300 if tf in ("M5","M15") else 500),tf,timezone_name)
                    if not values: raise MarketDataUnavailable("empty data")
                    # Ignore obviously stale latest candle; keep data but expose diagnostics.
                    with self.lock: self.bars_cache[key]=(time.monotonic(),values); self._set_success(p)
                    return values[-need:]
                except Exception as e:
                    errors.append(f"{p}: {e}")
                    if not isinstance(e,MarketDataRateLimit): self._set_error(p,e)
            raise MarketDataUnavailable(" | ".join(errors) if errors else "هیچ Provider فعالی برای داده وجود ندارد")
        finally:
            with self.lock:
                ev=self.inflight.pop(key,None)
                if ev: ev.set()

    def get_quote(self,symbol="XAU/USD"):
        symbol=self._canonical(symbol); now=time.monotonic()
        with self.lock:
            item=self.quote_cache.get(symbol)
            if item and now-item[0]<15 and item[1] is not None: self.cache_hits+=1; return item[1]
        # Use Twelve/OANDA-compatible quote where possible; fallback to latest candle close.
        for p,pcfg in self.provider_items():
            if not pcfg.get("enabled",True) or time.monotonic()<self.cooldowns.get(p,0): continue
            try:
                if p=="twelvedata" and self._secret(p):
                    d=self._request(p,"GET","https://api.twelvedata.com/quote",params={"symbol":self._mapped_symbol(p,symbol),"apikey":self._secret(p)}).json(); bid=d.get("bid"); ask=d.get("ask")
                    if bid is not None and ask is not None: val=(float(bid),float(ask)); self._set_success(p); self.quote_cache[symbol]=(now,val); return val
                vals=self._fetch(p,symbol,3,"M1","UTC")
                c=float(vals[-1]["close"]); val=(c,c); self._set_success(p); self.quote_cache[symbol]=(now,val); return val
            except Exception as e:
                if not isinstance(e,MarketDataRateLimit): self._set_error(p,e)
        return None

    def diagnostics(self):
        latest=[]
        with self.lock:
            for k,item in self.bars_cache.items():
                if item and item[1]: latest.append({"key":k,"latest":item[1][-1].get("datetime"),"age_seconds":round(time.monotonic()-item[0],1),"bars":len(item[1])})
        return {"last_provider":next((x["name"] for x in self.provider_status() if x.get("last_success")),"—"),"provider_states":self.provider_status(),"cache_hits":self.cache_hits,"cache_misses":self.cache_misses,"requests":self.request_count,"cached_series":latest}
