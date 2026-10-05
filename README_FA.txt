Bale Trading Bot V4 - نسخه کامل

این نسخه بر پایه V4 قبلی ساخته شده و همه استراتژی‌ها و Pluginها را حفظ می‌کند.

Market Data Providerها:
1) OANDA
2) Twelve Data
3) AllTick
4) TraderMade
5) Finnhub

ترتیب از داخل ربات بله قابل تغییر است. هیچ API Key در Repository ذخیره نمی‌شود.
کلیدها از GitHub Actions Secrets خوانده می‌شوند. برای تغییر کلید از داخل Bale،
Secret زیر لازم است:
BALE_GH_ADMIN_TOKEN

Secrets پیشنهادی:
BALE_TOKEN
BALE_CHAT_ID
OWNER_CHAT_ID
OANDA_API_TOKEN
OANDA_ACCOUNT_ID
OANDA_ENVIRONMENT (practice/live)
TWELVEDATA_API_KEY
ALLTICK_API_TOKEN
TRADERMADE_API_KEY
FINNHUB_API_KEY
BALE_GH_ADMIN_TOKEN

در ربات:
منوی اصلی -> 📡 مدیریت API

قابلیت‌ها:
- فعال/غیرفعال کردن Provider
- تغییر اولویت
- تغییر API Key و ذخیره مستقیم در GitHub Secrets
- تست اتصال
- مشاهده Health، Rate Limit و Cooldown
- Failover خودکار
- Cache مرکزی و جلوگیری از درخواست تکراری
- مدیریت HTTP 429 با cooldown
- Mapping مستقل نمادها برای هر Provider

نمونه Mapping:
XAU/USD -> OANDA:XAU_USD / TwelveData:XAU/USD / AllTick:GOLD
GBP/JPY -> OANDA:GBP_JPY / TwelveData:GBP/JPY / AllTick:GBPJPY

نکته امنیتی:
API Key را داخل config.json یا settings.json قرار ندهید. این فایل‌ها فقط نام متغیرها و تنظیمات غیرحساس را نگه می‌دارند.
BALE_GH_ADMIN_TOKEN باید دسترسی لازم برای Repository Secrets را داشته باشد و هرگز در کد یا پیام Bale نمایش داده نمی‌شود.

اجرای محلی:
pip install -r requirements.txt
python main.py

اجرای GitHub Actions:
Workflow اصلی secrets بالا را به محیط اجرای ربات منتقل می‌کند.
