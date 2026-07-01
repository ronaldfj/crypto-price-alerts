"""
daily_summary.py — Resumen diario del Crypto Sentinel Bot.

Envía un único mensaje de Telegram con el estado del día:
alertas enviadas, activas, outcomes y invalidaciones.

Uso:
    python daily_summary.py              # últimas 24h
    python daily_summary.py --hours 48   # últimas 48h
"""

from __future__ import annotations

import argparse
import html
import os
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import requests

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
DB_FILE = os.getenv("ALERT_DB_FILE", "alerts_state.db")
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "20"))

ACTIVE = "ACTIVE"
CLOSED = "CLOSED"
INVALIDATED = "INVALIDATED"


def get_db_connection(db_file: str = DB_FILE) -> sqlite3.Connection:
    conn = sqlite3.connect(db_file)
    conn.row_factory = sqlite3.Row
    return conn


def send_telegram(message: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️ Telegram no configurado.")
        print(message)
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        r = requests.post(
            url,
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=REQUEST_TIMEOUT,
        )
        if r.status_code == 200 and r.json().get("ok"):
            return True
        print(f"⚠️ Telegram respondió {r.status_code}: {r.text[:200]}")
    except Exception as exc:
        print(f"❌ Error Telegram: {exc}")
    return False


def fmt_price(price: float) -> str:
    if price >= 1000:
        return f"{price:,.0f}"
    if price >= 1:
        return f"{price:.3f}"
    return f"{price:.5f}"


def side_icon(side: str) -> str:
    return "🟢" if side == "LONG" else "🔴"


def build_summary(conn: sqlite3.Connection, since_ts: int) -> str:
    esc = html.escape
    now = int(time.time())
    since_dt = datetime.fromtimestamp(since_ts, tz=timezone.utc).strftime("%d/%m %H:%M UTC")

    lines: List[str] = []
    lines.append("📊 <b>RESUMEN DIARIO — Crypto Sentinel</b>")
    lines.append(f"<i>Últimas {round((now - since_ts) / 3600)}h  ({since_dt} → ahora)</i>")
    lines.append("")

    # ── Alertas enviadas en el período ───────────────────────────────────────
    sent = conn.execute(
        "SELECT * FROM alerts WHERE sent_at >= ? ORDER BY sent_at DESC",
        (since_ts,),
    ).fetchall()

    lines.append(f"📤 <b>Alertas enviadas: {len(sent)}</b>")
    for r in sent:
        sent_time = datetime.fromtimestamp(r["sent_at"], tz=timezone.utc).strftime("%d/%m %H:%M")
        status_tag = {ACTIVE: "🟡 ACTIVA", CLOSED: "✅ CERRADA", INVALIDATED: "❌ INVÁL"}.get(r["status"], r["status"])
        lines.append(
            f"  {side_icon(r['side'])} <b>{esc(r['symbol'])}</b> {esc(r['side'])} "
            f"@ {fmt_price(r['entry_price'])} | score {r['score']:.1f} | {status_tag} | {sent_time}"
        )

    # ── Actualmente activas (pueden ser de días anteriores) ───────────────────
    active = conn.execute(
        "SELECT * FROM alerts WHERE status = ? ORDER BY sent_at DESC",
        (ACTIVE,),
    ).fetchall()

    if active:
        lines.append("")
        lines.append(f"⏳ <b>Activas ahora: {len(active)}</b>")
        for r in active:
            sent_time = datetime.fromtimestamp(r["sent_at"], tz=timezone.utc).strftime("%d/%m %H:%M")
            tp1 = r["tp1"] or 0.0
            rr = r["rr_ratio"] or 0.0
            lines.append(
                f"  {side_icon(r['side'])} <b>{esc(r['symbol'])}</b> {esc(r['side'])} "
                f"@ {fmt_price(r['entry_price'])} → TP1 {fmt_price(tp1)} ({rr:.2f}R) | desde {sent_time}"
            )

    # ── Outcomes del período ──────────────────────────────────────────────────
    closed = conn.execute(
        "SELECT * FROM alerts WHERE status = ? AND (invalidated_at >= ? OR validated_at >= ?)"
        " ORDER BY COALESCE(validated_at, invalidated_at) DESC",
        (CLOSED, since_ts, since_ts),
    ).fetchall()

    if closed:
        lines.append("")
        lines.append(f"✅ <b>Cerradas con outcome: {len(closed)}</b>")
        for r in closed:
            result = r["validation_result"] or "?"
            outcome_rr = r["outcome_rr"]
            tp1_hit = "TP1 ✓" if r["tp1_hit"] else ""
            tp2_hit = "TP2 ✓" if r["tp2_hit"] else ""
            hits = " + ".join(filter(None, [tp1_hit, tp2_hit])) or result
            rr_str = f" | {outcome_rr:.2f}R" if outcome_rr else ""
            lines.append(
                f"  {side_icon(r['side'])} <b>{esc(r['symbol'])}</b> {esc(r['side'])} "
                f"@ {fmt_price(r['entry_price'])} → {esc(hits)}{rr_str}"
            )

    # ── Invalidadas en el período ─────────────────────────────────────────────
    inv = conn.execute(
        "SELECT symbol, side, entry_price, score, invalidation_reason, invalidated_at "
        "FROM alerts WHERE status = ? AND invalidated_at >= ? ORDER BY invalidated_at DESC",
        (INVALIDATED, since_ts),
    ).fetchall()

    if inv:
        lines.append("")
        lines.append(f"❌ <b>Invalidadas: {len(inv)}</b>")
        # Agrupar por razón
        by_reason: Dict[str, List[str]] = {}
        for r in inv:
            reason = r["invalidation_reason"] or "desconocida"
            by_reason.setdefault(reason, []).append(r["symbol"])
        for reason, symbols in by_reason.items():
            lines.append(f"  • {esc(reason)}: {esc(', '.join(symbols))}")

    # ── Mini estadísticas si hay suficientes datos ────────────────────────────
    all_period = conn.execute(
        "SELECT score, adx, rr_ratio, side, status FROM alerts WHERE sent_at >= ?",
        (since_ts,),
    ).fetchall()

    if len(all_period) >= 3:
        scores = [r["score"] for r in all_period if r["score"]]
        adxs   = [r["adx"]   for r in all_period if r["adx"]]
        lines.append("")
        lines.append("📈 <b>Stats del período</b>")
        if scores:
            lines.append(f"  Score promedio: {sum(scores)/len(scores):.1f} (min {min(scores):.1f} / max {max(scores):.1f})")
        if adxs:
            lines.append(f"  ADX promedio:   {sum(adxs)/len(adxs):.1f}")
        closed_n  = sum(1 for r in all_period if r["status"] == CLOSED)
        inv_n     = sum(1 for r in all_period if r["status"] == INVALIDATED)
        active_n  = sum(1 for r in all_period if r["status"] == ACTIVE)
        lines.append(f"  Resultados:     {closed_n} cerradas · {inv_n} invalidadas · {active_n} activas")

    lines.append("")
    lines.append(f"<i>Generado: {datetime.now(tz=timezone.utc).strftime('%d/%m/%Y %H:%M UTC')}</i>")

    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hours", type=int, default=24, help="Ventana de horas hacia atrás (default: 24)")
    parser.add_argument("--db", default=DB_FILE, help="Ruta al archivo SQLite")
    args = parser.parse_args()

    since_ts = int(time.time()) - args.hours * 3600

    conn = get_db_connection(args.db)
    message = build_summary(conn, since_ts)
    conn.close()

    print(message)
    print()

    ok = send_telegram(message)
    if ok:
        print("✅ Resumen enviado a Telegram.")
    else:
        print("⚠️ No se pudo enviar a Telegram (ver mensaje arriba).")


if __name__ == "__main__":
    main()
