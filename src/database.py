"""SQLite database for storing chart message metadata."""

import sqlite3
from typing import Optional, Dict, Any
from datetime import datetime
import logging

logger = logging.getLogger(__name__)


class ChartDatabase:
    """Database for persisting chart message information."""

    def __init__(self, db_path: str = "data/charts.db"):
        """Initialize database connection and create tables.

        Args:
            db_path: Path to SQLite database file
        """
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row  # Allow dict-like access
        self.create_table()
        logger.info(f"Database initialized at {db_path}")

    def create_table(self):
        """Create chart_messages table if it doesn't exist."""
        with self.conn:
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS chart_messages (
                    message_id INTEGER PRIMARY KEY,
                    channel_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    ticker TEXT NOT NULL,
                    expiration TEXT NOT NULL,
                    option_type TEXT NOT NULL,
                    days INTEGER NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            logger.info("Chart messages table ready")

    def store_chart(
        self,
        message_id: int,
        channel_id: int,
        user_id: int,
        ticker: str,
        expiration: str,
        option_type: str,
        days: int
    ) -> bool:
        """Store chart message metadata.

        Args:
            message_id: Discord message ID
            channel_id: Discord channel ID
            user_id: Discord user ID who requested
            ticker: Stock ticker
            expiration: Option expiration date (YYYY-MM-DD)
            option_type: "call" or "put"
            days: Number of days of data

        Returns:
            True if successful
        """
        try:
            with self.conn:
                self.conn.execute("""
                    INSERT INTO chart_messages
                    (message_id, channel_id, user_id, ticker, expiration, option_type, days)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (message_id, channel_id, user_id, ticker, expiration, option_type, days))

            logger.info(f"Stored chart message {message_id} for {ticker}")
            return True
        except Exception as e:
            logger.error(f"Failed to store chart message: {e}")
            return False

    def get_chart(self, message_id: int) -> Optional[Dict[str, Any]]:
        """Retrieve chart metadata by message ID.

        Args:
            message_id: Discord message ID

        Returns:
            Dictionary with chart metadata, or None if not found
        """
        cursor = self.conn.execute("""
            SELECT message_id, channel_id, user_id, ticker, expiration, option_type, days, created_at
            FROM chart_messages
            WHERE message_id = ?
        """, (message_id,))

        row = cursor.fetchone()
        if row:
            return dict(row)
        return None

    def delete_chart(self, message_id: int) -> bool:
        """Delete chart metadata.

        Args:
            message_id: Discord message ID

        Returns:
            True if deleted, False if not found
        """
        try:
            with self.conn:
                cursor = self.conn.execute("""
                    DELETE FROM chart_messages
                    WHERE message_id = ?
                """, (message_id,))

            deleted = cursor.rowcount > 0
            if deleted:
                logger.info(f"Deleted chart message {message_id}")
            return deleted
        except Exception as e:
            logger.error(f"Failed to delete chart message: {e}")
            return False

    def close(self):
        """Close database connection."""
        self.conn.close()
        logger.info("Database connection closed")
