import psycopg2
import time
from app.config import settings

# --- DB Connection ---
def get_db_conn():
    return psycopg2.connect(
        dbname=settings.postgres_db,
        user=settings.postgres_user,
        password=settings.postgres_password,
        host=settings.postgres_host,
        port=settings.postgres_port
    )

def init_db(retries=5, delay=5):
    """
    Try connecting to DB with retries before creating table.
    """
    for i in range(retries):
        try:
            conn = get_db_conn()
            cur = conn.cursor()
            cur.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id SERIAL PRIMARY KEY,
                sender VARCHAR(20),
                message TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """)
            conn.commit()
            cur.close()
            conn.close()
            print("Database initialized successfully")
            return
        except psycopg2.OperationalError as e:
            print(f"Database not ready ({i+1}/{retries}), retrying in {delay}s...")
            time.sleep(delay)
    raise Exception("Could not connect to database after retries")
