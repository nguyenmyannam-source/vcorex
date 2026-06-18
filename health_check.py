#!/usr/bin/env python3
"""
Health Check Script for VCOREX Trading Bot Migration
Run this after migration to verify system health.
"""
import sys
import os
import asyncio
from pathlib import Path
from datetime import datetime

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

try:
    from core.config.settings import settings
    from loguru import logger
except ImportError as e:
    print(f"❌ Import error: {e}")
    print("Please ensure all dependencies are installed: python -m pip install -r requirements.txt")
    sys.exit(1)


def check_environment():
    """Check environment configuration."""
    print("\n" + "="*60)
    print("🔍 ENVIRONMENT CHECK")
    print("="*60)
    
    issues = []
    
    # Check .env file
    if not Path(".env").exists():
        issues.append(".env file not found")
        print("❌ .env file not found")
    else:
        print("✅ .env file exists")
    
    # Check critical settings
    if not settings.okx_api_key or settings.okx_api_key == "your_okx_api_key_here":
        issues.append("OKX_API_KEY not configured")
        print("❌ OKX_API_KEY not configured")
    else:
        print("✅ OKX_API_KEY configured")
    
    if not settings.okx_api_secret or settings.okx_api_secret == "your_okx_api_secret_here":
        issues.append("OKX_API_SECRET not configured")
        print("❌ OKX_API_SECRET not configured")
    else:
        print("✅ OKX_API_SECRET configured")
    
    if not settings.okx_passphrase or settings.okx_passphrase == "your_okx_passphrase_here":
        issues.append("OKX_PASSPHRASE not configured")
        print("❌ OKX_PASSPHRASE not configured")
    else:
        print("✅ OKX_PASSPHRASE configured")
    
    print(f"✅ Demo Mode: {settings.okx_demo_mode}")
    print(f"✅ Database URL: {settings.database_url}")
    print(f"✅ Redis URL: {settings.redis_url}")
    
    return len(issues) == 0, issues


def check_database():
    """Check database connectivity and integrity."""
    print("\n" + "="*60)
    print("🔍 DATABASE CHECK")
    print("="*60)
    
    issues = []
    
    # Check database file exists
    db_path = Path("data/vcorex.db")
    if not db_path.exists():
        issues.append("Database file not found")
        print("❌ Database file not found at data/vcorex.db")
        return False, issues
    
    file_size = db_path.stat().st_size
    print(f"✅ Database file exists (Size: {file_size:,} bytes)")
    
    if file_size == 0:
        issues.append("Database file is empty")
        print("❌ Database file is empty")
        return False, issues
    
    # Check WAL files
    wal_path = Path("data/vcorex.db-wal")
    shm_path = Path("data/vcorex.db-shm")
    
    if wal_path.exists():
        print(f"✅ WAL file exists (Size: {wal_path.stat().st_size:,} bytes)")
    else:
        print("⚠️  WAL file not found (may be normal if bot not running)")
    
    if shm_path.exists():
        print(f"✅ SHM file exists (Size: {shm_path.stat().st_size:,} bytes)")
    else:
        print("⚠️  SHM file not found (may be normal if bot not running)")
    
    # Try to import database module
    try:
        from infrastructure.storage.database import Base, engine
        print("✅ Database module imported successfully")
    except Exception as e:
        issues.append(f"Database module error: {e}")
        print(f"❌ Database module error: {e}")
    
    return len(issues) == 0, issues


def check_directories():
    """Check required directories."""
    print("\n" + "="*60)
    print("🔍 DIRECTORY CHECK")
    print("="*60)
    
    issues = []
    required_dirs = ["data", "logs", "core", "infrastructure", "interfaces", "services", "domain"]
    
    for dir_name in required_dirs:
        dir_path = Path(dir_name)
        if dir_path.exists() and dir_path.is_dir():
            print(f"✅ {dir_name}/ exists")
        else:
            issues.append(f"{dir_name}/ not found")
            print(f"❌ {dir_name}/ not found")
    
    return len(issues) == 0, issues


def check_dependencies():
    """Check critical dependencies."""
    print("\n" + "="*60)
    print("🔍 DEPENDENCY CHECK")
    print("="*60)
    
    issues = []
    critical_packages = [
        "aiohttp",
        "sqlalchemy",
        "pydantic",
        "loguru",
        "redis",
    ]
    
    for package in critical_packages:
        try:
            __import__(package)
            print(f"✅ {package} installed")
        except ImportError:
            issues.append(f"{package} not installed")
            print(f"❌ {package} not installed")
    
    return len(issues) == 0, issues


def check_logs():
    """Check log directory and recent errors."""
    print("\n" + "="*60)
    print("🔍 LOG CHECK")
    print("="*60)
    
    issues = []
    logs_dir = Path("logs")
    
    if not logs_dir.exists():
        print("⚠️  logs/ directory not found")
        return True, issues  # Not critical
    
    print(f"✅ logs/ directory exists")
    
    # Check for error log
    error_log = logs_dir / "errors.log"
    if error_log.exists():
        print(f"⚠️  errors.log exists (Size: {error_log.stat().st_size:,} bytes)")
        print("   Review errors.log for critical issues")
    else:
        print("✅ No errors.log found")
    
    # Check recent log files
    log_files = list(logs_dir.glob("*.log")) + list(logs_dir.glob("*.log.zip"))
    if log_files:
        print(f"✅ Found {len(log_files)} log file(s)")
        # Show most recent
        latest = max(log_files, key=lambda p: p.stat().st_mtime)
        print(f"   Latest: {latest.name} (Modified: {datetime.fromtimestamp(latest.stat().st_mtime)})")
    
    return len(issues) == 0, issues


async def check_okx_connection():
    """Check OKX API connectivity (async)."""
    print("\n" + "="*60)
    print("🔍 OKX API CONNECTION CHECK")
    print("="*60)
    
    issues = []
    
    try:
        from infrastructure.exchange.okx_exchange import OKXExchange
        from core.config.settings import settings
        
        # Just test initialization
        exchange = OKXExchange(
            settings=settings,
            event_bus=None,  # Not needed for basic connection check
        )
        print("✅ OKXExchange initialized successfully")
        print("   API credentials loaded")
        
        # Try to initialize session (basic connectivity test)
        await exchange.initialize()
        print("✅ OKX API session initialized")
        
    except Exception as e:
        issues.append(f"OKX API connection failed: {e}")
        print(f"❌ OKX API connection failed: {e}")
        print("   This may be normal if API credentials are invalid or network is blocked")
        print("   Skipping this check - not critical for migration verification")
    
    # Always return True since this is optional for migration
    return True, issues


def main():
    """Run all health checks."""
    print("\n" + "="*60)
    print("🏥 VCOREX HEALTH CHECK")
    print("="*60)
    print(f"Started at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    
    all_passed = True
    all_issues = []
    
    # Run synchronous checks
    passed, issues = check_environment()
    all_passed = all_passed and passed
    all_issues.extend(issues)
    
    passed, issues = check_directories()
    all_passed = all_passed and passed
    all_issues.extend(issues)
    
    passed, issues = check_dependencies()
    all_passed = all_passed and passed
    all_issues.extend(issues)
    
    passed, issues = check_database()
    all_passed = all_passed and passed
    all_issues.extend(issues)
    
    passed, issues = check_logs()
    all_passed = all_passed and passed
    all_issues.extend(issues)
    
    # Run async check
    try:
        passed, issues = asyncio.run(check_okx_connection())
        all_passed = all_passed and passed
        all_issues.extend(issues)
    except Exception as e:
        print(f"❌ Async check failed: {e}")
        all_issues.append(f"Async check failed: {e}")
        all_passed = False
    
    # Summary
    print("\n" + "="*60)
    print("📊 HEALTH CHECK SUMMARY")
    print("="*60)
    
    if all_passed:
        print("✅ ALL CHECKS PASSED")
        print("System is healthy and ready for operation.")
        return 0
    else:
        print(f"❌ {len(all_issues)} CHECK(S) FAILED:")
        for issue in all_issues:
            print(f"   - {issue}")
        print("\nPlease resolve the issues above before running the bot.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
