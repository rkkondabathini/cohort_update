"""
login.py — One-time OTP login to warm the browser session.

Run this once (or whenever the session expires):
    python onwards-masai/login.py

After logging in, the session is saved to browser_profile/.
The main update_cohort.py will reuse it automatically (no OTP needed).
"""

import os
from playwright.sync_api import sync_playwright

LOGIN_URL   = "https://admissions-admin.masaischool.com/"
PROFILE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "browser_profile")

def run():
    print(f"Profile directory: {PROFILE_DIR}")
    print("Opening browser for OTP login...\n")

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

        # Already logged in?
        if LOGIN_URL.rstrip("/") not in page.url.rstrip("/") and "login" not in page.url.lower():
            print(f"Already logged in. Session is active. URL: {page.url}")
            print("No action needed — session is still valid.")
        else:
            print("Please log in with OTP in the browser window.")
            print("Complete the OTP flow, then come back here and press ENTER.")
            input("\nPress ENTER once you are on the dashboard... ")
            page.wait_for_load_state("networkidle", timeout=60_000)
            print(f"\nLogged in successfully. URL: {page.url}")
            print("Session saved to browser_profile/.")

        input("\nPress ENTER to close the browser...")
        context.close()

    print("\nDone. You can now run update_cohort.py headlessly.")

if __name__ == "__main__":
    run()
