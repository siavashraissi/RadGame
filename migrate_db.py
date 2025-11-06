#!/usr/bin/env python3

import sqlite3
import random
import os

def migrate_database():
    db_path = 'instance/training.db'
    
    if not os.path.exists(db_path):
        print(f"Database file not found at {db_path}")
        return
    
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    try:
        # add report_version column if missing
        cursor.execute("PRAGMA table_info(access_codes)")
        columns = [column[1] for column in cursor.fetchall()]
        
        if 'report_version' not in columns:
            print("Adding report_version column to access_codes table...")
            cursor.execute("ALTER TABLE access_codes ADD COLUMN report_version TEXT DEFAULT 'practice'")
            
            # Update existing records with random versions
            cursor.execute("SELECT code FROM access_codes")
            existing_codes = cursor.fetchall()
            
            for (code,) in existing_codes:
                version = random.choice(['practice', 'guided'])
                cursor.execute("UPDATE access_codes SET report_version = ? WHERE code = ?", (version, code))
            
            print(f"Updated {len(existing_codes)} existing access codes with random versions")
        else:
            print("report_version column already exists")

        # 2) Drop expiration_date if it exists (SQLite doesn't support DROP COLUMN directly)
        cursor.execute("PRAGMA table_info(access_codes)")
        columns = [column[1] for column in cursor.fetchall()]
        if 'expiration_date' in columns:
            print("Removing expiration_date column via table rebuild...")
            # Get current schema for access_codes
            cursor.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='access_codes'")
            row = cursor.fetchone()
            if not row:
                raise RuntimeError("access_codes table not found")

            # Create new table without expiration_date
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS access_codes_new (
                    code TEXT PRIMARY KEY,
                    status TEXT DEFAULT 'active',
                    created_at DATETIME,
                    first_login_at DATETIME,
                    last_login_at DATETIME,
                    login_attempts INTEGER DEFAULT 0,
                    cases_completed INTEGER DEFAULT 0,
                    cases_correct INTEGER DEFAULT 0,
                    report_version TEXT DEFAULT 'practice',
                    took_localize_pre BOOLEAN DEFAULT 0,
                    took_localize_post BOOLEAN DEFAULT 0,
                    took_report_pre BOOLEAN DEFAULT 0,
                    took_report_post BOOLEAN DEFAULT 0,
                    localize_cases_completed INTEGER DEFAULT 0,
                    report_cases_completed INTEGER DEFAULT 0
                )
            """)

            # Copy data (omit expiration_date)
            cursor.execute("""
                INSERT INTO access_codes_new (
                    code, status, created_at, first_login_at, last_login_at,
                    login_attempts, cases_completed, cases_correct, report_version,
                    took_localize_pre, took_localize_post, took_report_pre, took_report_post,
                    localize_cases_completed, report_cases_completed
                )
                SELECT 
                    code, status, created_at, first_login_at, last_login_at,
                    login_attempts, cases_completed, cases_correct, report_version,
                    took_localize_pre, took_localize_post, took_report_pre, took_report_post,
                    localize_cases_completed, report_cases_completed
                FROM access_codes
            """)

            # Rename tables
            cursor.execute("ALTER TABLE access_codes RENAME TO access_codes_old")
            cursor.execute("ALTER TABLE access_codes_new RENAME TO access_codes")
            cursor.execute("DROP TABLE access_codes_old")
            print("Removed expiration_date column successfully")
        else:
            print("expiration_date column already removed or never existed")

        # 3) Remove legacy cases_completed and cases_correct if they still exist
        cursor.execute("PRAGMA table_info(access_codes)")
        columns = [column[1] for column in cursor.fetchall()]
        if 'cases_completed' in columns or 'cases_correct' in columns:
            print("Rebuilding access_codes to drop cases_completed and cases_correct...")
            cursor.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='access_codes'")
            row = cursor.fetchone()
            if not row:
                raise RuntimeError("access_codes table not found for rebuild")
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS access_codes_new (
                    code TEXT PRIMARY KEY,
                    status TEXT DEFAULT 'active',
                    created_at DATETIME,
                    first_login_at DATETIME,
                    last_login_at DATETIME,
                    login_attempts INTEGER DEFAULT 0,
                    report_version TEXT DEFAULT 'practice',
                    took_localize_pre BOOLEAN DEFAULT 0,
                    took_localize_post BOOLEAN DEFAULT 0,
                    took_report_pre BOOLEAN DEFAULT 0,
                    took_report_post BOOLEAN DEFAULT 0,
                    localize_cases_completed INTEGER DEFAULT 0,
                    report_cases_completed INTEGER DEFAULT 0
                )
            """)
            cursor.execute("""
                INSERT INTO access_codes_new (
                    code, status, created_at, first_login_at, last_login_at,
                    login_attempts, report_version,
                    took_localize_pre, took_localize_post, took_report_pre, took_report_post,
                    localize_cases_completed, report_cases_completed
                )
                SELECT 
                    code, status, created_at, first_login_at, last_login_at,
                    login_attempts, report_version,
                    took_localize_pre, took_localize_post, took_report_pre, took_report_post,
                    localize_cases_completed, report_cases_completed
                FROM access_codes
            """)
            cursor.execute("ALTER TABLE access_codes RENAME TO access_codes_old")
            cursor.execute("ALTER TABLE access_codes_new RENAME TO access_codes")
            cursor.execute("DROP TABLE access_codes_old")
            print("Removed cases_completed and cases_correct columns successfully")
        else:
            print("cases_completed / cases_correct already removed or never existed")
        
        # Ensure time_spent_ms exists in localize_test_case_logs
        cursor.execute("PRAGMA table_info(localize_test_case_logs)")
        lcols = [column[1] for column in cursor.fetchall()]
        if 'time_spent_ms' not in lcols:
            print("Adding time_spent_ms to localize_test_case_logs...")
            cursor.execute("ALTER TABLE localize_test_case_logs ADD COLUMN time_spent_ms INTEGER DEFAULT 0")
        else:
            print("time_spent_ms already exists in localize_test_case_logs")
        
        # Ensure timer_checkpoint_ms exists in user_case_logs
        cursor.execute("PRAGMA table_info(user_case_logs)")
        ucols = [row[1] for row in cursor.fetchall()]
        if 'timer_checkpoint_ms' not in ucols:
            print("Adding timer_checkpoint_ms to user_case_logs...")
            cursor.execute("ALTER TABLE user_case_logs ADD COLUMN timer_checkpoint_ms INTEGER DEFAULT 0")
        else:
            print("timer_checkpoint_ms already exists in user_case_logs")

        # Ensure snapshot columns exist in user_case_logs
        cursor.execute("PRAGMA table_info(user_case_logs)")
        ucols = [row[1] for row in cursor.fetchall()]
        if 'localize_cases_completed_snapshot' not in ucols:
            print("Adding localize_cases_completed_snapshot to user_case_logs...")
            cursor.execute("ALTER TABLE user_case_logs ADD COLUMN localize_cases_completed_snapshot INTEGER DEFAULT 0")
        else:
            print("localize_cases_completed_snapshot already exists in user_case_logs")
        # report snapshot moved to radgame_report_logs
        cursor.execute("PRAGMA table_info(radgame_report_logs)")
        rcols = [row[1] for row in cursor.fetchall()]
        if 'report_cases_completed_snapshot' not in rcols:
            print("Adding report_cases_completed_snapshot to radgame_report_logs...")
            cursor.execute("ALTER TABLE radgame_report_logs ADD COLUMN report_cases_completed_snapshot INTEGER DEFAULT 0")
        else:
            print("report_cases_completed_snapshot already exists in radgame_report_logs")
        # Ensure report timer columns exist
        cursor.execute("PRAGMA table_info(radgame_report_logs)")
        rcols = [row[1] for row in cursor.fetchall()]
        if 'time_spent_ms' not in rcols:
            print("Adding time_spent_ms to radgame_report_logs...")
            cursor.execute("ALTER TABLE radgame_report_logs ADD COLUMN time_spent_ms INTEGER DEFAULT 0")
        else:
            print("time_spent_ms already exists in radgame_report_logs")
        cursor.execute("PRAGMA table_info(radgame_report_logs)")
        rcols = [row[1] for row in cursor.fetchall()]
        if 'timer_checkpoint_ms' not in rcols:
            print("Adding timer_checkpoint_ms to radgame_report_logs...")
            cursor.execute("ALTER TABLE radgame_report_logs ADD COLUMN timer_checkpoint_ms INTEGER DEFAULT 0")
        else:
            print("timer_checkpoint_ms already exists in radgame_report_logs")

        # Rebuild user_case_logs to drop obsolete report_cases_completed_snapshot if still present
        cursor.execute("PRAGMA table_info(user_case_logs)")
        ucols = [row[1] for row in cursor.fetchall()]
        if 'report_cases_completed_snapshot' in ucols:
            print("Rebuilding user_case_logs to drop report_cases_completed_snapshot...")
            cursor.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='user_case_logs'")
            # Create new table without the obsolete column
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS user_case_logs_new (
                    id INTEGER PRIMARY KEY, 
                    access_code_id VARCHAR(10) NOT NULL, 
                    case_id VARCHAR(128) NOT NULL, 
                    selections_json TEXT NOT NULL, 
                    time_spent_ms INTEGER NOT NULL DEFAULT 0, 
                    timer_checkpoint_ms INTEGER NOT NULL DEFAULT 0, 
                    correct_count INTEGER NOT NULL DEFAULT 0, 
                    incorrect_count INTEGER NOT NULL DEFAULT 0, 
                    localize_cases_completed_snapshot INTEGER NOT NULL DEFAULT 0, 
                    timestamp DATETIME
                )
                """
            )
            cursor.execute(
                """
                INSERT INTO user_case_logs_new (
                    id, access_code_id, case_id, selections_json, time_spent_ms, timer_checkpoint_ms, 
                    correct_count, incorrect_count, localize_cases_completed_snapshot, timestamp
                )
                SELECT 
                    id, access_code_id, case_id, selections_json, time_spent_ms, timer_checkpoint_ms, 
                    correct_count, incorrect_count, localize_cases_completed_snapshot, timestamp
                FROM user_case_logs
                """
            )
            cursor.execute("ALTER TABLE user_case_logs RENAME TO user_case_logs_old")
            cursor.execute("ALTER TABLE user_case_logs_new RENAME TO user_case_logs")
            cursor.execute("DROP TABLE user_case_logs_old")
            print("Removed report_cases_completed_snapshot from user_case_logs")
        else:
            print("report_cases_completed_snapshot already absent from user_case_logs")

        conn.commit()
        print("Migration completed successfully!")
        
    except Exception as e:
        print(f"Error during migration: {e}")
        conn.rollback()
    finally:
        conn.close()

if __name__ == "__main__":
    migrate_database() 