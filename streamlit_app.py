"""
streamlit_app.py — Cohort Management Updater UI

Modes (sidebar)
  • Prepleaf (iHub) — dashboard-admin.prepleaf.com  /  login: ihubiitrcourses.org
  • Masai           — admissions-admin.masaischool.com

Run locally: streamlit run streamlit_app.py
"""
import os
import sys
import queue
import subprocess
import threading   # still used for run_updates_fn thread
import time

import pandas as pd
import streamlit as st

# ── Page config ───────────────────────────────────────────────────────────────
st.set_page_config(page_title="Cohort Updater", page_icon="🎓", layout="wide")

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
UPDATE_SCRIPT = os.path.join(BASE_DIR, "update_cohort.py")

for _d in (INPUT_DIR, RUNS_DIR, ARCHIVE_DIR):
    os.makedirs(_d, exist_ok=True)

# ── Platform config ───────────────────────────────────────────────────────────
PLATFORMS = {
    "prepleaf": {
        "label":       "Prepleaf (iHub)",
        "base_url":    "https://dashboard-admin.prepleaf.com/iit/cohort-management",
        "login_url":   "https://www.ihubiitrcourses.org/signup",
        "profile_dir": os.path.join(BASE_DIR, "browser_profile_prepleaf"),
        "display_url": "https://dashboard-admin.prepleaf.com/iit/cohort-management/70",
    },
    "masai": {
        "label":       "masaischool.com",
        "base_url":    "https://admissions-admin.masaischool.com/iit/cohort-management",
        "login_url":   "https://admissions-admin.masaischool.com/",
        "profile_dir": os.path.join(BASE_DIR, "browser_profile"),
        "display_url": "https://admissions-admin.masaischool.com/user-management",
    },
}

# ── Sidebar ───────────────────────────────────────────────────────────────────
with st.sidebar:
    st.title("Cohort Updater")
    platform_key = st.radio(
        "Select platform",
        options=list(PLATFORMS.keys()),
        format_func=lambda k: PLATFORMS[k]["label"],
    )
    st.divider()
    p = PLATFORMS[platform_key]
    st.caption(f"Target: {p['display_url']}")

# ── Session state — keyed per platform so switching doesn't bleed ─────────────
_DEFAULTS = {
    "step":           "upload",
    "df":             None,
    "csv_path":       None,
    "session_status": None,
    "login_proc":     None,   # subprocess.Popen for headed login browser
    "run_thread":     None,
    "run_queue":      None,
    "output_lines":   [],
    "result_csv":     None,
}

def _k(key):        return f"{platform_key}__{key}"
def _get(key):      return st.session_state[_k(key)]
def _set(key, val): st.session_state[_k(key)] = val

for _key, _val in _DEFAULTS.items():
    if _k(_key) not in st.session_state:
        st.session_state[_k(_key)] = _val

# ── Helpers ───────────────────────────────────────────────────────────────────
def check_session(base_url: str, login_url: str, profile_dir: str) -> str:
    """Navigate to base_url with saved profile; return 'active' or 'expired'."""
    if not os.path.isdir(profile_dir):
        return "expired"
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            ctx = pw.chromium.launch_persistent_context(
                user_data_dir=profile_dir, headless=True,
            )
            pg = ctx.pages[0] if ctx.pages else ctx.new_page()
            pg.goto(base_url, timeout=20_000)
            pg.wait_for_load_state("networkidle", timeout=20_000)
            url = pg.url
            ctx.close()
        # Expired if redirected to a login/signup page (keyword check only —
        # do NOT check login_host because it matches valid pages on the same domain)
        base_domain = base_url.split("/")[2]   # e.g. "admissions-admin.masaischool.com"
        if "login" in url.lower() or "signup" in url.lower() or base_domain not in url:
            return "expired"
        return "active"
    except Exception as exc:
        return f"error:{exc}"


def _open_login_browser(login_url: str, profile_dir: str) -> subprocess.Popen:
    """
    Launch a headed Playwright browser as a subprocess (avoids macOS
    main-thread restriction that breaks threading approach).
    The subprocess blocks on stdin.readline() — sending a newline closes it.
    """
    os.makedirs(profile_dir, exist_ok=True)
    script = (
        "from playwright.sync_api import sync_playwright; import sys\n"
        f"with sync_playwright() as pw:\n"
        f"    ctx = pw.chromium.launch_persistent_context(\n"
        f"        user_data_dir={repr(profile_dir)},\n"
        f"        headless=False,\n"
        f"        args=['--start-maximized'],\n"
        f"        no_viewport=True,\n"
        f"    )\n"
        f"    pg = ctx.pages[0] if ctx.pages else ctx.new_page()\n"
        f"    pg.goto({repr(login_url)})\n"
        f"    pg.wait_for_load_state('networkidle')\n"
        f"    sys.stdin.readline()  # wait for close signal\n"
        f"    ctx.close()\n"
    )
    return subprocess.Popen(
        [sys.executable, "-c", script],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _run_updates_fn(csv_path: str, base_url: str, profile_dir: str, q: queue.Queue):
    """Run update_cohort.py --headless and stream output into queue."""
    try:
        proc = subprocess.Popen(
            [
                sys.executable, UPDATE_SCRIPT,
                "--headless",    csv_path,
                "--base-url",    base_url,
                "--profile-dir", profile_dir,
            ],
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
        # Fallback: find newest CSV in runs/
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
    done  = False
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
# Page title (updates when sidebar selection changes)
# ═════════════════════════════════════════════════════════════════════════════
st.title(p["label"])
st.caption(f"Target: `{p['display_url']}`")

# ═════════════════════════════════════════════════════════════════════════════
# STEP 1 — Upload CSV
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

        csv_path = os.path.join(INPUT_DIR, f"{platform_key}_{uploaded.name}")
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
                _set("session_status", check_session(
                    p["base_url"], p["login_url"], p["profile_dir"]
                ))
            st.rerun()
    with col_back:
        if st.button("Back"):
            _set("step", "upload")
            _set("session_status", None)
            st.rerun()

    # ── Login (when expired) ──────────────────────────────────────────────────
    if _get("session_status") == "expired":
        st.divider()
        st.subheader("Log in with OTP")

        proc = _get("login_proc")
        browser_running = proc is not None and proc.poll() is None

        if not browser_running:
            if st.button("Open Login Browser", type="primary"):
                _set("login_proc", _open_login_browser(p["login_url"], p["profile_dir"]))
                st.rerun()
        else:
            st.info(
                "A browser window has opened.  \n"
                "Complete the OTP login there, then click the button below."
            )
            if st.button("Done — I'm logged in", type="primary"):
                proc = _get("login_proc")
                try:
                    proc.stdin.write(b"\n")
                    proc.stdin.flush()
                    proc.wait(timeout=3)
                except Exception:
                    proc.terminate()
                _set("login_proc", None)
                time.sleep(0.8)
                with st.spinner("Verifying…"):
                    _set("session_status", check_session(
                        p["base_url"], p["login_url"], p["profile_dir"]
                    ))
                st.rerun()

    # ── Run (when active) ─────────────────────────────────────────────────────
    if _get("session_status") == "active":
        st.divider()
        df      = _get("df")
        csvname = os.path.basename(_get("csv_path") or "?")
        st.write(f"Ready to update **{len(df)} cohort(s)** from `{csvname}`.")

        if st.button("Start Cohort Updates", type="primary"):
            _set("step",         "running")
            _set("output_lines", [])
            _set("result_csv",   None)
            q = queue.Queue()
            t = threading.Thread(
                target=_run_updates_fn,
                args=(_get("csv_path"), p["base_url"], p["profile_dir"], q),
                daemon=True,
            )
            t.start()
            _set("run_thread", t)
            _set("run_queue",  q)
            st.rerun()


# ═════════════════════════════════════════════════════════════════════════════
# STEP 3 — Running
# ═════════════════════════════════════════════════════════════════════════════
elif _get("step") == "running":
    st.header("Step 3 — Running Updates")

    done          = _drain_queue()
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
            return {
                "CHANGED": "background-color: #d4edda",
                "FAILED":  "background-color: #f8d7da",
                "ERROR":   "background-color: #f8d7da",
                "SKIPPED": "background-color: #e2e3e5",
            }.get(str(val).upper(), "")

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
