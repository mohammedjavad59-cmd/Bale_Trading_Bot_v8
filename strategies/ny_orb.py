from datetime import datetime,timedelta,time as dtime
from zoneinfo import ZoneInfo
import requests


def _tf_minutes(value):
    s=str(value or "M5").upper().strip()
    if s.startswith("M"): return max(1,int(s[1:]))
    if s.startswith("H"): return max(1,int(s[1:])*60)
    if s.startswith("D"): return 1440
    return 5

def _aggregate(bars,timeframe):
    mins=_tf_minutes(timeframe)
    if mins<=1: return bars
    groups={}
    for x in bars:
        dt=x['dt']; total=dt.hour*60+dt.minute; b=(total//mins)*mins
        st=dt.replace(hour=b//60,minute=b%60,second=0,microsecond=0); groups.setdefault(st,[]).append(x)
    out=[]
    for st,g in sorted(groups.items()):
        out.append({'dt':st,'open':g[0]['open'],'high':max(x['high'] for x in g),'low':min(x['low'] for x in g),'close':g[-1]['close'],'volume':sum(x.get('volume',0) for x in g)})
    return out

class NYORBStrategy:
    key="ny_orb"
    def __init__(self,cfg,settings,record_signal,bale_send,market_data=None):
        self.cfg=cfg; self.settings=settings; self.record_signal=record_signal; self.bale_send=bale_send; self.market_data=market_data
        self.ny=ZoneInfo("America/New_York"); self.last=None
    def cfgs(self): return self.settings.get('strategies',{}).get(self.key,{})
    def symbol(self): return str(self.cfgs().get('symbol','XAU/USD')).strip() or 'XAU/USD'
    def timeframe(self): return str(self.cfgs().get('timeframe','M5')).upper()
    def fetch(self):
        symbol=self.symbol(); need=600
        values=self.market_data.get_bars(symbol,need,'America/New_York',self.timeframe()) if self.market_data else requests.get('https://api.twelvedata.com/time_series',params={'symbol':symbol,'interval':'1min','outputsize':need,'timezone':'America/New_York','apikey':self.cfg['TWELVEDATA_API_KEY']},timeout=20).json().get('values',[])
        if not values: raise RuntimeError('No market data')
        a=[]
        for x in values:
            dt=datetime.strptime(x['datetime'],'%Y-%m-%d %H:%M:%S').replace(tzinfo=self.ny)
            a.append({'dt':dt,'open':float(x['open']),'high':float(x['high']),'low':float(x['low']),'close':float(x['close']),'volume':float(x.get('volume',0) or 0)})
        return _aggregate(sorted(a,key=lambda z:z['dt']),self.timeframe())
    def enabled_day(self,dt):
        flags=[self.cfg.get('TRADE_MONDAY',False),self.cfg.get('TRADE_TUESDAY',False),self.cfg.get('TRADE_WEDNESDAY',True),self.cfg.get('TRADE_THURSDAY',False),self.cfg.get('TRADE_FRIDAY',False)]
        return dt.weekday()<5 and flags[dt.weekday()]
    def atr(self,a,n=5):
        b=a[:-1]
        if len(b)<n+1:return 0
        t=[]
        for i in range(len(b)-n,len(b)):
            c,p=b[i],b[i-1]; t.append(max(c['high']-c['low'],abs(c['high']-p['close']),abs(c['low']-p['close'])))
        return sum(t)/len(t)
    def run_once(self):
        now=datetime.now(self.ny)
        if not self.enabled_day(now): return
        tf=_tf_minutes(self.timeframe())
        start=datetime.combine(now.date(),dtime(9,30),tzinfo=self.ny)
        end=start+timedelta(minutes=5)
        stop=datetime.combine(now.date(),dtime(10,0),tzinfo=self.ny)
        if not end<=now<=stop:return
        a=self.fetch()
        # Opening range remains the canonical 09:30-09:35 NY window.
        opening=[x for x in a if start<=x['dt']<end]
        # For non-M1/M5, require at least one candle covering the OR window.
        if not opening:return
        hi=max(x['high'] for x in opening); lo=min(x['low'] for x in opening)
        cur=now.replace(minute=(now.minute//tf)*tf,second=0,microsecond=0)
        closed=[x for x in a if end<=x['dt']<cur]
        if not closed:return
        c=closed[-1]; rng=c['high']-c['low']; body=abs(c['close']-c['open']); atr=self.atr(a)
        if rng<=0 or atr<=0 or body<.8*atr or body/rng<.60:return
        buy=c['close']>hi and c['open']<=hi; sell=c['close']<lo and c['open']>=lo
        if not(buy or sell):return
        key=c['dt'].isoformat()
        if self.cfgs().get('one_signal_per_candle',True) and key==self.last:return
        self.last=key; entry=c['close']
        if buy: sl=c['low']-.10; direction='BUY'; tp=entry+abs(entry-sl)*1.5
        else: sl=c['high']+.10; direction='SELL'; tp=entry-abs(entry-sl)*1.5
        text=(f"🧠 استراتژی: NY Open Range Breakout\n"
              f"{'🟢' if buy else '🔴'} {self.symbol()} {direction} SIGNAL\n\n"
              f"Timeframe: {self.timeframe()}\nEntry: {entry:.5f}\nSL: {sl:.5f}\nTP: {tp:.5f}\nRR: 1:1.5\n"
              f"OR High: {hi:.5f}\nOR Low: {lo:.5f}\nTime: {c['dt'].strftime('%Y-%m-%d %H:%M')} NY\n\n"
              f"⚠️ فقط سیگنال؛ معامله خودکار انجام نمی‌شود.")
        self.record_signal(self.key,self.symbol(),direction,entry,sl,tp,text)
