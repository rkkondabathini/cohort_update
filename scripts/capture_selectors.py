"""
capture_selectors.py — Captures detailed HTML of every interactive element
across all 4 tabs of the cohort management page.

Saves full outerHTML snapshots to: onwards-masai/scripts/captured/
One file per tab. Share these files and we'll build the script from real selectors.

Run:
    python onwards-masai/scripts/capture_selectors.py
"""

import os
import json
from playwright.sync_api import sync_playwright

COHORT_ID  = "2007"
TARGET_URL = f"https://admissions-admin.masaischool.com/iit/cohort-management/{COHORT_ID}"
LOGIN_URL  = "https://admissions-admin.masaischool.com/"

PROFILE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "browser_profile"
)

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "captured")
os.makedirs(OUT_DIR, exist_ok=True)


def capture_tab(page, tab_name: str):
    """Dump full interactive element details for the current tab."""

    data = page.evaluate("""() => {
        const out = {};

        // ── Buttons ───────────────────────────────────────────────────────────
        out.buttons = [...document.querySelectorAll('button')]
            .filter(el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; })
            .map(el => ({
                text:      el.textContent.trim().slice(0, 100),
                id:        el.id || null,
                class:     el.className.slice(0, 200),
                type:      el.type || null,
                outerHTML: el.outerHTML.slice(0, 400),
            }));

        // ── All inputs ────────────────────────────────────────────────────────
        out.inputs = [...document.querySelectorAll('input, textarea')]
            .filter(el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; })
            .map(el => ({
                tag:         el.tagName,
                type:        el.type || null,
                id:          el.id || null,
                name:        el.name || null,
                placeholder: el.placeholder || null,
                value:       el.value ? el.value.slice(0, 100) : null,
                checked:     el.type === 'checkbox' ? el.checked : null,
                class:       el.className.slice(0, 200),
                outerHTML:   el.outerHTML.slice(0, 400),
            }));

        // ── Native selects ────────────────────────────────────────────────────
        out.selects = [...document.querySelectorAll('select')]
            .filter(el => { const r = el.getBoundingClientRect(); return r.width > 0; })
            .map(el => ({
                id:       el.id || null,
                name:     el.name || null,
                selected: el.options[el.selectedIndex]?.text || null,
                options:  [...el.options].map(o => o.text.trim()).slice(0, 20),
                class:    el.className.slice(0, 200),
            }));

        // ── React-select containers ───────────────────────────────────────────
        out.react_selects = [...document.querySelectorAll(
            '[class*="react-select"][class*="container"]'
        )]
            .filter(el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; })
            .map((el, i) => {
                const sv  = el.querySelector('[class*="single-value"]');
                const ph  = el.querySelector('[class*="placeholder"]');
                const mvs = [...el.querySelectorAll('[class*="multi-value__label"]')];
                return {
                    index:        i,
                    id:           el.id || null,
                    class:        el.className.slice(0, 200),
                    single_value: sv?.textContent.trim() || null,
                    placeholder:  ph?.textContent.trim() || null,
                    multi_values: mvs.map(m => m.textContent.trim()),
                    outerHTML:    el.outerHTML.slice(0, 600),
                };
            });

        // ── Labels + their linked controls ────────────────────────────────────
        out.labels = [...document.querySelectorAll('label')]
            .filter(el => { const r = el.getBoundingClientRect(); return r.width > 0 && el.textContent.trim(); })
            .map(el => ({
                text:      el.textContent.trim().slice(0, 100),
                for:       el.htmlFor || null,
                class:     el.className.slice(0, 150),
                outerHTML: el.outerHTML.slice(0, 500),
            }));

        // ── Pencil / edit SVG buttons ─────────────────────────────────────────
        out.edit_icons = [...document.querySelectorAll('button svg, [role="button"] svg')]
            .filter(el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; })
            .slice(0, 30)
            .map(el => {
                const btn = el.closest('button') || el.closest('[role="button"]');
                return {
                    btn_class:   btn?.className.slice(0, 200) || null,
                    btn_text:    btn?.textContent.trim().slice(0, 80) || null,
                    btn_html:    btn?.outerHTML.slice(0, 400) || null,
                    aria_label:  el.getAttribute('aria-label') || null,
                    parent_text: el.closest('tr,li,div[class*="row"],div[class*="item"]')
                                  ?.textContent.trim().slice(0, 100) || null,
                };
            });

        // ── Headings / section titles ─────────────────────────────────────────
        out.headings = [...document.querySelectorAll('h1,h2,h3,h4,h5,h6,[class*="heading"],[class*="title"]')]
            .filter(el => { const r = el.getBoundingClientRect(); return r.width > 0 && el.textContent.trim(); })
            .slice(0, 30)
            .map(el => ({
                tag:  el.tagName,
                text: el.textContent.trim().slice(0, 100),
            }));

        // ── Full page HTML (for fallback analysis) ────────────────────────────
        out.body_html = document.body.innerHTML.slice(0, 80000);

        return out;
    }""")

    filename = os.path.join(OUT_DIR, f"{tab_name.lower().replace(' ', '_')}.json")
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    screenshot = os.path.join(OUT_DIR, f"{tab_name.lower().replace(' ', '_')}.png")
    page.screenshot(path=screenshot, full_page=True)

    print(f"  Saved: {filename}")
    print(f"  Screenshot: {screenshot}")
    print(f"  Buttons:        {len(data.get('buttons', []))}")
    print(f"  Inputs:         {len(data.get('inputs', []))}")
    print(f"  React-selects:  {len(data.get('react_selects', []))}")
    print(f"  Edit icons:     {len(data.get('edit_icons', []))}")


def dismiss_any_dialog(page):
    """Close any open modal/dialog before navigating tabs."""
    try:
        page.keyboard.press("Escape")
        page.wait_for_timeout(500)
    except Exception:
        pass


def go_to_tab(page, name: str):
    """Tabs are <button> elements in the left sidebar nav."""
    dismiss_any_dialog(page)
    btn = page.get_by_role("button", name=name)
    if btn.count() > 0:
        btn.first.click()
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(2_000)
        return
    # fallback: any element with exact text
    page.locator(f"text={name}").first.click()
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(2_000)


def run():
    print(f"Profile : {PROFILE_DIR}")
    print(f"Output  : {OUT_DIR}\n")

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir = PROFILE_DIR,
            headless      = False,
            slow_mo       = 200,
            args          = ["--start-maximized"],
            no_viewport   = True,
        )
        page = context.pages[0] if context.pages else context.new_page()

        # ── Login check ───────────────────────────────────────────────────────
        page.goto(LOGIN_URL)
        page.wait_for_load_state("networkidle")
        if LOGIN_URL.rstrip("/") in page.url.rstrip("/") or "login" in page.url.lower():
            print("Not logged in — please log in with OTP.")
            input("Press ENTER once logged in... ")
            page.wait_for_load_state("networkidle", timeout=30_000)
        else:
            print(f"Logged in. URL: {page.url}")

        # ── Navigate to cohort ────────────────────────────────────────────────
        print(f"\nLoading cohort {COHORT_ID}...")
        page.goto(TARGET_URL)
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(3_000)

        # ── Tab 1: Basic Details (default) ────────────────────────────────────
        print("\n[1/4] Basic Details...")
        dismiss_any_dialog(page)
        go_to_tab(page, "Basic Details")
        capture_tab(page, "Basic Details")

        # ── Tab 2: Identifiers ────────────────────────────────────────────────
        print("\n[2/4] Identifiers...")
        go_to_tab(page, "Identifiers")
        capture_tab(page, "Identifiers")

        # ── Tab 3: Dates ──────────────────────────────────────────────────────
        print("\n[3/4] Dates...")
        go_to_tab(page, "Dates")
        capture_tab(page, "Dates")

        # ── Tab 4: Course Onboarding ──────────────────────────────────────────
        print("\n[4/4] Course Onboarding...")
        go_to_tab(page, "Course Onboarding")
        capture_tab(page, "Course Onboarding")

        print("\nAll tabs captured.")
        input("Press ENTER to close browser...")
        context.close()

    print(f"\nDone. Files saved to: {OUT_DIR}")


if __name__ == "__main__":
    run()
