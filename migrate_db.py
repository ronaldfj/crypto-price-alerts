import sqlite3
import os
from alert import DB_FILE

def migrate():
    conn = sqlite3.connect(DB_FILE)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS execution_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            alert_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            action TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'PENDING',
            requested_at INTEGER NOT NULL,
            processed_at INTEGER,
            order_id TEXT,
            error_message TEXT,
            FOREIGN KEY(alert_id) REFERENCES alerts(id)
        )
        """
    )
    conn.commit()
    print("Tabla execution_requests añadida correctamente.")
    conn.close()

if __name__ == "__main__":
    migrate()
