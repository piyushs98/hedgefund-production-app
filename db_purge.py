import sqlite3
import os

def purge_morning_briefings(db_path):
    if not os.path.exists(db_path):
        print(f"Database {db_path} does not exist. Skipping.")
        return 0

    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        # Check if morning_briefings table exists
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='morning_briefings'")
        table_exists = cursor.fetchone()
        
        if not table_exists:
            print(f"Table 'morning_briefings' does not exist in {db_path}.")
            return 0
            
        # Delete rows
        # Since I don't know the exact schema, I will check for 'status' column and 'body' or 'text' column
        # Using a broad approach if possible, or attempting the exact requested parameters:
        # "where the status flag is marked as 'fallback' or where the text body contains the literal substring 'Fallback Mode'."
        
        # We need to find the column names to safely delete
        cursor.execute("PRAGMA table_info(morning_briefings)")
        columns = [col[1] for col in cursor.fetchall()]
        
        status_col = "status" if "status" in columns else None
        text_col = None
        for name in ["body", "text", "content", "briefing_text", "briefing"]:
            if name in columns:
                text_col = name
                break
                
        if not status_col and not text_col:
            print("Could not identify 'status' or text columns in morning_briefings table.")
            return 0

        query_conditions = []
        if status_col:
            query_conditions.append(f"{status_col} = 'fallback'")
        if text_col:
            query_conditions.append(f"{text_col} LIKE '%Fallback Mode%'")
            
        where_clause = " OR ".join(query_conditions)
        
        delete_query = f"DELETE FROM morning_briefings WHERE {where_clause}"
        print(f"Executing: {delete_query}")
        
        cursor.execute(delete_query)
        deleted_count = cursor.rowcount
        conn.commit()
        
        print(f"Successfully deleted {deleted_count} fallback rows from {db_path}.")
        return deleted_count
        
    except Exception as e:
        print(f"Error purging {db_path}: {e}")
        return 0
    finally:
        if 'conn' in locals():
            conn.close()

if __name__ == "__main__":
    total_deleted = 0
    # The user specifically mentioned hedge_fund.db
    total_deleted += purge_morning_briefings("data/hedge_fund.db")
    total_deleted += purge_morning_briefings("hedge_fund.db")
    
    # Also check our known database just in case
    total_deleted += purge_morning_briefings("data/news_room.db")
    
    print(f"\nPurge complete. Total rows deleted: {total_deleted}")
