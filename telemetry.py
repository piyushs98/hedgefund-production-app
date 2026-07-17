"""
telemetry.py — Simulated Backtesting Logging Hook (Task 3c).

Every portfolio scan writes one row per ticker into `backtest_telemetry`
inside hedge_fund.db: raw indicator state, pillar ratios, weighted scores,
active weights, adversarial result, final flag, and the selected contract
(when EXECUTE). This is the dataset the Saturday audit — and any future
offline backtester — should consume.
"""

import json
import os
import sqlite3
from dataclasses import asdict

import config


def init_telemetry_table(db_path=None):
    db_path = db_path or config.HEDGE_DB_PATH
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    with sqlite3.connect(db_path, timeout=30.0) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS backtest_telemetry (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                scan_id TEXT,
                ticker TEXT,
                liquidity_ratio REAL,
                technical_ratio REAL,
                sentiment_ratio REAL,
                liquidity_score REAL,
                technical_score REAL,
                sentiment_score REAL,
                adversarial_penalty REAL,
                total_score REAL,
                action_flag TEXT,
                weights_json TEXT,
                raw_metrics_json TEXT,
                reasons_json TEXT,
                adversarial_json TEXT,
                selected_contract_json TEXT,
                agent_params_json TEXT
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_bt_ticker_ts
            ON backtest_telemetry (ticker, timestamp)
        """)
        conn.commit()


def log_scan_result(scan_id, scorecard, adversarial_result=None,
                    selected_contract=None, agent_params=None, db_path=None):
    """Persist one ticker evaluation. Never raises — telemetry must not be
    able to take down the trading loop."""
    db_path = db_path or config.HEDGE_DB_PATH
    try:
        init_telemetry_table(db_path)
        card = asdict(scorecard)
        with sqlite3.connect(db_path, timeout=30.0) as conn:
            conn.execute(
                """INSERT INTO backtest_telemetry (
                       scan_id, ticker,
                       liquidity_ratio, technical_ratio, sentiment_ratio,
                       liquidity_score, technical_score, sentiment_score,
                       adversarial_penalty, total_score, action_flag,
                       weights_json, raw_metrics_json, reasons_json,
                       adversarial_json, selected_contract_json, agent_params_json
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    scan_id, card["ticker"],
                    card["liquidity_ratio"], card["technical_ratio"], card["sentiment_ratio"],
                    card["liquidity_score"], card["technical_score"], card["sentiment_score"],
                    card["adversarial_penalty"], card["total_score"], card["action_flag"],
                    json.dumps(card["weights"]),
                    json.dumps(card["metrics"], default=str),
                    json.dumps(card["reasons"]),
                    json.dumps(adversarial_result or {}, default=str),
                    json.dumps(selected_contract or {}, default=str),
                    json.dumps(agent_params or {}, default=str),
                ),
            )
            conn.commit()
        print(f"[Telemetry] Logged {card['ticker']} scan {scan_id} "
              f"({card['total_score']}/100 -> {card['action_flag']}).")
    except Exception as e:
        print(f"[Telemetry] WARNING: failed to log scan for "
              f"{getattr(scorecard, 'ticker', '?')}: {e}")
