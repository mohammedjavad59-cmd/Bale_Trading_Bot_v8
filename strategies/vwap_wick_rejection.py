from datetime import datetime,timedelta,timezone
import requests


def _tf_minutes(value):
    s=str(value or 'M1').upper().strip()
    if s.startswith('M'): return max(1,int(s[1:]))
    if s.startswith('H'): return max(1,int(s[1:])*60)
    if s.startswith('D'): return 1440
    return 1

def _aggregate(bars,timeframe):
    mins=_tf_minutes(timeframe)
    if mins<=1:return bars
    groups={}
    for x in bars:
        dt=x['dt']; total=dt.hour*60+dt.minute; b=(total//mins)*mins
        st=dt.replace(hour=b//60,minute=b%60,second=0,microsecond=0); groups.setdefault(st,[]).append(x)
    out=[]
    for st,g in sorted(groups.items()):
        out.append({'dt':st,'open':g[0]['open'],'high':max(x['high'] for x in g),'low':min(x['low'] for x in g),'close':g[-1]['close'],'volume':sum(x.get('volume',0) for x in g)})
    return out

class VWAPWickRejectionStrategy:
    key='vwap_wick_rejection'
    def __init__(self,cfg,settings,record_signal,bale_send,market_data=None):
        self.cfg=cfg; self.settings=settings; self.record_signal=record_signal; self.bale_send=bale_send; self.market_data=market_data
        offset=self.cfg.get('BROKER_UTC_OFFSET','-03:30'); sign=-1 if str(offset).startswith('-') else 1; raw=str(offset).lstrip('+-'); hh,mm=[int(x) for x in raw.split(':')]
        self.server_tz=timezone(sign*timedelta(hours=hh,minutes=mm)); self.last_signal_bar=None
    def cfgs(self): return self.settings.get('strategies',{}).get(self.key,{})
    def symbol(self): return str(self.cfgs().get('symbol','XAU/USD')).strip() or 'XAU/USD'
    def timeframe(self): return str(self.cfgs().get('timeframe','M1')).upper()
    def fetch(self):
        symbol=self.symbol(); need=1000
        values=self.market_data.get_bars(symbol,need,'UTC',self.timeframe()) if self.market_data else requests.get('https://api.twelvedata.com/time_series',params={'symbol':symbol,'interval':'1min','outputsize':need,'timezone':'UTC','apikey':self.cfg['TWELVEDATA_API_KEY']},timeout=20).json().get('values',[])
        if not values: raise RuntimeError('No market data')
        out=[]
        for x in values:
            dt=datetime.strptime(x['datetime'],'%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc).astimezone(self.server_tz)
            out.append({'dt':dt,'open':float(x['open']),'high':float(x['high']),'low':float(x['low']),'close':float(x['close']),'volume':float(x.get('volume',0) or 0)})
        return _aggregate(sorted(out,key=lambda z:z['dt']),self.timeframe())
    def is_allowed_time(self,bar_time): return bar_time.weekday()==1 and bar_time.hour in (6,9,17,18,23)
    def daily_vwap(self,bars,now):
        day_start=now.replace(hour=0,minute=0,second=0,microsecond=0); day=[b for b in bars if day_start<=b['dt']<=now]
        pv=vol=0.0
        for b in day:
            v=float(b.get('volume',0) or 0); pv+=((b['high']+b['low']+b['close'])/3.0)*v; vol+=v
        return pv/vol if vol>0 else 0.0
    def get_quote(self):
        try:
            if self.market_data:return self.market_data.get_quote(self.symbol())
            d=requests.get('https://api.twelvedata.com/quote',params={'symbol':self.symbol(),'apikey':self.cfg['TWELVEDATA_API_KEY']},timeout=10).json(); bid=d.get('bid'); ask=d.get('ask')
            return None if bid is None or ask is None else (float(bid),float(ask))
        except Exception:return None
    def run_once(self):
        now=datetime.now(self.server_tz)
        if not self.is_allowed_time(now):return
        bars=self.fetch()
        if len(bars)<3:return
        tf=_tf_minutes(self.timeframe()); current=now.replace(minute=(now.minute//tf)*tf,second=0,microsecond=0)
        closed=[b for b in bars if b['dt']<current]
        if not closed:return
        c=closed[-1]
        if self.cfgs().get('one_signal_per_candle',True) and self.last_signal_bar==c['dt']:return
        self.last_signal_bar=c['dt']
        quote=self.get_quote(); point=float(self.cfg.get('XAUUSD_POINT',0.01)); max_spread=int(self.cfg.get('VWAP_MAX_SPREAD_POINTS',80))
        if quote is not None:
            spread=(quote[1]-quote[0])/point
            if spread>max_spread:return
        vwap=self.daily_vwap(bars,now)
        if vwap<=0:return
        band=float(self.cfg.get('VWAP_BAND_POINTS',100))*point
        bullish=c['low']<=vwap and c['close']>vwap and c['open']>vwap and c['close']>c['open']
        bearish=c['high']>=vwap and c['close']<vwap and c['open']<vwap and c['close']<c['open']
        if not(bullish or bearish):return
        entry=c['close']; rr=float(self.cfg.get('VWAP_RR',1.5))
        if bullish:
            sl=min(c['low'],vwap-band); direction='BUY'; risk=entry-sl
            if risk<=0:return
            tp=entry+risk*rr
        else:
            sl=max(c['high'],vwap+band); direction='SELL'; risk=sl-entry
            if risk<=0:return
            tp=entry-risk*rr
        text=(f"🧠 استراتژی: VWAP Wick Rejection\n"
              f"{'🟢' if bullish else '🔴'} {self.symbol()} {direction} SIGNAL\n\n"
              f"Timeframe: {self.timeframe()}\nEntry: {entry:.5f}\nSL: {sl:.5f}\nTP: {tp:.5f}\nRR: 1:{rr:g}\n\n"
              f"VWAP: {vwap:.5f}\nVWAP Band: {band:.5f}\nCandle: {c['dt'].strftime('%Y-%m-%d %H:%M')} server\nAllowed hours: 06, 09, 17, 18, 23\nDay: Tuesday\n\n"
              f"⚠️ فقط سیگنال؛ معامله خودکار انجام نمی‌شود.")
        self.record_signal(self.key,self.symbol(),direction,entry,sl,tp,text)
