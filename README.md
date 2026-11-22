# M2R — Migrate to Rebecca

[فارسی](#فارسی) | [English](#english) | [GitHub](https://github.com/ali-ar1/M2R)

---

## فارسی

**M2R** دیتابیس پنل‌های **Marzban**، **Marzneshin** و **3x-ui (سنایی)** را کامل (ادمین‌ها، یوزرها، حجم مصرفی، تاریخ انقضا، کانفیگ‌ها و لینک ساب) خروجی می‌گیرد و داخل پنل **[Rebecca](https://github.com/rebeccapanel/Rebecca)** وارد می‌کند.

### پشتیبانی

| پنل | SQLite | MySQL / MariaDB | PostgreSQL |
|---|:-:|:-:|:-:|
| Marzban | ✅ | ✅ | ✅ |
| Marzneshin | ✅ | ✅ | ✅ |
| 3x-ui | ✅ | — | ✅ |
| **Rebecca (مقصد)** | — | ✅ | — |

### نصب و اجرا (یک خطی)
با کاربر root روی سرور بزنید:
```bash
bash <(curl -Ls https://raw.githubusercontent.com/ali-ar1/M2R/main/m2r.sh)
```

اسکریپت اول خودش پیش‌نیازها را نصب می‌کند (`python3`، کلاینت `mysql` و `psql`، و `bcrypt` و `xxhash` برای پایتون). روی Debian/Ubuntu با `apt` و روی CentOS/Alma/Rocky با `dnf`/`yum`. بعد آخرین نسخه `m2r.py` را در `/opt/M2R` دانلود و اجرا می‌کند.

### پیش‌نیاز
- اگر کلاینت دیتابیس نصب نشد، دیتابیسی که داخل داکر اجرا می‌شود هم کار می‌کند (اسکریپت خودش کانتینر را پیدا می‌کند)
- ربکا باید روی **MySQL یا MariaDB** نصب باشد و حداقل یک بار اجرا شده باشد

### منو
```
1) Exporting
2) Importing
3) Exit
```

**۱ — Exporting** (روی سرور پنل قدیمی)
1. پنل فعلی را با عدد انتخاب کنید.
2. مسیر ذخیره خروجی را بدهید (اینتر = `/root`).
3. اسکریپت خودش فایل `.env` پنل را پیدا می‌کند و اطلاعات دیتابیس را می‌خواند:

   | پنل | فایل تنظیمات | دیتابیس پیش‌فرض |
   |---|---|---|
   | Marzban | `/opt/marzban/.env` | `/var/lib/marzban/db.sqlite3` |
   | Marzneshin | `/etc/opt/marzneshin/.env` | `/var/lib/marzneshin/db.sqlite3` |
   | 3x-ui | `/etc/default/x-ui` | `/etc/x-ui/x-ui.db` |

   اگر پیدا نشد، مسیر `.env` یا خود فایل دیتابیس را از شما می‌پرسد.
4. خروجی یک فایل `m2r_<panel>_<date>.sql` است (با دسترسی 600، چون هش پسوردها داخلش است).

**۲ — Importing** (روی سرور ربکا)
1. مسیر فایل خروجی را بدهید. اینتر = `/root` و جدیدترین فایل `m2r_*.sql` آنجا انتخاب می‌شود.
2. اسکریپت `/opt/rebecca/.env` را باز می‌کند و به MySQL ربکا وصل می‌شود.
3. همه‌چیز داخل یک تراکنش وارد می‌شود. بعدش ربکا را ری‌استارت کنید: `rebecca restart`

**۳ — Exit**: خروج.

### نام‌های تکراری
- اگر **یوزری** هم‌نام یوزرِ موجود در ربکا باشد، اسمش عوض می‌شود (`ali` ← `ali-2`) ولی ادمین، حجم مصرفی، انقضا و کانفیگ‌هایش دست نمی‌خورد.
- اگر **ادمینی** تکراری باشد، اسمش عوض می‌شود (`admin` ← `admin-2`) و تمام یوزرها و حجم مصرفی‌اش به ادمین جدید وصل می‌شود.
- مقایسه بدون حساسیت به حروف بزرگ و کوچک است. لیست تغییر نام‌ها آخر کار نمایش داده می‌شود.

### چه چیزهایی منتقل می‌شود
- **ادمین‌ها**: نام کاربری، هش پسورد (همان پسورد قبلی کار می‌کند)، نقش (sudo/owner)، آیدی تلگرام، حجم مصرفی، محدودیت حجم، وضعیت
- **یوزرها**: وضعیت، حجم مصرفی، محدودیت حجم و استراتژی ریست، انقضا، on-hold، محدودیت IP، یادداشت، تاریخ ساخت
- **کانفیگ‌ها**: UUID و پسوردهای VMess / VLESS (+flow) / Trojan / Shadowsocks، همان مقادیر قبلی
- **لینک ساب**:
  - Marzban: secret ساب کپی می‌شود تا لینک‌های قبلی `/sub/<token>` کار کنند. اگر ربکا از قبل یوزر داشته باشد، اول از شما می‌پرسد (چون لینک یوزرهای فعلی ربکا عوض می‌شود).
  - Marzneshin: لینک `/sub/<username>/<key>` کار می‌کند.
  - 3x-ui: لینک `/sub/<subId>` کار می‌کند. کلاینت‌هایی که subId یکسان دارند یک یوزر می‌شوند و حجمشان جمع می‌شود.
  - دامنه ساب قبلی را به سرور ربکا اشاره دهید.
- همه یوزرهای واردشده داخل سرویس **`M2R`** قرار می‌گیرند که به همه هاست‌های فعلی ربکا وصل است. (در ربکا یوزرِ بدون سرویس کانفیگ نمی‌گیرد.) بعداً از پنل می‌توانید سرویس‌ها را جابه‌جا کنید.

### محدودیت‌ها
- اینباندها، هاست‌ها، نودها و تمپلیت‌ها منتقل نمی‌شوند؛ این‌ها را در ربکا خودتان تنظیم کنید.
- پروتکل‌هایی غیر از VMess/VLESS/Trojan/Shadowsocks (مثل WireGuard و Hysteria) رد می‌شوند و تعدادشان نمایش داده می‌شود.
- یوزری که اسمش عوض شده و لینکش شامل نام کاربری است (توکن‌های Marzban و لینک Marzneshin) لینک جدید لازم دارد.
- Marzneshin: برای حفظ UUID قبلی، `xxhash` لازم است (از کانتینر Marzneshin خودکار استفاده می‌شود). اگر نبود، UUIDها عوض می‌شوند و یوزرها باید یک بار ساب را آپدیت کنند.
- 3x-ui قدیمی که پسورد را به صورت متن ساده نگه می‌داشت: اسکریپت با `rebecca cli admin set-password` پسورد را ست می‌کند، وگرنه دستورش را نمایش می‌دهد.

> قبل از ایمپورت از دیتابیس ربکا بکاپ بگیرید.

---

## English

**M2R** exports the full database of **Marzban**, **Marzneshin** and **3x-ui (Sanaei)** (admins, users, traffic usage, expiry, proxy credentials and subscription links) and imports it into **[Rebecca](https://github.com/rebeccapanel/Rebecca)**.

### Support

| Panel | SQLite | MySQL / MariaDB | PostgreSQL |
|---|:-:|:-:|:-:|
| Marzban | ✅ | ✅ | ✅ |
| Marzneshin | ✅ | ✅ | ✅ |
| 3x-ui | ✅ | — | ✅ |
| **Rebecca (target)** | — | ✅ | — |

### Install & run (one line)
As root on the server:
```bash
bash <(curl -Ls https://raw.githubusercontent.com/ali-ar1/M2R/main/m2r.sh)
```

The launcher first installs the prerequisites (`python3`, the `mysql` and `psql` clients, and Python's `bcrypt` and `xxhash`) with `apt` on Debian/Ubuntu or `dnf`/`yum` on CentOS/Alma/Rocky. It then downloads the latest `m2r.py` to `/opt/M2R` and runs it.

### Requirements
- If a database client can't be installed, a database running in docker still works (the script finds the container)
- Rebecca on **MySQL or MariaDB**, started at least once so its tables exist

### Usage
Manual run without the launcher: `python3 m2r.py`

**1 — Exporting** (on the old panel's server): pick the panel by number, enter the output directory (Enter = `/root`). The script reads the panel's `.env` (paths in the Persian table above) to find the database, or asks for the `.env` or database file if it can't find it. It writes `m2r_<panel>_<date>.sql` (mode 600, since it holds password hashes).

**2 — Importing** (on the Rebecca server): enter the export file or a directory (Enter = `/root`, newest `m2r_*.sql`). The script reads `/opt/rebecca/.env`, loads the file into staging tables, renames clashing names and merges everything in one transaction. Then run `rebecca restart`.

**3 — Exit**.

### Duplicate names
- A **user** whose name already exists in Rebecca is renamed (`ali` → `ali-2`). Its admin, usage, expiry and credentials are kept.
- An **admin** whose name already exists is renamed (`admin` → `admin-2`), and all its users and usage are linked to the new admin.
- Matching ignores letter case. Every rename is printed at the end.

### What is migrated
- **Admins**: username, password hash (old passwords keep working), role, Telegram ID, usage, data limit, status
- **Users**: status, used traffic, data limit and reset strategy, expiry, on-hold, IP limit, note, created date
- **Credentials**: the same VMess / VLESS (+flow) / Trojan / Shadowsocks UUIDs and passwords
- **Subscription links**:
  - Marzban: the subscription secret is copied so old `/sub/<token>` links keep working. If Rebecca already has users, the script asks first, because their current links would change.
  - Marzneshin: `/sub/<username>/<key>` keeps working.
  - 3x-ui: `/sub/<subId>` keeps working. Clients that share a subId become one user, and their traffic is summed.
  - Point the old subscription domain at the Rebecca server.
- Imported users are put in a service named **`M2R`**, linked to all current Rebecca hosts (Rebecca gives no configs to users without a service). You can move them to other services later.

### Limitations
- Inbounds, hosts, nodes and templates are not migrated; set them up in Rebecca.
- Protocols other than VMess/VLESS/Trojan/Shadowsocks (WireGuard, Hysteria, ...) are skipped, and the script prints how many were skipped.
- A renamed user whose link contains the username (Marzban tokens, Marzneshin links) needs a new link.
- Marzneshin: keeping the old UUIDs needs `xxhash`, which the script uses from the Marzneshin container automatically. Without it, UUIDs change and users must refresh their subscription once.
- Old 3x-ui stored the panel password as plain text. The script sets it with `rebecca cli admin set-password`, or prints that command for you to run.

> Back up Rebecca's database before importing.

### Self-test
```bash
python3 m2r.py --selftest
```
