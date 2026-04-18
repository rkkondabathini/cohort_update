"""
streamlit_app.py — Cohort Management Updater UI

Modes (sidebar)
  • Prepleaf (iHub) — dashboard-admin.prepleaf.com/iit/cohort-management/70
  • Masai           — admissions-admin.masaischool.com/user-management

Run locally: streamlit run streamlit_app.py
"""
import os
import sys
import queue
import subprocess
import threading
import time
from datetime import datetime

import pandas as pd
import streamlit as st

# ── Page config ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Cohort Updater",
    page_icon="🎓",
    layout="wide",
)

# ── Auto-install Playwright browser on cold start (Streamlit Cloud) ───────────
@st.cache_resource(show_spinner="Installing browser (first run only)…")
def _install_playwright_browser():
    try:
        result = subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            capture_output=True, text=True, timeout=180,
        )
        if result.returncode != 0:
            st.warning(f"Browser install warning: {result.stderr[:400]}")
    except Exception as exc:
        st.warning(f"Could not auto-install browser: {exc}")

_install_playwright_browser()

# ── Paths ─────────────────────────────────────────────────────────────────────
BASE_DIR      = os.path.dirname(os.path.abspath(__file__))
INPUT_DIR     = os.path.join(BASE_DIR, "input")
RUNS_DIR      = os.path.join(BASE_DIR, "runs")
ARCHIVE_DIR   = os.path.join(BASE_DIR, "archive")
PROFILE_DIR   = os.path.join(BASE_DIR, "browser_profile")
UPDATE_SCRIPT = os.path.join(BASE_DIR, "update_cohort.py")

for _d in (INPUT_DIR, RUNS_DIR, ARCHIVE_DIR):
    os.makedirs(_d, exist_ok=True)

# ── Mode config ───────────────────────────────────────────────────────────────
MODES = {
    "prepleaf": {
        "label":    "Prepleaf (iHub)",
        "url":      "https://dashboard-admin.prepleaf.com/iit/cohort-management/70",
        "login_url":"https://dashboard-admin.prepleaf.com/",
    },
    "masai": {
        "label":    "Masai",
        "url":      "https://admissions-admin.masaischool.com/user-management",
        "login_url":"https://admissions-admin.masaischool.com/",
    },
}

# ── Sidebar — mode selector ───────────────────────────────────────────────────
with st.sidebar:
    st.title("Cohort Updater")
    mode_key = st.radio(
        "Select platform",
        options=list(MODES.keys()),
        format_func=lambda k: MODES[k]["label"],
    )
    st.divider()
    st.caption(f"Target: {MODES[mode_key]['url']}")

mode      = MODES[mode_key]
LOGIN_URL = mode["login_url"]

# ── Session state — keyed per mode so switching doesn't bleed state ───────────
_DEFAULTS = {
    "step":           "upload",
    "df":             None,
    "csv_path":       None,
    "session_status": None,
    "login_thread":   None,
    "login_event":    None,
    "run_thread":     None,
    "run_queue":      None,
    "output_lines":   [],
    "result_csv":     None,
}

def _key(k):
    return f"{mode_key}__{k}"

for _k, _v in _DEFAULTS.items():
    if _key(_k) not in st.session_state:
        st.session_state[_key(_k)] = _v

def _get(k):
    return st.session_state[_key(k)]

def _set(k, v):
    st.session_state[_key(k)] = v


# ── Helpers ───────────────────────────────────────────────────────────────────
def check_session(login_url: str) -> str:
    """
    Launch a headless browser with the saved profile and navigate to
    login_url.  Returns 'active', 'expired', or 'error:<msg>'.
    """
    if not os.path.isdir(PROFILE_DIR):
        return "expired"
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            ctx = p.chromium.launch_persistent_context(
                user_data_dir=PROFILE_DIR, headless=True,
            )
            pg = ctx.pages[0] if ctx.pages else ctx.new_page()
            pg.goto(login_url, timeout=20_000)
            pg.wait_for_load_state("networkidle", timeout=20_000)
            url = pg.url
            ctx.close()
        if "login" in url.lower() or url.rstrip("/") == login_url.rstrip("/"):
            return "expired"
        return "active"
    except Exception as exc:
        return f"error:{exc}"


def _login_browser_fn(login_url: str, close_event: threading.Event):
    """Open a headed browser for OTP login; stays open until close_event set."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=PROFILE_DIR,
            headless=False,
            args=["--start-maximized"],
            no_viewport=True,
        )
        pg = ctx.pages[0] if ctx.pages else ctx.new_page()
        pg.goto(login_url)
        pg.wait_for_load_state("networkidle")
        close_event.wait()
        ctx.close()


def _run_updates_fn(csv_path: str, q: queue.Queue):
    """Run update_cohort.py --headless, stream output into queue."""
    try:
        proc = subprocess.Popen(
            [sys.executable, UPDATE_SCRIPT, "--headless", csv_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True, bufsize=1, cwd=BASE_DIR,
        )
        for line in proc.stdout:
            stripped = line.rstrip("\n")
            if stripped.startswith("Done. RESULT_CSV:"):
                q.put(f"__RESULT__:{stripped.split(':', 1)[1]}")
            else:
                q.put(stripped)
        proc.wait()
    except Exception as exc:
        q.put(f"[FATAL] {exc}")
    finally:
        # Fallback: find the newest CSV in runs/
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


def _drain_queue() -> bool:
    q = _get("run_queue")
    if not q:
        return False
    done = False
    lines = list(_get("output_lines"))
    while True:
        try:
            line = q.get_nowait()
        except queue.Empty:
            break
        if line.startswith("__RESULT__:"):
            _set("result_csv", line[len("__RESULT__:"):])
        elif line == "__DONE__":
            done = True
        else:
            lines.append(line)
    _set("output_lines", lines)
    return done


# ═════════════════════════════════════════════════════════════════════════════
# Title
# ═════════════════════════════════════════════════════════════════════════════
st.title(mode["label"])
st.caption(f"Target: `{mode['url']}`")

# ── Prepleaf placeholder ──────────────────────────────────────────────────────
if mode_key == "prepleaf":
    st.info(
        "Prepleaf (iHub) automation is coming soon.  \n"
        "Share the list of fields you want to update on "
        f"`{mode['url']}` and we'll build it next."
    )
    st.stop()

# ═════════════════════════════════════════════════════════════════════════════
# STEP 1 — Upload CSV  (Masai only for now)
# ═════════════════════════════════════════════════════════════════════════════
if _get("step") == "upload":
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

        csv_path = os.path.join(INPUT_DIR, uploaded.name)
        uploaded.seek(0)
        with open(csv_path, "wb") as fh:
            fh.write(uploaded.read())

        _set("df", df)
        _set("csv_path", csv_path)

        st.success(f"Loaded **{len(df)} rows** · {len(df.columns)} columns")
        st.dataframe(df.head(10), use_container_width=True)

        optional_missing = [c for c in [
            "batch_id", "hall_ticket_prefix", "student_prefix",
            "foundation_starts", "batch_start_date", "lms_batch_id",
            "lms_section_ids", "manager_id", "enable_kit",
            "disable_welcome_kit_tshirt",
        ] if c not in df.columns]
        if optional_missing:
            st.caption(f"Optional columns not in CSV (will be skipped): {', '.join(optional_missing)}")

        if st.button("Next — Check Login", type="primary"):
            _set("step", "session")
            st.rerun()


# ═════════════════════════════════════════════════════════════════════════════
# STEP 2 — Session / Login
# ═════════════════════════════════════════════════════════════════════════════
elif _get("step") == "session":
    st.header("Step 2 — Login Session")

    status = _get("session_status")
    if status is None:
        st.info("Click **Check Session** to verify your login status.")
    elif status == "active":
        st.success("Session is **active**. You are logged in.")
    elif status == "expired":
        st.warning("Session **expired** — please log in again.")
    elif str(status).startswith("error:"):
        st.error(f"Session check failed: {status[6:]}")

    col_check, col_back = st.columns([1, 6])
    with col_check:
        if st.button("Check Session"):
            with st.spinner("Checking…"):
                _set("session_status", check_session(LOGIN_URL))
            st.rerun()
    with col_back:
        if st.button("Back"):
            _set("step", "upload")
            _set("session_status", None)
            st.rerun()

    # Login flow
    if _get("session_status") == "expired":
        st.divider()
        st.subheader("Log in with OTP")
        browser_running = (
            _get("login_thread") is not None and _get("login_thread").is_alive()
        )
        if not browser_running:
            if st.button("Open Login Browser", type="primary"):
                ev = threading.Event()
                t  = threading.Thread(
                    target=_login_browser_fn,
                    args=(LOGIN_URL, ev),
                    daemon=True,
                )
                t.start()
                _set("login_thread", t)
                _set("login_event", ev)
                st.rerun()
        else:
            st.info(
                "A browser window has opened.  \n"
                "Complete the OTP login there, then click the button below."
            )
            if st.button("Done — I'm logged in", type="primary"):
                _get("login_event").set()
                time.sleep(1.2)
                with st.spinner("Verifying…"):
                    _set("session_status", check_session(LOGIN_URL))
                _set("login_thread", None)
                _set("login_event", None)
                st.rerun()

    # Run button
    if _get("session_status") == "active":
        st.divider()
        df      = _get("df")
        csvname = os.path.basename(_get("csv_path") or "?")
        st.write(f"Ready to update **{len(df)} cohort(s)** from `{csvname}`.")

        if st.button("Start Cohort Updates", type="primary"):
            _set("step", "running")
            _set("output_lines", [])
            _set("result_csv", None)
            q = queue.Queue()
            t = threading.Thread(
                target=_run_updates_fn,
                args=(_get("csv_path"), q),
                daemon=True,
            )
            t.start()
            _set("run_thread", t)
            _set("run_queue", q)
            st.rerun()


# ═════════════════════════════════════════════════════════════════════════════
# STEP 3 — Running
# ═════════════════════════════════════════════════════════════════════════════
elif _get("step") == "running":
    st.header("Step 3 — Running Updates")

    done = _drain_queue()
    still_running = _get("run_thread") and _get("run_thread").is_alive()

    if still_running:
        st.spinner("Updates in progress…")

    lines = _get("output_lines")
    st.code(
        "\n".join(lines) if lines else "(waiting for first output…)",
        language="text",
    )

    if done or not still_running:
        _drain_queue()
        _set("step", "done")
        st.rerun()
    else:
        time.sleep(0.8)
        st.rerun()


# ═════════════════════════════════════════════════════════════════════════════
# STEP 4 — Done
# ═════════════════════════════════════════════════════════════════════════════
elif _get("step") == "done":
    st.header("Updates Complete")
    st.success("All cohort updates have finished.")

    with st.expander("Full output log", expanded=False):
        st.code("\n".join(_get("output_lines")), language="text")

    result_csv = _get("result_csv")
    if result_csv and os.path.exists(result_csv):
        df_results = pd.read_csv(result_csv, dtype=str)
        st.subheader("Results")

        STATUS_COLS = [c for c in df_results.columns if c not in ("cohort_id", "notes")]

        def _cell_color(val):
            colors = {
                "CHANGED": "#d4edda",
                "FAILED":  "#f8d7da",
                "ERROR":   "#f8d7da",
                "SKIPPED": "#e2e3e5",
            }
            bg = colors.get(str(val).upper(), "")
            return f"background-color: {bg}" if bg else ""

        st.dataframe(
            df_results.style.applymap(_cell_color, subset=STATUS_COLS),
            use_container_width=True,
        )
        with open(result_csv, "rb") as fh:
            st.download_button(
                "Download Results CSV",
                data=fh,
                file_name=os.path.basename(result_csv),
                mime="text/csv",
            )
    else:
        st.warning("Result CSV not found — check the `runs/` folder manually.")

    if st.button("Run Another Update"):
        for k, v in _DEFAULTS.items():
            _set(k, v)
        st.rerun()
