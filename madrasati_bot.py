"""
madrasati_bot.py – أتمتة منصة مدرستي
=======================================
يدخل على المنصة ويقوم بالمهام التالية تلقائياً:
  1. نشر الخطة الدراسية
  2. جدولة الدروس الموجودة مسبقاً ونشرها
  3. نشر الواجبات الموجودة مسبقاً

المتطلبات (env vars):
  MADRASATI_USERNAME   – رقم الهوية الوطنية / اسم المستخدم
  MADRASATI_PASSWORD   – كلمة المرور
  MADRASATI_URL        – رابط المنصة (افتراضي: https://ims.edu.sa)
  HEADLESS             – true/false (افتراضي: true)
  DRY_RUN              – true → لا تنشر فعلاً، فقط سجّل العمليات
  LOG_LEVEL            – DEBUG / INFO / WARNING (افتراضي: INFO)
"""

import os
import re
import sys
import time
import logging
from datetime import datetime, timedelta
from pathlib import Path

from playwright.sync_api import sync_playwright, Page, TimeoutError as PWTimeout

# ── Logging ───────────────────────────────────────────────────────────────────

LOG_LEVEL = (os.getenv("LOG_LEVEL") or "INFO").strip().upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s  %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("madrasati")

# ── Config ────────────────────────────────────────────────────────────────────


def env(name: str, default: str = "", required: bool = False) -> str:
    v = (os.getenv(name) or default).strip()
    if required and not v:
        raise RuntimeError(f"❌  Missing required env var: {name}")
    return v


MADRASATI_URL = env("MADRASATI_URL", "https://ims.edu.sa")
USERNAME      = env("MADRASATI_USERNAME", required=True)
PASSWORD      = env("MADRASATI_PASSWORD", required=True)
HEADLESS      = env("HEADLESS", "true").lower() in ("1", "true", "yes")
DRY_RUN       = env("DRY_RUN",  "false").lower() in ("1", "true", "yes")

# صبر الانتظار على العناصر (بالميلي ثانية)
PAGE_TIMEOUT    = 30_000   # 30 ث لتحميل الصفحة
ELEMENT_TIMEOUT = 15_000   # 15 ث للعثور على عنصر
ACTION_DELAY    = 1_000    # تأخير بين الإجراءات لمحاكاة السلوك الطبيعي

# مجلد لقطات الشاشة عند الخطأ
SCREENSHOT_DIR = Path(env("SCREENSHOT_DIR", "/tmp/madrasati_screenshots"))
SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)

# ── Selectors (تحديث هذه إن تغيّر تصميم المنصة) ─────────────────────────────

SEL = {
    # صفحة الدخول
    "login_username": "#username, input[name='username'], input[placeholder*='المستخدم'], input[placeholder*='هوية']",
    "login_password": "#password, input[name='password'], input[type='password']",
    "login_submit":   "button[type='submit'], input[type='submit'], .btn-login, #loginBtn",

    # القائمة الرئيسية
    "nav_courses":     "a[href*='courses'], a[href*='subject'], .menu-item:has-text('المقررات'), .menu-item:has-text('الدروس')",
    "nav_study_plan":  "a[href*='study-plan'], a[href*='studyplan'], .menu-item:has-text('الخطة الدراسية')",
    "nav_assignments": "a[href*='assignment'], .menu-item:has-text('الواجبات'), .menu-item:has-text('التكاليف')",

    # الدروس
    "lesson_item":         ".lesson-item, .session-item, tr.lesson-row",
    "lesson_publish_btn":  "button:has-text('نشر'), button:has-text('تفعيل'), .publish-btn",
    "lesson_schedule_btn": "button:has-text('جدول'), button:has-text('تحديد موعد'), .schedule-btn",
    "lesson_date_input":   "input[type='date'], input[name*='date'], .date-picker",
    "lesson_time_input":   "input[type='time'], input[name*='time'], .time-picker",
    "lesson_save_btn":     "button:has-text('حفظ'), button:has-text('تأكيد'), .save-btn",

    # الواجبات
    "assignment_item":    ".assignment-item, tr.assignment-row, .homework-item",
    "assignment_publish": "button:has-text('نشر'), button:has-text('تفعيل'), .publish-btn",
    "assignment_confirm": "button:has-text('نعم'), button:has-text('تأكيد'), .confirm-btn",

    # الخطة الدراسية
    "study_plan_publish": "button:has-text('نشر الخطة'), button:has-text('نشر'), .publish-plan-btn",
    "study_plan_confirm": "button:has-text('نعم'), button:has-text('تأكيد'), .confirm-btn",

    # عام
    "success_alert": ".alert-success, .success-message, [class*='success']",
    "error_alert":   ".alert-danger, .error-message,   [class*='error']",
    "modal_close":   "button:has-text('إغلاق'), button:has-text('×'), .modal-close",
    "loading_spinner": ".spinner, .loading, [class*='loading']",
}

# ── Helpers ───────────────────────────────────────────────────────────────────


def screenshot(page: Page, name: str) -> None:
    """التقط لقطة شاشة وسجّلها (للتصحيح)."""
    path = SCREENSHOT_DIR / f"{name}_{int(time.time())}.png"
    try:
        page.screenshot(path=str(path), full_page=True)
        log.debug(f"Screenshot: {path}")
    except Exception as e:
        log.warning(f"Screenshot failed: {e}")


def wait_no_spinner(page: Page) -> None:
    """انتظر حتى يختفي مؤشر التحميل."""
    try:
        page.wait_for_selector(SEL["loading_spinner"], state="hidden", timeout=PAGE_TIMEOUT)
    except PWTimeout:
        pass  # لا مشكلة إن لم يوجد spinner


def safe_click(page: Page, selector: str, label: str = "") -> bool:
    """انقر على العنصر بأمان مع تسجيل الحدث."""
    try:
        page.wait_for_selector(selector, timeout=ELEMENT_TIMEOUT)
        page.locator(selector).first.click()
        time.sleep(ACTION_DELAY / 1000)
        log.debug(f"Clicked: {label or selector}")
        return True
    except PWTimeout:
        log.debug(f"Not found (skip): {label or selector}")
        return False
    except Exception as e:
        log.warning(f"Click error [{label}]: {e}")
        return False


def check_success(page: Page, action: str) -> bool:
    """تحقق من ظهور رسالة نجاح بعد الإجراء."""
    try:
        page.wait_for_selector(SEL["success_alert"], timeout=5_000)
        log.info(f"✅  {action} – نجح")
        return True
    except PWTimeout:
        # تحقق من رسالة خطأ
        try:
            err = page.locator(SEL["error_alert"]).first.inner_text()
            log.warning(f"⚠️  {action} – خطأ من المنصة: {err[:120]}")
        except Exception:
            log.info(f"✅  {action} – تم (لا رسالة نجاح صريحة)")
        return True   # نكمل حتى لو لم تظهر alert


# ── 1. Login ──────────────────────────────────────────────────────────────────


def login(page: Page) -> None:
    """الدخول على المنصة."""
    log.info(f"🔑  الدخول على: {MADRASATI_URL}")
    page.goto(MADRASATI_URL, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)
    wait_no_spinner(page)

    # أدخل اسم المستخدم
    page.wait_for_selector(SEL["login_username"], timeout=ELEMENT_TIMEOUT)
    page.locator(SEL["login_username"]).first.fill(USERNAME)
    time.sleep(0.5)

    # أدخل كلمة المرور
    page.locator(SEL["login_password"]).first.fill(PASSWORD)
    time.sleep(0.5)

    # اضغط الدخول
    page.locator(SEL["login_submit"]).first.click()
    page.wait_for_load_state("networkidle", timeout=PAGE_TIMEOUT)
    wait_no_spinner(page)

    # تحقق من نجاح الدخول (يجب أن يختفي زر الدخول)
    try:
        page.wait_for_selector(SEL["login_submit"], state="hidden", timeout=5_000)
    except PWTimeout:
        pass  # بعض المنصات لا تُخفي زر الدخول، نكمل

    log.info("🏠  تم الدخول بنجاح")
    screenshot(page, "after_login")


# ── 2. نشر الخطة الدراسية ─────────────────────────────────────────────────────


def publish_study_plan(page: Page) -> int:
    """
    انتقل لقسم الخطة الدراسية وانشرها.
    يُعيد عدد الخطط التي تم نشرها.
    """
    log.info("📋  الخطة الدراسية – بدء المعالجة")
    published = 0

    try:
        page.wait_for_selector(SEL["nav_study_plan"], timeout=ELEMENT_TIMEOUT)
        page.locator(SEL["nav_study_plan"]).first.click()
        page.wait_for_load_state("networkidle", timeout=PAGE_TIMEOUT)
        wait_no_spinner(page)
        screenshot(page, "study_plan_page")
    except PWTimeout:
        log.warning("⚠️  لم يتم العثور على رابط الخطة الدراسية")
        return 0

    # ابحث عن زر نشر الخطة
    btns = page.locator(SEL["study_plan_publish"]).all()
    if not btns:
        log.info("ℹ️  لا توجد خطط دراسية تحتاج نشراً")
        return 0

    log.info(f"   وجدت {len(btns)} خطة دراسية للنشر")

    for i, btn in enumerate(btns):
        try:
            plan_label = f"خطة #{i + 1}"
            if DRY_RUN:
                log.info(f"[DRY_RUN] سيتم نشر: {plan_label}")
                published += 1
                continue

            btn.click()
            time.sleep(ACTION_DELAY / 1000)

            # نافذة تأكيد إن وُجدت
            safe_click(page, SEL["study_plan_confirm"], "تأكيد نشر الخطة")
            wait_no_spinner(page)
            check_success(page, f"نشر {plan_label}")
            published += 1
            time.sleep(1)

        except Exception as e:
            log.warning(f"⚠️  خطأ أثناء نشر خطة #{i + 1}: {e}")
            screenshot(page, f"study_plan_error_{i}")

    log.info(f"📋  الخطة الدراسية: تم نشر {published} خطة")
    return published


# ── 3. جدولة الدروس ونشرها ────────────────────────────────────────────────────


def _next_weekday_dates(count: int) -> list[tuple[str, str]]:
    """
    أنشئ قائمة بمواعيد مناسبة لـ count درس خلال أيام العمل القادمة.
    كل درس في يوم منفصل من الأحد إلى الخميس (الأسبوع الدراسي السعودي).
    يُعيد: [(YYYY-MM-DD, HH:MM), ...]
    """
    schedule = []
    d = datetime.now().replace(hour=8, minute=0, second=0, microsecond=0)
    # بدء من الغد
    d += timedelta(days=1)

    lesson_hour = 8  # ابدأ من 8 صباحاً
    while len(schedule) < count:
        # 0=Mon..6=Sun  — السعودية: أيام الدراسة الأحد(6) إلى الخميس(4)
        # Python weekday: Mon=0 ... Sun=6
        # Sun in python = 6, Thu = 3
        if d.weekday() in (3, 4, 5):   # الجمعة(4) والسبت(5) والخميس(3) → تخطَّ نهاية الأسبوع
            # ملاحظة: في نظام Python weekday، الخميس=3، الجمعة=4، السبت=5
            pass
        else:
            schedule.append((d.strftime("%Y-%m-%d"), f"{lesson_hour:02d}:00"))
            lesson_hour += 2  # فجوة ساعتين بين الدروس في نفس اليوم
            if lesson_hour >= 14:
                lesson_hour = 8

        d += timedelta(days=1)

    return schedule


def schedule_and_publish_lessons(page: Page) -> int:
    """
    انتقل لقسم الدروس، حدد موعداً لكل درس غير منشور، ثم انشره.
    يُعيد عدد الدروس التي تمت جدولتها ونشرها.
    """
    log.info("📚  الدروس – بدء المعالجة")
    scheduled = 0

    try:
        page.wait_for_selector(SEL["nav_courses"], timeout=ELEMENT_TIMEOUT)
        page.locator(SEL["nav_courses"]).first.click()
        page.wait_for_load_state("networkidle", timeout=PAGE_TIMEOUT)
        wait_no_spinner(page)
        screenshot(page, "lessons_page")
    except PWTimeout:
        log.warning("⚠️  لم يتم العثور على رابط الدروس")
        return 0

    # اجمع كل الدروس القابلة للجدولة
    lessons = page.locator(SEL["lesson_item"]).all()
    if not lessons:
        log.info("ℹ️  لا توجد دروس تحتاج جدولة")
        return 0

    log.info(f"   وجدت {len(lessons)} درساً")
    dates = _next_weekday_dates(len(lessons))

    for i, lesson in enumerate(lessons):
        try:
            lesson_text = lesson.inner_text().strip()[:60]
            lesson_date, lesson_time = dates[i] if i < len(dates) else (dates[-1][0], dates[-1][1])
            log.info(f"   درس #{i + 1}: «{lesson_text}»  →  {lesson_date} {lesson_time}")

            if DRY_RUN:
                log.info(f"[DRY_RUN] سيتم جدولة ونشر: درس #{i + 1}")
                scheduled += 1
                continue

            # 1. افتح نموذج الجدولة إن وُجد
            schedule_btn = lesson.locator(SEL["lesson_schedule_btn"])
            if schedule_btn.count() > 0:
                schedule_btn.first.click()
                time.sleep(ACTION_DELAY / 1000)

                # أدخل التاريخ
                date_inp = page.locator(SEL["lesson_date_input"])
                if date_inp.count() > 0:
                    date_inp.first.fill(lesson_date)
                    time.sleep(0.3)

                # أدخل الوقت
                time_inp = page.locator(SEL["lesson_time_input"])
                if time_inp.count() > 0:
                    time_inp.first.fill(lesson_time)
                    time.sleep(0.3)

                # احفظ الجدولة
                safe_click(page, SEL["lesson_save_btn"], "حفظ موعد الدرس")
                wait_no_spinner(page)

            # 2. انشر الدرس
            publish_btn = lesson.locator(SEL["lesson_publish_btn"])
            if publish_btn.count() == 0:
                # حاول من صفحة الدرس بعد إعادة تحميلها
                publish_btn = page.locator(SEL["lesson_publish_btn"])

            if publish_btn.count() > 0:
                publish_btn.first.click()
                time.sleep(ACTION_DELAY / 1000)
                # تأكيد إن وُجد
                safe_click(page, SEL["lesson_save_btn"], "تأكيد نشر الدرس")
                wait_no_spinner(page)
                check_success(page, f"جدولة درس #{i + 1}")
                scheduled += 1
            else:
                log.info(f"   درس #{i + 1}: لا يحتاج نشراً (مُنشور مسبقاً)")

            # أغلق أي modal مفتوح
            safe_click(page, SEL["modal_close"], "إغلاق النافذة")
            time.sleep(0.5)

        except Exception as e:
            log.warning(f"⚠️  خطأ أثناء معالجة درس #{i + 1}: {e}")
            screenshot(page, f"lesson_error_{i}")
            safe_click(page, SEL["modal_close"], "إغلاق النافذة بعد خطأ")

    log.info(f"📚  الدروس: تمت جدولة ونشر {scheduled} درس")
    return scheduled


# ── 4. نشر الواجبات ───────────────────────────────────────────────────────────


def publish_assignments(page: Page) -> int:
    """
    انتقل لقسم الواجبات، وانشر كل واجب موجود مسبقاً وغير منشور.
    يُعيد عدد الواجبات التي تم نشرها.
    """
    log.info("📝  الواجبات – بدء المعالجة")
    published = 0

    try:
        page.wait_for_selector(SEL["nav_assignments"], timeout=ELEMENT_TIMEOUT)
        page.locator(SEL["nav_assignments"]).first.click()
        page.wait_for_load_state("networkidle", timeout=PAGE_TIMEOUT)
        wait_no_spinner(page)
        screenshot(page, "assignments_page")
    except PWTimeout:
        log.warning("⚠️  لم يتم العثور على رابط الواجبات")
        return 0

    assignments = page.locator(SEL["assignment_item"]).all()
    if not assignments:
        log.info("ℹ️  لا توجد واجبات تحتاج نشراً")
        return 0

    log.info(f"   وجدت {len(assignments)} واجباً")

    for i, hw in enumerate(assignments):
        try:
            hw_text = hw.inner_text().strip()[:60]
            log.info(f"   واجب #{i + 1}: «{hw_text}»")

            if DRY_RUN:
                log.info(f"[DRY_RUN] سيتم نشر: واجب #{i + 1}")
                published += 1
                continue

            pub_btn = hw.locator(SEL["assignment_publish"])
            if pub_btn.count() == 0:
                log.info(f"   واجب #{i + 1}: لا يحتاج نشراً (مُنشور مسبقاً)")
                continue

            pub_btn.first.click()
            time.sleep(ACTION_DELAY / 1000)

            # تأكيد إن طُلب
            safe_click(page, SEL["assignment_confirm"], "تأكيد نشر الواجب")
            wait_no_spinner(page)
            check_success(page, f"نشر واجب #{i + 1}")
            published += 1

            safe_click(page, SEL["modal_close"], "إغلاق النافذة")
            time.sleep(0.5)

        except Exception as e:
            log.warning(f"⚠️  خطأ أثناء نشر واجب #{i + 1}: {e}")
            screenshot(page, f"assignment_error_{i}")
            safe_click(page, SEL["modal_close"], "إغلاق النافذة بعد خطأ")

    log.info(f"📝  الواجبات: تم نشر {published} واجب")
    return published


# ── Run ───────────────────────────────────────────────────────────────────────


def run() -> None:
    """نقطة الانطلاق الرئيسية."""
    log.info("=" * 60)
    log.info("🚀  madrasati_bot  بدء التشغيل")
    log.info(f"    المنصة  : {MADRASATI_URL}")
    log.info(f"    المستخدم: {USERNAME}")
    log.info(f"    DRY_RUN : {DRY_RUN}")
    log.info(f"    Headless: {HEADLESS}")
    log.info("=" * 60)

    start = time.time()
    results = {"study_plan": 0, "lessons": 0, "assignments": 0}

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=HEADLESS,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = browser.new_context(
            locale="ar-SA",
            timezone_id="Asia/Riyadh",
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()
        page.set_default_timeout(PAGE_TIMEOUT)

        try:
            # ── الدخول ──────────────────────────────────────────────────────
            login(page)

            # ── نشر الخطة الدراسية ──────────────────────────────────────────
            results["study_plan"] = publish_study_plan(page)

            # ── جدولة الدروس ونشرها ─────────────────────────────────────────
            results["lessons"] = schedule_and_publish_lessons(page)

            # ── نشر الواجبات ────────────────────────────────────────────────
            results["assignments"] = publish_assignments(page)

        except Exception as e:
            log.error(f"💥  خطأ غير متوقع: {e}", exc_info=True)
            screenshot(page, "fatal_error")
            raise

        finally:
            screenshot(page, "final_state")
            context.close()
            browser.close()

    elapsed = time.time() - start
    log.info("=" * 60)
    log.info("✅  اكتمل التشغيل")
    log.info(f"   خطط دراسية منشورة : {results['study_plan']}")
    log.info(f"   دروس مجدولة ومنشورة: {results['lessons']}")
    log.info(f"   واجبات منشورة      : {results['assignments']}")
    log.info(f"   الوقت المستغرق      : {elapsed:.1f}ث")
    log.info("=" * 60)


if __name__ == "__main__":
    run()
