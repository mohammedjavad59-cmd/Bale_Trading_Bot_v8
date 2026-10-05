# V4 Update — Diagnostic + Coding Control

## اضافه‌شده
- عیب‌یابی جداگانه برای هر Strategy در Bale.
- تشخیص «سیستم سالم ولی ستاپ نداریم» در برابر «خطای فنی».
- نمایش آخرین اسکن، تعداد اسکن، وضعیت داده بازار، آخرین کندل، آخرین سیگنال و آخرین خطا.
- بخش «💻 کدنویسی» مخصوص Owner.
- تغییرات کدنویسی به‌صورت Manifest JSON با عملیات write / patch / json_set / delete.
- Commit مستقیم به GitHub با Git Data API.
- امکان Restart/Deploy بعد از تغییر.
- اعتبارسنجی سخت‌تر Pluginها و محدودکردن importهای Plugin.
- امکان جایگزینی Plugin موجود با همان `STRATEGY_KEY`.
- اصلاح نمایش نام و توضیحات Pluginهای جدید در Dashboard.
- Workflow چرخشی امن‌تر: در حالت timeout برنامه‌ریزی‌شده Restart می‌شود، ولی لغو دستی باعث زنجیره ناخواسته نمی‌شود.

## نکته
بخش «کدنویسی» کد Python دلخواه را داخل Runner اجرا نمی‌کند؛ تغییرات را به‌صورت عملیات کنترل‌شده روی Repository اعمال می‌کند و در صورت درخواست Commit/Deploy می‌کند.


## Market Data / AllTick
- AllTick به‌عنوان منبع اصلی K-Line و quote اضافه شد.
- TwelveData به‌عنوان fallback حفظ شد.
- کش مرکزی بر اساس نماد، تایم‌فریم و timezone از درخواست‌های تکراری جلوگیری می‌کند.
- خطای Rate Limit/Provider در بخش «🩺 عیب‌یابی» قابل مشاهده است.
- `ALLTICK_API_TOKEN` از GitHub Actions Secret خوانده می‌شود.
- بررسی نتیجه سیگنال‌ها همچنان با M1 انجام می‌شود تا لمس TP/SL از دست نرود.
- Workflow دیگر بعد از پایان Runner خودش `repository_dispatch` ایجاد نمی‌کند و از زنجیره ناخواسته Runner جلوگیری می‌شود.


## Final integration
- بخش «💻 کدنویسی» با تأیید اجباری قبل از اعمال تغییرات.
- Manifest قبل از Commit اعتبارسنجی و Preview می‌شود.
- بدون تأیید Owner هیچ write/patch/json_set/delete یا Deploy انجام نمی‌شود.
- اجرای shell/command دلخواه از بخش کدنویسی وجود ندارد.
