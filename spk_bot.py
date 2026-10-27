"""
SPK Telegram bot

Usage:
    python spk_bot.py alert    -> sends an alert for NEW SPK whose tglspk and tglinputspk dates differ
    python spk_bot.py report   -> sends the daily report (message + Excel file)
    python spk_bot.py test     -> sends a test message to check that everything is connected

All settings come from environment variables (GitHub Secrets). Never write
passwords or tokens in this file.
"""
import io
import json
import os
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import requests
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL

TABLE = "view_penerimaannbebanLSN"
DATE_COL = "tglspk"
INPUT_COL = "tglinputspk"
TZ = ZoneInfo("Asia/Jakarta")   # all times are Jakarta time (WIB)
TELEGRAM_LIMIT = 3900           # Telegram allows max 4096 characters per message


# =====================================================================
# 1. Settings
# =====================================================================
# ---------------------------------------------------------------------
# FILL IN YOUR VALUES HERE. Keep the quotes, replace only the text inside.
# WARNING: anyone who can read this file can read these passwords.
# Keep your GitHub repository PRIVATE.
# ---------------------------------------------------------------------
CONFIG = {
    "MYSQL_HOST": "103.185.52.24",
    "MYSQL_PORT": "3306",
    "MYSQL_USER": "dashcris",
    "MYSQL_PASSWORD": "B3r80eBhzYgCFrD2",
    "MYSQL_DATABASE": "rpt_dashboard",
    "TELEGRAM_BOT_TOKEN": "8810102766:AAHzzJW6y7Y_6R5I9_3oKiI8IXTSdfFBf6U",
    "TELEGRAM_CHAT_ID": "1714904109",
}


def setting(name, default=None):
    # Order: GitHub secret (if it exists) -> CONFIG above -> default
    value = os.environ.get(name) or CONFIG.get(name) or default
    if not value or str(value).startswith("PASTE_"):
        sys.exit(f"Missing setting: {name} (fill it in the CONFIG block at the top of spk_bot.py)")
    return value


LOOKBACK_HOURS = int(setting("LOOKBACK_HOURS", "72"))   # how far back the alert looks
STATE_FILE = setting("STATE_FILE", "state/notified.json")

DB_HOST = setting("MYSQL_HOST")
DB_PORT = int(setting("MYSQL_PORT", "3306"))
DB_USER = setting("MYSQL_USER")
DB_PASSWORD = setting("MYSQL_PASSWORD")
DB_NAME = setting("MYSQL_DATABASE")

BOT_TOKEN = setting("TELEGRAM_BOT_TOKEN")
CHAT_IDS = [c.strip() for c in setting("TELEGRAM_CHAT_ID").split(",")]
API = f"https://api.telegram.org/bot{BOT_TOKEN}"


# =====================================================================
# 2. Small helpers
# =====================================================================
def now_local():
    """Current Jakarta time, without timezone info (to match the database)."""
    return datetime.now(TZ).replace(tzinfo=None)


def txt(v):
    return "-" if pd.isna(v) else str(v)


def d(v):
    return "-" if pd.isna(v) else str(v)[:10]


def dt(v):
    return "-" if pd.isna(v) else str(v)[:19]


def num(v):
    return "-" if pd.isna(v) else f"{float(v):g}"


def get_engine():
    url = URL.create(
        "mysql+pymysql",
        username=DB_USER,
        password=DB_PASSWORD,
        host=DB_HOST,
        port=DB_PORT,
        database=DB_NAME,
    )
    return create_engine(url, pool_pre_ping=True)


# =====================================================================
# 3. Telegram
# =====================================================================
def send_message(message):
    for chat_id in CHAT_IDS:
        r = requests.post(
            f"{API}/sendMessage",
            json={"chat_id": chat_id, "text": message},
            timeout=30,
        )
        if not r.ok:
            sys.exit(f"Telegram error (chat {chat_id}): {r.text}")


def send_document(filename, content, caption):
    for chat_id in CHAT_IDS:
        r = requests.post(
            f"{API}/sendDocument",
            data={"chat_id": chat_id, "caption": caption},
            files={"document": (filename, content)},
            timeout=60,
        )
        if not r.ok:
            sys.exit(f"Telegram error (chat {chat_id}): {r.text}")


def chunk(header, blocks):
    """Split a long message into several messages that fit Telegram's limit."""
    messages, current = [], header
    for block in blocks:
        if len(current) + len(block) + 2 > TELEGRAM_LIMIT:
            messages.append(current.strip())
            current = ""
        current += "\n\n" + block
    messages.append(current.strip())
    return messages


def fmt_block(i, r):
    """One SPK as a readable text block."""
    return (
        f"{i}) SPK: {r.no_spk}\n"
        f"   Cabang: {txt(r.cabang)}\n"
        f"   No Polisi: {txt(r.no_polisi)} ({txt(r.model)})\n"
        f"   Panel: {num(r.panel)}\n"
        f"   tglspk: {d(r.tglspk)}\n"
        f"   tglinputspk: {dt(r.tglinputspk)}\n"
        f"   Selisih: {int(r.selisih_hari):+d} hari"
    )


# =====================================================================
# 4. Find mismatched SPK (used by alert and test)
# =====================================================================
def find_mismatch(engine, since):
    query = text(
        "SELECT no_spk, cabang, "
        "MAX(no_polisi) AS no_polisi, MAX(model) AS model, "
        "SUM(panel) AS panel, "
        f"MIN({DATE_COL}) AS tglspk, MAX({INPUT_COL}) AS tglinputspk, "
        f"DATEDIFF(MAX({INPUT_COL}), MIN({DATE_COL})) AS selisih_hari "
        f"FROM {TABLE} "
        f"WHERE {INPUT_COL} >= :since "
        f"AND DATE({INPUT_COL}) <> DATE({DATE_COL}) "
        "GROUP BY no_spk, cabang "
        f"ORDER BY MAX({INPUT_COL}) DESC"
    )
    with engine.connect() as conn:
        return pd.read_sql(query, conn, params={"since": since})


# =====================================================================
# 5. State (remembers which SPK were already alerted, so no duplicates)
# =====================================================================
def load_state():
    if not os.path.exists(STATE_FILE):
        return None
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_state(state):
    folder = os.path.dirname(STATE_FILE)
    if folder:
        os.makedirs(folder, exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=1)


# =====================================================================
# 6. MODE: alert
# =====================================================================
def run_alert():
    engine = get_engine()
    now = now_local()
    df = find_mismatch(engine, now - timedelta(hours=LOOKBACK_HOURS))
    df["no_spk"] = df["no_spk"].astype(str)

    state = load_state()
    stamp = now.isoformat(timespec="seconds")

    # First ever run: remember what already exists, do not flood the chat
    if state is None:
        state = {"notified": {s: stamp for s in df["no_spk"]}}
        save_state(state)
        send_message(
            "✅ SPK monitoring started.\n"
            f"{len(df)} existing mismatched SPK (last {LOOKBACK_HOURS} hours) were recorded "
            "and will NOT be alerted.\n"
            "From now on you will get an alert only for NEW mismatches."
        )
        print("First run: baseline saved.")
        return

    known = state["notified"]
    new = df[~df["no_spk"].isin(known)]

    if new.empty:
        print("No new mismatch.")
        return

    header = (
        f"🚨 ALERT: {len(new)} new SPK with different tglspk and tglinputspk\n"
        f"Checked at {now:%Y-%m-%d %H:%M} WIB"
    )
    blocks = [fmt_block(i, r) for i, r in enumerate(new.itertuples(index=False), 1)]
    for message in chunk(header, blocks):
        send_message(message)

    # Only after sending successfully, remember them
    for s in new["no_spk"]:
        known[s] = stamp
    cutoff = (now - timedelta(days=60)).isoformat(timespec="seconds")
    state["notified"] = {s: t for s, t in known.items() if t >= cutoff}
    save_state(state)
    print(f"Alert sent for {len(new)} SPK.")


# =====================================================================
# 7. MODE: report (daily, with Excel file)
# =====================================================================
def run_report():
    engine = get_engine()
    now = now_local()
    today = now.date()
    start = datetime.combine(today, datetime.min.time())
    end = start + timedelta(days=1)

    # Scope of the report: every row whose tglspk is today OR whose tglinputspk is today
    query = text(
        f"SELECT * FROM {TABLE} "
        f"WHERE ({DATE_COL} >= :s AND {DATE_COL} < :e) "
        f"OR ({INPUT_COL} >= :s AND {INPUT_COL} < :e) "
        f"ORDER BY {INPUT_COL} DESC"
    )
    with engine.connect() as conn:
        df = pd.read_sql(query, conn, params={"s": start, "e": end})

    title = f"📊 DAILY REPORT - {now:%A, %Y-%m-%d} (sent {now:%H:%M} WIB)"

    if df.empty:
        send_message(f"{title}\n\nNo SPK data found for today.")
        print("Report sent (no data).")
        return

    df.columns = [c.lower() for c in df.columns]   # "Panel" -> "panel"
    df[DATE_COL] = pd.to_datetime(df[DATE_COL], errors="coerce")
    df[INPUT_COL] = pd.to_datetime(df[INPUT_COL], errors="coerce")
    df["panel"] = pd.to_numeric(df["panel"], errors="coerce")

    both = df[DATE_COL].notna() & df[INPUT_COL].notna()
    mask = both & (df[DATE_COL].dt.normalize() != df[INPUT_COL].dt.normalize())

    # ---- summary per cabang ----
    summary = (
        df.groupby("cabang")
        .agg(
            count_spk=("no_spk", "nunique"),
            sum_panel=("panel", "sum"),
            last_tglinputspk=(INPUT_COL, "max"),
        )
        .reset_index()
    )
    mm_counts = df[mask].groupby("cabang")["no_spk"].nunique()
    summary["count_spk_beda"] = summary["cabang"].map(mm_counts).fillna(0).astype(int)

    # ---- mismatched SPK ----
    if mask.any():
        mm = (
            df[mask]
            .groupby(["no_spk", "cabang"], as_index=False)
            .agg(
                no_polisi=("no_polisi", "max"),
                model=("model", "max"),
                panel=("panel", "sum"),
                tglspk=(DATE_COL, "min"),
                tglinputspk=(INPUT_COL, "max"),
            )
        )
        mm["selisih_hari"] = (
            mm["tglinputspk"].dt.normalize() - mm["tglspk"].dt.normalize()
        ).dt.days
    else:
        mm = pd.DataFrame(
            columns=["no_spk", "cabang", "no_polisi", "model", "panel",
                     "tglspk", "tglinputspk", "selisih_hari"]
        )

    # ---- last tglinputspk (overall) ----
    last_line = ""
    if df[INPUT_COL].notna().any():
        row = df.loc[df[INPUT_COL].idxmax()]
        last_line = f"🕓 Last tglinputspk: {dt(row[INPUT_COL])} ({txt(row['cabang'])})"

    # ---- build the Telegram message ----
    header = (
        f"{title}\n\n"
        f"{last_line}\n\n"
        f"Total SPK: {df['no_spk'].nunique()}  |  Panel: {num(df['panel'].sum())}  |  Rows: {len(df)}\n"
        f"SPK with different dates: {mm['no_spk'].nunique()}\n\n"
        "Per cabang:\n"
        + "\n".join(
            f"• {r.cabang}: {r.count_spk} SPK | {num(r.sum_panel)} panel | "
            f"last input {dt(r.last_tglinputspk)} | mismatch {r.count_spk_beda}"
            for r in summary.itertuples(index=False)
        )
    )
    if mm.empty:
        blocks = ["✅ No SPK with different tglspk and tglinputspk today."]
    else:
        blocks = ["⚠️ SPK with different dates:"] + [
            fmt_block(i, r) for i, r in enumerate(mm.itertuples(index=False), 1)
        ]
    for message in chunk(header, blocks):
        send_message(message)

    # ---- Excel file with all the details ----
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        summary.to_excel(writer, sheet_name="Summary", index=False)
        mm.to_excel(writer, sheet_name="Mismatch", index=False)
        df.to_excel(writer, sheet_name="Data", index=False)
        for ws in writer.book.worksheets:
            for col in ws.columns:
                width = max(len(str(c.value)) if c.value is not None else 0 for c in col)
                ws.column_dimensions[col[0].column_letter].width = min(width + 2, 40)

    send_document(
        f"spk_report_{today}.xlsx",
        buffer.getvalue(),
        f"Full details {today} (sheets: Summary, Mismatch, Data)",
    )
    print("Report sent.")


# =====================================================================
# 8. MODE: test
# =====================================================================
def run_test():
    engine = get_engine()
    df = find_mismatch(engine, now_local() - timedelta(hours=LOOKBACK_HOURS))
    header = (
        "✅ TEST OK: the bot can reach Telegram and the database.\n"
        f"Mismatched SPK in the last {LOOKBACK_HOURS} hours: {df['no_spk'].nunique()}\n"
        "(Up to 3 examples shown. This is only a test, nothing is recorded.)"
    )
    blocks = [fmt_block(i, r) for i, r in enumerate(df.head(3).itertuples(index=False), 1)]
    for message in chunk(header, blocks):
        send_message(message)
    print("Test message sent.")


# =====================================================================
# 9. Run
# =====================================================================
if __name__ == "__main__":
    modes = {"alert": run_alert, "report": run_report, "test": run_test}
    mode = sys.argv[1] if len(sys.argv) > 1 else "alert"
    if mode not in modes:
        sys.exit("Usage: python spk_bot.py [alert|report|test]")
    modes[mode]()
