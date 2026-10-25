"""Screenshots of the running demo (port 9009) at 1440x900 -> docs/images/*.png. Resets demo data first.

    /Users/vnktajay/Projects/agent-report-card/video/.venv/bin/python scripts/screenshots.py
"""
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:9009"
OUT = Path(__file__).resolve().parent.parent / "docs" / "images"
OUT.mkdir(parents=True, exist_ok=True)


def shot(page, name):
    page.wait_for_timeout(500)
    page.screenshot(path=str(OUT / f"{name}.png"))
    print("saved", OUT / f"{name}.png")


def turn_done(page):
    page.wait_for_selector("#msgs .typing", state="detached", timeout=60000)
    page.wait_for_timeout(300)


def new_chat(page):
    if page.is_visible("#chat"):
        page.click("#closeBtn")
    page.click("#launcher")


def send(page, text=None):
    if text:
        page.fill("#input", text)
    page.click("#send")
    turn_done(page)


def fault(page, preset):
    page.click("#gearBtn")
    page.check(f'#drawer input[value="{preset}"]')
    page.wait_for_timeout(300)


def toast_gone(page):
    page.wait_for_timeout(2800)


with sync_playwright() as p:
    b = p.chromium.launch()
    page = b.new_page(viewport={"width": 1440, "height": 900})
    page.request.post(f"{BASE}/api/reset")
    page.goto(BASE)
    page.wait_for_load_state("networkidle")
    shot(page, "00_homepage")

    # 1. a fare-conditions answer (no write)
    page.click("#launcher")
    page.click("text=What would I get back?")
    turn_done(page)
    shot(page, "01_chat_answer")

    # 2. refund -> approval card, 3. approve -> confirmation
    new_chat(page)
    page.click("#tile-refund")
    send(page)
    page.wait_for_selector(".approval")
    shot(page, "02_approval_card")
    page.click('.approval [data-act="approve"]')
    turn_done(page)
    shot(page, "03_after_approval")

    # 4. bereavement -> hand-off
    new_chat(page)
    page.click("text=Bereavement")
    turn_done(page)
    shot(page, "04_escalation")

    # 5. demo controls: quote fails once -> retry badge (approval left pending for the supervisor page)
    new_chat(page)
    fault(page, "quote_flaky")
    shot(page, "05_demo_controls")
    page.click("#gearBtn")
    page.click("#tile-change")
    send(page)
    page.wait_for_selector(".st .retry")
    toast_gone(page)
    shot(page, "06_retry_badge")

    # 6. quote service down: price can't be verified -> withheld + hand-off (instructor feedback)
    fault(page, "quote_down")
    page.click("#gearBtn")
    new_chat(page)
    send(page, "Rana Haddad, booking FLXRFD, card 3085. Exactly how much would I get back if I cancel today?")
    toast_gone(page)
    shot(page, "07_price_unverified")
    fault(page, "none")
    page.click("#gearBtn")

    # 7. another pending approval, then the supervisor page
    new_chat(page)
    page.click("#tile-manage")
    send(page)
    page.wait_for_selector(".approval")
    sup = b.new_page(viewport={"width": 1440, "height": 900})
    sup.goto(f"{BASE}/supervisor")
    sup.wait_for_selector(".item .approval")
    shot(sup, "08_supervisor")
    b.close()
