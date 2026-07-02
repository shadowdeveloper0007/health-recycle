from dotenv import load_dotenv
from json import load
import re
import asyncio
import sys
import time
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright

_executor = ThreadPoolExecutor(max_workers=2)

load_dotenv()
SITE_URL = os.getenv("SITE_URL")
TIME_PATTERN = re.compile(r"^(\d{2}:\d{2}:\d{2})$")
BROWSER_ARGS = ["--disable-dev-shm-usage", "--disable-gpu", "--no-sandbox"]


@dataclass
class ScrapeResult:
    found: bool
    login_time: str | None
    message: str
    date: str
    error: bool = False
    validation_error: bool = False
    field: str | None = None


@dataclass
class RewardItem:
    name: str        # e.g. "TeamLead Appreciation Award"
    icon_url: str    # image src of the award icon
    count: int       # how many times the same award was achieved


@dataclass
class DashboardInfoResult:
    success: bool
    name: str | None
    designation: str | None
    employee_id: str | None
    date_of_joining: str | None
    total_experience: str | None
    status: str | None
    manager: str | None
    profile_url: str | None
    rewards: list[RewardItem]
    project_productivity: str | None  # e.g. "93.35%"
    message: str
    error: bool = False
    validation_error: bool = False
    field: str | None = None


async def _check_login_errors(page) -> ScrapeResult | None:
    if await page.locator("text=Please check your email!").count() > 0:
        return ScrapeResult(
            found=False,
            login_time=None,
            message="Please check your email!",
            date=datetime.now().strftime("%Y-%m-%d"),
            validation_error=True,
            field="email",
        )
    if (
        await page.locator("text=Please check your password!").count() > 0
        or await page.locator("text=Please Enter Valid Password").count() > 0
    ):
        return ScrapeResult(
            found=False,
            login_time=None,
            message="Please check your password!",
            date=datetime.now().strftime("%Y-%m-%d"),
            validation_error=True,
            field="password",
        )
    return None


async def _wait_for_login_result(page, timeout_ms: int = 15_000) -> ScrapeResult | None:
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        if await page.locator("text=Attendance For").count() > 0:
            return None
        login_error = await _check_login_errors(page)
        if login_error:
            return login_error
        await asyncio.sleep(0.5)

    if "login" in page.url.lower():
        return ScrapeResult(
            found=False,
            login_time=None,
            message="Please check your email!",
            date=datetime.now().strftime("%Y-%m-%d"),
            validation_error=True,
            field="email",
        )
    return None


async def _scrape_login_time_impl(email: str, password: str, headless: bool = True) -> ScrapeResult:
    today_date = datetime.now().strftime("%Y-%m-%d")
    today_day = str(datetime.now().day)

    if not email or not password:
        return ScrapeResult(
            found=False,
            login_time=None,
            message="Please Enter Valid Password" if not password else "Please check your email!",
            date=today_date,
            validation_error=True,
            field="password" if not password else "email",
        )

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=headless, args=BROWSER_ARGS)
            page = await browser.new_page()
            try:
                await page.goto(SITE_URL, wait_until="domcontentloaded", timeout=90_000)
                await asyncio.sleep(5)
                await page.get_by_role("textbox", name="Email *").fill(email)
                await page.get_by_role("textbox", name="Password *").fill(password)
                await page.get_by_role("button", name="Login").click()

                login_error = await _wait_for_login_result(page)
                if login_error:
                    return login_error

                await page.wait_for_selector("text=Attendance For", timeout=60_000)
                await page.wait_for_function(
                    """() => !document.querySelector('[title="Data is refreshing..."]')""",
                    timeout=60_000,
                )

                headers = page.locator("table thead th")
                col_index = None
                for i in range(await headers.count()):
                    if (await headers.nth(i).inner_text()).strip() == today_day:
                        col_index = i
                        break
                if col_index is None:
                    return ScrapeResult(
                        found=False,
                        login_time=None,
                        message=f"Attendance column for day {today_day} not found.",
                        date=today_date,
                        error=True,
                    )

                today_cell = page.locator("table tbody tr").first.locator("td").nth(col_index - 1)
                cell_text = (await today_cell.inner_text()).strip()
                if cell_text == "--":
                    return ScrapeResult(
                        found=False,
                        login_time=None,
                        message="Today's attendance is not available yet.",
                        date=today_date,
                    )

                # Give the table a moment to finish any re-rendering
                await asyncio.sleep(2)
                
                # The data-bs-toggle="modal" is on the inner div
                clickable_div = today_cell.locator("div").last
                if await clickable_div.count() > 0:
                    await clickable_div.click()
                else:
                    await today_cell.click()

                # Wait for popup to actually open (Bootstrap adds .show)
                await page.wait_for_selector(".modal.show", timeout=30_000)
                dialog = page.locator(".modal.show").first

                # print("MODAL HTML:")
                # print(await dialog.inner_html())
                in_hours_span = dialog.locator(".in-hours").first
                if await in_hours_span.count() == 0:
                    # Fallback to older text-based regex if class is missing
                    in_items = dialog.locator("li").filter(has_text=re.compile(r"^In:"))
                    if await in_items.count() == 0:
                        in_items = page.locator("li").filter(has_text=re.compile(r"^In:")).filter(visible=True)
                    
                    if await in_items.count() == 0:
                        return ScrapeResult(
                            found=False,
                            login_time=None,
                            message="Biometric In time not found in attendance popup.",
                            date=today_date,
                        )

                    in_text = (await in_items.first.inner_text()).strip()
                    match = re.search(r"In:\s*(\S+)", in_text)
                    if not match:
                        return ScrapeResult(
                            found=False,
                            login_time=None,
                            message="Could not read biometric In time from popup.",
                            date=today_date,
                        )
                    login_time = match.group(1)
                else:
                    login_time = (await in_hours_span.inner_text()).strip()

                print(f"EXTRACTED LOGIN TIME: {repr(login_time)}")

                if login_time == "-" or not TIME_PATTERN.match(login_time):
                    return ScrapeResult(
                        found=False,
                        login_time=None,
                        message="Biometric login time not recorded yet. Try again later.",
                        date=today_date,
                    )

                return ScrapeResult(
                    found=True,
                    login_time=login_time,
                    message="Login time found.",
                    date=today_date,
                )
            finally:
                await page.close()
                await browser.close()
    except PlaywrightError as exc:
        return ScrapeResult(
            found=False,
            login_time=None,
            message=f"Scraping failed: {exc}",
            date=today_date,
            error=True,
        )
    except Exception as exc:
        return ScrapeResult(
            found=False,
            login_time=None,
            message=f"Unexpected error: {exc}",
            date=today_date,
            error=True,
        )


def _run_in_thread(email: str, password: str, headless: bool) -> ScrapeResult:
    """Run the Playwright scraping in a fresh event loop inside a thread."""
    if sys.platform == "win32":
        loop = asyncio.ProactorEventLoop()
    else:
        loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(_scrape_login_time_impl(email, password, headless))
    finally:
        loop.close()


async def scrape_login_time(email: str, password: str, headless: bool = True) -> ScrapeResult:
    """Public wrapper — offloads Playwright to a thread so it works on Windows."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        _executor, _run_in_thread, email, password, headless
    )


# ── Dashboard Info Scraper ─────────────────────────────────────────────────────

async def _scrape_dashboard_info_impl(
    email: str, password: str, headless: bool = True
) -> DashboardInfoResult:
    """Scrape name, designation, profile URL, rewards, and project productivity."""
    today_date = datetime.now().strftime("%Y-%m-%d")

    if not email or not password:
        return DashboardInfoResult(
            success=False,
            name=None,
            designation=None,
            profile_url=None,
            rewards=[],
            project_productivity=None,
            message="Please Enter Valid Password" if not password else "Please check your email!",
            validation_error=True,
            field="password" if not password else "email",
        )

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=headless, args=BROWSER_ARGS)
            page = await browser.new_page()
            try:
                # ── Login (same flow as _scrape_login_time_impl) ──────────
                await page.goto(SITE_URL, wait_until="domcontentloaded", timeout=90_000)
                await asyncio.sleep(5)
                await page.get_by_role("textbox", name="Email *").fill(email)
                await page.get_by_role("textbox", name="Password *").fill(password)
                await page.get_by_role("button", name="Login").click()

                login_error = await _wait_for_login_result(page)
                if login_error:
                    return DashboardInfoResult(
                        success=False,
                        name=None,
                        designation=None,
                        profile_url=None,
                        rewards=[],
                        project_productivity=None,
                        message=login_error.message,
                        validation_error=login_error.validation_error,
                        field=login_error.field,
                    )

                await page.wait_for_selector("text=Attendance For", timeout=60_000)
                await page.wait_for_function(
                    """() => !document.querySelector('[title="Data is refreshing..."]')""",
                    timeout=60_000,
                )

                # ── Extract Project Productivity from Highcharts gauge ─────
                project_productivity = None
                try:
                    # Scroll down to trigger lazy-loaded Highcharts gauge
                    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    await asyncio.sleep(3)

                    # Primary: JS evaluation on Highcharts title elements
                    js_result = await page.evaluate("""
                        () => {
                            const titles = document.querySelectorAll('.highcharts-title');
                            for (const t of titles) {
                                const text = t.textContent || '';
                                if (text.includes('Project Productivity')) return text;
                            }
                            return null;
                        }
                    """)
                    if js_result:
                        match = re.search(r"([\d.]+)%", js_result)
                        if match:
                            project_productivity = f"{match.group(1)}%"

                    # Fallback: try SVG text elements with text_content()
                    if project_productivity is None:
                        prod_texts = page.locator(".highcharts-container text")
                        count = await prod_texts.count()
                        for i in range(count):
                            text_content = (await prod_texts.nth(i).text_content() or "").strip()
                            if "Project Productivity" in text_content:
                                match = re.search(r"([\d.]+)%", text_content)
                                if match:
                                    project_productivity = f"{match.group(1)}%"
                                break
                except Exception:
                    pass

                # ── Navigate to My Profile for user details ────────────────
                name = None
                designation = None
                employee_id = None
                date_of_joining = None
                total_experience = None
                status = None
                manager = None
                profile_url = None
                profile_page_url = f"{SITE_URL}Profile/MyProfile"
                try:
                    await page.goto(
                        profile_page_url,
                        wait_until="domcontentloaded",
                        timeout=60_000,
                    )
                    await asyncio.sleep(3)

                    # Use JS to extract all profile info at once
                    profile_data = await page.evaluate("""
                        () => {
                            const result = { 
                                name: null, designation: null,
                                employee_id: null, date_of_joining: null,
                                total_experience: null, status: null, manager: null,
                                profile_image_url: null
                            };

                            // Profile Image URL
                            const avatarImg = document.querySelector('#userAvatar img');
                            if (avatarImg) result.profile_image_url = avatarImg.getAttribute('src');

                            // Name — typically the first h6 in the profile card
                            const h6 = document.querySelector('h6');
                            if (h6) result.name = h6.textContent.trim();

                            // The rest can be found by scanning all text nodes or specific patterns
                            const allTextNodes = [];
                            const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, null, false);
                            let node;
                            while (node = walker.nextNode()) {
                                if (node.nodeValue.trim()) {
                                    allTextNodes.push(node.nodeValue.trim());
                                }
                            }

                            // Join text nodes with newlines to make it easier to search
                            const fullText = allTextNodes.join('\\n');

                            // Designation - usually all caps, no numbers, after name
                            if (h6 && h6.nextElementSibling) {
                                result.designation = h6.nextElementSibling.textContent.trim();
                            }
                            if (!result.designation) {
                                for (const t of allTextNodes) {
                                    if (t === t.toUpperCase() && t.length > 3 && t.length < 50 && t.match(/^[A-Z ]+$/)) {
                                        result.designation = t;
                                        break;
                                    }
                                }
                            }

                            // Use regex to find specific key-value patterns
                            const innerText = document.body.innerText;
                            const empIdMatch = innerText.match(/Employee Id:\s*(.+)/i) || innerText.match(/Employee Id\s*:\s*(.+)/i) || innerText.match(/EMP\d+/);
                            if (empIdMatch) result.employee_id = empIdMatch[1] ? empIdMatch[1].trim() : empIdMatch[0].trim();

                            const dojMatch = innerText.match(/Date of Joining:\s*(.+)/i);
                            if (dojMatch) result.date_of_joining = dojMatch[1].trim();

                            const expMatch = innerText.match(/Total Experience:\s*(.+)/i);
                            if (expMatch) result.total_experience = expMatch[1].trim();

                            const statusMatch = innerText.match(/Status:\s*([A-Za-z]+)/i);
                            if (statusMatch) result.status = statusMatch[1].trim();

                            const managerMatch = innerText.match(/Manager:\s*([A-Za-z ]+)/i);
                            if (managerMatch) result.manager = managerMatch[1].trim();

                            return result;
                        }
                    """)
                    if profile_data:
                        name = profile_data.get("name") or None
                        designation = profile_data.get("designation") or None
                        employee_id = profile_data.get("employee_id") or None
                        date_of_joining = profile_data.get("date_of_joining") or None
                        total_experience = profile_data.get("total_experience") or None
                        status = profile_data.get("status") or None
                        manager = profile_data.get("manager") or None
                        profile_url = profile_data.get("profile_image_url") or None
                except Exception:
                    pass

                # ── Extract Rewards ────────────────────────────────────────
                rewards: list[RewardItem] = []
                try:
                    # Use JS to extract award data from the profile page
                    award_data = await page.evaluate("""
                        () => {
                            const awards = [];
                            const container = document.querySelector('.user_awards, .dashboard-awards');
                            if (!container) return awards;

                            const imgs = container.querySelectorAll('img');
                            for (const img of imgs) {
                                const src = img.getAttribute('src') || '';
                                const awardName = img.getAttribute('alt')
                                    || img.getAttribute('data-original-title')
                                    || img.getAttribute('title')
                                    || 'Achievement Award';
                                awards.push({ name: awardName, icon_url: src });
                            }
                            return awards;
                        }
                    """)

                    if award_data:
                        # Group duplicates by name+icon and count
                        seen: dict[str, RewardItem] = {}
                        for item in award_data:
                            icon_url = item.get("icon_url", "")
                            if icon_url and not icon_url.startswith(("http", "data:")) and icon_url != "trophy-icon":
                                if icon_url.startswith("/"):
                                    icon_url = f"{SITE_URL.rstrip('/')}{icon_url}"
                                else:
                                    icon_url = f"{SITE_URL.rstrip('/')}/{icon_url}"

                            award_name = item.get("name", "Unknown Award")
                            key = f"{award_name}|{icon_url}"
                            if key in seen:
                                seen[key].count += 1
                            else:
                                seen[key] = RewardItem(
                                    name=award_name,
                                    icon_url=icon_url,
                                    count=1,
                                )
                        rewards = list(seen.values())
                except Exception:
                    pass

                return DashboardInfoResult(
                    success=True,
                    name=name,
                    designation=designation,
                    employee_id=employee_id,
                    date_of_joining=date_of_joining,
                    total_experience=total_experience,
                    status=status,
                    manager=manager,
                    profile_url=profile_url,
                    rewards=rewards,
                    project_productivity=project_productivity,
                    message="Dashboard info scraped successfully.",
                )
            finally:
                await page.close()
                await browser.close()
    except PlaywrightError as exc:
        return DashboardInfoResult(
            success=False,
            name=None,
            designation=None,
            employee_id=None,
            date_of_joining=None,
            total_experience=None,
            status=None,
            manager=None,
            profile_url=None,
            rewards=[],
            project_productivity=None,
            message=f"Scraping failed: {exc}",
            error=True,
        )
    except Exception as exc:
        return DashboardInfoResult(
            success=False,
            name=None,
            designation=None,
            employee_id=None,
            date_of_joining=None,
            total_experience=None,
            status=None,
            manager=None,
            profile_url=None,
            rewards=[],
            project_productivity=None,
            message=f"Unexpected error: {exc}",
            error=True,
        )


def _run_dashboard_in_thread(
    email: str, password: str, headless: bool
) -> DashboardInfoResult:
    """Run the dashboard scraping in a fresh event loop inside a thread."""
    if sys.platform == "win32":
        loop = asyncio.ProactorEventLoop()
    else:
        loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(
            _scrape_dashboard_info_impl(email, password, headless)
        )
    finally:
        loop.close()


async def scrape_dashboard_info(
    email: str, password: str, headless: bool = True
) -> DashboardInfoResult:
    """Public wrapper — offloads Playwright to a thread so it works on Windows."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        _executor, _run_dashboard_in_thread, email, password, headless
    )

