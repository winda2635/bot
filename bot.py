import asyncio
import contextlib
import os
import re
import traceback
from urllib.parse import urlparse, parse_qs

from telethon import TelegramClient, events

from playwright.async_api import (
    async_playwright,
    TimeoutError as PlaywrightTimeoutError,
)

# ==========================================================
# Configuration
# ==========================================================
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]

# استخدام مسار /tmp الافتراضي والآمن في Koyeb
BROWSER_PROFILE_DIR = os.environ.get("BROWSER_PROFILE_DIR", "/tmp/browser_profile")

BROWSER_LOCK: asyncio.Lock | None = None

print("STEP 1: Python started", flush=True)

client = TelegramClient("koyeb_bot", API_ID, API_HASH)

# ==========================================================
# Utility helpers
# ==========================================================
def debug_log(step: str, message: str):
    print(f"[{step}] {message}", flush=True)


def _clean_browser_profile():
    lock = os.path.join(BROWSER_PROFILE_DIR, "SingletonLock")
    if os.path.exists(lock):
        with contextlib.suppress(OSError):
            os.remove(lock)


def extract_domain_from_service_url(service_url: str) -> str:
    s = (service_url or "").strip()
    if s.startswith("http://") or s.startswith("https://"):
        return urlparse(s).netloc.strip()
    return s.replace("http://", "").replace("https://", "").split("/")[0].strip()


def extract_project_id(url: str):
    for pattern in (
        r"(qwiklabs-gcp-[\w-]+)",
        r"project[=/]([\w-]+)",
        r"projects/([\w-]+)",
    ):
        m = re.search(pattern, url or "")
        if m:
            return m.group(1)
    return None


async def handle_error(page, chat_id, step_name, error_msg):
    print(f"Error at {step_name}: {error_msg}", flush=True)
    safe_error_msg = str(error_msg)
    if len(safe_error_msg) > 1200:
        safe_error_msg = safe_error_msg[:1200] + "\n..."

    screenshot_path = None
    try:
        if page is not None and not page.is_closed():
            safe_step = re.sub(r"[^a-zA-Z0-9_-]+", "_", step_name).strip("_") or "error"
            screenshot_path = os.path.abspath(f"error_{safe_step}.png")
            try:
                await page.screenshot(path=screenshot_path, full_page=True)
                await client.send_file(
                    chat_id,
                    file=screenshot_path,
                    caption=f"Error in step:\n{step_name}\n\nError details:\n{safe_error_msg}",
                )
            except Exception as shot_err:
                print(f"Screenshot failed: {shot_err}", flush=True)
                await client.send_message(chat_id, f"Error in step: {step_name}\n\nError details:\n{safe_error_msg}")
        else:
            await client.send_message(chat_id, f"Error in step: {step_name}\n\nError details:\n{safe_error_msg}")
    except Exception as report_error:
        print(f"Failed to send error report: {report_error}", flush=True)
    finally:
        if screenshot_path and os.path.exists(screenshot_path):
            with contextlib.suppress(OSError):
                os.remove(screenshot_path)


async def log_page_state(page, step: str, include_body=False, body_limit=3000):
    try:
        debug_log(step, f"URL: {page.url}")
    except Exception as e:
        debug_log(step, f"Could not read URL: {type(e).__name__}: {e}")
    try:
        debug_log(step, f"Title: {await page.title()}")
    except Exception as e:
        debug_log(step, f"Could not read title: {type(e).__name__}: {e}")


async def save_debug_screenshot(page, step: str):
    try:
        safe = re.sub(r"[^a-zA-Z0-9_-]+", "_", step).strip("_") or "debug"
        path = os.path.abspath(f"debug_{safe}.png")
        await page.screenshot(path=path, full_page=True)
        return path
    except Exception:
        return None


async def log_exception(page, step: str, exc: Exception, include_body=True):
    debug_log(step, f"ERROR: {type(exc).__name__}: {exc}")
    await log_page_state(page, step, include_body)
    await save_debug_screenshot(page, step)


# ==========================================================
# Lab session monitoring
# ==========================================================
async def check_lab_session_status(page, step="LAB CHECK"):
    try:
        current_url = (page.url or "").lower()
    except Exception:
        current_url = ""

    if "cloudskillsboost.google" not in current_url and "skills.google" not in current_url:
        return False

    try:
        body_text = (await page.locator("body").inner_text(timeout=2500)).strip()
    except Exception:
        body_text = ""

    try:
        title = (await page.title()).strip()
    except Exception:
        title = ""

    combined_lower = f"{title} {body_text}".lower()
    strong_expired_signals = (
        "time's up",
        "times up",
        "lab has ended",
        "lab ended",
        "lab is over",
        "lab has expired",
        "session expired",
    )
    matched = next((signal for signal in strong_expired_signals if signal in combined_lower), None)
    if matched:
        raise RuntimeError(f"Lab session is no longer active or has expired. Detected: {matched!r}")
    return True


async def monitor_lab_session(monitor_page, stop_event, expired_event, error_holder):
    while not stop_event.is_set():
        try:
            await check_lab_session_status(monitor_page, "LAB MONITOR")
        except RuntimeError as ex:
            error_holder.append(str(ex))
            expired_event.set()
            return
        except Exception:
            pass
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=5)
        except asyncio.TimeoutError:
            pass


async def ensure_lab_active(expired_event, error_holder):
    if expired_event.is_set():
        raise RuntimeError(error_holder[0] if error_holder else "Lab session is no longer active.")


# ==========================================================
# Browser launch
# ==========================================================
async def _launch_browser(p):
    _clean_browser_profile()
    os.makedirs(BROWSER_PROFILE_DIR, exist_ok=True)
    return await p.chromium.launch_persistent_context(
        user_data_dir=BROWSER_PROFILE_DIR,
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
            "--disable-infobars",
            "--window-size=1920,1080",
            "--disable-gpu",
            "--no-zygote",
            "--single-process",
        ],
        viewport={"width": 1920, "height": 1080},
        locale="en-US",
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
    )


async def _wait_for_any_visible(locators, timeout_ms):
    deadline = asyncio.get_running_loop().time() + timeout_ms / 1000
    while asyncio.get_running_loop().time() < deadline:
        for loc in locators:
            try:
                if await loc.is_visible(timeout=500):
                    return loc
            except Exception:
                pass
        await asyncio.sleep(0.5)
    return None


# ==========================================================
# Automation steps
# ==========================================================
async def step1_welcome_screen(page):
    step = "STEP 1"
    await page.wait_for_load_state("domcontentloaded", timeout=30000)
    await page.wait_for_timeout(1500)
    try:
        button = page.get_by_text("I understand", exact=True).first
        if await button.is_visible(timeout=3000):
            await button.click()
            await page.wait_for_timeout(1500)
    except Exception:
        pass


async def wait_for_lab_dashboard(page, timeout_ms=600000):
    deadline = asyncio.get_running_loop().time() + (timeout_ms / 1000)
    while asyncio.get_running_loop().time() < deadline:
        current_url_lower = (page.url or "").lower()
        if "/home/dashboard" in current_url_lower:
            return
        await check_lab_session_status(page, "LAB DASHBOARD")
        await page.wait_for_timeout(1000)
    raise RuntimeError("Lab dashboard was not reached within timeout.")


async def step2_tos_and_country(page):
    step = "STEP 2"
    await check_lab_session_status(page, step)
    agree_btn = page.get_by_role("button", name="Agree and continue")
    try:
        await agree_btn.wait_for(state="visible", timeout=10000)
        checkboxes = page.get_by_role("checkbox")
        count = await checkboxes.count()
        if count >= 2:
            await checkboxes.nth(0).click()
            await asyncio.sleep(1)
            await checkboxes.nth(1).click()
        await agree_btn.click()
        await page.wait_for_load_state("domcontentloaded")
    except PlaywrightTimeoutError:
        pass


async def step3_enable_api(page, project_id, authuser):
    step = "STEP 3"
    # NOTE: reconstructed URL (was corrupted in the source).
    api_url = (
        "https://console.cloud.google.com/apis/library/run.googleapis.com"
        f"?project={project_id}&authuser={authuser}"
    )
    try:
        await page.goto(api_url, wait_until="domcontentloaded", timeout=60000)
    except PlaywrightTimeoutError:
        pass

    auth_deadline = asyncio.get_running_loop().time() + 900
    while asyncio.get_running_loop().time() < auth_deadline:
        if (
            "console.cloud.google.com" in (page.url or "").lower()
            and "sso" not in (page.url or "").lower()
        ):
            break
        await page.wait_for_timeout(1500)

    enable_btn = page.get_by_role("button", name="Enable")
    manage_btn = page.get_by_role("button", name="Manage")
    disable_btn = page.get_by_text("Disable API")

    controls_deadline = asyncio.get_running_loop().time() + 120
    while asyncio.get_running_loop().time() < controls_deadline:
        if await enable_btn.is_visible(timeout=1500):
            await enable_btn.click()
            found = await _wait_for_any_visible([manage_btn, disable_btn], 120000)
            if found:
                return
        if await manage_btn.is_visible(timeout=1500) or await disable_btn.is_visible(timeout=1500):
            return
        await page.wait_for_timeout(2000)
    raise RuntimeError("Cloud Run API controls did not appear.")


async def step4_create_cloud_run(page, project_id, authuser):
    step = "STEP 4"
    # NOTE: reconstructed URL (was corrupted in the source).
    run_url = (
        "https://console.cloud.google.com/run/create"
        f"?project={project_id}&authuser={authuser}"
    )
    await page.goto(run_url, wait_until="domcontentloaded")
    await page.wait_for_timeout(5000)

    try:
        label = page.get_by_text("Container Image URL").first
        await label.click()
        await page.wait_for_timeout(500)
        await page.keyboard.type("soshope35/xray-vless:latest", delay=50)
    except Exception as ex:
        raise RuntimeError(f"Container image input failed: {ex}")

    await page.wait_for_timeout(3000)

    try:
        await page.get_by_role("radio", name="Allow public access").click()
        await page.get_by_role("radio", name="Instance-based").click()
        with contextlib.suppress(Exception):
            await page.get_by_role("button", name="Hide").click(timeout=2000)
        await page.keyboard.press("End")
        await page.wait_for_timeout(1000)
        await page.get_by_role("button", name="Create").click(force=True)
    except Exception as ex:
        raise RuntimeError(f"Cloud Run Settings failed: {ex}")


async def step5_get_deployed_url(page, expired_event=None, error_holder=None):
    link_locator = page.locator('a[href*="run.app"]')
    deadline = asyncio.get_running_loop().time() + 120
    while asyncio.get_running_loop().time() < deadline:
        if expired_event is not None:
            await ensure_lab_active(expired_event, error_holder or [])
        try:
            if await link_locator.is_visible(timeout=1500):
                return await link_locator.get_attribute("href")
        except PlaywrightTimeoutError:
            pass
        await page.wait_for_timeout(2000)
    raise PlaywrightTimeoutError("run.app URL was not detected.")


# ==========================================================
# Main pipeline
# ==========================================================
async def process_sso_link(chat_id, sso_url):
    project_id = extract_project_id(sso_url)
    if not project_id:
        await client.send_message(chat_id, "❌ رابط غير صحيح (لم نجد معرف المشروع).")
        return

    await client.send_message(chat_id, "⏳ جاري بدء معالجة الرابط، يرجى الانتظار...")

    assert BROWSER_LOCK is not None
    async with BROWSER_LOCK:
        async with async_playwright() as p:
            context, page, lab_monitor_page, lab_monitor_task = None, None, None, None
            lab_monitor_stop = asyncio.Event()
            lab_expired_event = asyncio.Event()
            lab_monitor_errors = []

            try:
                context = await _launch_browser(p)
                page = context.pages[0] if context.pages else await context.new_page()

                await page.goto(sso_url, wait_until="domcontentloaded", timeout=60000)
                await client.send_message(chat_id, "🚀 الخطوة 1: تخطي شاشة الترحيب...")
                await step1_welcome_screen(page)

                await client.send_message(chat_id, "🔄 جاري الانتظار حتى يفتح الـ Dashboard الخاص بالمختبر...")
                await wait_for_lab_dashboard(page, timeout_ms=600000)
                await ensure_lab_active(lab_expired_event, lab_monitor_errors)

                lab_monitor_page = await context.new_page()
                with contextlib.suppress(Exception):
                    await lab_monitor_page.goto(sso_url, wait_until="domcontentloaded", timeout=60000)
                lab_monitor_task = asyncio.create_task(
                    monitor_lab_session(lab_monitor_page, lab_monitor_stop, lab_expired_event, lab_monitor_errors)
                )

                current_qs = parse_qs(urlparse(page.url).query)
                authuser = current_qs.get("authuser", ["1"])[0]

                await client.send_message(chat_id, "🚀 الخطوة 2: الموافقة على الشروط والأحكام...")
                await ensure_lab_active(lab_expired_event, lab_monitor_errors)
                await step2_tos_and_country(page)

                await client.send_message(chat_id, "🚀 الخطوة 3: تفعيل Cloud Run API...")
                await ensure_lab_active(lab_expired_event, lab_monitor_errors)
                await step3_enable_api(page, project_id, authuser)

                await client.send_message(chat_id, "🚀 الخطوة 4: إنشاء خدمة Cloud Run...")
                await ensure_lab_active(lab_expired_event, lab_monitor_errors)
                await step4_create_cloud_run(page, project_id, authuser)

                await client.send_message(chat_id, "🚀 الخطوة 5: جاري بناء الخدمة واستخراج الرابط...")
                await ensure_lab_active(lab_expired_event, lab_monitor_errors)
                final_url = await step5_get_deployed_url(page, lab_expired_event, lab_monitor_errors)

                domain = extract_domain_from_service_url(final_url)
                if not domain:
                    raise RuntimeError(f"Could not extract domain from: {final_url}")

                await client.send_message(
                    chat_id,
                    f"✅ اكتمل النشر بنجاح!\n\nرابط الخدمة:\n{final_url}\n\nالنطاق:\n{domain}",
                )

            except Exception as e:
                traceback.print_exc()
                if "Lab session is no longer active" in str(e):
                    await handle_error(
                        page,
                        chat_id,
                        "انتهى وقت المختبر",
                        "انتهت جلسة المختبر الحالية. يرجى تشغيل مختبر جديد وإرسال الرابط الجديد.",
                    )
                else:
                    await handle_error(page, chat_id, "خطأ عام", str(e))
            finally:
                lab_monitor_stop.set()
                if lab_monitor_task:
                    with contextlib.suppress(Exception):
                        await lab_monitor_task
                if lab_monitor_page:
                    with contextlib.suppress(Exception):
                        await lab_monitor_page.close()
                if context:
                    with contextlib.suppress(Exception):
                        await context.close()


# ==========================================================
# Telegram handlers
# ==========================================================
@client.on(events.NewMessage(pattern=r"^/start$"))
async def start(event):
    welcome_msg = (
        "مرحباً بك في بوت إنشاء Cloud Run التلقائي 🤖\n\n"
        "🕓 مختبر 4 ساعات و 30 دقيقة:\n"
        "cloudskillsboost.google\n\n"
        "🕓 مختبر 3 ساعات:\n"
        "skills.google\n\n"
        "إرسل رابط الـ SSO لتسجيل الدخول مباشرة هنا لبدء العمل."
    )
    await event.reply(welcome_msg)


# تحديث الـ Regex ليدعم النطاق الجديد والقديم معاً بشكل تلقائي
@client.on(events.NewMessage(pattern=r"https://www.(skills.google|cloudskillsboost.google)/google_sso\S+"))
async def handler(event):
    if not event.text:
        return
    m = re.search(r"https://www.(skills.google|cloudskillsboost.google)/google_sso\S+", event.text)
    if not m:
        return
    sso_url = m.group(0)

    if BROWSER_LOCK is None or BROWSER_LOCK.locked():
        await event.reply("⏳ هناك طلب آخر قيد المعالجة حالياً، يرجى الانتظار دقيقة ثم إعادة المحاولة.")
        return

    asyncio.create_task(process_sso_link(event.chat_id, sso_url))


async def main():
    global BROWSER_LOCK
    BROWSER_LOCK = asyncio.Lock()
    print("STEP 2: Starting Telegram Client", flush=True)
    await client.start(bot_token=BOT_TOKEN)
    print("Connected & Running...", flush=True)
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
