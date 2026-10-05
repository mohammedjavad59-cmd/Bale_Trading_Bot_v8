import json, logging, threading, time, os, base64, importlib.util, ast, shutil
from datetime import datetime, timezone
from pathlib import Path
import requests
from flask import Flask, jsonify, render_template_string, request
from strategies.ny_orb import NYORBStrategy
from strategies.vwap_wick_rejection import VWAPWickRejectionStrategy
from strategies.sp2l import SP2LStrategy
from market_data import MarketDataCache

BASE=Path(__file__).resolve().parent
CONFIG=BASE/"config.json"
PLUGINS_DIR=BASE/"data"/"plugins"
PLUGINS_DIR.mkdir(parents=True, exist_ok=True)
PLUGIN_REGISTRY={}
PLUGIN_META={}

CFG=json.loads(CONFIG.read_text(encoding="utf-8"))
# GitHub Actions / local environment variables override blank config values.
for _k in ("BALE_TOKEN","BALE_CHAT_ID","OWNER_CHAT_ID","TWELVEDATA_API_KEY","ALLTICK_API_TOKEN","OANDA_API_TOKEN","OANDA_ACCOUNT_ID","OANDA_ENVIRONMENT","TRADERMADE_API_KEY","FINNHUB_API_KEY","BALE_GH_ADMIN_TOKEN"):
    if os.getenv(_k): CFG[_k]=os.getenv(_k)
DATA=BASE/"data"; DATA.mkdir(exist_ok=True)
SETTINGS_PATH=BASE/CFG["SETTINGS_FILE"]; HISTORY_PATH=BASE/CFG["HISTORY_FILE"]
HISTORY_DIR=BASE/"data"/"history"; HISTORY_DIR.mkdir(parents=True, exist_ok=True)
SETTINGS=json.loads(SETTINGS_PATH.read_text(encoding="utf-8")) if SETTINGS_PATH.exists() else {}

STRATEGY_KEYS=["ny_orb","vwap_wick_rejection","sp2l"]

ALLOWED_PLUGIN_IMPORTS={"datetime","math","statistics","decimal","zoneinfo","typing","collections","time"}
BANNED_PLUGIN_NAMES={"open","exec","eval","compile","__import__","input"}

def validate_plugin_source(source, filename):
    tree=ast.parse(source,filename=filename)
    for node in ast.walk(tree):
        if isinstance(node,ast.Import):
            for n in node.names:
                root=n.name.split(".")[0]
                if root not in ALLOWED_PLUGIN_IMPORTS:
                    raise ValueError(f"Plugin import ممنوع: {n.name}")
        elif isinstance(node,ast.ImportFrom):
            root=(node.module or "").split(".")[0]
            if root not in ALLOWED_PLUGIN_IMPORTS:
                raise ValueError(f"Plugin import ممنوع: {node.module}")
        elif isinstance(node,ast.Call) and isinstance(node.func,ast.Name) and node.func.id in BANNED_PLUGIN_NAMES:
            raise ValueError(f"فراخوانی ممنوع در Plugin: {node.func.id}")
    return tree

def load_strategy_plugins():
    """Load owner-provided strategy .py plugins using the documented contract."""
    for path in sorted(PLUGINS_DIR.glob("*.py")):
        if path.name.startswith("_"): continue
        try:
            source=path.read_text(encoding="utf-8")
            tree=validate_plugin_source(source,str(path))
            name=f"user_strategy_{path.stem}"
            spec=importlib.util.spec_from_file_location(name,path)
            mod=importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
            cls=getattr(mod,"STRATEGY_CLASS",None)
            key=str(getattr(mod,"STRATEGY_KEY","") or getattr(cls,"key","")).strip()
            display=str(getattr(mod,"STRATEGY_NAME","") or getattr(cls,"name","") or key).strip()
            if not cls or not key or not display: raise ValueError("STRATEGY_CLASS / STRATEGY_KEY / STRATEGY_NAME ناقص است")
            if not hasattr(cls,"run_once"): raise ValueError("کلاس استراتژی باید run_once() داشته باشد")
            PLUGIN_REGISTRY[key]=cls
            PLUGIN_META[key]={"name":display,"description":getattr(mod,"STRATEGY_DESCRIPTION",display),"sections":getattr(mod,"STRATEGY_SECTIONS",[]),"defaults":getattr(mod,"DEFAULT_SETTINGS",{}) or {},"file":path.name}
            if key not in STRATEGY_KEYS: STRATEGY_KEYS.append(key)
            logging.info("Loaded strategy plugin: %s (%s)",key,display)
        except Exception as e:
            logging.error("Strategy plugin %s failed: %s",path.name,e)

load_strategy_plugins()

def _load_history_file(path):
    try:
        data=json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data,list) else []
    except Exception:
        return []

def _load_histories():
    result=[]
    # New per-strategy history files
    for key in STRATEGY_KEYS:
        result.extend(_load_history_file(HISTORY_DIR/f"{key}.json"))
    result.sort(key=lambda x:str(x.get("time_utc","")))
    return result

HISTORY=_load_histories()
for _item in HISTORY:
    _item.setdefault("status","OPEN")
    _item.setdefault("result_r",None)
    _item.setdefault("result_price",None)
    _item.setdefault("closed_time_utc",None)
SETTINGS.setdefault("bot", {})
SETTINGS["bot"].setdefault("bale_enabled", True)
legacy_owner = str(CFG.get("OWNER_CHAT_ID") or CFG.get("BALE_CHAT_ID") or "").strip()
SETTINGS["bot"]["owner_chat_id"] = legacy_owner
SETTINGS["bot"].setdefault("admin_chat_ids", [])
if not isinstance(SETTINGS["bot"].get("admin_chat_ids"), list): SETTINGS["bot"]["admin_chat_ids"]=[]
SETTINGS["bot"]["admin_chat_ids"]=[str(x).strip() for x in SETTINGS["bot"]["admin_chat_ids"] if str(x).strip().isdigit() and str(x).strip()!=legacy_owner]
# allowed_chat_ids is retained for compatibility, but is now derived from owner + admins.
SETTINGS["bot"]["allowed_chat_ids"]=[legacy_owner]+[x for x in SETTINGS["bot"]["admin_chat_ids"] if x!=legacy_owner] if legacy_owner else list(SETTINGS["bot"]["admin_chat_ids"])
SETTINGS.setdefault("strategies", {})
SETTINGS.setdefault("providers", {})
_PROVIDER_DEFAULTS={
    "oanda":{"name":"OANDA","env_key":"OANDA_API_TOKEN","enabled":True,"priority":1},
    "twelvedata":{"name":"Twelve Data","env_key":"TWELVEDATA_API_KEY","enabled":True,"priority":2},
    "alltick":{"name":"AllTick","env_key":"ALLTICK_API_TOKEN","enabled":True,"priority":3},
    "tradermade":{"name":"TraderMade","env_key":"TRADERMADE_API_KEY","enabled":True,"priority":4},
    "finnhub":{"name":"Finnhub","env_key":"FINNHUB_API_KEY","enabled":True,"priority":5},
}
for _pk,_pv in _PROVIDER_DEFAULTS.items():
    SETTINGS["providers"].setdefault(_pk,dict(_pv))
    for _fk,_fv in _pv.items(): SETTINGS["providers"][_pk].setdefault(_fk,_fv)

def normalize_strategy_settings():
    defaults={
        "ny_orb":{"enabled":True,"symbol":"XAU/USD","timeframe":"M5","scan_interval":20,"cooldown":0,"max_signals_per_day":None,"one_signal_per_candle":True},
        "vwap_wick_rejection":{"enabled":True,"symbol":"XAU/USD","timeframe":"M1","scan_interval":20,"cooldown":0,"max_signals_per_day":None,"one_signal_per_candle":True},
        "sp2l":{"enabled":True,"symbol":"XAU/USD","timeframe":"M5","scan_interval":20,"cooldown":0,"max_signals_per_day":None,"one_signal_per_candle":True,
                 "direction":"BOTH","min_spike_bars":1,"max_spike_bars":3,"min_spike_body_ratio":0.65,"min_spike_vs_neighbors":1.50,
                 "gap_mode":"three_candle","min_gap_points":0,"max_bars_after_pattern":20,"tp1_r":1.0,"use_tp2":True,"tp2_r":2.0,"second_entry_fraction":0.50,"use_second_entry_2x":True}
    }
    for key,meta in PLUGIN_META.items():
        d={"enabled":True,"symbol":"XAU/USD","timeframe":"M5","scan_interval":20,"cooldown":0,"max_signals_per_day":None,"one_signal_per_candle":True}
        d.update(meta.get("defaults",{})); defaults[key]=d
    old=SETTINGS.get("strategies",{})
    for key,d in defaults.items():
        cur=old.get(key,{})
        if isinstance(cur,bool): cur={"enabled":cur}
        for k,v in d.items(): cur.setdefault(k,v)
        old[key]=cur
    SETTINGS["strategies"]=old

normalize_strategy_settings()

BALE=f"https://tapi.bale.ai/bot{CFG['BALE_TOKEN']}"
logging.basicConfig(level=logging.INFO,format="%(asctime)s | %(levelname)s | %(message)s")
SETTINGS_LOCK=threading.RLock()
HISTORY_LOCK=threading.RLock()
WORKER_EVENTS={k:threading.Event() for k in SETTINGS["strategies"]}
RUNTIME={k:{"status":"idle","last_scan":None,"last_error":None} for k in WORKER_EVENTS}
# Runtime diagnostics are intentionally kept in memory only; they expose whether a
# worker is healthy, market data is reachable, and whether the last scan produced
# a signal or simply found no setup.
for _k in RUNTIME:
    RUNTIME[_k].update({
        "phase":"starting", "scan_count":0, "error_count":0,
        "market_data":"unknown", "bars_count":0, "latest_bar":None,
        "last_decision":"هنوز اسکن انجام نشده", "last_signal_time":None, "data_provider":"—",
    })

def runtime_update(key, **values):
    if key in RUNTIME:
        RUNTIME[key].update(values)

def _parse_market_bar_datetime(value):
    try:
        return datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except Exception:
        return None

PENDING_INPUT={}
market_data_global=None


def save_settings():
    data=json.dumps(SETTINGS,ensure_ascii=False,indent=2)
    with SETTINGS_LOCK:
        SETTINGS_PATH.write_text(data,encoding="utf-8")
    # When running on GitHub Actions, persist Bale-made changes back to the
    # repository so a new runner does not reset the user's settings.
    if os.getenv("GITHUB_ACTIONS") == "true" and os.getenv("GITHUB_TOKEN") and os.getenv("GITHUB_REPOSITORY"):
        try:
            api=f"https://api.github.com/repos/{os.getenv('GITHUB_REPOSITORY')}/contents/data/settings.json"
            headers={"Accept":"application/vnd.github+json","Authorization":f"Bearer {os.getenv('GITHUB_TOKEN')}","X-GitHub-Api-Version":"2022-11-28"}
            current=requests.get(api,headers=headers,timeout=15)
            sha=None
            if current.ok:
                sha=(current.json() or {}).get("sha")
            payload={"message":"chore: update bot settings","content":base64.b64encode(data.encode('utf-8')).decode('ascii')}
            if sha: payload["sha"]=sha
            r=requests.put(api,headers=headers,json=payload,timeout=20)
            if not r.ok:
                logging.warning("GitHub settings persistence failed: %s %s",r.status_code,r.text[:300])
        except Exception as e:
            logging.warning("GitHub settings persistence error: %s",e)

def save_history():
    with HISTORY_LOCK:
        # Keep a separate history file for every strategy.
        for key in STRATEGY_KEYS:
            items=[x for x in HISTORY if x.get("strategy")==key][-500:]
            (HISTORY_DIR/f"{key}.json").write_text(json.dumps(items,ensure_ascii=False,indent=2),encoding="utf-8")
        # Histories are intentionally stored only per strategy; there is no shared signal history file.

def bale(method,payload=None):
    if not CFG.get("BALE_TOKEN"):
        raise RuntimeError("BALE_TOKEN is empty")
    r=requests.post(f"{BALE}/{method}",json=payload or {},timeout=20); r.raise_for_status()
    d=r.json()
    if not d.get("ok"): raise RuntimeError(d.get("description",str(d)))
    return d.get("result")

def owner_chat_id(): return str(SETTINGS["bot"].get("owner_chat_id") or CFG.get("OWNER_CHAT_ID") or CFG.get("BALE_CHAT_ID") or "").strip()

def admin_chat_ids():
    with SETTINGS_LOCK: return [str(x) for x in SETTINGS["bot"].get("admin_chat_ids",[]) if str(x).isdigit()]

def allowed_chat_ids():
    ids=[owner_chat_id()]+admin_chat_ids(); return list(dict.fromkeys([x for x in ids if x]))

def is_owner_chat(chat):
    return isinstance(chat,dict) and str(chat.get("id"))==owner_chat_id() and str(chat.get("type",""))=="private"

def is_admin_chat(chat):
    return isinstance(chat,dict) and str(chat.get("id")) in set(admin_chat_ids()) and str(chat.get("type",""))=="private"

def is_allowed_private_chat(chat): return is_owner_chat(chat) or is_admin_chat(chat)

def is_allowed_callback(cq): return is_allowed_private_chat((cq.get("message") or {}).get("chat") or {})

def send(chat,text,markup=None):
    if not SETTINGS["bot"].get("bale_enabled",True):
        return None
    target = str(chat).strip() if chat is not None else ""
    targets = [target] if target in set(allowed_chat_ids()) else allowed_chat_ids()
    if not targets:
        return None
    results=[]
    for chat_id in targets:
        payload={"chat_id":chat_id,"text":text}
        if markup: payload["reply_markup"]=markup
        try:
            results.append(bale("sendMessage",payload))
        except Exception as e:
            logging.warning("sendMessage to %s: %s",chat_id,e)
    return results[-1] if results else None

def answer_callback(callback_id,text=""):
    try: bale("answerCallbackQuery",{"callback_query_id":callback_id,"text":text})
    except Exception as e: logging.warning("answerCallbackQuery: %s",e)

def inline(rows): return {"inline_keyboard":rows}

def main_menu(owner=True):
    rows=[
        [{"text":"💰 قیمت‌ها","callback_data":"menu:prices"},{"text":"📚 تاریخچه","callback_data":"menu:history"}],
        [{"text":"🧠 استراتژی‌ها","callback_data":"menu:strategies"},{"text":"📊 وضعیت سیستم","callback_data":"menu:status"}],
        [{"text":"🩺 عیب‌یابی","callback_data":"menu:diagnostics"},{"text":"📡 مدیریت API","callback_data":"menu:providers"}],
        [{"text":"💻 کدنویسی","callback_data":"menu:coding"}],
    ]
    if owner:
        rows.append([{"text":"👤 افزودن ادمین","callback_data":"admin:menu"}])
    return inline(rows)

def price_menu():
    return inline([
        [{"text":"🥇 XAU/USD","callback_data":"price:XAU/USD"},{"text":"💵 EUR/USD","callback_data":"price:EUR/USD"}],
        [{"text":"💷 GBP/JPY","callback_data":"price:GBP/JPY"}],
        [{"text":"↩️ منوی اصلی","callback_data":"menu:home"}],
    ])

STRATEGY_NAMES = {
    "ny_orb": "NY Open Range Breakout",
    "vwap_wick_rejection": "VWAP Wick Rejection",
    "sp2l": "SP2L (Spike–2Leg)",
}

STRATEGY_DESCRIPTIONS = {
    "ny_orb": {"title":"استراتژی NY Open Range Breakout","summary":"شکست محدوده ابتدایی بازگشایی نیویورک و معامله شکست معتبر سقف یا کف آن.","sections":[
        ("⏱ زمان‌بندی", ["نماد و تایم‌فریم از تنظیمات ربات قابل تغییر است؛ پیش‌فرض XAU/USD و M5.","Opening Range از 09:30 تا 09:35 نیویورک است.","روز فعال پیش‌فرض چهارشنبه است."]),
        ("📈 BUY / 📉 SELL", ["BUY: Close بالاتر از سقف OR و Open روی/زیر سقف OR باشد.","SELL: Close پایین‌تر از کف OR و Open روی/بالای کف OR باشد.","بدنه حداقل 0.8 برابر ATR(5) و حداقل 60٪ دامنه کندل باشد."]),
        ("🛡 مدیریت معامله", ["Entry = Close کندل شکست.","BUY SL = Low کندل شکست − 0.10 و SELL SL = High کندل شکست + 0.10.","RR = 1:1.5."]),
    ]},
    "vwap_wick_rejection": {"title":"استراتژی VWAP Wick Rejection","summary":"بررسی برخورد سایه کندل با Daily VWAP و بسته‌شدن قیمت در سمت مقابل برای تشخیص برگشت.","sections":[
        ("⏱ زمان‌بندی", ["نماد و تایم‌فریم از تنظیمات ربات قابل تغییر است؛ پیش‌فرض XAU/USD و M1.","روز فعال سه‌شنبه و ساعات سرور 06، 09، 17، 18 و 23."]),
        ("🟢 BUY / 🔴 SELL", ["BUY: Low به VWAP برسد، Open و Close بالای VWAP باشند و Close > Open.","SELL: High به VWAP برسد، Open و Close زیر VWAP باشند و Close < Open."]),
        ("🛡 مدیریت معامله", ["Entry = Close کندل بسته‌شده.","BUY SL = min(Low, VWAP − Band) و SELL SL = max(High, VWAP + Band).","RR پیش‌فرض 1:1.5 و حداکثر Spread پیش‌فرض 80 Point."]),
    ]},
    "sp2l": {"title":"استراتژی SP2L (Spike–2Leg)","summary":"ورود پس از یک Spike قدرتمند دارای P-Gap، اصلاح 2Leg و ادامه حرکت در جهت Spike.","sections":[
        ("⚡ ساختار استراتژی", ["Power → Correction → Continuation.","ابتدا Spike شناسایی می‌شود و وجود P-Gap شرط اعتبار Setup است.","پس از Spike منتظر اصلاح 2Leg می‌مانیم."]),
        ("🎯 ورود", ["BUY: اصلاح نزولی باید Low کندل قبلی را لمس کند؛ سپس Entry روی سطح Low کندل قبلی است.","SELL: اصلاح صعودی باید High کندل قبلی را لمس کند؛ سپس Entry روی سطح High کندل قبلی است.","ورود دوم 2X به‌صورت پیش‌فرض در 50٪ فاصله Entry تا SL محاسبه می‌شود."]),
        ("🛡 مدیریت معامله", ["SL پشت Origin Candle قرار می‌گیرد.","TP1 پیش‌فرض = 1R.","TP2 پیش‌فرض = 2R و قابل تغییر است."]),
        ("⚙️ فیلترهای قابل تنظیم", ["جهت BUY/SELL/BOTH، حداقل قدرت Spike، نوع P-Gap، تعداد کندل انتظار برای 2Leg، TP1/TP2 و 2X از تنظیمات قابل تغییر هستند."]),
    ]}
}

for _pk,_pm in PLUGIN_META.items():
    STRATEGY_NAMES[_pk]=_pm.get("name",_pk)
    STRATEGY_DESCRIPTIONS[_pk]={"title":_pm.get("name",_pk),"summary":_pm.get("description",_pm.get("name",_pk)),"sections":_pm.get("sections",[])}

def strategy_details(key):
    info=STRATEGY_DESCRIPTIONS.get(key)
    if not info: return "❌ توضیحات این استراتژی در سیستم ثبت نشده."
    lines=[f"📖 {info['title']}","",info["summary"]]
    for title,items in info["sections"]:
        lines += ["",title] + [f"• {item}" for item in items]
    lines += ["","⚠️ این توضیحات مطابق منطق فعلی کد ربات است."]
    return "\n".join(lines)

def strategy_menu():
    rows=[]
    for k,v in SETTINGS["strategies"].items():
        enabled=v.get("enabled",False) if isinstance(v,dict) else bool(v)
        rows.append([{"text":("🟢 " if enabled else "🔴 ")+STRATEGY_NAMES.get(k,k),"callback_data":"strategy:info:"+k}])
    rows.append([{"text":"➕ افزودن استراتژی","callback_data":"strategy:add"}])
    rows.append([{"text":"⚙️ تنظیمات ربات","callback_data":"menu:settings"}])
    rows.append([{"text":"↩️ منوی اصلی","callback_data":"menu:home"}])
    return inline(rows)

def strategy_info(key):
    v=SETTINGS["strategies"].get(key,{})
    enabled=v.get("enabled",False); interval=v.get("scan_interval",20); cooldown=v.get("cooldown",0); mx=v.get("max_signals_per_day")
    mx_text="∞" if mx in (None,0,"0","infinity") else str(mx)
    return (f"🧠 {STRATEGY_NAMES.get(key,key)}\n\n"
            f"وضعیت: {'🟢 فعال' if enabled else '🔴 خاموش'}\n"
            f"نماد: {v.get('symbol','XAU/USD')}\nتایم‌فریم: {v.get('timeframe','M5')}\n"
            f"اسکن: هر {interval} ثانیه\nCooldown: {cooldown} ثانیه\nحداکثر سیگنال روزانه: {mx_text}\n"
            f"یک سیگنال برای هر کندل: {'فعال' if v.get('one_signal_per_candle',True) else 'خاموش'}")


def strategy_diagnostic_text(key):
    v=SETTINGS.get("strategies",{}).get(key,{})
    r=RUNTIME.get(key,{})
    status=r.get("status","idle")
    status_text={"ok":"🟢 سالم","scanning":"🟡 در حال اسکن","disabled":"⚪ غیرفعال","error":"🔴 خطای فنی"}.get(status,status)
    md=r.get("market_data","unknown")
    md_text={"ok":"✅ در دسترس","empty":"⚠️ داده‌ای برنگشت","error":"❌ خطا","unknown":"—"}.get(md,md)
    lines=[f"🩺 عیب‌یابی: {STRATEGY_NAMES.get(key,key)}","",
           f"وضعیت Worker: {status_text}",f"نماد: {v.get('symbol','XAU/USD')}",
           f"تایم‌فریم: {v.get('timeframe','M5')}",f"آخرین اسکن: {r.get('last_scan') or '—'}",
           f"تعداد اسکن: {r.get('scan_count',0)}",f"داده بازار: {md_text}",
           f"تعداد داده آزمایشی: {r.get('bars_count',0)}",f"آخرین کندل دریافت‌شده: {r.get('latest_bar') or '—'}",f"منبع داده: {r.get('data_provider','—')}","",
           f"نتیجه آخرین اسکن: {r.get('last_decision') or '—'}",
           f"آخرین سیگنال: {r.get('last_signal_time') or '—'}",f"تعداد خطا: {r.get('error_count',0)}"]
    if r.get("last_error"): lines += ["","🔴 آخرین خطا:",str(r.get("last_error"))[:1200]]
    else: lines += ["","خطای فنی: ندارد"]
    return "\n".join(lines)

def diagnostic_menu():
    rows=[]
    for key in STRATEGY_KEYS:
        if key in SETTINGS.get("strategies",{}):
            rows.append([{"text":f"🩺 {STRATEGY_NAMES.get(key,key)}","callback_data":f"diag:{key}"}])
    rows.append([{"text":"🔎 عیب‌یابی همه","callback_data":"diag:all"}])
    rows.append([{"text":"↩️ منوی اصلی","callback_data":"menu:home"}])
    return inline(rows)

def provider_menu():
    rows=[]
    for key,cfg in sorted(SETTINGS.get("providers",{}).items(), key=lambda kv:int(kv[1].get("priority",999))):
        icon="🟢" if cfg.get("enabled",True) else "🔴"
        rows.append([{"text":f"{icon} {cfg.get('name',key)} · اولویت {cfg.get('priority',999)}","callback_data":f"provider:info:{key}"}])
    rows += [[{"text":"🩺 Health همه APIها","callback_data":"provider:health"}], [{"text":"↩️ منوی اصلی","callback_data":"menu:home"}]]
    return inline(rows)

def provider_info(key):
    cfg=SETTINGS.get("providers",{}).get(key,{})
    env=cfg.get("env_key","")
    configured=bool(os.getenv(env) or CFG.get(env))
    st=(market_data_global.provider_status() if market_data_global else [])
    state=next((x for x in st if x.get("key")==key),{})
    cd=state.get("cooldown_seconds",0)
    return (f"📡 {cfg.get('name',key)}\n\n"
            f"وضعیت: {'🟢 فعال' if cfg.get('enabled',True) else '🔴 غیرفعال'}\n"
            f"اولویت: {cfg.get('priority',999)}\n"
            f"کلید: {'🟢 تنظیم شده' if configured else '🟡 تنظیم نشده'}\n"
            f"Health: {state.get('status','unknown')}\n"
            f"Cooldown: {cd} ثانیه\n"
            f"آخرین موفقیت: {state.get('last_success') or '—'}\n"
            f"خطاها: {state.get('errors',0)} | Rate Limit: {state.get('rate_limits',0)}")

def provider_info_menu(key):
    cfg=SETTINGS.get("providers",{}).get(key,{})
    enabled=cfg.get("enabled",True)
    return inline([
        [{"text":"🔴 غیرفعال کردن" if enabled else "🟢 فعال کردن","callback_data":f"provider:toggle:{key}"}],
        [{"text":"⬆️ افزایش اولویت","callback_data":f"provider:up:{key}"},{"text":"⬇️ کاهش اولویت","callback_data":f"provider:down:{key}"}],
        [{"text":"🔐 تغییر API Key","callback_data":f"provider:key:{key}"}],
        [{"text":"🧪 تست اتصال","callback_data":f"provider:test:{key}"}],
        [{"text":"📖 وضعیت","callback_data":f"provider:info:{key}"}],
        [{"text":"↩️ لیست APIها","callback_data":"menu:providers"}],
    ])

def provider_health_text():
    if not market_data_global: return "📡 Market Data Manager هنوز راه‌اندازی نشده است."
    lines=["📡 وضعیت Market Data Providerها","",f"Cache Hit: {market_data_global.cache_hits}",f"Cache Miss: {market_data_global.cache_misses}",f"Requests: {market_data_global.request_count}",""]
    for x in market_data_global.provider_status():
        lines += [f"{x['priority']}. {x['name']} — {'🟢' if x['enabled'] else '🔴'} {'Configured' if x['configured'] else 'No Key'}", f"   Health: {x.get('status','unknown')} | 429: {x.get('rate_limits',0)} | Cooldown: {x.get('cooldown_seconds',0)}s"]
    return "\n".join(lines)

def provider_set_priority(key,direction):
    providers=SETTINGS.get("providers",{}); keys=sorted(providers,key=lambda k:int(providers[k].get("priority",999)))
    if key not in keys:return
    i=keys.index(key); j=i-1 if direction=="up" else i+1
    if j<0 or j>=len(keys): return
    a,b=keys[i],keys[j]; pa,pb=providers[a].get("priority",i+1),providers[b].get("priority",j+1)
    providers[a]["priority"],providers[b]["priority"]=pb,pa; save_settings()

def prompt_provider_key(chat,key):
    PENDING_INPUT[str(chat)]={"type":"provider_secret","provider":key}
    name=SETTINGS.get("providers",{}).get(key,{}).get("name",key)
    send(chat,f"🔐 کلید API برای «{name}» را ارسال کن.\n\nکلید در Repository ذخیره نمی‌شود و فقط در GitHub Secrets با نام مربوطه قرار می‌گیرد.\nبرای لغو: /cancel")

def _github_set_secret(name,value):
    token=os.getenv("BALE_GH_ADMIN_TOKEN") or CFG.get("BALE_GH_ADMIN_TOKEN")
    repo=os.getenv("GITHUB_REPOSITORY")
    if not token or not repo: raise RuntimeError("BALE_GH_ADMIN_TOKEN یا GITHUB_REPOSITORY تنظیم نشده است")
    try:
        from nacl.public import PublicKey, SealedBox
        from nacl.encoding import Base64Encoder
    except Exception:
        raise RuntimeError("PyNaCl نصب نیست")
    headers={"Accept":"application/vnd.github+json","Authorization":f"Bearer {token}","X-GitHub-Api-Version":"2022-11-28"}
    r=requests.get(f"https://api.github.com/repos/{repo}/actions/secrets/public-key",headers=headers,timeout=15); r.raise_for_status()
    d=r.json(); public_key=PublicKey(d["key"],encoder=Base64Encoder); encrypted=SealedBox(public_key).encrypt(value.encode("utf-8")); encoded=base64.b64encode(encrypted).decode("ascii")
    r=requests.put(f"https://api.github.com/repos/{repo}/actions/secrets/{name}",headers=headers,json={"encrypted_value":encoded,"key_id":d["key_id"]},timeout=20); r.raise_for_status()
    return True

def set_provider_secret(key,value):
    cfg=SETTINGS.get("providers",{}).get(key)
    if not cfg: raise ValueError("Provider پیدا نشد")
    env=cfg.get("env_key")
    if not env: raise ValueError("env key تعریف نشده")
    if len(value.strip())<4: raise ValueError("کلید API نامعتبر است")
    _github_set_secret(env,value.strip())
    os.environ[env]=value.strip(); CFG[env]=value.strip()
    save_settings()

def coding_menu():
    return inline([
        [{"text":"📘 راهنمای کدنویسی","callback_data":"coding:help"}],
        [{"text":"🧩 ارسال تغییرات","callback_data":"coding:input"}],
        [{"text":"📊 وضعیت Deploy","callback_data":"coding:status"}],
        [{"text":"🔄 Restart / Deploy","callback_data":"coding:restart"}],
        [{"text":"↩️ منوی اصلی","callback_data":"menu:home"}],
    ])

CODING_EXAMPLE = '{"commit":"Update SP2L scan interval","restart":true,"operations":[{"op":"json_set","path":"data/settings.json","key":"strategies.sp2l.scan_interval","value":10}]}'

def coding_help_text():
    return ("💻 کدنویسی\n\nفقط مالک می‌تواند تغییرات کد و GitHub را اعمال کند. "
            "تغییرات ابتدا اعتبارسنجی و سپس با تأیید شما Deploy می‌شوند.\n\n"
            "عملیات: write | patch | json_set | delete\n\nنمونه:\n"+CODING_EXAMPLE+
            "\n\nمی‌توانی همین JSON را به‌صورت پیام یا فایل .json ارسال کنی.")

def _safe_repo_path(value):
    raw=str(value or "").replace("\\","/").lstrip("/")
    p=(BASE/raw).resolve()
    base=BASE.resolve()
    if p==base or base not in p.parents: raise ValueError("مسیر فایل خارج از Repository مجاز نیست")
    if ".git" in p.relative_to(base).parts: raise ValueError("مسیر .git مجاز نیست")
    return p

def _apply_coding_manifest(manifest):
    if not isinstance(manifest,dict): raise ValueError("Manifest باید JSON object باشد")
    ops=manifest.get("operations")
    if not isinstance(ops,list) or not ops or len(ops)>20: raise ValueError("operations باید بین 1 تا 20 مورد باشد")
    changes={}; summary=[]
    for op in ops:
        typ=str(op.get("op","")).lower()
        rel=str(op.get("path","")).replace("\\","/").lstrip("/")
        path=_safe_repo_path(rel)
        if typ=="write":
            content=op.get("content")
            if not isinstance(content,str): raise ValueError(f"write برای {rel} به content متنی نیاز دارد")
            path.parent.mkdir(parents=True,exist_ok=True); path.write_text(content,encoding="utf-8")
            changes[rel]=content.encode("utf-8"); summary.append(f"✏️ write: {rel}")
        elif typ=="patch":
            old=op.get("old"); new=op.get("new")
            if not isinstance(old,str) or not isinstance(new,str): raise ValueError(f"patch برای {rel} به old/new نیاز دارد")
            if not path.exists(): raise ValueError(f"فایل پیدا نشد: {rel}")
            current=path.read_text(encoding="utf-8"); count=current.count(old); requested=max(1,int(op.get("count",1)))
            if count==0: raise ValueError(f"بخش old در {rel} پیدا نشد")
            if count>requested: raise ValueError(f"old در {rel} بیش از حد تکرار شده؛ patch دقیق‌تر ارسال کن")
            updated=current.replace(old,new,requested)
            path.write_text(updated,encoding="utf-8"); changes[rel]=updated.encode("utf-8"); summary.append(f"🩹 patch: {rel}")
        elif typ=="json_set":
            if not path.exists(): raise ValueError(f"فایل JSON پیدا نشد: {rel}")
            data=json.loads(path.read_text(encoding="utf-8")); key=str(op.get("key","")).strip()
            if not key: raise ValueError("json_set بدون key")
            parts=key.split("."); cur=data
            for part in parts[:-1]:
                if not isinstance(cur,dict): raise ValueError(f"مسیر JSON نامعتبر: {key}")
                cur=cur.setdefault(part,{})
            cur[parts[-1]]=op.get("value")
            updated=json.dumps(data,ensure_ascii=False,indent=2)+"\n"
            path.write_text(updated,encoding="utf-8"); changes[rel]=updated.encode("utf-8"); summary.append(f"🔧 json_set: {rel} → {key}")
        elif typ=="delete":
            if path.exists(): path.unlink()
            changes[rel]=None; summary.append(f"🗑 delete: {rel}")
        else: raise ValueError(f"operation نامعتبر: {typ}")
    return changes, summary

def _github_commit_changes(changes, message):
    token=os.getenv("GITHUB_TOKEN") or ""; repo=os.getenv("GITHUB_REPOSITORY") or ""
    if not token or not repo: return False,"GITHUB_TOKEN/GITHUB_REPOSITORY در این اجرا موجود نیست."
    branch=os.getenv("GITHUB_REF_NAME") or "main"; api=f"https://api.github.com/repos/{repo}"
    headers={"Accept":"application/vnd.github+json","Authorization":f"Bearer {token}","X-GitHub-Api-Version":"2022-11-28"}
    ref=requests.get(f"{api}/git/ref/heads/{branch}",headers=headers,timeout=15); ref.raise_for_status()
    commit_sha=ref.json()["object"]["sha"]
    base=requests.get(f"{api}/git/commits/{commit_sha}",headers=headers,timeout=15); base.raise_for_status()
    base_tree=base.json()["tree"]["sha"]; tree=[]
    for path,content in changes.items():
        if content is None: tree.append({"path":path,"mode":"100644","type":"blob","sha":None})
        else:
            blob=requests.post(f"{api}/git/blobs",headers=headers,json={"content":base64.b64encode(content).decode("ascii"),"encoding":"base64"},timeout=20)
            blob.raise_for_status(); tree.append({"path":path,"mode":"100644","type":"blob","sha":blob.json()["sha"]})
    tr=requests.post(f"{api}/git/trees",headers=headers,json={"base_tree":base_tree,"tree":tree},timeout=20); tr.raise_for_status()
    cm=requests.post(f"{api}/git/commits",headers=headers,json={"message":str(message)[:120],"tree":tr.json()["sha"],"parents":[commit_sha]},timeout=20); cm.raise_for_status()
    up=requests.patch(f"{api}/git/refs/heads/{branch}",headers=headers,json={"sha":cm.json()["sha"]},timeout=20); up.raise_for_status()
    return True,cm.json().get("html_url") or cm.json().get("sha")

def _github_dispatch(event_type="bot-redeploy"):
    token=os.getenv("GITHUB_TOKEN") or ""; repo=os.getenv("GITHUB_REPOSITORY") or ""
    if not token or not repo: return False,"GITHUB_TOKEN/GITHUB_REPOSITORY موجود نیست"
    headers={"Accept":"application/vnd.github+json","Authorization":f"Bearer {token}","X-GitHub-Api-Version":"2022-11-28"}
    r=requests.post(f"https://api.github.com/repos/{repo}/dispatches",headers=headers,json={"event_type":event_type},timeout=15)
    if not r.ok: return False,f"GitHub dispatch {r.status_code}: {r.text[:300]}"
    return True,"dispatch sent"

def strategy_info_menu(key):
    return inline([
        [{"text":"📖 توضیحات استراتژی","callback_data":"strategy:details:"+key}],
        [{"text":"⚙️ تنظیمات","callback_data":"strategy:settings:"+key}],
        [{"text":"↩️ بازگشت به استراتژی‌ها","callback_data":"menu:strategies"}],
    ])

def strategy_settings_menu(key):
    v=SETTINGS["strategies"].get(key,{})
    rows=[
        [{"text":("🟢 غیرفعال کردن" if v.get('enabled',False) else "🔴 فعال کردن"),"callback_data":"strategy:toggle:"+key}],
        [{"text":f"📌 نماد: {v.get('symbol','XAU/USD')}","callback_data":"strategy:symbol:"+key}],
        [{"text":f"⏱ تایم‌فریم: {v.get('timeframe','M5')}","callback_data":"strategy:tf:"+key}],
        [{"text":f"⏳ اسکن: {v.get('scan_interval',20)}s","callback_data":"strategy:interval:"+key},{"text":f"🔁 Cooldown: {v.get('cooldown',0)}s","callback_data":"strategy:cooldown:"+key}],
        [{"text":f"🔢 سقف روزانه: {'∞' if v.get('max_signals_per_day') in (None,0) else v.get('max_signals_per_day')}","callback_data":"strategy:max:"+key},{"text":("1️⃣ یک‌بار/کندل: روشن" if v.get('one_signal_per_candle',True) else "1️⃣ یک‌بار/کندل: خاموش"),"callback_data":"strategy:one:"+key}],
    ]
    if key=="sp2l":
        rows += [
            [{"text":f"↕️ جهت: {v.get('direction','BOTH')}","callback_data":"strategy:sp2l_direction:"+key},{"text":f"🎯 TP1: {v.get('tp1_r',1)}R","callback_data":"strategy:sp2l_tp1:"+key}],
            [{"text":f"🎯 TP2: {v.get('tp2_r',2)}R","callback_data":"strategy:sp2l_tp2:"+key},{"text":("2️⃣ 2X روشن" if v.get('use_second_entry_2x',True) else "2️⃣ 2X خاموش"),"callback_data":"strategy:sp2l_2x:"+key}],
        ]
    rows.append([{"text":"↩️ بازگشت","callback_data":"strategy:info:"+key}])
    return inline(rows)

def sp2l_direction_menu(key):
    return inline([[{"text":"BUY","callback_data":"strategy:set_sp2l_direction:"+key+":BUY"},{"text":"SELL","callback_data":"strategy:set_sp2l_direction:"+key+":SELL"},{"text":"BOTH","callback_data":"strategy:set_sp2l_direction:"+key+":BOTH"}],[{"text":"↩️ بازگشت","callback_data":"strategy:settings:"+key}]])

def sp2l_rr_menu(key,field,title):
    vals=[0.5,1.0,1.5,2.0,3.0,4.0]
    return inline([[{"text":f"{v:g}R","callback_data":f"strategy:set_sp2l_{field}:{key}:{v:g}"} for v in vals[i:i+3]] for i in range(0,len(vals),3)] + [[{"text":"↩️ بازگشت","callback_data":"strategy:settings:"+key}]])

def symbol_menu(key):
    return inline([
        [{"text":"🥇 XAU/USD","callback_data":"strategy:set_symbol:"+key+":XAU/USD"},{"text":"💵 EUR/USD","callback_data":"strategy:set_symbol:"+key+":EUR/USD"}],
        [{"text":"💷 GBP/USD","callback_data":"strategy:set_symbol:"+key+":GBP/USD"},{"text":"💴 USD/JPY","callback_data":"strategy:set_symbol:"+key+":USD/JPY"}],
        [{"text":"💷 GBP/JPY","callback_data":"strategy:set_symbol:"+key+":GBP/JPY"},{"text":"₿ BTC/USD","callback_data":"strategy:set_symbol:"+key+":BTC/USD"}],
        [{"text":"✏️ نماد دلخواه","callback_data":"strategy:custom_symbol:"+key}],
        [{"text":"↩️ بازگشت","callback_data":"strategy:settings:"+key}],
    ])

def timeframe_menu(key):
    vals=['M1','M5','M15','M30','H1','H4']
    rows=[]
    for i in range(0,len(vals),3):
        rows.append([{"text":v,"callback_data":"strategy:set_tf:"+key+":"+v} for v in vals[i:i+3]])
    rows.append([{"text":"↩️ بازگشت","callback_data":"strategy:settings:"+key}])
    return inline(rows)

def interval_menu(key):
    vals=[5,10,20,30,60,120]
    return inline([[{"text":f"{v} ثانیه","callback_data":f"strategy:set_interval:{key}:{v}"} for v in vals[i:i+3]] for i in range(0,len(vals),3)] + [[{"text":"↩️ بازگشت","callback_data":"strategy:settings:"+key}]])

def cooldown_menu(key):
    vals=[0,30,60,300,900,1800]
    return inline([[{"text":f"{v} ثانیه","callback_data":f"strategy:set_cooldown:{key}:{v}"} for v in vals[i:i+3]] for i in range(0,len(vals),3)] + [[{"text":"↩️ بازگشت","callback_data":"strategy:settings:"+key}]])

def max_menu(key):
    vals=[None,1,2,3,5,10,20,50]
    rows=[]
    for i in range(0,len(vals),4):
        row=[]
        for v in vals[i:i+4]: row.append({"text":"∞" if v is None else str(v),"callback_data":f"strategy:set_max:{key}:{'none' if v is None else v}"})
        rows.append(row)
    rows.append([{"text":"↩️ بازگشت","callback_data":"strategy:settings:"+key}])
    return inline(rows)

def strategy_page(key):
    info=STRATEGY_DESCRIPTIONS.get(key)
    if not info: return None
    v=SETTINGS["strategies"].get(key,{})
    return info,v,v.get("enabled",False)

def alias(s):
    s=s.strip(); return CFG.get("SYMBOL_ALIASES",{}).get(s.lower(),s.upper())

def get_price(s):
    sym=alias(s)
    p={"symbol":sym,"interval":"1min","outputsize":1,"apikey":CFG["TWELVEDATA_API_KEY"]}
    d=requests.get("https://api.twelvedata.com/time_series",params=p,timeout=15).json()
    if not d.get("values"): raise RuntimeError(d.get("message",str(d)))
    v=d["values"][0]; return sym,float(v["close"]),v["datetime"]

def _strategy_key(strategy):
    return strategy.key if hasattr(strategy,"key") else str(strategy)

def record_signal(strategy,symbol,direction,entry,sl,tp,text,extra=None):
    key=_strategy_key(strategy)
    with HISTORY_LOCK:
        item={
            "id":f"{key}-{len([x for x in HISTORY if x.get('strategy')==key])+1}",
            "time_utc":datetime.now(timezone.utc).isoformat(),
            "strategy":key,"symbol":symbol,"direction":direction,
            "entry":float(entry),"sl":float(sl),"tp":float(tp),
            "status":"OPEN","result_r":None,"result_price":None,
            "closed_time_utc":None,"message":text
        }
        item.setdefault("timeframe",SETTINGS.get("strategies",{}).get(key,{}).get("timeframe"))
        if extra: item.update(extra)
        HISTORY.append(item); HISTORY.sort(key=lambda x:str(x.get("time_utc",""))); save_history()
        return item

def can_emit(strategy_key):
    s=SETTINGS["strategies"].get(strategy_key,{})
    cooldown=max(0,int(s.get("cooldown",0) or 0))
    max_daily=s.get("max_signals_per_day")
    now=datetime.now(timezone.utc)
    if max_daily not in (None,0,"0","infinity"):
        try: max_daily=int(max_daily)
        except: max_daily=None
        if max_daily is not None:
            today=now.date().isoformat()
            count=sum(1 for x in HISTORY if x.get("strategy")==strategy_key and str(x.get("time_utc","" )).startswith(today))
            if count>=max_daily: return False,"daily_limit"
    if cooldown>0:
        last=None
        for x in reversed(HISTORY):
            if x.get("strategy")==strategy_key:
                try: last=datetime.fromisoformat(x["time_utc"].replace("Z","+00:00")); break
                except: pass
        if last and (now-last).total_seconds()<cooldown: return False,"cooldown"
    return True,None

def record_and_send(strategy,symbol,direction,entry,sl,tp,text,extra=None):
    key=_strategy_key(strategy)
    ok,reason=can_emit(key)
    if not ok:
        runtime_update(key, last_decision=("سیگنال پیدا شد اما ارسال متوقف شد: " + (
            "محدودیت روزانه" if reason=="daily_limit" else "Cooldown"
        )))
        return False
    label=STRATEGY_NAMES.get(key,key)
    if not text.startswith("🧠 استراتژی:"):
        text=f"🧠 استراتژی: {label}\n"+text
    item=record_signal(key,symbol,direction,entry,sl,tp,text,extra)
    runtime_update(key,last_decision="✅ موقعیت ورود پیدا شد و سیگنال ثبت شد",
                   last_signal_time=item.get("time_utc"))
    if SETTINGS["bot"].get("bale_enabled",True):
        broadcast_signal(text+"\n\n⏳ وضعیت: OPEN")
    return item

def signal_menu():
    return inline([[{"text":"📚 تاریخچه","callback_data":"menu:history"},{"text":"🧠 استراتژی‌ها","callback_data":"menu:strategies"}],
                   [{"text":"💰 قیمت XAU/USD","callback_data":"price:XAU/USD"},{"text":"🏠 منوی اصلی","callback_data":"menu:home"}]])

def broadcast_signal(text):
    """Owner gets interactive controls; admins receive signal-only text."""
    if not SETTINGS["bot"].get("bale_enabled",True): return
    oid=owner_chat_id()
    if oid: send(oid,text,signal_menu())
    for aid in admin_chat_ids(): send(aid,text,None)

def set_strategy_value(key,field,value):
    if key not in SETTINGS["strategies"]: return False
    with SETTINGS_LOCK:
        SETTINGS["strategies"][key][field]=value
        save_settings()
    WORKER_EVENTS[key].set()
    return True

def admin_menu():
    admins=admin_chat_ids()
    rows=[[{"text":"➕ افزودن ادمین","callback_data":"admin:add"}]]
    for aid in admins: rows.append([{"text":f"👤 {aid}","callback_data":f"admin:remove:{aid}"}])
    rows.append([{"text":"↩️ منوی اصلی","callback_data":"menu:home"}]); return inline(rows)

def prompt_add_admin(chat):
    PENDING_INPUT[str(chat)]={"type":"admin_id"}; send(chat,"👤 آیدی عددی کاربر را ارسال کن.\n\nادمین فقط سیگنال دریافت می‌کند و هیچ دسترسی مدیریتی ندارد.\n\nبرای لغو: /cancel")

def prompt_add_strategy(chat):
    PENDING_INPUT[str(chat)]={"type":"strategy_file"}; send(chat,"➕ افزودن استراتژی\n\nفایل Python استراتژی که از طرف من دریافت کرده‌ای را همینجا به‌صورت Document ارسال کن.\nفرمت: .py\n\nربات فایل را اعتبارسنجی و به‌عنوان Plugin ثبت می‌کند.\nبرای لغو: /cancel")

def set_admin_id(chat_id):
    aid=str(chat_id).strip()
    if not aid.isdigit() or aid==owner_chat_id(): return False,"آیدی نامعتبر یا آیدی مالک است."
    with SETTINGS_LOCK:
        ids=admin_chat_ids()
        if aid not in ids: ids.append(aid)
        SETTINGS["bot"]["admin_chat_ids"]=ids; SETTINGS["bot"]["allowed_chat_ids"]=[owner_chat_id()]+ids; save_settings()
    return True,"ادمین اضافه شد."

def remove_admin_id(aid):
    with SETTINGS_LOCK:
        ids=[x for x in admin_chat_ids() if x!=str(aid)]
        SETTINGS["bot"]["admin_chat_ids"]=ids; SETTINGS["bot"]["allowed_chat_ids"]=[owner_chat_id()]+ids; save_settings()
    return True

def download_bale_file(file_id):
    meta=bale("getFile",{"file_id":file_id}) or {}
    fp=meta.get("file_path") or meta.get("path")
    if not fp: raise RuntimeError("file_path دریافت نشد")
    url=f"https://tapi.bale.ai/file/bot{CFG['BALE_TOKEN']}/{fp}"
    r=requests.get(url,timeout=30); r.raise_for_status(); return r.content

def install_strategy_plugin(filename,content):
    if not filename.lower().endswith(".py"): raise ValueError("فقط فایل .py مجاز است")
    if len(content)>150*1024: raise ValueError("حجم فایل بیش از 150KB است")
    source=content.decode("utf-8")
    validate_plugin_source(source,filename)
    target=PLUGINS_DIR/Path(filename).name
    target.write_bytes(content)
    load_strategy_plugins()
    matches=[k for k,meta in PLUGIN_META.items() if meta.get("file")==target.name]
    if not matches: raise ValueError("استراتژی جدیدی از فایل استخراج نشد")
    key=matches[0]
    defaults={"enabled":True,"symbol":"XAU/USD","timeframe":"M5","scan_interval":20,"cooldown":0,"max_signals_per_day":None,"one_signal_per_candle":True}
    defaults.update(PLUGIN_META[key].get("defaults",{}))
    SETTINGS["strategies"].setdefault(key,defaults)
    save_settings()
    # Persist the plugin file into the GitHub repository so it survives the next runner.
    if os.getenv("GITHUB_ACTIONS")=="true" and os.getenv("GITHUB_TOKEN") and os.getenv("GITHUB_REPOSITORY"):
        try:
            api=f"https://api.github.com/repos/{os.getenv('GITHUB_REPOSITORY')}/contents/data/plugins/{target.name}"
            headers={"Accept":"application/vnd.github+json","Authorization":f"Bearer {os.getenv('GITHUB_TOKEN')}","X-GitHub-Api-Version":"2022-11-28"}
            old=requests.get(api,headers=headers,timeout=15); payload={"message":f"feat: add strategy plugin {key}","content":base64.b64encode(content).decode('ascii')}
            if old.ok: payload["sha"]=(old.json() or {}).get("sha")
            r=requests.put(api,headers=headers,json=payload,timeout=20); r.raise_for_status()
        except Exception as e: logging.warning("GitHub plugin persistence failed: %s",e)
    # Start the newly uploaded strategy immediately when the bot is already running.
    WORKER_EVENTS[key]=threading.Event(); RUNTIME[key]={"status":"idle","last_scan":None,"last_error":None}
    cls=PLUGIN_REGISTRY[key]
    try:
        obj=cls(CFG,SETTINGS,record_and_send,send,market_data_global)
        threading.Thread(target=worker_loop,args=(obj,),daemon=True,name=key).start()
    except Exception as e: logging.warning("Plugin runtime start failed: %s",e)
    return key

def prompt_custom_symbol(chat,key):
    PENDING_INPUT[str(chat)]={"type":"symbol","strategy":key}
    send(chat,f"✏️ نماد دلخواه برای «{STRATEGY_NAMES.get(key,key)}» را ارسال کن.\nمثال: XAU/USD یا EUR/USD\n\nبرای لغو: /cancel")

def execute_coding_manifest(manifest):
    changes,summary=_apply_coding_manifest(manifest)
    message=str(manifest.get("commit") or "chore: apply coding change")
    ok,detail=_github_commit_changes(changes,message)
    result={"summary":summary,"github_ok":ok,"github":detail,"restart":bool(manifest.get("restart",False))}
    if result["restart"] and ok:
        result["dispatch_ok"],result["dispatch"]=_github_dispatch("bot-redeploy")
    return result

def coding_status_text():
    gh="🟢 متصل" if os.getenv("GITHUB_TOKEN") and os.getenv("GITHUB_REPOSITORY") else "🔴 در دسترس نیست"
    return ("💻 وضعیت کدنویسی\n\n"
            f"GitHub: {gh}\n"
            f"Repository: {os.getenv('GITHUB_REPOSITORY') or '—'}\n"
            f"Branch: {os.getenv('GITHUB_REF_NAME') or 'main'}\n\n"
            "تغییرات فقط از حساب مالک پذیرفته می‌شوند.")

def coding_preview_text(manifest):
    ops=manifest.get("operations") if isinstance(manifest,dict) else None
    if not isinstance(ops,list) or not ops or len(ops)>20:
        raise ValueError("operations باید بین 1 تا 20 مورد باشد")
    lines=["💻 پیش‌نمایش تغییرات",""]
    for i,op in enumerate(ops,1):
        typ=str(op.get("op","" )).lower(); path=str(op.get("path",""))
        if typ not in {"write","patch","json_set","delete"}: raise ValueError(f"عملیات نامعتبر: {typ}")
        _safe_repo_path(path)
        lines.append(f"{i}. {typ.upper()} → {path}")
    commit=str(manifest.get("commit") or "chore: apply coding change")
    lines += ["",f"Commit: {commit[:120]}",f"Restart/Deploy: {'بله' if manifest.get('restart') else 'خیر'}"]
    return "\n".join(lines)

def handle_pending_input(chat,text):
    state=PENDING_INPUT.get(str(chat))
    if not state: return False
    if text.strip().lower()=='/cancel':
        PENDING_INPUT.pop(str(chat),None); send(chat,'❌ عملیات لغو شد.',strategy_settings_menu(state['strategy'])); return True
    if state['type']=='admin_id':
        ok,msg=set_admin_id(text); PENDING_INPUT.pop(str(chat),None); send(chat,('✅ '+msg if ok else '❌ '+msg),admin_menu()); return True
    if state['type']=='strategy_file':
        send(chat,'📎 برای افزودن استراتژی باید فایل .py را به‌صورت Document ارسال کنی.'); return True
    if state['type']=='provider_secret':
        try:
            set_provider_secret(state['provider'],text.strip()); key=state['provider']; PENDING_INPUT.pop(str(chat),None); send(chat,'✅ API Key با موفقیت در GitHub Secrets ذخیره شد و برای اجرای فعلی نیز فعال شد.',provider_info_menu(key))
        except Exception as e: send(chat,f'❌ ذخیره API Key انجام نشد.\n{e}',provider_info_menu(state['provider']))
        return True
    if state['type']=='coding_manifest':
        try:
            manifest=json.loads(text)
            preview=coding_preview_text(manifest)
            PENDING_INPUT[str(chat)]={"type":"coding_confirm","manifest":manifest}
            send(chat,preview+"\n\n⚠️ هیچ تغییری هنوز اعمال نشده است.\nآیا تأیید و Deploy شود؟",inline([[{"text":"✅ تأیید و Deploy","callback_data":"coding:confirm"},{"text":"❌ لغو","callback_data":"coding:cancel"}]]))
        except Exception as e:
            send(chat,f"❌ Manifest نامعتبر است.\n{e}",coding_menu())
        return True
    if state['type']=='coding_confirm':
        return True
    if state['type']=='symbol':
        value=alias(text)
        if not value or len(value)>40 or any(ch.isspace() for ch in value):
            send(chat,'❌ نماد نامعتبر است. نماد را مثل XAU/USD یا NAS100 ارسال کن.',strategy_settings_menu(state['strategy'])); return True
        key=state['strategy']; PENDING_INPUT.pop(str(chat),None); set_strategy_value(key,'symbol',value); send(chat,f'✅ نماد استراتژی روی {value} تنظیم شد.',strategy_settings_menu(key)); return True
    return False

def bale_loop():
    offset=0
    while True:
        try:
            updates=bale("getUpdates",{"offset":offset,"timeout":10})
            for u in updates:
                offset=max(offset,int(u.get("update_id",0))+1)
                if not SETTINGS["bot"].get("bale_enabled",True): continue
                cq=u.get("callback_query")
                if cq:
                    if not is_allowed_callback(cq): continue
                    chat=((cq.get("message") or {}).get("chat") or {}).get("id"); data=cq.get("data",""); answer_callback(cq.get("id"))
                    if not is_owner_chat({"id":chat,"type":"private"}) and (data.startswith("admin:") or data.startswith("strategy:") or data.startswith("menu:settings") or data.startswith("coding:") or data=="menu:coding" or data.startswith("provider:") or data=="menu:providers"):
                        continue
                    if not chat: continue
                    try:
                        if data=="menu:home": send(chat,"🏠 منوی اصلی\n\nیکی از گزینه‌ها را انتخاب کن:",main_menu())
                        elif data=="menu:prices": send(chat,"💰 قیمت کدام نماد؟",price_menu())
                        elif data.startswith("price:"):
                            sym,px,dt=get_price(data.split(":",1)[1]); send(chat,f"💰 {sym}\nPrice: {px}\nTime: {dt}",price_menu())
                        elif data=="menu:history": send_history(chat)
                        elif data.startswith("history:item:"):
                            parts=data.split(":",3); send_history_detail(chat,parts[2],parts[3] if len(parts)>3 else None)
                        elif data.startswith("history:"): send_history(chat,data.split(":",1)[1])
                        elif data=="menu:strategies": send(chat,"🧠 وضعیت استراتژی‌ها:",strategy_menu())
                        elif data=="menu:settings": send(chat,"⚙️ استراتژی موردنظر را برای تنظیم انتخاب کن:",strategy_menu())
                        elif data=="admin:menu": send(chat,"👤 مدیریت ادمین‌ها\n\nادمین فقط سیگنال دریافت می‌کند.",admin_menu())
                        elif data=="admin:add": prompt_add_admin(chat)
                        elif data.startswith("admin:remove:"):
                            aid=data.split(":",2)[2]; remove_admin_id(aid); send(chat,"✅ ادمین حذف شد.",admin_menu())
                        elif data=="strategy:add": prompt_add_strategy(chat)
                        elif data=="menu:status":
                            b=SETTINGS["bot"].get("bale_enabled",True); send(chat,f"📊 سیستم روشن است\nبله: {'🟢 فعال' if b else '🔴 خاموش'}\n\nاستراتژی‌ها مستقل اجرا می‌شوند.",main_menu())
                        elif data=="menu:diagnostics":
                            send(chat,"🩺 کدام بخش را عیب‌یابی کنم؟",diagnostic_menu())
                        elif data=="menu:providers":
                            send(chat,"📡 مدیریت APIهای بازار\n\nاولویت و فعال/غیرفعال بودن از همین بخش قابل تغییر است.",provider_menu())
                        elif data=="provider:health":
                            send(chat,provider_health_text(),provider_menu())
                        elif data.startswith("provider:info:"):
                            key=data.split(":",2)[2]; send(chat,provider_info(key),provider_info_menu(key))
                        elif data.startswith("provider:toggle:"):
                            key=data.split(":",2)[2]; SETTINGS["providers"][key]["enabled"]=not bool(SETTINGS["providers"][key].get("enabled",True)); save_settings(); send(chat,provider_info(key),provider_info_menu(key))
                        elif data.startswith("provider:up:") or data.startswith("provider:down:"):
                            parts=data.split(":"); key=parts[2]; provider_set_priority(key,"up" if parts[1]=="up" else "down"); send(chat,provider_info(key),provider_info_menu(key))
                        elif data.startswith("provider:key:"):
                            prompt_provider_key(chat,data.split(":",2)[2])
                        elif data.startswith("provider:test:"):
                            key=data.split(":",2)[2]
                            try:
                                if not market_data_global: raise RuntimeError("Market Data Manager آماده نیست")
                                vals=market_data_global._fetch(key,"XAU/USD",3,"M1","UTC")
                                send(chat,f"🧪 تست {SETTINGS['providers'][key].get('name',key)}\n\n🟢 موفق\nکندل دریافت‌شده: {len(vals)}\nآخرین کندل: {vals[-1].get('datetime') if vals else '—'}",provider_info_menu(key))
                            except Exception as e: send(chat,f"🧪 تست {SETTINGS['providers'].get(key,{}).get('name',key)}\n\n🔴 ناموفق\n{str(e)[:1000]}",provider_info_menu(key))
                        elif data.startswith("diag:"):
                            key=data.split(":",1)[1]
                            if key=="all":
                                text="\n\n".join(strategy_diagnostic_text(k) for k in STRATEGY_KEYS if k in SETTINGS.get("strategies",{}))
                                send(chat,text,diagnostic_menu())
                            elif key in SETTINGS.get("strategies",{}):
                                send(chat,strategy_diagnostic_text(key),diagnostic_menu())
                        elif data=="menu:coding":
                            send(chat,"💻 بخش کدنویسی\n\nفقط مالک دسترسی دارد.",coding_menu())
                        elif data=="coding:help":
                            send(chat,coding_help_text(),coding_menu())
                        elif data=="coding:input":
                            PENDING_INPUT[str(chat)]={"type":"coding_manifest"}
                            send(chat,"🧩 JSON تغییرات را ارسال کن.\n\nبرای امنیت، فقط عملیات تعریف‌شده در راهنما پذیرفته می‌شود و Manifest باید صریحاً ارسال شود.\n\nبرای لغو: /cancel")
                        elif data=="coding:confirm":
                            state=PENDING_INPUT.get(str(chat))
                            if not state or state.get("type")!="coding_confirm":
                                send(chat,"❌ درخواست کدنویسی منقضی شده است.",coding_menu())
                            else:
                                try:
                                    result=execute_coding_manifest(state["manifest"])
                                    PENDING_INPUT.pop(str(chat),None)
                                    lines=["✅ تغییرات کدنویسی اعمال شد.",""]+result["summary"]
                                    lines += ["",("✅ GitHub Commit شد." if result["github_ok"] else "⚠️ Commit به GitHub انجام نشد: "+str(result["github"]))]
                                    if result.get("restart"):
                                        lines.append("🔄 درخواست Restart/Deploy ارسال شد." if result.get("dispatch_ok") else "⚠️ Restart انجام نشد: "+str(result.get("dispatch")))
                                    send(chat,"\n".join(lines),coding_menu())
                                except Exception as e:
                                    PENDING_INPUT.pop(str(chat),None)
                                    send(chat,f"❌ تغییرات اعمال نشد.\n{e}",coding_menu())
                        elif data=="coding:cancel":
                            PENDING_INPUT.pop(str(chat),None)
                            send(chat,"❌ تغییرات کدنویسی لغو شد و هیچ تغییری اعمال نشد.",coding_menu())
                        elif data=="coding:status":
                            send(chat,coding_status_text(),coding_menu())
                        elif data=="coding:restart":
                            ok,msg=_github_dispatch("bot-redeploy")
                            send(chat,("✅ درخواست Restart به GitHub ارسال شد." if ok else "❌ Restart نشد: "+str(msg)),coding_menu())
                        elif data.startswith("strategy:info:"):
                            key=data.split(":",2)[2]
                            if key in SETTINGS["strategies"]: send(chat,strategy_info(key),strategy_info_menu(key))
                        elif data.startswith("strategy:details:"):
                            key=data.split(":",2)[2]
                            if key in SETTINGS["strategies"]: send(chat,strategy_details(key),strategy_info_menu(key))
                        elif data.startswith("strategy:settings:"):
                            key=data.split(":",2)[2]
                            if key in SETTINGS["strategies"]: send(chat,f"⚙️ تنظیمات «{STRATEGY_NAMES.get(key,key)}»",strategy_settings_menu(key))
                        elif data.startswith("strategy:toggle:"):
                            key=data.split(":",2)[2]
                            if key in SETTINGS["strategies"]:
                                val=not bool(SETTINGS["strategies"][key].get('enabled',False)); set_strategy_value(key,'enabled',val); send(chat,f"{'🟢 فعال شد' if val else '🔴 غیرفعال شد'}\n\n{strategy_info(key)}",strategy_settings_menu(key))
                        elif data.startswith("strategy:symbol:"):
                            key=data.split(":",2)[2]; send(chat,"📌 نماد را انتخاب کن:",symbol_menu(key))
                        elif data.startswith("strategy:set_symbol:"):
                            _,_,key,sym=data.split(":",3); set_strategy_value(key,'symbol',sym); send(chat,f"✅ نماد {sym} برای {STRATEGY_NAMES.get(key,key)} تنظیم شد.",strategy_settings_menu(key))
                        elif data.startswith("strategy:custom_symbol:"):
                            key=data.split(":",2)[2]; prompt_custom_symbol(chat,key)
                        elif data.startswith("strategy:tf:"):
                            key=data.split(":",2)[2]; send(chat,"⏱ تایم‌فریم را انتخاب کن:",timeframe_menu(key))
                        elif data.startswith("strategy:set_tf:"):
                            _,_,key,tf=data.split(":",3); set_strategy_value(key,'timeframe',tf); send(chat,f"✅ تایم‌فریم {tf} برای {STRATEGY_NAMES.get(key,key)} تنظیم شد.",strategy_settings_menu(key))
                        elif data.startswith("strategy:interval:"):
                            key=data.split(":",2)[2]; send(chat,"⏳ فاصله اسکن را انتخاب کن:",interval_menu(key))
                        elif data.startswith("strategy:set_interval:"):
                            _,_,key,val=data.split(":",3); set_strategy_value(key,'scan_interval',max(1,int(val))); send(chat,"✅ فاصله اسکن ذخیره شد.",strategy_settings_menu(key))
                        elif data.startswith("strategy:cooldown:"):
                            key=data.split(":",2)[2]; send(chat,"🔁 Cooldown را انتخاب کن:",cooldown_menu(key))
                        elif data.startswith("strategy:set_cooldown:"):
                            _,_,key,val=data.split(":",3); set_strategy_value(key,'cooldown',max(0,int(val))); send(chat,"✅ Cooldown ذخیره شد.",strategy_settings_menu(key))
                        elif data.startswith("strategy:max:"):
                            key=data.split(":",2)[2]; send(chat,"🔢 حداکثر سیگنال روزانه را انتخاب کن:",max_menu(key))
                        elif data.startswith("strategy:set_max:"):
                            _,_,key,val=data.split(":",3); set_strategy_value(key,'max_signals_per_day',None if val=='none' else int(val)); send(chat,"✅ سقف سیگنال روزانه ذخیره شد.",strategy_settings_menu(key))
                        elif data.startswith("strategy:sp2l_direction:"):
                            key=data.split(":",2)[2]; send(chat,"↕️ جهت معاملات SP2L را انتخاب کن:",sp2l_direction_menu(key))
                        elif data.startswith("strategy:set_sp2l_direction:"):
                            _,_,_,key,val=data.split(":",4); set_strategy_value(key,'direction',val); send(chat,"✅ جهت SP2L ذخیره شد.",strategy_settings_menu(key))
                        elif data.startswith("strategy:sp2l_tp1:"):
                            key=data.split(":",2)[2]; send(chat,"🎯 RR مربوط به TP1 را انتخاب کن:",sp2l_rr_menu(key,'tp1','TP1'))
                        elif data.startswith("strategy:set_sp2l_tp1:"):
                            _,_,_,key,val=data.split(":",4); set_strategy_value(key,'tp1_r',float(val)); send(chat,"✅ TP1 ذخیره شد.",strategy_settings_menu(key))
                        elif data.startswith("strategy:sp2l_tp2:"):
                            key=data.split(":",2)[2]; send(chat,"🎯 RR مربوط به TP2 را انتخاب کن:",sp2l_rr_menu(key,'tp2','TP2'))
                        elif data.startswith("strategy:set_sp2l_tp2:"):
                            _,_,_,key,val=data.split(":",4); set_strategy_value(key,'tp2_r',float(val)); send(chat,"✅ TP2 ذخیره شد.",strategy_settings_menu(key))
                        elif data.startswith("strategy:sp2l_2x:"):
                            key=data.split(":",2)[2]; val=not bool(SETTINGS['strategies'][key].get('use_tp2',True)); set_strategy_value(key,'use_second_entry_2x',val); send(chat,"✅ وضعیت 2X تغییر کرد.",strategy_settings_menu(key))
                        elif data.startswith("strategy:one:"):
                            key=data.split(":",2)[2]; val=not bool(SETTINGS['strategies'][key].get('one_signal_per_candle',True)); set_strategy_value(key,'one_signal_per_candle',val); send(chat,"✅ تنظیم یک سیگنال برای هر کندل تغییر کرد.",strategy_settings_menu(key))
                    except Exception as e:
                        logging.warning('Bale callback error: %s',e); send(chat,f'❌ خطا: {e}',main_menu())
                    continue
                m=u.get("message") or {}; chat_obj=m.get("chat") or {}; chat=chat_obj.get("id"); text=(m.get("text") or "").strip()
                if not is_allowed_private_chat(chat_obj) or not chat: continue
                if is_admin_chat(chat_obj): continue
                doc=m.get("document") or {}
                if doc and is_owner_chat(chat_obj) and PENDING_INPUT.get(str(chat),{}).get("type")=="coding_manifest":
                    try:
                        filename=str(doc.get("file_name","changes.json"))
                        if not filename.lower().endswith(".json"):
                            raise ValueError("فایل کدنویسی باید .json باشد")
                        manifest=json.loads(download_bale_file(doc.get("file_id")))
                        preview=coding_preview_text(manifest)
                        PENDING_INPUT[str(chat)]={"type":"coding_confirm","manifest":manifest}
                        send(chat,preview+"\n\n⚠️ هیچ تغییری هنوز اعمال نشده است.\nآیا تأیید و Deploy شود؟",inline([[{"text":"✅ تأیید و Deploy","callback_data":"coding:confirm"},{"text":"❌ لغو","callback_data":"coding:cancel"}]]))
                    except Exception as e:
                        send(chat,f"❌ تغییرات اعمال نشد.\n{e}",coding_menu())
                    continue
                if doc and is_owner_chat(chat_obj) and PENDING_INPUT.get(str(chat),{}).get("type")=="strategy_file":
                    try:
                        key=install_strategy_plugin(doc.get("file_name","strategy.py"),download_bale_file(doc.get("file_id")))
                        PENDING_INPUT.pop(str(chat),None); send(chat,f"✅ استراتژی «{STRATEGY_NAMES.get(key,key)}» اضافه شد.\n\nاز این به بعد در منوی استراتژی‌ها قابل تنظیم و اجراست.",strategy_menu())
                    except Exception as e: send(chat,f"❌ فایل استراتژی پذیرفته نشد.\n{e}",strategy_menu())
                    continue
                if handle_pending_input(chat,text): continue
                p=text.split(); cmd=p[0].lower().split("@")[0] if p else ""
                if cmd=="/start": send(chat,"🤖 ربات آماده است. از دکمه‌ها استفاده کن:",main_menu())
                elif cmd=="/help": send(chat,"برای استفاده از ربات نیازی به تایپ دستور نیست؛ از دکمه‌های شیشه‌ای استفاده کن.",main_menu())
                elif cmd=="/cancel": send(chat,"عملیات فعالی وجود ندارد.",main_menu())
                elif cmd=="/price" and len(p)>=2:
                    try:
                        sym,px,dt=get_price(" ".join(p[1:])); send(chat,f"💰 {sym}\nPrice: {px}\nTime: {dt}",price_menu())
                    except Exception as e: send(chat,f"❌ قیمت دریافت نشد.\n{e}",price_menu())
                elif cmd=="/history": send_history(chat)
                elif cmd=="/strategies": send(chat,"🧠 وضعیت استراتژی‌ها:",strategy_menu())
        except Exception as e:
            logging.error("Bale: %s",e); time.sleep(5)

def history_menu():
    rows=[]
    for key in STRATEGY_KEYS:
        if key in SETTINGS.get("strategies",{}): rows.append([{"text":f"📊 {STRATEGY_NAMES.get(key,key)}","callback_data":f"history:{key}"}])
    rows.append([{"text":"📚 همه سیگنال‌ها","callback_data":"history:all"}])
    rows.append([{"text":"↩️ منوی اصلی","callback_data":"menu:home"}])
    return inline(rows)

def _history_status(x):
    s=x.get("status","OPEN"); return "⏳ OPEN" if s=="OPEN" else ("✅ TP HIT" if s=="TP_HIT" else ("❌ SL HIT" if s=="SL_HIT" else "⚠️ BOTH"))

def history_detail_menu(item_id, back_key):
    return inline([[{"text":"⬅️ بازگشت به تاریخچه","callback_data":f"history:{back_key}"}], [{"text":"🏠 منوی اصلی","callback_data":"menu:home"}]])

def send_history_detail(chat,item_id,back_key=None):
    item=next((x for x in HISTORY if str(x.get("id"))==str(item_id)),None)
    if not item: send(chat,"❌ سیگنال پیدا نشد.",history_menu()); return
    key=item.get("strategy","unknown"); status=_history_status(item); rr=item.get("result_r")
    lines=[f"📌 سیگنال {STRATEGY_NAMES.get(key,key)}","",f"وضعیت: {status}",f"نماد: {item.get('symbol','—')}",f"جهت: {item.get('direction','—')}",f"زمان سیگنال: {item.get('time_utc','—')}","",f"Entry: {item.get('entry','—')}",f"SL: {item.get('sl','—')}",f"TP: {item.get('tp','—')}"]
    for label,field in (("TP2","tp2"),("ورود دوم 2X","second_entry"),("RR","rr"),("تایم‌فریم","timeframe")):
        if item.get(field) is not None: lines.append(f"{label}: {item[field]}")
    if rr is not None: lines.append(f"نتیجه: {float(rr):+.2f}R")
    if item.get("result_price") is not None: lines.append(f"قیمت نتیجه: {item['result_price']}")
    if item.get("closed_time_utc"): lines.append(f"زمان بسته‌شدن: {item['closed_time_utc']}")
    if item.get("message"): lines += ["","📝 جزئیات سیگنال:",item["message"]]
    send(chat,"\n".join(lines),history_detail_menu(item_id,back_key or key))

def format_history(items,title,key):
    if not items: return f"📚 {title}\n\nهنوز سیگنالی ثبت نشده.", history_menu()
    rows=[]
    for x in items[-12:][::-1]:
        icon="⏳" if x.get("status")=="OPEN" else ("✅" if x.get("status")=="TP_HIT" else "❌")
        rr="" if x.get("result_r") is None else f" {float(x['result_r']):+.1f}R"
        label=f"{icon} {x.get('direction')} | {x.get('entry')} → {x.get('tp')} | {x.get('status','OPEN')}{rr}"
        rows.append([{"text":label[:60],"callback_data":f"history:item:{x.get('id')}:{key}"}])
    rows.append([{"text":"⬅️ انتخاب استراتژی","callback_data":"menu:history"}])
    return f"📚 {title}\n\nبرای دیدن جزئیات هر سیگنال روی آن بزن:", inline(rows)

def send_history(chat,key=None):
    if key is None: send(chat,"📚 تاریخچه کدام استراتژی؟",history_menu()); return
    items=HISTORY if key=="all" else [x for x in HISTORY if x.get("strategy")==key]
    title="همه سیگنال‌ها" if key=="all" else STRATEGY_NAMES.get(key,key)
    text,markup=format_history(items,title,key); send(chat,text,markup)

def _parse_utc(value):
    try: return datetime.fromisoformat(str(value).replace("Z","+00:00")).astimezone(timezone.utc)
    except Exception: return None

def _update_open_results():
    open_items=[x for x in HISTORY if x.get("status")=="OPEN"]
    if not open_items: return
    changed=False
    cache={}
    now=datetime.now(timezone.utc)
    for item in open_items:
        key=item.get("strategy"); symbol=item.get("symbol","XAU/USD")
        signal_time=_parse_utc(item.get("time_utc"))
        if not signal_time: continue
        try:
            bars=cache.get(symbol)
            if bars is None:
                vals=market_data_global.get_bars(symbol,500,"UTC", "M1") if market_data_global else []
                bars=[]
                for v in vals:
                    dt=datetime.strptime(v["datetime"],"%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                    bars.append({"dt":dt,"high":float(v["high"]),"low":float(v["low"]),"close":float(v["close"])})
                cache[symbol]=bars
            entry=float(item["entry"]); sl=float(item["sl"]); tp=float(item["tp"])
            direction=str(item.get("direction","BUY")).upper()
            hit=None; hit_price=None; hit_time=None
            for b in bars:
                if b["dt"] < signal_time: continue
                if direction=="BUY":
                    sl_hit=b["low"]<=sl; tp_hit=b["high"]>=tp
                else:
                    sl_hit=b["high"]>=sl; tp_hit=b["low"]<=tp
                if sl_hit and tp_hit:
                    # One M1 candle can touch both levels; sequence is unknown.
                    # Mark it ambiguous instead of falsely claiming TP/SL order.
                    hit="BOTH_SAME_BAR"; hit_price=None; hit_time=b["dt"]; break
                if sl_hit:
                    hit="SL_HIT"; hit_price=sl; hit_time=b["dt"]; break
                if tp_hit:
                    hit="TP_HIT"; hit_price=tp; hit_time=b["dt"]; break
            if hit is None and bars:
                latest=bars[-1]
                # Current completed M1 bar is enough for a reliable historical check.
            if hit:
                item["status"]=hit; item["result_price"]=hit_price; item["closed_time_utc"]=hit_time.isoformat() if hit_time else now.isoformat()
                risk=abs(entry-sl)
                item["result_r"]=None if not risk or hit=="BOTH_SAME_BAR" else ((tp-entry)/risk if hit=="TP_HIT" and direction=="BUY" else (entry-tp)/risk if hit=="TP_HIT" else -1.0)
                if hit=="BOTH_SAME_BAR":
                    msg=(f"⚠️ نتیجه مبهم | {STRATEGY_NAMES.get(key,key)}\n"
                         f"{symbol} {direction}\nهر دو سطح TP و SL در یک کندل M1 لمس شدند و ترتیب دقیق قابل تشخیص نیست.\n"
                         f"Entry: {entry:.5f} | TP: {tp:.5f} | SL: {sl:.5f}")
                else:
                    icon="✅" if hit=="TP_HIT" else "❌"; rr=item.get("result_r")
                    msg=(f"{icon} {'TP خورد' if hit=='TP_HIT' else 'SL خورد'}\n"
                         f"🧠 استراتژی: {STRATEGY_NAMES.get(key,key)}\n"
                         f"{symbol} {direction}\nEntry: {entry:.5f}\n"
                         f"نتیجه: {float(rr):+.2f}R\nزمان: {hit_time.strftime('%Y-%m-%d %H:%M')} UTC")
                if SETTINGS["bot"].get("bale_enabled",True): broadcast_signal(msg)
                changed=True
        except Exception as e:
            logging.warning("result monitor %s: %s",item.get("id"),e)
    if changed: save_history()

def result_monitor_loop():
    while True:
        try:
            _update_open_results()
        except Exception as e: logging.warning("result monitor: %s",e)
        time.sleep(15)

def worker_loop(strategy):
    key=strategy.key; event=WORKER_EVENTS[key]
    while True:
        try:
            cfgs=SETTINGS["strategies"].get(key,{})
            enabled=cfgs.get("enabled",False)
            interval=max(1,int(cfgs.get("scan_interval",20) or 20))
            if enabled:
                now_iso=datetime.now(timezone.utc).isoformat()
                before_count=sum(1 for x in HISTORY if x.get("strategy")==key)
                runtime_update(key,status="scanning",phase="market_data",last_scan=now_iso,
                               last_error=None,scan_count=RUNTIME[key].get("scan_count",0)+1)
                # A cheap cached data probe separates infrastructure/API failures
                # from a strategy that simply has no setup at the moment.
                try:
                    symbol=str(cfgs.get("symbol","XAU/USD")).strip() or "XAU/USD"
                    timeframe=str(cfgs.get("timeframe","M5")).upper()
                    probe=market_data_global.get_bars(symbol,3,"UTC", timeframe) if market_data_global else []
                    latest=probe[-1] if probe else None
                    runtime_update(key,market_data="ok" if probe else "empty",
                                   bars_count=len(probe),
                                   latest_bar=(latest.get("datetime") if isinstance(latest,dict) else None),
                                   data_provider=(market_data_global.diagnostics().get("last_provider") if market_data_global else "—"))
                except Exception as data_error:
                    runtime_update(key,market_data="error",last_decision="❌ مشکل در دریافت داده بازار")
                    raise
                runtime_update(key,phase="strategy")
                strategy.run_once()
                after_count=sum(1 for x in HISTORY if x.get("strategy")==key)
                if after_count>before_count:
                    runtime_update(key,last_decision="✅ موقعیت ورود پیدا شد و سیگنال ثبت شد",
                                   last_signal_time=HISTORY[-1].get("time_utc") if HISTORY else None)
                elif RUNTIME[key].get("last_decision","").startswith("سیگنال پیدا شد"):
                    pass
                else:
                    # Successful execution with no new history item means the
                    # strategy did not find a valid entry (or its own filters
                    # rejected the setup). It is explicitly not a system error.
                    runtime_update(key,last_decision="🟡 سیستم سالم است؛ در آخرین اسکن موقعیت ورود معتبر پیدا نشد")
                runtime_update(key,status="ok",phase="idle")
            else:
                runtime_update(key,status="disabled",phase="idle",last_decision="استراتژی غیرفعال است")
            event.wait(interval); event.clear()
        except Exception as e:
            runtime_update(key,status="error",phase="error",last_error=str(e),
                           error_count=RUNTIME[key].get("error_count",0)+1,
                           last_decision="🔴 خطای فنی؛ استراتژی کامل اجرا نشد")
            logging.error("%s worker: %s",key,e)
            event.wait(2); event.clear()

app=Flask(__name__)
HTML=r"""<!doctype html><html lang='fa' dir='rtl'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>Trading Control Center</title>
<style>*{box-sizing:border-box}body{margin:0;min-height:100vh;font-family:Vazirmatn,Tahoma,Arial;background:#07101d;color:#eef4ff}.wrap{max-width:1000px;margin:auto;padding:24px 14px}.glass{background:rgba(255,255,255,.065);border:1px solid rgba(255,255,255,.11);box-shadow:0 18px 50px #0005;backdrop-filter:blur(18px);border-radius:20px}.header{padding:20px;margin-bottom:14px}.title{font-size:21px;font-weight:800}.muted{color:#94a3b8;font-size:12px;margin-top:5px}.toolbar{display:flex;gap:10px;flex-wrap:wrap;margin-top:15px}.btn{border:1px solid #ffffff1c;background:#ffffff0d;color:#fff;border-radius:12px;padding:10px 14px;cursor:pointer}.btn.active{background:#16a34a55}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:14px}.card{padding:18px}.row{display:flex;align-items:center;justify-content:space-between;gap:10px}.name{font-size:16px;font-weight:800}.pill{font-size:11px;color:#b9c6d8;margin-top:5px}.settings{margin-top:16px;padding-top:14px;border-top:1px solid #ffffff10;display:grid;gap:10px}.field{display:flex;align-items:center;justify-content:space-between;gap:12px}.field label{font-size:12px;color:#cbd5e1}.input{width:115px;background:#07111f;border:1px solid #ffffff18;color:#fff;border-radius:10px;padding:9px;text-align:center}.small{font-size:11px;color:#8291a7}.switch{width:52px;height:29px;position:relative;display:inline-block}.switch input{display:none}.slider{position:absolute;inset:0;border-radius:99px;background:#334155;cursor:pointer}.slider:before{content:'';position:absolute;width:21px;height:21px;right:4px;top:4px;background:white;border-radius:50%;transition:.2s}input:checked+.slider{background:#16a34a}input:checked+.slider:before{transform:translateX(-23px)}table{width:100%;border-collapse:collapse;font-size:11px;margin-top:12px}td,th{padding:9px;border-bottom:1px solid #ffffff0b;text-align:right}.status{display:flex;align-items:center;gap:8px}.dot{width:9px;height:9px;border-radius:50%;background:#22c55e;box-shadow:0 0 14px #22c55e}.dot.off{background:#ef4444;box-shadow:0 0 14px #ef4444}.modal{position:fixed;inset:0;background:#0008;display:none;align-items:center;justify-content:center;padding:18px}.modal.open{display:flex}.modalbox{width:min(560px,100%);padding:20px}.save{width:100%;margin-top:8px;background:#ffffff0d;border:1px solid #ffffff1b;color:#fff;padding:11px;border-radius:11px;cursor:pointer}</style></head><body><div class='wrap'>
<div class='glass header'><div class='row'><div><div class='title'>🧠 Trading Control Center</div><div class='muted'>کنترل مستقل استراتژی‌ها، زمان‌بندی و بله</div></div><div class='status'><span class='dot {{"off" if not bot.bale_enabled else ""}}'></span><span>{{"بله خاموش" if not bot.bale_enabled else "بله روشن"}}</span></div></div><div class='toolbar'><button class='btn {{"active" if bot.bale_enabled else ""}}' onclick='toggleBale()'>{{"🔴 خاموش کردن بله" if bot.bale_enabled else "🟢 روشن کردن بله"}}</button></div></div>
<div class='glass card' style='margin-bottom:14px'><div class='row'><div><div class='name'>🔐 دسترسی به ربات بله</div><div class='pill'>فقط آیدی‌های عددی این فهرست می‌توانند پیام بدهند، دکمه‌ها را استفاده کنند و سیگنال دریافت کنند.</div></div></div><div class='settings'><div class='field' style='align-items:flex-start;flex-direction:column'><label>آیدی‌های مجاز (هر آیدی در یک خط)</label><textarea id='allowed-ids' class='input' style='width:100%;min-height:130px;text-align:left;direction:ltr;resize:vertical'>{{ allowed_ids|join('\n') }}</textarea></div><div class='small'>آیدی‌ها باید فقط عدد باشند. آیدی مالک فعلی در شروع به‌صورت خودکار حفظ می‌شود.</div><button class='save' onclick='saveAccess()'>💾 ذخیره لیست دسترسی</button></div></div>
<div class='grid'>{% for k,v in strategies.items() %}<div class='glass card'><div class='row'><div><div class='name'>{{names.get(k,k)}}</div><div class='pill'>{{descs.get(k,'')}} · {{runtime[k].status}} · آخرین اسکن: {{runtime[k].last_scan or '—'}}<br>🩺 {{runtime[k].last_decision or '—'}}{% if runtime[k].last_error %}<br>🔴 {{runtime[k].last_error}}{% endif %}</div></div><label class='switch'><input id='en-{{k}}' type='checkbox' {% if v.enabled %}checked{% endif %} onchange='saveStrategy("{{k}}")'><span class='slider'></span></label></div><div class='settings'><div class='field'><label>نماد</label><input class='input' id='sym-{{k}}' type='text' value='{{v.symbol}}'></div><div class='field'><label>تایم‌فریم</label><input class='input' id='tf-{{k}}' type='text' value='{{v.timeframe}}'><div></div></div><div class='field'><label>فاصله اسکن (ثانیه)</label><input class='input' id='int-{{k}}' type='number' min='1' value='{{v.scan_interval}}'></div><div class='field'><label>Cooldown (ثانیه)</label><input class='input' id='cool-{{k}}' type='number' min='0' value='{{v.cooldown}}'></div><div class='field'><label>حداکثر سیگنال روزانه</label><input class='input' id='max-{{k}}' type='text' value='{{"∞" if v.max_signals_per_day in [None,0] else v.max_signals_per_day}}' placeholder='∞'></div><div class='field'><label>یک سیگنال برای هر کندل</label><label class='switch'><input id='one-{{k}}' type='checkbox' {% if v.one_signal_per_candle %}checked{% endif %}><span class='slider'></span></label></div><button class='save' onclick='saveStrategy("{{k}}")'>ذخیره تنظیمات</button><a class='save' style='display:block;text-align:center;text-decoration:none;margin-top:8px' href='/strategy/{{k}}'>📖 توضیحات کامل استراتژی</a></div></div>{% endfor %}</div>
{% for sk in ['ny_orb','vwap_wick_rejection','sp2l'] %}<div class='glass card' style='margin-top:14px'><div class='name'>📚 تاریخچه {{names.get(sk,sk)}}</div><table><tr><th>نماد</th><th>جهت</th><th>Entry</th><th>SL</th><th>TP</th><th>وضعیت</th><th>نتیجه</th></tr>{% for x in history if x.strategy==sk %}<tr><td>{{x.symbol}}</td><td>{{x.direction}}</td><td>{{x.entry}}</td><td>{{x.sl}}</td><td>{{x.tp}}</td><td>{{x.status or 'OPEN'}}</td><td>{{x.result_r if x.result_r is not none else '—'}}</td></tr>{% endfor %}</table></div>{% endfor %}</div>
<script>async function saveStrategy(k){const raw=document.getElementById('max-'+k).value.trim();let max=null;if(raw&&raw!=='∞'&&raw.toLowerCase()!=='infinity'){max=parseInt(raw);if(isNaN(max)||max<1){max=null;document.getElementById('max-'+k).value='∞'}}await fetch('/api/strategy/'+k,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled:document.getElementById('en-'+k).checked,symbol:document.getElementById('sym-'+k).value.trim(),timeframe:document.getElementById('tf-'+k).value.trim().toUpperCase(),scan_interval:parseInt(document.getElementById('int-'+k).value)||20,cooldown:parseInt(document.getElementById('cool-'+k).value)||0,max_signals_per_day:max,one_signal_per_candle:document.getElementById('one-'+k).checked})});location.reload()}async function toggleBale(){await fetch('/api/bale',{method:'POST'});location.reload()}async function saveAccess(){const raw=document.getElementById('allowed-ids').value.split(/\s+/).map(x=>x.trim()).filter(Boolean);const ids=[...new Set(raw)];if(ids.some(x=>!/^[0-9]+$/.test(x))){alert('فقط آیدی عددی مجاز است.');return}const r=await fetch('/api/access',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({allowed_chat_ids:ids})});const d=await r.json();if(!d.ok){alert(d.error||'ذخیره نشد');return}location.reload()}</script></body></html>"""

@app.get('/')
def dashboard():
    descs={k:(v.get("summary") if isinstance(v,dict) else "") for k,v in STRATEGY_DESCRIPTIONS.items()}
    return render_template_string(HTML,strategies=SETTINGS['strategies'],history=HISTORY,bot=SETTINGS['bot'],allowed_ids=allowed_chat_ids(),runtime=RUNTIME,names=STRATEGY_NAMES,descs=descs)

@app.get('/strategy/<key>')
def strategy_page_route(key):
    result=strategy_page(key)
    if result is None:
        return "استراتژی پیدا نشد.", 404
    info,v,enabled=result
    page = """<!doctype html>
<html lang='fa' dir='rtl'>
<head>
<meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>
<title>{{ info.title }}</title>
<style>
*{box-sizing:border-box}body{margin:0;background:#07101d;color:#eef4ff;font-family:Vazirmatn,Tahoma,Arial;line-height:1.95}
.wrap{max-width:900px;margin:auto;padding:22px 14px}.glass{background:rgba(255,255,255,.065);border:1px solid rgba(255,255,255,.11);box-shadow:0 18px 50px #0005;backdrop-filter:blur(18px);border-radius:20px}
.header{padding:22px}.title{font-size:24px;font-weight:900}.summary{color:#cbd5e1;margin-top:10px}.status{display:inline-block;margin-top:14px;padding:5px 12px;border-radius:99px;background:rgba(34,197,94,.16);color:#86efac;font-size:12px}.off{background:rgba(239,68,68,.16);color:#fca5a5}
.section{padding:22px;margin-top:14px}.section h2{font-size:17px;margin:0 0 12px}.content{color:#dbe5f3}.item{padding:9px 0;border-bottom:1px solid #ffffff0d}.item:last-child{border-bottom:0}.label{font-weight:800;color:#fff}.bullet{margin:6px 0 0;padding-right:18px}.bullet li{margin:6px 0}.settings-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:10px}.setting{padding:12px;border:1px solid #ffffff0d;border-radius:12px;background:#ffffff05}.setting b{display:block;font-size:12px;color:#94a3b8;margin-bottom:4px}.setting span{font-weight:700}.back{display:inline-block;margin-top:14px;color:#fff;text-decoration:none;border:1px solid #ffffff1c;background:#ffffff0d;padding:9px 14px;border-radius:12px}
</style></head>
<body><div class='wrap'>
<div class='glass header'>
<div class='title'>{{ info.title }}</div>
<div class='summary'>{{ info.summary }}</div>
<span class='status {{ "off" if not enabled else "" }}'>{{ "🟢 فعال" if enabled else "🔴 خاموش" }}</span>
</div>
{% for title, items in info.sections %}<div class='glass section'><h2>{{ title }}</h2><div class='content'><ul class='bullet'>{% for item in items %}<li>{{ item }}</li>{% endfor %}</ul></div></div>{% endfor %}
<div class='glass section'><h2>⚙️ تنظیمات فعلی ربات</h2><div class='settings-grid'><div class='setting'><b>نماد</b><span>{{ v.get('symbol','XAU/USD') }}</span></div><div class='setting'><b>تایم‌فریم</b><span>{{ v.get('timeframe','M5') }}</span></div><div class='setting'><b>فاصله اسکن</b><span>هر {{ v.get('scan_interval',20) }} ثانیه</span></div><div class='setting'><b>Cooldown</b><span>{{ v.get('cooldown',0) }} ثانیه</span></div><div class='setting'><b>حداکثر سیگنال روزانه</b><span>{{ '∞' if v.get('max_signals_per_day') in [None,0,'0','infinity'] else v.get('max_signals_per_day') }}</span></div><div class='setting'><b>یک سیگنال برای هر کندل</b><span>{{ 'فعال' if v.get('one_signal_per_candle',True) else 'خاموش' }}</span></div></div></div>
<a class='back' href='/'>↩️ بازگشت به پنل</a>
</div></body></html>"""
    return render_template_string(page,info=info,v=v,enabled=enabled)

@app.post('/api/strategy/<key>')
def update_strategy(key):
    if key not in SETTINGS['strategies']: return jsonify(ok=False,error='unknown strategy'),404
    b=request.get_json(silent=True) or {}; s=SETTINGS['strategies'][key]
    s['enabled']=bool(b.get('enabled',s.get('enabled',True))); s['symbol']=str(b.get('symbol',s.get('symbol','XAU/USD'))).strip() or s.get('symbol','XAU/USD'); s['timeframe']=str(b.get('timeframe',s.get('timeframe','M5'))).upper().strip() or s.get('timeframe','M5'); s['scan_interval']=max(1,int(b.get('scan_interval',s.get('scan_interval',20)))); s['cooldown']=max(0,int(b.get('cooldown',s.get('cooldown',0))))
    mx=b.get('max_signals_per_day',s.get('max_signals_per_day'))
    s['max_signals_per_day']=None if mx in (None,'',0,'0','∞','infinity') else max(1,int(mx)); s['one_signal_per_candle']=bool(b.get('one_signal_per_candle',True)); save_settings(); WORKER_EVENTS[key].set()
    return jsonify(ok=True,settings=s)

@app.post('/api/bale')
def toggle_bale():
    SETTINGS['bot']['bale_enabled']=not SETTINGS['bot'].get('bale_enabled',True); save_settings(); return jsonify(ok=True,enabled=SETTINGS['bot']['bale_enabled'])

@app.get('/api/access')
def access_api():
    return jsonify({'allowed_chat_ids': allowed_chat_ids()})

@app.post('/api/access')
def update_access():
    b=request.get_json(silent=True) or {}
    raw=b.get('allowed_chat_ids',[])
    if not isinstance(raw,list):
        return jsonify(ok=False,error='allowed_chat_ids باید یک لیست باشد'),400
    ids=[]
    for value in raw:
        value=str(value).strip()
        if value and value.isdigit() and value not in ids:
            ids.append(value)
    if not ids:
        return jsonify(ok=False,error='حداقل یک آیدی عددی وارد کنید'),400
    with SETTINGS_LOCK:
        SETTINGS['bot']['allowed_chat_ids']=ids
        save_settings()
    return jsonify(ok=True,allowed_chat_ids=ids)

@app.get('/api/history')
def history_api(): return jsonify(HISTORY)
@app.get('/api/status')
def status_api(): return jsonify({'bale_enabled':SETTINGS['bot'].get('bale_enabled',True),'strategies':SETTINGS['strategies'],'runtime':RUNTIME})

if __name__=='__main__':
    save_history()
    market_data=MarketDataCache(CFG, SETTINGS)
    market_data_global=market_data
    strategies=[NYORBStrategy(CFG,SETTINGS,record_and_send,send,market_data),VWAPWickRejectionStrategy(CFG,SETTINGS,record_and_send,send,market_data),SP2LStrategy(CFG,SETTINGS,record_and_send,send,market_data)]
    for key,cls in PLUGIN_REGISTRY.items():
        try: strategies.append(cls(CFG,SETTINGS,record_and_send,send,market_data))
        except Exception as e: logging.error("Plugin %s instantiate failed: %s",key,e)
    threading.Thread(target=bale_loop,daemon=True,name='bale-loop').start()
    threading.Thread(target=result_monitor_loop,daemon=True,name='result-monitor').start()
    for s in strategies: threading.Thread(target=worker_loop,args=(s,),daemon=True,name=s.key).start()
    port=int(CFG.get('PORT',8787)); print(f'Dashboard: http://127.0.0.1:{port}')
    app.run(host='127.0.0.1',port=port,debug=False,use_reloader=False)
