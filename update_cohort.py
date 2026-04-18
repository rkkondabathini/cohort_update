"""
update_cohort.py — Bulk cohort management updater
Site: https://admissions-admin.masaischool.com

CSV columns (cohort_id required; all others optional — leave blank to skip):
    cohort_id                  — numeric cohort ID  (e.g. 2007)
    batch_id                   — text  [Basic Details pencil, skip-if-same]
    hall_ticket_prefix         — text  [Identifiers pencil, skip-if-same]
    student_prefix             — text  [Identifiers pencil, skip-if-same]
    foundation_starts          — DD/MM/YYYY HH:MM  [Dates tab, auto-saves, skip-if-same]
    batch_start_date           — DD/MM/YYYY HH:MM  [Dates tab, auto-saves, skip-if-same]
    lms_batch_id               — full batch name to search & select
    lms_section_ids            — comma-separated full section names (replaces existing)
    manager_id                 — numeric/text
    enable_kit                 — TRUE / FALSE
    disable_welcome_kit_tshirt — TRUE / FALSE

Run:
    python onwards-masai/update_cohort.py
"""

import re
import os
import sys
import glob
import shutil
import pandas as pd
from datetime import datetime
from playwright.sync_api import sync_playwright

# ── Directories ────────────────────────────────────────────────────────────────
BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
INPUT_DIR   = os.path.join(BASE_DIR, "input")
RUNS_DIR    = os.path.join(BASE_DIR, "runs")
ARCHIVE_DIR = os.path.join(BASE_DIR, "archive")
PROFILE_DIR = os.path.join(BASE_DIR, "browser_profile")

for d in (INPUT_DIR, RUNS_DIR, ARCHIVE_DIR):
    os.makedirs(d, exist_ok=True)

BASE_URL  = "https://admissions-admin.masaischool.com/iit/cohort-management"
LOGIN_URL = "https://admissions-admin.masaischool.com/"

SKIPPED = "SKIPPED"
CHANGED = "CHANGED"
FAILED  = "FAILED"
ERROR   = "ERROR"


# ── Tee logger ─────────────────────────────────────────────────────────────────
class _Tee:
    def __init__(self, filepath):
        self._file    = open(filepath, "w", buffering=1, encoding="utf-8")
        self._stdout  = sys.stdout
        self._pending = ""

    def write(self, data):
        self._stdout.write(data)
        self._pending += data
        while "\n" in self._pending:
            line, self._pending = self._pending.split("\n", 1)
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self._file.write(f"{ts} | {line}\n")

    def flush(self):
        self._stdout.flush()
        self._file.flush()

    def close(self):
        if self._pending:
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self._file.write(f"{ts} | {self._pending}\n")
        self._file.close()

    def __getattr__(self, name):
        return getattr(self._stdout, name)


_tee = None


def _start_log(stem: str):
    global _tee
    path = os.path.join(RUNS_DIR, f"{stem}.log")
    _tee = _Tee(path)
    sys.stdout = _tee
    print(f"Log → {path}")


def _stop_log():
    global _tee
    if _tee:
        sys.stdout = _tee._stdout
        _tee.close()
        _tee = None


# ── Helpers ────────────────────────────────────────────────────────────────────
def is_empty(val) -> bool:
    if val is None:
        return True
    try:
        if pd.isna(val):
            return True
    except Exception:
        pass
    return str(val).strip() == ""


def to_bool(val):
    if is_empty(val):
        return None
    s = str(val).strip().upper()
    if s in ("TRUE", "YES", "1"):
        return True
    if s in ("FALSE", "NO", "0"):
        return False
    return None


def parse_dt(val: str):
    """DD/MM/YYYY HH:MM or DD-MM-YYYY HH:MM  →  YYYY-MM-DDTHH:MM  (datetime-local format)."""
    val = str(val).strip()
    for fmt in ("%d/%m/%Y %H:%M", "%d/%m/%Y",
                "%d-%m-%Y %H:%M", "%d-%m-%Y",
                "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(val, fmt).strftime("%Y-%m-%dT%H:%M")
        except ValueError:
            pass
    return None


def dt_display(val: str) -> str:
    """YYYY-MM-DDTHH:MM  →  DD/MM/YYYY HH:MM  for readable logging."""
    try:
        return datetime.strptime(val.strip(), "%Y-%m-%dT%H:%M").strftime("%d/%m/%Y %H:%M")
    except Exception:
        return val.strip()


def _dismiss_dialog(page):
    """Press Escape to close any open modal before acting."""
    try:
        page.keyboard.press("Escape")
        page.wait_for_timeout(400)
    except Exception:
        pass


# ── Tab navigation ─────────────────────────────────────────────────────────────
def _go_to_tab(page, name: str):
    """Tabs are <button> elements in the left sidebar nav."""
    _dismiss_dialog(page)
    btn = page.get_by_role("button", name=name)
    btn.wait_for(state="visible", timeout=15_000)
    btn.first.click()
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(1_500)


# ═══════════════════════════════════════════════════════════════════════════════
# Shared: open an inline field editor by label text, then read/write/save
# Used by: Batch ID (Basic Details), Hall Ticket Prefix, Student Prefix (Identifiers)
#
# Structure on page:
#   <div class="p-3 hover:bg-gray-50">
#     <span class="text-gray-600 ...">LABEL TEXT</span>
#     ...
#     <button class="text-blue-600 ...">pencil SVG</button>
#   </div>
# ═══════════════════════════════════════════════════════════════════════════════
def _update_labeled_field(page, label_text: str, desired, field_name: str) -> str:
    if is_empty(desired):
        print(f"  {field_name} → SKIP (blank in CSV)")
        return SKIPPED

    desired = str(desired).strip()
    try:
        # Find the card that contains the matching label span
        section = page.locator("div.p-3").filter(
            has=page.locator("span.text-gray-600", has_text=label_text)
        )
        section.wait_for(state="visible", timeout=6_000)

        # Click the pencil (blue) button inside it
        pencil = section.locator("button.text-blue-600")
        pencil.wait_for(state="visible", timeout=6_000)
        pencil.click()
        page.wait_for_timeout(600)

        # A Chakra dialog opens with a text input
        textbox = page.get_by_role("textbox").first
        textbox.wait_for(state="visible", timeout=6_000)
        current = textbox.input_value().strip()

        if current == desired:
            print(f"  {field_name} → SKIP (already '{desired}')")
            try:
                page.get_by_role("button", name="Cancel").click()
            except Exception:
                page.keyboard.press("Escape")
            page.wait_for_timeout(400)
            return SKIPPED

        print(f"  {field_name} → UPDATE '{current}' → '{desired}'")
        textbox.fill(desired)
        page.wait_for_timeout(200)
        page.get_by_role("button", name="Save Changes").click()
        page.wait_for_timeout(800)
        return CHANGED

    except Exception as e:
        print(f"  {field_name} → FAILED: {e}")
        try:
            page.get_by_role("button", name="Cancel").click()
        except Exception:
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass
        return FAILED


# ═══════════════════════════════════════════════════════════════════════════════
# Basic Details — Batch ID
# ═══════════════════════════════════════════════════════════════════════════════
def _update_batch_id(page, desired) -> str:
    return _update_labeled_field(page, "Batch ID", desired, "Batch ID")


# ═══════════════════════════════════════════════════════════════════════════════
# Identifiers — Hall Ticket Prefix & Student Prefix
# ═══════════════════════════════════════════════════════════════════════════════
def _update_hall_ticket_prefix(page, desired) -> str:
    return _update_labeled_field(page, "Hall Ticket Prefix", desired, "Hall Ticket Prefix")


def _update_student_prefix(page, desired) -> str:
    return _update_labeled_field(page, "Student Prefix", desired, "Student Prefix")


# ═══════════════════════════════════════════════════════════════════════════════
# Dates — Foundation Starts & Batch Start Date
#
# Structure (Dates tab):
#   <table>
#     <tr>
#       <td>Foundation Starts</td>
#       <td><input type="datetime-local" value="YYYY-MM-DDTHH:MM"></td>
#       <td><button aria-label="Clear date">×</button></td>
#     </tr>
#     ...
#   </table>
#
# NOTE: There is NO "Save date" button — dates auto-save on change (input event).
# ═══════════════════════════════════════════════════════════════════════════════
def _update_date_field(page, row_label: str, desired_csv, field_name: str) -> str:
    blank = is_empty(desired_csv)
    desired_dt = None if blank else parse_dt(str(desired_csv).strip())

    if not blank and not desired_dt:
        print(f"  {field_name} → SKIP (invalid date '{desired_csv}')")
        return SKIPPED

    try:
        # Find the <tr> whose first <td> exactly matches the label
        row = page.locator("tr").filter(
            has=page.locator("td", has_text=re.compile(rf"^{re.escape(row_label)}$"))
        )
        row.wait_for(state="visible", timeout=6_000)

        dt_input = row.locator("input[type='datetime-local']")
        dt_input.wait_for(state="visible", timeout=6_000)
        current = dt_input.input_value().strip()

        if blank:
            # Blank in CSV → clear the existing date (if any)
            if not current:
                print(f"  {field_name} → SKIP (already empty)")
                return SKIPPED
            print(f"  {field_name} → CLEAR (was '{dt_display(current)}')")
            clear_btn = row.locator("button[aria-label='Clear date']")
            clear_btn.wait_for(state="visible", timeout=6_000)
            clear_btn.click()
            page.wait_for_timeout(800)
            return CHANGED

        if current == desired_dt:
            print(f"  {field_name} → SKIP (already '{dt_display(current)}')")
            return SKIPPED

        print(f"  {field_name} → UPDATE "
              f"'{dt_display(current) if current else 'empty'}' → '{dt_display(desired_dt)}'")

        # Set via JavaScript (most reliable for datetime-local in React)
        dt_input.evaluate(
            f"el => {{ el.value = '{desired_dt}'; "
            f"el.dispatchEvent(new Event('input', {{bubbles: true}})); "
            f"el.dispatchEvent(new Event('change', {{bubbles: true}})); }}"
        )
        page.wait_for_timeout(800)
        return CHANGED

    except Exception as e:
        print(f"  {field_name} → FAILED: {e}")
        return FAILED


# ═══════════════════════════════════════════════════════════════════════════════
# Course Onboarding — LMS Settings
#
# Key selectors (from DOM capture):
#   LMS Batch:    div.lms-batch-dropdown button  (opens batch search)
#   LMS Sections: span.bg-green-50 button        (remove chip)
#                 div.lms-section-dropdown button (open section picker)
#   Manager ID:   input[placeholder="Enter manager ID"]
#   Save:         auto-saves — no explicit save button found in DOM
# ═══════════════════════════════════════════════════════════════════════════════
def _update_lms_settings(page, row) -> dict:
    results = {
        "lms_batch_id":    SKIPPED,
        "lms_section_ids": SKIPPED,
        "manager_id":      SKIPPED,
    }

    lms_batch    = str(row.get("lms_batch_id",    "")).strip() if not is_empty(row.get("lms_batch_id"))    else ""
    sections_raw = str(row.get("lms_section_ids",  "")).strip() if not is_empty(row.get("lms_section_ids")) else ""
    sections     = [s.strip() for s in sections_raw.split(",") if s.strip()]
    manager_id   = str(row.get("manager_id",       "")).strip() if not is_empty(row.get("manager_id"))      else ""

    if not lms_batch and not sections and not manager_id:
        print("  LMS Settings → SKIP (all blank in CSV)")
        return results

    # ── LMS Batch ID ──────────────────────────────────────────────────────────
    if lms_batch:
        print(f"  LMS Batch ID → '{lms_batch}'")
        try:
            # Open the batch dropdown (inside div.lms-batch-dropdown)
            page.locator(".lms-batch-dropdown button").click()
            page.wait_for_timeout(600)

            search = page.get_by_placeholder("Search batches...")
            search.wait_for(state="visible", timeout=6_000)
            search.fill(lms_batch)
            page.wait_for_timeout(1_000)

            # Click first result button matching the batch name
            page.get_by_role("button").filter(
                has_text=re.compile(re.escape(lms_batch), re.I)
            ).first.click()
            page.wait_for_timeout(600)
            results["lms_batch_id"] = CHANGED

        except Exception as e:
            print(f"    [ERROR] LMS Batch ID: {e}")
            results["lms_batch_id"] = FAILED
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass
            page.wait_for_timeout(400)

    # ── LMS Section IDs ───────────────────────────────────────────────────────
    if sections:
        print(f"  LMS Section IDs → {sections}")
        try:
            # Step 1: Remove all existing section chips
            # Section chips are <span class="...bg-green-50..."> containing a <button>
            removed = 0
            for _ in range(50):  # safety cap
                rm = page.locator("span.bg-green-50 button")
                if rm.count() == 0:
                    break
                rm.first.click()
                page.wait_for_timeout(300)
                removed += 1

            if removed:
                print(f"    Cleared {removed} existing section(s)")
                page.wait_for_timeout(400)
            else:
                print("    No existing sections to clear")

            # Step 2: Open the section picker (inside div.lms-section-dropdown)
            page.locator(".lms-section-dropdown button").click()
            page.wait_for_timeout(600)

            for section in sections:
                search = page.get_by_placeholder("Search sections...")
                search.wait_for(state="visible", timeout=6_000)
                search.fill(section)
                page.wait_for_timeout(800)

                # Click the matching result
                page.get_by_role("button").filter(
                    has_text=re.compile(re.escape(section), re.I)
                ).first.click()
                page.wait_for_timeout(400)

                # Clear search for next section
                search.fill("")
                page.wait_for_timeout(200)

            # Confirm: "Done (N selected)"
            page.get_by_role("button").filter(
                has_text=re.compile(r"Done \(\d+ selected\)")
            ).click()
            page.wait_for_timeout(600)
            results["lms_section_ids"] = CHANGED

        except Exception as e:
            print(f"    [ERROR] LMS Section IDs: {e}")
            results["lms_section_ids"] = FAILED
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass
            page.wait_for_timeout(400)

    # ── Manager ID ────────────────────────────────────────────────────────────
    if manager_id:
        print(f"  Manager ID → '{manager_id}'")
        try:
            mgr = page.get_by_placeholder("Enter manager ID")
            mgr.wait_for(state="visible", timeout=6_000)
            current = mgr.input_value().strip()
            if current == manager_id:
                print(f"    SKIP (already '{manager_id}')")
            else:
                mgr.fill(manager_id)
                mgr.press("Tab")
                page.wait_for_timeout(400)
                results["manager_id"] = CHANGED
        except Exception as e:
            print(f"    [ERROR] Manager ID: {e}")
            results["manager_id"] = FAILED

    # ── Save LMS Settings (if button appears after changes) ───────────────────
    try:
        save_btn = page.locator("button", has_text="Save LMS Settings")
        if save_btn.count() > 0 and save_btn.first.is_visible():
            save_btn.first.click()
            page.wait_for_timeout(1_200)
            print("  [LMS SAVED via button]")
        else:
            # Auto-saves — wait a moment for the API call to complete
            page.wait_for_timeout(800)
            print("  [LMS auto-saved]")
    except Exception as e:
        print(f"  [LMS SAVE WARNING] {e}")

    return results


# ═══════════════════════════════════════════════════════════════════════════════
# Course Onboarding — Kit Toggles
#
# Toggle structure (label-based, not ID-based — IDs are dynamic React values):
#   <div class="p-3 hover:bg-gray-50">
#     <span class="text-gray-600 ...">Enable Kit</span>
#     ...
#     <label class="chakra-switch__root">
#       <input type="checkbox" value="on" [checked]>
#       <span data-part="control">...</span>
#     </label>
#   </div>
#
# Click the visible <span data-part="control"> (the toggle knob), not the hidden input.
# ═══════════════════════════════════════════════════════════════════════════════
def _update_toggle(page, label_contains: str, desired, field_name: str) -> str:
    desired_bool = to_bool(desired)
    if desired_bool is None:
        print(f"  {field_name} → SKIP (blank in CSV)")
        return SKIPPED

    try:
        row = page.locator("div.p-3").filter(
            has=page.locator("span.text-gray-600", has_text=label_contains)
        )
        row.wait_for(state="visible", timeout=6_000)

        checkbox = row.locator("input[type='checkbox']")
        current = checkbox.is_checked()

        if current == desired_bool:
            print(f"  {field_name} → SKIP (already {'ON' if desired_bool else 'OFF'})")
            return SKIPPED

        print(f"  {field_name} → UPDATE → {'ON' if desired_bool else 'OFF'}")

        # Click the visible toggle control span (the knob), not the hidden input
        toggle_control = row.locator("[data-part='control']")
        toggle_control.click()
        page.wait_for_timeout(600)

        new_state = checkbox.is_checked()
        if new_state != desired_bool:
            print(f"    [WARN] Toggle verify: got {new_state}, want {desired_bool}")
            return FAILED
        return CHANGED

    except Exception as e:
        print(f"  {field_name} → FAILED: {e}")
        return FAILED


# ═══════════════════════════════════════════════════════════════════════════════
# Per-cohort processor
# ═══════════════════════════════════════════════════════════════════════════════
def process_cohort(page, row, base_url: str = BASE_URL) -> dict:
    cohort_id = str(row["cohort_id"]).strip()
    url       = f"{base_url}/{cohort_id}"

    s = {
        "cohort_id":                   cohort_id,
        "batch_id":                    SKIPPED,
        "hall_ticket_prefix":          SKIPPED,
        "student_prefix":              SKIPPED,
        "foundation_starts":           SKIPPED,
        "batch_start_date":            SKIPPED,
        "lms_batch_id":                SKIPPED,
        "lms_section_ids":             SKIPPED,
        "manager_id":                  SKIPPED,
        "enable_kit":                  SKIPPED,
        "disable_welcome_kit_tshirt":  SKIPPED,
        "notes":                       "",
    }

    print(f"  Loading: {url}")
    page.goto(url)
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(3_000)
    _dismiss_dialog(page)

    # ── 1. Basic Details ──────────────────────────────────────────────────────
    print("  [Basic Details]")
    _go_to_tab(page, "Basic Details")
    s["batch_id"] = _update_batch_id(page, row.get("batch_id"))

    # ── 2. Identifiers ────────────────────────────────────────────────────────
    print("  [Identifiers]")
    _go_to_tab(page, "Identifiers")
    s["hall_ticket_prefix"] = _update_hall_ticket_prefix(page, row.get("hall_ticket_prefix"))
    s["student_prefix"]     = _update_student_prefix(page, row.get("student_prefix"))

    # ── 3. Dates ──────────────────────────────────────────────────────────────
    print("  [Dates]")
    _go_to_tab(page, "Dates")
    s["foundation_starts"] = _update_date_field(
        page, "Foundation Starts", row.get("foundation_starts"), "Foundation Starts"
    )
    s["batch_start_date"] = _update_date_field(
        page, "Batch Start Date", row.get("batch_start_date"), "Batch Start Date"
    )

    # ── 4. Course Onboarding ──────────────────────────────────────────────────
    print("  [Course Onboarding]")
    _go_to_tab(page, "Course Onboarding")

    lms = _update_lms_settings(page, row)
    s.update(lms)

    s["enable_kit"] = _update_toggle(
        page, "Enable Kit", row.get("enable_kit"), "Enable Kit"
    )
    s["disable_welcome_kit_tshirt"] = _update_toggle(
        page, "Disable Welcome Kit T-Shirt", row.get("disable_welcome_kit_tshirt"),
        "Disable Welcome Kit T-Shirt"
    )

    return s


# ═══════════════════════════════════════════════════════════════════════════════
# Login helper — headed browser, handles OTP if session is expired
# ═══════════════════════════════════════════════════════════════════════════════
def _ensure_logged_in():
    """
    Open a headed browser to check / restore the session.
    - If already logged in: shows confirmation, user presses ENTER to proceed.
    - If session expired: waits for OTP login, then user presses ENTER to proceed.
    Closes the headed browser before returning so headless can open the same profile.
    """
    print("\n── Step 1 of 2: Login check ─────────────────────────────")
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir = PROFILE_DIR,
            headless      = False,
            args          = ["--start-maximized"],
            no_viewport   = True,
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(LOGIN_URL)
        page.wait_for_load_state("networkidle")

        if LOGIN_URL.rstrip("/") in page.url.rstrip("/") or "login" in page.url.lower():
            print("Session expired — please log in with OTP in the browser window.")
            input("Press ENTER once you are on the dashboard... ")
            page.wait_for_load_state("networkidle", timeout=60_000)
            print(f"Logged in. URL: {page.url}")
        else:
            print(f"Session active. URL: {page.url}")

        input("Press ENTER to start updating cohorts... ")
        context.close()
    print("Login confirmed. Opening headless browser for updates...\n")


# ═══════════════════════════════════════════════════════════════════════════════
# Entry point
# ═══════════════════════════════════════════════════════════════════════════════
def run():
    # ── Step 1: CSV selection ─────────────────────────────────────────────────
    csv_files = sorted(glob.glob(os.path.join(INPUT_DIR, "*.csv")))

    if not csv_files:
        print(f"[ERROR] No CSV files found in {INPUT_DIR}/")
        print("Place your input CSV there and re-run.")
        return

    print(f"Found {len(csv_files)} CSV file(s):")
    for i, f in enumerate(csv_files):
        print(f"  [{i}] {os.path.basename(f)}")

    if len(csv_files) == 1:
        chosen = csv_files[0]
        print(f"Auto-selecting: {os.path.basename(chosen)}")
    else:
        idx = input("\nEnter file number: ").strip()
        try:
            chosen = csv_files[int(idx)]
        except (ValueError, IndexError):
            print("[ERROR] Invalid selection.")
            return

    df = pd.read_csv(chosen, dtype=str)

    if "cohort_id" not in df.columns:
        print("[ERROR] CSV must have a 'cohort_id' column.")
        return

    print(f"\nRows to process: {len(df)}")

    # ── Step 2: Login (headed, with OTP if needed) ────────────────────────────
    _ensure_logged_in()

    # ── Step 3: Run updates (headless) ────────────────────────────────────────
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base      = os.path.splitext(os.path.basename(chosen))[0]
    log_stem  = f"cohort_{base}_{timestamp}"

    _start_log(log_stem)

    all_results = []

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir = PROFILE_DIR,
            headless      = True,
            slow_mo       = 200,
            viewport      = {"width": 1920, "height": 1080},
            args          = [
                "--window-size=1920,1080",
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
        page = context.pages[0] if context.pages else context.new_page()

        print("── Step 2 of 2: Cohort updates ──────────────────────────")
        print("Starting cohort updates...\n")

        print("Starting cohort updates...\n")

        for i, row in df.iterrows():
            cohort_id = str(row.get("cohort_id", "")).strip()
            print(f"{'─'*60}")
            print(f"[{i+1}/{len(df)}] Cohort ID: {cohort_id}")
            try:
                result = process_cohort(page, row)
            except Exception as e:
                print(f"  [ERROR] {e}")
                result = {
                    "cohort_id":                   cohort_id,
                    "batch_id":                    ERROR,
                    "hall_ticket_prefix":          ERROR,
                    "student_prefix":              ERROR,
                    "foundation_starts":           ERROR,
                    "batch_start_date":            ERROR,
                    "lms_batch_id":                ERROR,
                    "lms_section_ids":             ERROR,
                    "manager_id":                  ERROR,
                    "enable_kit":                  ERROR,
                    "disable_welcome_kit_tshirt":  ERROR,
                    "notes":                       str(e),
                }

            all_results.append(result)
            print()

        context.close()

    # ── Report ────────────────────────────────────────────────────────────────
    csv_path = os.path.join(RUNS_DIR, f"{log_stem}.csv")
    pd.DataFrame(all_results).to_csv(csv_path, index=False)
    print(f"\nCSV report  → {csv_path}")

    dest = os.path.join(ARCHIVE_DIR, f"{base}_{timestamp}.csv")
    shutil.copy2(chosen, dest)
    print(f"Input archived → {dest}")

    df_log = pd.DataFrame(all_results)
    print("\n══ Summary ══════════════════════════════════════════════")
    for col in ["batch_id", "hall_ticket_prefix", "student_prefix",
                "foundation_starts", "batch_start_date",
                "lms_batch_id", "lms_section_ids", "manager_id",
                "enable_kit", "disable_welcome_kit_tshirt"]:
        if col in df_log.columns:
            print(f"  {col:35s}: {df_log[col].value_counts().to_dict()}")
    print("═════════════════════════════════════════════════════════")
    print("Done.")

    _stop_log()


# ═══════════════════════════════════════════════════════════════════════════════
# Headless entry point (no interactive prompts — used by streamlit_app.py)
# ═══════════════════════════════════════════════════════════════════════════════
def run_headless(
    csv_path:    str,
    base_url:    str = BASE_URL,
    profile_dir: str = PROFILE_DIR,
) -> str:
    """
    Run updates for a given CSV without any input() prompts.
    Returns the path of the results CSV when done.
    """
    # Debug banner — visible immediately in Streamlit output
    print("=" * 60)
    print(f"run_headless() called")
    print(f"  csv_path    : {csv_path}")
    print(f"  base_url    : {base_url}")
    print(f"  profile_dir : {profile_dir}")
    print(f"  profile_exists: {os.path.isdir(profile_dir)}")
    print("=" * 60)
    sys.stdout.flush()

    df = pd.read_csv(csv_path, dtype=str)

    if "cohort_id" not in df.columns:
        print("[ERROR] CSV must have a 'cohort_id' column.")
        return ""

    print(f"Rows to process: {len(df)}\n")

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base      = os.path.splitext(os.path.basename(csv_path))[0]
    log_stem  = f"cohort_{base}_{timestamp}"

    _start_log(log_stem)

    all_results = []

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir = profile_dir,
            headless      = True,
            slow_mo       = 200,
            viewport      = {"width": 1920, "height": 1080},
            args          = [
                "--window-size=1920,1080",
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
        page = context.pages[0] if context.pages else context.new_page()

        print("Starting cohort updates...\n")

        for i, row in df.iterrows():
            cohort_id = str(row.get("cohort_id", "")).strip()
            print(f"{'─'*60}")
            print(f"[{i+1}/{len(df)}] Cohort ID: {cohort_id}")
            try:
                result = process_cohort(page, row, base_url=base_url)
            except Exception as e:
                print(f"  [ERROR] {e}")
                result = {
                    "cohort_id":                   cohort_id,
                    "batch_id":                    ERROR,
                    "hall_ticket_prefix":          ERROR,
                    "student_prefix":              ERROR,
                    "foundation_starts":           ERROR,
                    "batch_start_date":            ERROR,
                    "lms_batch_id":                ERROR,
                    "lms_section_ids":             ERROR,
                    "manager_id":                  ERROR,
                    "enable_kit":                  ERROR,
                    "disable_welcome_kit_tshirt":  ERROR,
                    "notes":                       str(e),
                }
            all_results.append(result)
            print()

        context.close()

    csv_out = os.path.join(RUNS_DIR, f"{log_stem}.csv")
    pd.DataFrame(all_results).to_csv(csv_out, index=False)
    print(f"\nResults CSV → {csv_out}")

    dest = os.path.join(ARCHIVE_DIR, f"{base}_{timestamp}.csv")
    shutil.copy2(csv_path, dest)
    print(f"Input archived → {dest}")

    df_log = pd.DataFrame(all_results)
    print("\n══ Summary ══════════════════════════════════════════════")
    for col in ["batch_id", "hall_ticket_prefix", "student_prefix",
                "foundation_starts", "batch_start_date",
                "lms_batch_id", "lms_section_ids", "manager_id",
                "enable_kit", "disable_welcome_kit_tshirt"]:
        if col in df_log.columns:
            print(f"  {col:35s}: {df_log[col].value_counts().to_dict()}")
    print("═════════════════════════════════════════════════════════")
    print(f"Done. RESULT_CSV:{csv_out}")

    _stop_log()
    return csv_out


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--headless",    metavar="CSV",  help="CSV path — run headless (no prompts)")
    parser.add_argument("--base-url",    default=BASE_URL,    help="Cohort management base URL")
    parser.add_argument("--profile-dir", default=PROFILE_DIR, help="Playwright browser profile dir")
    args = parser.parse_args()

    if args.headless:
        run_headless(
            csv_path    = args.headless,
            base_url    = args.base_url,
            profile_dir = args.profile_dir,
        )
    else:
        run()
