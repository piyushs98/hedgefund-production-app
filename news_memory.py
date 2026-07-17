import sqlite3
import os
from datetime import datetime

# Centralized DB File Path
DB_PATH = "data/news_room.db"

def init_db():
    """
    Initializes the SQLite database and creates the headlines table 
    with a UNIQUE constraint to automatically prevent duplicate headlines.
    """
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with sqlite3.connect(DB_PATH, timeout=30.0) as conn:
        # Enable Write-Ahead Logging (WAL) for concurrency
        conn.execute('PRAGMA journal_mode=WAL;')
        cursor = conn.cursor()
        
        # Create table with UNIQUE constraint on (ticker, title)
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS headlines (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            ticker TEXT,
            sector TEXT,
            publisher TEXT,
            title TEXT,
            sentiment_score REAL,
            UNIQUE(ticker, title) ON CONFLICT IGNORE
        )
        """)
        
        # Create Innovation Hub unified table
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS innovation_data (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            ticker TEXT,
            source_tag TEXT,
            content TEXT,
            UNIQUE(ticker, source_tag, content) ON CONFLICT IGNORE
        )
        """)
        
        # Create Active Positions table for Risk Sentinel
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS active_positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            ticker TEXT,
            entry_price REAL,
            option_type TEXT,
            strike REAL,
            contract_id TEXT,
            status TEXT DEFAULT 'OPEN'
        )
        """)
        
        # Create Trade Logs table for Saturday Audit
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS trade_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            ticker TEXT,
            outcome TEXT,
            profit_loss REAL,
            duration_hours REAL,
            agent_prediction TEXT
        )
        """)
        conn.commit()

def save_headline(ticker, sector, publisher, title, sentiment_score=None):
    """
    Inserts a new headline record into the database.
    If the combination of ticker and title already exists, it is ignored.

    Args:
        ticker (str): The stock ticker (e.g., AAPL).
        sector (str): The industry sector (e.g., Tech, Macro, Politics).
        publisher (str): The news publisher (e.g., Reuters).
        title (str): The headline text.
        sentiment_score (float, optional): Score calculated for sentiment.

    Returns:
        bool: True if inserted successfully, False if ignored (duplicate).
    """
    init_db()
    success = False
    try:
        with sqlite3.connect(DB_PATH, timeout=30.0) as conn:
            cursor = conn.cursor()
            cursor.execute("""
            INSERT INTO headlines (ticker, sector, publisher, title, sentiment_score)
            VALUES (?, ?, ?, ?, ?)
            """, (ticker.upper(), sector, publisher, title, sentiment_score))
            conn.commit()
            # Rowcount is 1 if inserted, 0 if ignored due to constraint
            success = cursor.rowcount > 0
    except sqlite3.Error as e:
        print(f"[Memory Master] SQLite error saving headline: {e}")
    return success

def get_historical_context(ticker, days=90):
    """
    Queries the database and returns a formatted text summary of all news
    collected for that ticker over the last N days (defaulting to 90 days).

    Args:
        ticker (str): The stock ticker.
        days (int): Days range to retrieve.

    Returns:
        str: Clean bulleted list of headlines grouped chronologically.
    """
    init_db()
    rows = []
    
    # Calculate offset parameter
    time_offset = f"-{days} days"
    
    try:
        with sqlite3.connect(DB_PATH, timeout=30.0) as conn:
            cursor = conn.cursor()
            cursor.execute("""
            SELECT timestamp, publisher, title 
            FROM headlines 
            WHERE ticker = ? AND timestamp >= datetime('now', ?)
            ORDER BY timestamp DESC
            """, (ticker.upper(), time_offset))
            rows = cursor.fetchall()
    except sqlite3.Error as e:
        print(f"[Memory Master] SQLite error fetching context: {e}")
        
    if not rows:
        return ""
        
    headlines_list = []
    for row in rows:
        timestamp, publisher, title = row
        # Clean timestamp formatting if needed
        headlines_list.append(f"- [{timestamp}] {title} ({publisher})")
        
    return "\n".join(headlines_list)

def save_innovation_data(ticker, source_tag, content):
    """
    Inserts a macro innovation record into the database.
    """
    init_db()
    success = False
    try:
        with sqlite3.connect(DB_PATH, timeout=30.0) as conn:
            cursor = conn.cursor()
            cursor.execute("""
            INSERT INTO innovation_data (ticker, source_tag, content)
            VALUES (?, ?, ?)
            """, (ticker.upper(), source_tag, content))
            conn.commit()
            success = cursor.rowcount > 0
    except sqlite3.Error as e:
        print(f"[Memory Master] SQLite error saving innovation data: {e}")
    return success

def get_innovation_context(ticker, days=7):
    """
    Queries the database for Innovation Hub macro catalysts over the last N days.
    """
    init_db()
    rows = []
    time_offset = f"-{days} days"
    
    try:
        with sqlite3.connect(DB_PATH, timeout=30.0) as conn:
            cursor = conn.cursor()
            cursor.execute("""
            SELECT timestamp, source_tag, content 
            FROM innovation_data 
            WHERE ticker = ? AND timestamp >= datetime('now', ?)
            ORDER BY timestamp DESC
            """, (ticker.upper(), time_offset))
            rows = cursor.fetchall()
    except sqlite3.Error as e:
        print(f"[Memory Master] SQLite error fetching innovation context: {e}")
        
    if not rows:
        return ""
        
    data_list = []
    for row in rows:
        timestamp, source_tag, content = row
        data_list.append(f"- [{timestamp}] [{source_tag}] {content}")
        
    return "\n".join(data_list)

def clear_expired_news():
    """
    Housekeeping function to delete all news articles older than 90 days.
    Reduces database file size and maintains performance.
    """
    init_db()
    try:
        with sqlite3.connect(DB_PATH, timeout=30.0) as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM headlines WHERE timestamp < datetime('now', '-90 days')")
            conn.commit()
            deleted_count = cursor.rowcount
            if deleted_count > 0:
                print(f"[Memory Master] Cleaned up {deleted_count} expired records older than 90 days.")
    except sqlite3.Error as e:
        print(f"[Memory Master] SQLite error clearing expired news: {e}")


# ==========================================
# 🧪 TEST THE DATABASE
# ==========================================
if __name__ == "__main__":
    print("[Memory Master] Running Standalone Database Tests...")
    init_db()
    
    ticker_test = "TSLA"
    sector_test = "Tech"
    publisher_test = "Bloomberg"
    title_test = "Tesla rolls out next-gen FSD software to early testers"
    
    # Test Insert
    print("\n1. Testing Headline Insertion...")
    inserted_1 = save_headline(ticker_test, sector_test, publisher_test, title_test)
    print(f"   Insert Status (First time): {inserted_1} (Expected: True)")
    
    # Test Duplicate Prevention
    print("\n2. Testing Duplicate Prevention...")
    inserted_2 = save_headline(ticker_test, sector_test, publisher_test, title_test)
    print(f"   Insert Status (Second time): {inserted_2} (Expected: False - Ignored)")
    
    # Test Historical Query
    print("\n3. Testing Historical Context Retrieval (90 days)...")
    context = get_historical_context(ticker_test, days=90)
    print(f"   Context Output:\n{context}")
    
    # Test Expired news cleanup
    print("\n4. Testing News Database Cleanup...")
    clear_expired_news()
    
    print("\n[Memory Master] Standalone Tests Completed Successfully!")
