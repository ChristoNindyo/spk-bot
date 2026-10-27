"""
SPK Telegram bot

Usage:
    python spk_bot.py alert    -> alert for NEW SPK whose tglspk and tglinputspk dates differ
    python spk_bot.py report   -> daily report (messages + Excel file)
    python spk_bot.py test     -> test message to check that everything is connected

Your private values (database + Telegram) live in config.py, not in this file.
"""
import html
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

try:
    from config import CONFIG   # your private values (see config.py)
except ImportError:
    CONFIG = {}

TABLE = "view_penerimaannbebanLSN"
DATE_COL = "tglspk"
INPUT_COL = "tglinputspk"
TZ = ZoneInfo("Asia/Jakarta")   # all times are Jakarta time (WIB)
TELEGRAM_LIMIT = 3900           # Telegram allows max 4096 characters per message
ALL_CABANG_DAYS = 365           # the report lists every cabang that had an input in the last N days


# =====================================================================
# 1. Settings
# =====================================================================
def setting(name, default=None):
    # Order: GitHub secret (if it exists) -> config.py -> default
    value = os.environ.get(name) or CONFIG.get(name) or default
    if not value or str(value).startswith("PASTE_"):
        sys.exit(f"Missing setting: {name} (fill it in config.py)")
    return value


LOOKBACK_HOURS = int(setting("LOOKBACK_HOURS", "72"))   # how far back the alert looks
STATE_FILE = setting("STATE_FILE", "state/notified.json")

DB_HOST = setting("MYSQL_HOST")
DB_PORT = int(setting("MYSQL_PORT", "3306"))
DB_USER = setting("MYSQL_USER")
DB_PASSWORD = setting("MYSQL_PASSWORD")
DB_NAME = setting("MYSQL_DATABASE")

BOT_TOKEN = setting("TELEGRAM_BOT_TOKEN")
CHAT_IDS = [c.strip() for c in str(setting("TELEGRAM_CHAT_ID")).split(",")]
API = f"https://api.telegram.org/bot{BOT_TOKEN}"


# =====================================================================
# 2. Small helpers
# =====================================================================
def now_local():
    """Current Jakarta time, without timezone info (to match the database)."""
    return datetime.now(TZ).replace(tzinfo=None)


def txt(v):
    return "-" if pd.isna(v) else str(v)


def esc(v):
    """Text that is safe to put inside a Telegram HTML message."""
    return html.escape(txt(v))


def num(v):
    return "-" if pd.isna(v) else f"{float(v):g}"


def plural(n, word):
    return f"{n} {word}" + ("" if n == 1 else "s")


def fmt_d(v):
    """29 Sep 2026"""
    if pd.isna(v):
        return "-"
    t = pd.Timestamp(v)
    return f"{t.day} {t:%b %Y}"


def fmt_dt(v, year=False):
    """27 Oct 14:13:46   (or 27 Oct 2026 14:13:46 with year=True)"""
    if pd.isna(v):
        return "-"
    t = pd.Timestamp(v)
    return f"{t.day} {t:%b}" + (f" {t:%Y}" if year else "") + f" {t:%H:%M:%S}"


def ago(v, today):
    if pd.isna(v):
        return "no data"
    n = (today - pd.Timestamp(v).date()).days
    if n <= 0:
        return "today"
    if n == 1:
        return "yesterday"
    return f"{n} days ago"


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
            json={"chat_id": chat_id, "text": message, "parse_mode": "HTML"},
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


def chunk(header, blocks, sep="\n\n"):
    """Split a long message into several messages that fit Telegram's limit."""
    messages, current = [], header
    for block in blocks:
        if len(current) + len(sep) + len(block) > TELEGRAM_LIMIT:
            messages.append(current.strip())
            current = ""
        current += sep + block
    messages.append(current.strip())
    return messages


def fmt_block(i, r):
    """One mismatched SPK as a tidy block."""
    diff = int(r.selisih_hari)
    when = "after" if diff > 0 else "before"
    return (
        f"🔴 {i}. <b>{esc(r.no_spk)}</b>\n"
        f"   🏢 {esc(r.cabang)}\n"
        f"   🚗 {esc(r.no_polisi)} · {esc(r.model)}\n"
        f"   📦 Panel: {num(r.panel)}\n"
        f"   📅 SPK date: {fmt_d(r.tglspk)}\n"
        f"   ⌨️ Input: {fmt_dt(r.tglinputspk, year=True)}\n"
        f"   ⏱ Input is {plural(abs(diff), 'day')} {when} the SPK date"
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
            "✅ <b>SPK monitoring started</b>\n"
            f"{len(df)} existing mismatched SPK (last {LOOKBACK_HOURS} hours) were recorded "
            "and will not be alerted.\n"
            "From now on you only get alerts for NEW mismatches."
        )
        print("First run: baseline saved.")
        return

    known = state["notified"]
    new = df[~df["no_spk"].isin(known)]

    if new.empty:
        print("No new mismatch.")
        return

    header = (
        f"🚨 <b>ALERT: {len(new)} new SPK with different dates</b>\n"
        f"tglspk ≠ tglinputspk · checked {now.day} {now:%b %Y} {now:%H:%M} WIB"
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
def cabang_entry(r, today):
    if r.count_spk_beda > 0:
        icon = "🔴"
    elif r.count_spk > 0:
        icon = "🟢"
    else:
        icon = "⚪"

    if r.count_spk > 0:
        line = f"{r.count_spk} SPK · {num(r.sum_panel)} panel"
        if r.count_spk_beda > 0:
            line += f" · ⚠️ {r.count_spk_beda} mismatch"
    else:
        line = "No data today"

    return (
        f"{icon} <b>{esc(r.cabang)}</b>\n"
        f"   {line}\n"
        f"   Last input: {fmt_dt(r.last_input)} ({ago(r.last_input, today)})"
    )


def build_report(df, last_df, now):
    """Returns (list of Telegram messages, Excel file as bytes)."""
    today = now.date()

    df = df.copy()
    df.columns = [c.lower() for c in df.columns]        # "Panel" -> "panel"
    last_df = last_df.copy()
    last_df.columns = [c.lower() for c in last_df.columns]

    df[DATE_COL] = pd.to_datetime(df[DATE_COL], errors="coerce")
    df[INPUT_COL] = pd.to_datetime(df[INPUT_COL], errors="coerce")
    df["panel"] = pd.to_numeric(df["panel"], errors="coerce")
    last_df["last_tglinputspk"] = pd.to_datetime(last_df["last_tglinputspk"], errors="coerce")

    both = df[DATE_COL].notna() & df[INPUT_COL].notna()
    mask = both & (df[DATE_COL].dt.normalize() != df[INPUT_COL].dt.normalize())

    # ---- today's numbers per cabang ----
    if df.empty:
        today_summary = pd.DataFrame(
            columns=["cabang", "count_spk", "sum_panel", "last_today", "count_spk_beda"]
        )
    else:
        today_summary = (
            df.groupby("cabang")
            .agg(
                count_spk=("no_spk", "nunique"),
                sum_panel=("panel", "sum"),
                last_today=(INPUT_COL, "max"),
            )
            .reset_index()
        )
        mm_counts = df[mask].groupby("cabang")["no_spk"].nunique()
        today_summary["count_spk_beda"] = (
            today_summary["cabang"].map(mm_counts).fillna(0).astype(int)
        )

    # ---- ALL cabang (even with no data today) + their last tglinputspk ----
    all_c = last_df.merge(today_summary, on="cabang", how="outer")
    all_c = all_c[all_c["cabang"].notna()].copy()
    all_c["last_today"] = pd.to_datetime(all_c["last_today"], errors="coerce")
    all_c["last_input"] = all_c["last_tglinputspk"].fillna(all_c["last_today"])
    for col in ("count_spk", "count_spk_beda"):
        all_c[col] = pd.to_numeric(all_c[col], errors="coerce").fillna(0).astype(int)
    all_c["sum_panel"] = pd.to_numeric(all_c["sum_panel"], errors="coerce").fillna(0)
    all_c = all_c.sort_values("cabang", key=lambda s: s.str.lower()).reset_index(drop=True)

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
    n_mm = mm["no_spk"].nunique()

    # ---- latest input overall ----
    if all_c["last_input"].notna().any():
        top = all_c.loc[all_c["last_input"].idxmax()]
        latest = f"{fmt_dt(top['last_input'], year=True)} - {esc(top['cabang'])}"
    else:
        latest = "-"

    # ---- Telegram messages ----
    mismatch_line = (
        f"⚠️ Date mismatch: <b>{plural(n_mm, 'SPK')}</b>"
        if n_mm > 0
        else "✅ No date mismatch"
    )
    header = (
        f"📊 <b>DAILY REPORT</b>\n"
        f"{now:%A}, {now.day} {now:%b %Y} · {now:%H:%M} WIB\n\n"
        f"🕓 <b>Latest input</b>\n{latest}\n\n"
        f"📈 <b>Today</b>\n"
        f"SPK {df['no_spk'].nunique()} · Panel {num(df['panel'].sum())} · Rows {len(df)}\n"
        f"{mismatch_line}\n\n"
        f"🏢 <b>All cabang ({len(all_c)})</b>\n"
        f"🔴 mismatch · 🟢 data today · ⚪ no data today\n"
    )
    entries = [cabang_entry(r, today) for r in all_c.itertuples(index=False)]
    messages = chunk(header, entries, sep="\n")

    if n_mm > 0:
        mm_header = f"⚠️ <b>Date mismatch detail ({plural(n_mm, 'SPK')})</b>"
        mm_blocks = [fmt_block(i, r) for i, r in enumerate(mm.itertuples(index=False), 1)]
        messages += chunk(mm_header, mm_blocks)

    # ---- Excel file with all the details ----
    summary_out = all_c[
        ["cabang", "count_spk", "sum_panel", "count_spk_beda", "last_input"]
    ].rename(columns={"last_input": "last_tglinputspk"})

    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        summary_out.to_excel(writer, sheet_name="Summary", index=False)
        mm.to_excel(writer, sheet_name="Mismatch", index=False)
        df.to_excel(writer, sheet_name="Data", index=False)
        for ws in writer.book.worksheets:
            for col in ws.columns:
                width = max(len(str(c.value)) if c.value is not None else 0 for c in col)
                ws.column_dimensions[col[0].column_letter].width = min(width + 2, 40)

    return messages, buffer.getvalue()


def run_report():
    engine = get_engine()
    now = now_local()
    today = now.date()
    start = datetime.combine(today, datetime.min.time())
    end = start + timedelta(days=1)

    # Scope of "today": every row whose tglspk is today OR whose tglinputspk is today
    q_today = text(
        f"SELECT * FROM {TABLE} "
        f"WHERE ({DATE_COL} >= :s AND {DATE_COL} < :e) "
        f"OR ({INPUT_COL} >= :s AND {INPUT_COL} < :e) "
        f"ORDER BY {INPUT_COL} DESC"
    )
    # Every cabang + its last tglinputspk
    q_last = text(
        f"SELECT cabang, MAX({INPUT_COL}) AS last_tglinputspk "
        f"FROM {TABLE} WHERE {INPUT_COL} >= :since GROUP BY cabang"
    )
    with engine.connect() as conn:
        df = pd.read_sql(q_today, conn, params={"s": start, "e": end})
        last_df = pd.read_sql(
            q_last, conn, params={"since": start - timedelta(days=ALL_CABANG_DAYS)}
        )

    messages, xlsx = build_report(df, last_df, now)
    for message in messages:
        send_message(message)
    send_document(
        f"spk_report_{today}.xlsx",
        xlsx,
        f"Full details {today}\nSheets: Summary, Mismatch, Data",
    )
    print("Report sent.")


# =====================================================================
# 8. MODE: test
# =====================================================================
def run_test():
    engine = get_engine()
    df = find_mismatch(engine, now_local() - timedelta(hours=LOOKBACK_HOURS))
    header = (
        "✅ <b>TEST OK</b>\n"
        "The bot can reach Telegram and the database.\n"
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
