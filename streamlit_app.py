"""
streamlit_app.py — Cohort Management Updater UI
Run locally: streamlit run streamlit_app.py
"""
import os
import sys
import queue
import shutil
import subprocess
import threading
import time
from datetime import datetime

import pandas as pd
import streamlit as st

# ── Paths ────────────────────────────────────────────────────────────────────
BASE_DIR      = os.path.dirname(os.path.abspath(__file__))
INPUT_DIR     = os.path.join(BASE_DIR, "input")
RUNS_DIR      = os.path.join(BASE_DIR, "runs")
ARCHIVE_DIR   = os.path.join(BASE_DIR, "archive")
PROFILE_DIR   = os.path.join(BASE_DIR, "browser_profile")
UPDATE_SCRIPT = os.path.join(BASE_DIR, "update_cohort.py")
LOGIN_URL     = "https://admissions-admin.masaischool.com/"

for _d in (INPUT_DIR, RUNS_DIR, ARCHIVE_DIR):
    os.makedirs(_d, exist_ok=True)

# ── Page config ───────────────────────────────────────────────────────────────
st.set_page_config(page_title="Cohort Updater", layout="wide")
st.title("Cohort Management Updater")
st.caption("Bulk updates for admissions-admin.masaischool.com")

# ── Session state defaults ────────────────────────────────────────────────────
_DEFAULTS = {
    "step":           "upload",   # upload | session | running | done
    "df":             None,
    "csv_path":       None,
    "session_status": None,       # None | "active" | "expired" | "error"
    "login_thread":   None,
    "login_event":    None,       # threading.Event — set to close headed browser
    "run_thread":     None,
    "run_queue":      None,
    "output_lines":   [],
    "result_csv":     None,
}
for _k, _v in _DEFAULTS.items():
    if _k not in st.session_state:
        st.session_state[_k] = _v

ss = st.session_state  # shorthand


# ── Helpers ───────────────────────────────────────────────────────────────────
def check_session() -> str:
    """
    Launch a headless browser with the saved profile and check if we're
    logged in.  Returns 'active', 'expired', or 'error:<msg>'.
    """
    if not os.path.isdir(PROFILE_DIR):
        return "expired"
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            ctx = p.chromium.launch_persistent_context(
                user_data_dir=PROFILE_DIR,
                headless=True,
            )
            pg = ctx.pages[0] if ctx.pages else ctx.new_page()
            pg.goto(LOGIN_URL, timeout=20_000)
            pg.wait_for_load_state("networkidle", timeout=20_000)
            url = pg.url
            ctx.close()
        if "login" in url.lower() or url.rstrip("/") == LOGIN_URL.rstrip("/"):
            return "expired"
        return "active"
    except Exception as exc:
        return f"error:{exc}"


def _login_browser_fn(close_event: threading.Event):
    """
    Open a headed browser for OTP login.
    Stays open until close_event is set by the UI.
    """
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=PROFILE_DIR,
            headless=False,
            args=["--start-maximized"],
            no_viewport=True,
        )
        pg = ctx.pages[0] if ctx.pages else ctx.new_page()
        pg.goto(LOGIN_URL)
        pg.wait_for_load_state("networkidle")
        close_event.wait()   # block until user clicks "Done"
        ctx.close()


def _run_updates_fn(csv_path: str, q: queue.Queue):
    """
    Invoke update_cohort.py --headless <csv_path> as a subprocess and
    pipe every output line into the queue.
    Puts '__DONE__' when finished, '__RESULT__:<path>' with the CSV path.
    """
    try:
        proc = subprocess.Popen(
            [sys.executable, UPDATE_SCRIPT, "--headless", csv_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            cwd=BASE_DIR,
        )
        for line in proc.stdout:
            stripped = line.rstrip("\n")
            # The script prints "Done. RESULT_CSV:<path>" — capture it
            if stripped.startswith("Done. RESULT_CSV:"):
                q.put(f"__RESULT__:{stripped.split(':', 1)[1]}")
            else:
                q.put(stripped)
        proc.wait()
    except Exception as exc:
        q.put(f"[FATAL] {exc}")
    finally:
        # Fallback: find the newest CSV in runs/
        if not any(
            line.startswith("__RESULT__:")
            for line in list(q.queue)          # peek without consuming
        ):
            try:
                csvs = sorted(
                    [f for f in os.listdir(RUNS_DIR) if f.endswith(".csv")],
                    key=lambda f: os.path.getmtime(os.path.join(RUNS_DIR, f)),
                    reverse=True,
                )
                if csvs:
                    q.put(f"__RESULT__:{os.path.join(RUNS_DIR, csvs[0])}")
            except Exception:
                pass
        q.put("__DONE__")


def _drain_queue():
    """Pull all available lines from run_queue into session state."""
    if not ss.run_queue:
        return False
    done = False
    while True:
        try:
            line = ss.run_queue.get_nowait()
        except queue.Empty:
            break
        if line.startswith("__RESULT__:"):
            ss.result_csv = line[len("__RESULT__:"):]
        elif line == "__DONE__":
            done = True
        else:
            ss.output_lines.append(line)
    return done


# ═════════════════════════════════════════════════════════════════════════════
# STEP 1 — Upload CSV
# ═════════════════════════════════════════════════════════════════════════════
if ss.step == "upload":
    st.header("Step 1 — Upload CSV")

    uploaded = st.file_uploader("Choose your cohort CSV file", type="csv")

    if uploaded:
        try:
            df = pd.read_csv(uploaded, dtype=str)
        except Exception as exc:
            st.error(f"Could not read CSV: {exc}")
            st.stop()

        if "cohort_id" not in df.columns:
            st.error("CSV must contain a `cohort_id` column.")
            st.stop()

        # Save to input/ so the script can access it
        csv_path = os.path.join(INPUT_DIR, uploaded.name)
        uploaded.seek(0)
        with open(csv_path, "wb") as fh:
            fh.write(uploaded.read())

        ss.df       = df
        ss.csv_path = csv_path

        st.success(f"Loaded **{len(df)} rows** · {len(df.columns)} columns")
        st.dataframe(df.head(10), use_container_width=True)

        col_hint = [c for c in [
            "cohort_id", "batch_id", "hall_ticket_prefix", "student_prefix",
            "foundation_starts", "batch_start_date", "lms_batch_id",
            "lms_section_ids", "manager_id", "enable_kit",
            "disable_welcome_kit_tshirt",
        ] if c not in df.columns]
        if col_hint:
            st.caption(f"Optional columns not in CSV (will be skipped): {', '.join(col_hint)}")

        if st.button("Next — Check Login", type="primary"):
            ss.step = "session"
            st.rerun()


# ═════════════════════════════════════════════════════════════════════════════
# STEP 2 — Session / Login
# ═════════════════════════════════════════════════════════════════════════════
elif ss.step == "session":
    st.header("Step 2 — Login Session")

    # ── Session status banner ─────────────────────────────────────────────────
    if ss.session_status is None:
        st.info("Click **Check Session** to verify your login status.")
    elif ss.session_status == "active":
        st.success("Session is **active**. You are logged in.")
    elif ss.session_status == "expired":
        st.warning("Session **expired** — please log in again.")
    elif str(ss.session_status).startswith("error:"):
        st.error(f"Session check failed: {ss.session_status[6:]}")

    col_check, col_back = st.columns([1, 5])
    with col_check:
        if st.button("Check Session"):
            with st.spinner("Checking…"):
                ss.session_status = check_session()
            st.rerun()
    with col_back:
        if st.button("Back"):
            ss.step           = "upload"
            ss.session_status = None
            st.rerun()

    # ── Login flow (only shown when expired) ──────────────────────────────────
    if ss.session_status == "expired":
        st.divider()
        st.subheader("Log in with OTP")

        browser_running = (
            ss.login_thread is not None and ss.login_thread.is_alive()
        )

        if not browser_running:
            if st.button("Open Login Browser", type="primary"):
                ev = threading.Event()
                t  = threading.Thread(
                    target=_login_browser_fn, args=(ev,), daemon=True
                )
                t.start()
                ss.login_thread = t
                ss.login_event  = ev
                st.rerun()
        else:
            st.info(
                "A browser window has opened.  \n"
                "Complete the OTP login there, then click the button below."
            )
            if st.button("Done — I'm logged in", type="primary"):
                ss.login_event.set()          # signal browser to close
                time.sleep(1.2)               # let it close cleanly
                with st.spinner("Verifying…"):
                    ss.session_status = check_session()
                ss.login_thread = None
                ss.login_event  = None
                st.rerun()

    # ── Run button (only when active) ────────────────────────────────────────
    if ss.session_status == "active":
        st.divider()
        csv_name = os.path.basename(ss.csv_path) if ss.csv_path else "?"
        st.write(
            f"Ready to update **{len(ss.df)} cohort(s)** from `{csv_name}`."
        )

        if st.button("Start Cohort Updates", type="primary"):
            ss.step         = "running"
            ss.output_lines = []
            ss.result_csv   = None
            q = queue.Queue()
            t = threading.Thread(
                target=_run_updates_fn,
                args=(ss.csv_path, q),
                daemon=True,
            )
            t.start()
            ss.run_thread = t
            ss.run_queue  = q
            st.rerun()


# ═════════════════════════════════════════════════════════════════════════════
# STEP 3 — Running
# ═════════════════════════════════════════════════════════════════════════════
elif ss.step == "running":
    st.header("Step 3 — Running Updates")

    done = _drain_queue()

    still_running = ss.run_thread and ss.run_thread.is_alive()

    if still_running:
        st.spinner("Updates in progress…")

    output_text = "\n".join(ss.output_lines) if ss.output_lines else "(waiting for first output…)"
    st.code(output_text, language="text")

    if done or (not still_running and not done):
        # Make sure we drained everything before moving on
        _drain_queue()
        ss.step = "done"
        st.rerun()
    else:
        time.sleep(0.8)
        st.rerun()


# ═════════════════════════════════════════════════════════════════════════════
# STEP 4 — Done
# ═════════════════════════════════════════════════════════════════════════════
elif ss.step == "done":
    st.header("Updates Complete")
    st.success("All cohort updates have finished.")

    with st.expander("Full output log", expanded=False):
        st.code("\n".join(ss.output_lines), language="text")

    if ss.result_csv and os.path.exists(ss.result_csv):
        df_results = pd.read_csv(ss.result_csv, dtype=str)

        st.subheader("Results")

        STATUS_COLS = [
            c for c in df_results.columns
            if c not in ("cohort_id", "notes")
        ]

        def _cell_color(val):
            colors = {
                "CHANGED": "#d4edda",
                "FAILED":  "#f8d7da",
                "ERROR":   "#f8d7da",
                "SKIPPED": "#e2e3e5",
            }
            bg = colors.get(str(val).upper(), "")
            return f"background-color: {bg}" if bg else ""

        styled = df_results.style.applymap(_cell_color, subset=STATUS_COLS)
        st.dataframe(styled, use_container_width=True)

        with open(ss.result_csv, "rb") as fh:
            st.download_button(
                label="Download Results CSV",
                data=fh,
                file_name=os.path.basename(ss.result_csv),
                mime="text/csv",
            )
    else:
        st.warning("Result CSV not found — check the `runs/` folder manually.")

    if st.button("Run Another Update"):
        for k, v in _DEFAULTS.items():
            st.session_state[k] = v
        st.rerun()
