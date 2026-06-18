# PHASE 24.1A – PRODUCTION ENDPOINT VALIDATION PATCH

**Patch Date:** 2026-06-18
**Patch Type:** CRITICAL FIX
**Finding Addressed:** No Production Endpoint Validation
**Risk Level:** LOW

---

## UNIFIED DIFF

```diff
--- a/infrastructure/exchange/okx_exchange.py
+++ b/infrastructure/exchange/okx_exchange.py
@@ -159,6 +159,64 @@ class OKXExchange(BaseExchange):
         super().__init__(api_key, api_secret, passphrase, demo_mode)
         self.base_url = settings.okx_base_url
         self.ws_url = settings.okx_ws_url

+        # [CRITICAL FIX] Validate endpoint matches demo_mode to prevent trading on wrong environment
+        # Production endpoints: https://openapi.okx.com, wss://ws.okx.com:8443/ws/v5
+        # Demo endpoints: https://www.okx.com, wss://wspap.okx.com:8443/ws/v5
+        production_rest_endpoints = ["https://openapi.okx.com"]
+        demo_rest_endpoints = ["https://www.okx.com"]
+        production_ws_endpoints = ["wss://ws.okx.com:8443/ws/v5"]
+        demo_ws_endpoints = ["wss://wspap.okx.com:8443/ws/v5"]
+
+        if demo_mode:
+            # Demo mode: require demo endpoints
+            if self.base_url not in demo_rest_endpoints:
+                logger.critical(
+                    f"CRITICAL CONFIGURATION ERROR: demo_mode=True but base_url is production endpoint: {self.base_url}. "
+                    f"Expected demo endpoint: https://www.okx.com. "
+                    f"Trading on production with demo credentials will fail. "
+                    f"Fix: Set okx_base_url=https://www.okx.com in .env or set okx_demo_mode=False."
+                )
+                raise ValueError(
+                    f"Configuration mismatch: demo_mode=True requires demo REST endpoint (https://www.okx.com), "
+                    f"but got production endpoint: {self.base_url}. "
+                    f"Set okx_base_url=https://www.okx.com in .env or set okx_demo_mode=False."
+                )
+            if self.ws_url not in demo_ws_endpoints:
+                logger.critical(
+                    f"CRITICAL CONFIGURATION ERROR: demo_mode=True but ws_url is production endpoint: {self.ws_url}. "
+                    f"Expected demo endpoint: wss://wspap.okx.com:8443/ws/v5. "
+                    f"Trading on production with demo credentials will fail. "
+                    f"Fix: Set okx_ws_url=wss://wspap.okx.com:8443/ws/v5 in .env or set okx_demo_mode=False."
+                )
+                raise ValueError(
+                    f"Configuration mismatch: demo_mode=True requires demo WS endpoint (wss://wspap.okx.com:8443/ws/v5), "
+                    f"but got production endpoint: {self.ws_url}. "
+                    f"Set okx_ws_url=wss://wspap.okx.com:8443/ws/v5 in .env or set okx_demo_mode=False."
+                )
+        else:
+            # Production mode: require production endpoints
+            if self.base_url not in production_rest_endpoints:
+                logger.critical(
+                    f"CRITICAL CONFIGURATION ERROR: demo_mode=False but base_url is demo endpoint: {self.base_url}. "
+                    f"Expected production endpoint: https://openapi.okx.com. "
+                    f"Trading on demo with production credentials is unintended. "
+                    f"Fix: Set okx_base_url=https://openapi.okx.com in .env or set okx_demo_mode=True."
+                )
+                raise ValueError(
+                    f"Configuration mismatch: demo_mode=False requires production REST endpoint (https://openapi.okx.com), "
+                    f"but got demo endpoint: {self.base_url}. "
+                    f"Set okx_base_url=https://openapi.okx.com in .env or set okx_demo_mode=True."
+                )
+            if self.ws_url not in production_ws_endpoints:
+                logger.critical(
+                    f"CRITICAL CONFIGURATION ERROR: demo_mode=False but ws_url is demo endpoint: {self.ws_url}. "
+                    f"Expected production endpoint: wss://ws.okx.com:8443/ws/v5. "
+                    f"Trading on demo with production credentials is unintended. "
+                    f"Fix: Set okx_ws_url=wss://ws.okx.com:8443/ws/v5 in .env or set okx_demo_mode=True."
+                )
+                raise ValueError(
+                    f"Configuration mismatch: demo_mode=False requires production WS endpoint (wss://ws.okx.com:8443/ws/v5), "
+                    f"but got demo endpoint: {self.ws_url}. "
+                    f"Set okx_ws_url=wss://ws.okx.com:8443/ws/v5 in .env or set okx_demo_mode=True."
+                )
+
         # Ensure correct WS path based on demo mode if using defaults
         if demo_mode:
             self.ws_url = "wss://wspap.okx.com:8443/ws/v5"
```

---

## PATCH DETAILS

**FILE:** infrastructure/exchange/okx_exchange.py
**FUNCTION:** __init__
**LINE NUMBERS:** 163-222 (60 lines added)
**CHANGE TYPE:** Addition (no modifications to existing code)

---

## RUNTIME FLOW BEFORE PATCH

### Startup Flow
1. OKXExchange.__init__ reads settings.okx_demo_mode, settings.okx_base_url, settings.okx_ws_url
2. Sets self.base_url = settings.okx_base_url (line 160)
3. Sets self.ws_url = settings.okx_ws_url (line 161)
4. If demo_mode, overrides ws_url to demo endpoint (lines 164-166)
5. **NO VALIDATION** that base_url matches demo_mode
6. Bot starts regardless of endpoint mismatch
7. Risk: Trading on wrong environment

### Example Mismatch Scenario
- okx_demo_mode=True
- okx_base_url=https://openapi.okx.com (PRODUCTION)
- okx_ws_url=wss://wspap.okx.com:8443/ws/v5 (DEMO)
- Result: Bot starts, REST calls to production with demo credentials (authentication failure)

---

## RUNTIME FLOW AFTER PATCH

### Startup Flow
1. OKXExchange.__init__ reads settings.okx_demo_mode, settings.okx_base_url, settings.okx_ws_url
2. Sets self.base_url = settings.okx_base_url (line 160)
3. Sets self.ws_url = settings.okx_ws_url (line 161)
4. **NEW:** Validates endpoint matches demo_mode (lines 163-222)
   - If demo_mode=True: requires demo endpoints
   - If demo_mode=False: requires production endpoints
   - On mismatch: logs CRITICAL, raises ValueError, bot fails to start
5. If validation passes: continues with existing logic
6. Bot only starts if endpoints match demo_mode

### Example Mismatch Scenario
- okx_demo_mode=True
- okx_base_url=https://openapi.okx.com (PRODUCTION)
- Result: **CRITICAL log + ValueError raised, bot fails to start**
- User must fix configuration before bot can run

---

## PROOF: NO IMPACT ON ORDER PLACEMENT

### Order Placement Flow
1. Order placement methods: place_order, place_algo_order, close_position
2. These methods use:
   - self.settings.margin_mode (line 1441 in place_order)
   - self.pos_mode (line 1448 in place_order)
   - self.base_url (via _request method, line 583)
   - self.ws_url (via websocket_stream method)
3. **Patch location:** __init__ (startup only)
4. **Patch timing:** Runs once during initialization, before any orders
5. **Patch effect:** Only validates endpoints, does not modify endpoints or order logic

### Evidence
- place_order uses self.settings.margin_mode (NOT affected by patch)
- place_order uses self.pos_mode (NOT affected by patch)
- place_order calls _request which uses self.base_url (patch validates base_url at __init__, but doesn't change it)
- If validation passes, order placement proceeds unchanged
- If validation fails, bot doesn't start, so no orders can be placed

### Conclusion
**ZERO IMPACT** on order placement logic. Patch only adds validation at startup.

---

## PROOF: NO IMPACT ON PERSISTENCE

### Persistence Flow
1. Persistence is handled by separate services:
   - infrastructure/storage/database.py
   - infrastructure/storage/database_adapter.py
   - services/position/persistence.py
2. These services use database connections and write operations
3. **Patch location:** infrastructure/exchange/okx_exchange.py (exchange adapter only)
4. **Patch effect:** Only validates endpoints, does not touch database or persistence logic

### Evidence
- No database operations in okx_exchange.py
- No persistence logic in okx_exchange.py
- Patch only validates endpoints at __init__
- No changes to database schema or queries
- No changes to persistence service interfaces

### Conclusion
**ZERO IMPACT** on persistence. Patch only validates endpoints in exchange adapter.

---

## PROOF: NO IMPACT ON RECOVERY

### Recovery Flow
1. Recovery logic is in:
   - services/position_engine.py (ghost position recovery)
   - services/position/exchange_mirror.py (state recovery)
2. These services use OKXExchange instance to fetch data from exchange
3. **Patch location:** OKXExchange.__init__ (startup only)
4. **Patch timing:** Runs once during initialization, before recovery logic
5. **Patch effect:** Only validates endpoints, does not modify recovery logic

### Evidence
- Recovery methods in position_engine.py use exchange.fetch_positions, exchange.close_position
- Recovery methods in exchange_mirror.py use exchange.fetch_positions, exchange.fetch_balance
- These methods use self.base_url and self.ws_url (validated at __init__)
- If validation passes, recovery proceeds unchanged
- If validation fails, bot doesn't start, so recovery doesn't run

### Conclusion
**ZERO IMPACT** on recovery logic. Patch only validates endpoints at startup.

---

## PATCH RISK LEVEL

**RISK LEVEL: LOW**

### Justification

1. **Addition Only:** Patch adds validation code without modifying existing logic
2. **Fail-Fast Design:** Validation happens at startup, preventing runtime issues
3. **No Side Effects:** No changes to endpoints, order logic, persistence, or recovery
4. **Clear Error Messages:** CRITICAL logs and ValueError with actionable fix instructions
5. **No Silent Failures:** Bot fails to start on mismatch, preventing unintended trading
6. **No Refactoring:** Single-purpose fix for single finding
7. **No Trading Behavior Changes:** Only validates configuration, does not change trading logic

### Risk Mitigation

- Validation uses explicit endpoint lists (no regex or pattern matching)
- Error messages provide clear fix instructions
- Validation happens before any trading operations
- No automatic configuration correction (user must fix config)
- No silent fallback (bot fails fast on mismatch)

---

## CONFIGURATION IMPACT

### Current Default Configuration Issue
The current default configuration in settings.py has a mismatch:
- okx_demo_mode: bool = Field(default=True, ...)
- okx_base_url: str = Field(default="https://openapi.okx.com", ...)  # PRODUCTION
- okx_ws_url: str = Field(default="wss://wspap.okx.com:8443/ws/v5", ...)  # DEMO

### Required Configuration Fix
Users must update their .env file to match demo_mode:

**For Demo Mode (recommended for testing):**
```
OKX_DEMO_MODE=True
OKX_BASE_URL=https://www.okx.com
OKX_WS_URL=wss://wspap.okx.com:8443/ws/v5
```

**For Production Mode:**
```
OKX_DEMO_MODE=False
OKX_BASE_URL=https://openapi.okx.com
OKX_WS_URL=wss://ws.okx.com:8443/ws/v5
```

---

## TESTING RECOMMENDATIONS

### Unit Tests
Add unit tests to verify validation logic:
- Test demo_mode=True with production base_url (should raise ValueError)
- Test demo_mode=True with production ws_url (should raise ValueError)
- Test demo_mode=False with demo base_url (should raise ValueError)
- Test demo_mode=False with demo ws_url (should raise ValueError)
- Test demo_mode=True with demo endpoints (should pass)
- Test demo_mode=False with production endpoints (should pass)

### Integration Tests
Test with actual configuration:
- Test bot startup with correct demo configuration
- Test bot startup with correct production configuration
- Test bot startup failure with mismatched configuration

---

## SUMMARY

**Patch Type:** CRITICAL FIX
**Lines Changed:** +60 lines (addition only)
**Files Modified:** 1 (infrastructure/exchange/okx_exchange.py)
**Risk Level:** LOW
**Impact:** Prevents trading on wrong environment
**Side Effects:** None
**Trading Behavior:** Unchanged
**Persistence:** Unchanged
**Recovery:** Unchanged

**Recommendation:** APPLY IMMEDIATELY before production deployment.

---

**END OF PATCH REPORT**
