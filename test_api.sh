#!/usr/bin/env bash
set -euo pipefail

TOKEN="eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJhYmYwZWEyNC1jMDgyLTQzNjEtYWQwZC04Y2FmOTBmZTg4OWMiLCJyb2xlIjoiYWRtaW4iLCJlbWFpbCI6ImFkbWluQGNvbXBhbnkuY29tIiwiZXhwIjoxNzcyOTM3MjY4fQ.cbyRIUi30k_05B09b8uHl7EmXyf_AVMmq-Vb0yZ77hw"
APIKEY="pod_bPsyRLVye83hl4hqr4RmayvbKycbSLJ7ljWR3016xAA"
BASE="http://localhost:8000"

pass=0; fail=0

check() {
  local label="$1" expected="$2" got="$3"
  if echo "$got" | grep -qE "$expected"; then
    echo "  PASS  $label"
    ((pass++)) || true
  else
    echo "  FAIL  $label"
    echo "        expected pattern: $expected"
    echo "        got: $(echo "$got" | head -c 200)"
    ((fail++)) || true
  fi
}

# Helper: returns "BODY\nHTTP_CODE"
req() {
  curl -s -o /tmp/resp_body -w "%{http_code}" "$@"
}

echo ""
echo "============================================================"
echo " SECTION 1: API KEY MANAGEMENT"
echo "============================================================"

echo ""
echo "--- 1.1 List API keys (admin) ---"
CODE=$(req "$BASE/api/auth/apikeys" -H "Authorization: Bearer $TOKEN")
BODY=$(cat /tmp/resp_body)
check "HTTP 200" "200" "$CODE"
check "key_prefix present" "pod_bPsyRLVy" "$BODY"
# Raw key must never be in list
check "raw key NOT exposed in list" "OK" "$(echo "$BODY" | python3 -c 'import sys,json; d=json.load(sys.stdin); print("OK" if not any("api_key" in k for item in d for k in item) else "EXPOSED")' 2>/dev/null || echo OK)" 

echo ""
echo "--- 1.2 Create a second key (for revoke test) ---"
CODE=$(req -X POST "$BASE/api/auth/apikeys" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name":"key-to-revoke"}')
BODY=$(cat /tmp/resp_body)
check "HTTP 201" "201" "$CODE"
check "contains api_key" "api_key" "$BODY"
KEY2_ID=$(echo "$BODY" | python3 -c 'import sys,json; print(json.load(sys.stdin)["id"])')
KEY2_RAW=$(echo "$BODY" | python3 -c 'import sys,json; print(json.load(sys.stdin)["api_key"])')
echo "        key2 id: $KEY2_ID"

echo ""
echo "--- 1.3 Invalid JWT cannot create keys ---"
CODE=$(req -X POST "$BASE/api/auth/apikeys" \
  -H "Authorization: Bearer INVALIDTOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name":"hack"}')
check "401 with bad JWT" "401" "$CODE"

echo ""
echo "--- 1.4 Revoke second key ---"
CODE=$(req -X DELETE "$BASE/api/auth/apikeys/$KEY2_ID" \
  -H "Authorization: Bearer $TOKEN")
check "HTTP 204 on revoke" "204" "$CODE"

echo ""
echo "--- 1.5 Revoked key is rejected ---"
CODE=$(req "$BASE/api/v1/documents/lookup?delivery_number=DEL-2024-0001" \
  -H "X-API-Key: $KEY2_RAW")
BODY=$(cat /tmp/resp_body)
check "401 after revoke" "401" "$CODE"
check "Invalid/inactive message" "Invalid|inactive" "$BODY"

echo ""
echo "--- 1.6 Delete non-existent key ---"
CODE=$(req -X DELETE "$BASE/api/auth/apikeys/00000000-0000-0000-0000-000000000000" \
  -H "Authorization: Bearer $TOKEN")
check "404 for unknown key" "404" "$CODE"

echo ""
echo "--- 1.7 Create key with missing name field ---"
CODE=$(req -X POST "$BASE/api/auth/apikeys" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{}')
check "422 validation error" "422" "$CODE"

echo ""
echo "============================================================"
echo " SECTION 2: LOOKUP — AUTHENTICATION"
echo "============================================================"

echo ""
echo "--- 2.1 No API key ---"
CODE=$(req "$BASE/api/v1/documents/lookup?delivery_number=DEL-2024-0001")
BODY=$(cat /tmp/resp_body)
check "HTTP 401" "401" "$CODE"
check "Error message clear" "API key required" "$BODY"

echo ""
echo "--- 2.2 Invalid API key ---"
CODE=$(req "$BASE/api/v1/documents/lookup?delivery_number=DEL-2024-0001" \
  -H "X-API-Key: pod_thisisnotavalidkey")
BODY=$(cat /tmp/resp_body)
check "HTTP 401" "401" "$CODE"
check "Invalid key message" "Invalid" "$BODY"

echo ""
echo "--- 2.3 JWT token rejected as API key ---"
CODE=$(req "$BASE/api/v1/documents/lookup?delivery_number=DEL-2024-0001" \
  -H "X-API-Key: $TOKEN")
check "JWT rejected (401)" "401" "$CODE"

echo ""
echo "============================================================"
echo " SECTION 3: LOOKUP — INPUT VALIDATION"
echo "============================================================"

echo ""
echo "--- 3.1 No query params at all ---"
CODE=$(req "$BASE/api/v1/documents/lookup" -H "X-API-Key: $APIKEY")
BODY=$(cat /tmp/resp_body)
check "HTTP 400" "400" "$CODE"
check "Helpful error" "delivery_number" "$BODY"

echo ""
echo "============================================================"
echo " SECTION 4: LOOKUP — DOCUMENT RESOLUTION"
echo "============================================================"

echo ""
echo "--- DB state ---"
docker exec pod_postgres psql -U pod_user -d pod_system -c \
  "SELECT delivery_number, customer_po, status, filename IS NOT NULL as has_file FROM pod_registry ORDER BY created_at LIMIT 12;" 2>/dev/null || true

echo ""
echo "--- 4.1 delivery_number with have_pod ---"
CODE=$(req "$BASE/api/v1/documents/lookup?delivery_number=DEL-2024-0001" \
  -H "X-API-Key: $APIKEY")
BODY=$(cat /tmp/resp_body)
check "HTTP 200" "200" "$CODE"
check "delivery_number echoed" "DEL-2024-0001" "$BODY"
check "pod.status=have_pod" "have_pod" "$BODY"
check "pod.available=true" '"available":true' "$BODY"
check "download_url present" "download_url" "$BODY"
check "pod section exists" '"pod"' "$BODY"
check "packing_slip section exists" '"packing_slip"' "$BODY"
check "invoice section exists" '"invoice"' "$BODY"

echo ""
echo "--- 4.2 delivery_number with pending status ---"
CODE=$(req "$BASE/api/v1/documents/lookup?delivery_number=DEL-2024-0004" \
  -H "X-API-Key: $APIKEY")
BODY=$(cat /tmp/resp_body)
check "HTTP 200" "200" "$CODE"
check "pod.available=false" '"available":false' "$BODY"
check "download_url is null" '"download_url":null' "$BODY"

echo ""
echo "--- 4.3 delivery_number with requested status ---"
CODE=$(req "$BASE/api/v1/documents/lookup?delivery_number=DEL-2024-0006" \
  -H "X-API-Key: $APIKEY")
BODY=$(cat /tmp/resp_body)
check "HTTP 200" "200" "$CODE"
check "status=requested" "requested" "$BODY"

echo ""
echo "--- 4.4 Delivery number not in registry ---"
CODE=$(req "$BASE/api/v1/documents/lookup?delivery_number=DOES-NOT-EXIST-99999" \
  -H "X-API-Key: $APIKEY")
BODY=$(cat /tmp/resp_body)
check "HTTP 200 (graceful)" "200" "$CODE"
check "pod.status=not_found" "not_found" "$BODY"
check "pod.available=false" '"available":false' "$BODY"

echo ""
echo "--- 4.5 Lookup by customer_po ---"
CUSTOMER_PO=$(docker exec pod_postgres psql -U pod_user -d pod_system -t -c \
  "SELECT customer_po FROM pod_registry WHERE customer_po IS NOT NULL LIMIT 1;" 2>/dev/null | tr -d ' \n')
if [ -n "$CUSTOMER_PO" ]; then
  echo "        using customer_po: $CUSTOMER_PO"
  CODE=$(req "$BASE/api/v1/documents/lookup?customer_po=$CUSTOMER_PO" \
    -H "X-API-Key: $APIKEY")
  BODY=$(cat /tmp/resp_body)
  check "HTTP 200" "200" "$CODE"
  check "customer_po echoed" "$CUSTOMER_PO" "$BODY"
else
  echo "  SKIP  No customer_po in test data"
fi

echo ""
echo "--- 4.6 Lookup by order_number ---"
ORDER_NUM=$(docker exec pod_postgres psql -U pod_user -d pod_system -t -c \
  "SELECT customer_order_number FROM orders LIMIT 1;" 2>/dev/null | tr -d ' \n')
if [ -n "$ORDER_NUM" ]; then
  echo "        using order_number: $ORDER_NUM"
  CODE=$(req "$BASE/api/v1/documents/lookup?order_number=$ORDER_NUM" \
    -H "X-API-Key: $APIKEY")
  BODY=$(cat /tmp/resp_body)
  check "HTTP 200" "200" "$CODE"
else
  echo "  SKIP  No orders in DB"
fi

echo ""
echo "--- 4.7 request_if_missing=true on pending entry ---"
# DEL-2024-0004 should now be pending (reset to verify)
docker exec pod_postgres psql -U pod_user -d pod_system -c \
  "UPDATE pod_registry SET status='pending' WHERE delivery_number='DEL-2024-0004';" > /dev/null 2>&1 || true

CODE=$(req "$BASE/api/v1/documents/lookup?delivery_number=DEL-2024-0004&request_if_missing=true" \
  -H "X-API-Key: $APIKEY")
BODY=$(cat /tmp/resp_body)
check "HTTP 200" "200" "$CODE"
check "requested_now=true" '"requested_now":true' "$BODY"

DB_STATUS=$(docker exec pod_postgres psql -U pod_user -d pod_system -t -c \
  "SELECT status FROM pod_registry WHERE delivery_number='DEL-2024-0004';" 2>/dev/null | tr -d ' \n')
echo "        DB status after: $DB_STATUS"
check "DB status changed to requested" "requested" "$DB_STATUS"

echo ""
echo "--- 4.8 request_if_missing=true on have_pod (no re-request) ---"
CODE=$(req "$BASE/api/v1/documents/lookup?delivery_number=DEL-2024-0001&request_if_missing=true" \
  -H "X-API-Key: $APIKEY")
BODY=$(cat /tmp/resp_body)
check "requested_now=false for have_pod" '"requested_now":false' "$BODY"

echo ""
echo "--- 4.9 request_if_missing=true on already-requested (no re-request) ---"
CODE=$(req "$BASE/api/v1/documents/lookup?delivery_number=DEL-2024-0006&request_if_missing=true" \
  -H "X-API-Key: $APIKEY")
BODY=$(cat /tmp/resp_body)
check "requested_now=false for already-requested" '"requested_now":false' "$BODY"

echo ""
echo "--- 4.10 request_if_missing=true on not-found entry ---"
CODE=$(req "$BASE/api/v1/documents/lookup?delivery_number=DOES-NOT-EXIST-99999&request_if_missing=true" \
  -H "X-API-Key: $APIKEY")
BODY=$(cat /tmp/resp_body)
check "HTTP 200 (no crash)" "200" "$CODE"
check "requested_now=false (no delivery_number to request)" '"requested_now":false' "$BODY"

echo ""
echo "============================================================"
echo " SECTION 5: FILE DOWNLOADS"
echo "============================================================"

VALID_FILE=$(docker exec pod_postgres psql -U pod_user -d pod_system -t -c \
  "SELECT filename FROM pod_registry WHERE status='have_pod' AND filename IS NOT NULL LIMIT 1;" 2>/dev/null | tr -d ' \n')
echo ""
echo "        POD filename from DB: $VALID_FILE"

echo ""
echo "--- 5.1 Download POD (valid filename per DB, may not be on disk in test env) ---"
if [ -n "$VALID_FILE" ]; then
  CODE=$(req "$BASE/api/v1/documents/download/pod/$VALID_FILE" \
    -H "X-API-Key: $APIKEY")
  echo "        HTTP $CODE (200=served, 404=not on disk — both acceptable in test env)"
  check "200 or 404 (not 500/401)" "^(200|404)$" "$CODE"
else
  echo "  SKIP  No have_pod filenames in DB"
fi

echo ""
echo "--- 5.2 Download POD — non-existent file ---"
CODE=$(req "$BASE/api/v1/documents/download/pod/completely_fake_file_xyz.pdf" \
  -H "X-API-Key: $APIKEY")
BODY=$(cat /tmp/resp_body)
check "HTTP 404" "404" "$CODE"
check "404 error message" "not found" "$BODY"

echo ""
echo "--- 5.3 Path traversal — URL-encoded slash ---"
CODE=$(req "$BASE/api/v1/documents/download/pod/..%2F..%2Fetc%2Fpasswd" \
  -H "X-API-Key: $APIKEY")
check "Blocked (400 or 404)" "^(400|404|422)$" "$CODE"

echo ""
echo "--- 5.4 Path traversal — double-dot in name ---"
CODE=$(req "$BASE/api/v1/documents/download/pod/../../../etc/passwd" \
  -H "X-API-Key: $APIKEY")
BODY=$(cat /tmp/resp_body)
check "Blocked (400 or 404 or 422)" "400|404|422" "$CODE"

echo ""
echo "--- 5.5 Download POD without API key ---"
CODE=$(req "$BASE/api/v1/documents/download/pod/somefile.pdf")
check "HTTP 401" "401" "$CODE"

echo ""
echo "--- 5.6 Download packing-slip without API key ---"
CODE=$(req "$BASE/api/v1/documents/download/packing-slip/somefile.pdf")
check "HTTP 401" "401" "$CODE"

echo ""
echo "--- 5.7 Download invoice without API key ---"
CODE=$(req "$BASE/api/v1/documents/download/invoice/somefile.pdf")
check "HTTP 401" "401" "$CODE"

echo ""
echo "--- 5.8 Download packing-slip — non-existent ---"
CODE=$(req "$BASE/api/v1/documents/download/packing-slip/fakefile.pdf" \
  -H "X-API-Key: $APIKEY")
check "HTTP 404" "404" "$CODE"

echo ""
echo "--- 5.9 Download invoice — non-existent ---"
CODE=$(req "$BASE/api/v1/documents/download/invoice/fakefile.pdf" \
  -H "X-API-Key: $APIKEY")
check "HTTP 404" "404" "$CODE"

echo ""
echo "--- 5.10 Path traversal on packing-slip ---"
CODE=$(req "$BASE/api/v1/documents/download/packing-slip/..%2F..%2Fetc%2Fpasswd" \
  -H "X-API-Key: $APIKEY")
check "Blocked (400 or 404)" "400|404|422" "$CODE"

echo ""
echo "============================================================"
echo " SECTION 6: last_used_at TRACKING"
echo "============================================================"

echo ""
echo "--- 6.1 last_used_at updated after use ---"
BEFORE=$(docker exec pod_postgres psql -U pod_user -d pod_system -t -c \
  "SELECT last_used_at FROM api_keys WHERE key_prefix='pod_bPsyRLVy';" 2>/dev/null | tr -d ' \n')
echo "        before: $BEFORE"

req "$BASE/api/v1/documents/lookup?delivery_number=DEL-2024-0001" \
  -H "X-API-Key: $APIKEY" > /dev/null

sleep 1

AFTER=$(docker exec pod_postgres psql -U pod_user -d pod_system -t -c \
  "SELECT last_used_at FROM api_keys WHERE key_prefix='pod_bPsyRLVy';" 2>/dev/null | tr -d ' \n')
echo "        after: $AFTER"

if [ -n "$AFTER" ]; then
  check "last_used_at is populated" "2026" "$AFTER"
else
  echo "  FAIL  last_used_at is NULL"
  ((fail++)) || true
fi

echo ""
echo "============================================================"
echo " SECTION 7: API KEY SECURITY CHECKS"
echo "============================================================"

echo ""
echo "--- 7.1 Key hash not exposed in list endpoint ---"
CODE=$(req "$BASE/api/auth/apikeys" -H "Authorization: Bearer $TOKEN")
BODY=$(cat /tmp/resp_body)
check "key_hash not in response" "OK" "$(echo "$BODY" | python3 -c 'import sys,json; d=json.load(sys.stdin); print("OK" if not any("key_hash" in k for item in d for k in item) else "EXPOSED")' 2>/dev/null || echo OK)" 

echo ""
echo "--- 7.2 api_key (raw) not in list endpoint ---"
check "api_key field not in list" "OK" "$(echo "$BODY" | python3 -c 'import sys,json; d=json.load(sys.stdin); print("OK" if not any("api_key" in k for item in d for k in item) else "EXPOSED")' 2>/dev/null || echo OK)"

echo ""
echo "--- 7.3 key_prefix exposed in list (for identification) ---"
check "key_prefix present" "pod_" "$BODY"

echo ""
echo "============================================================"
echo " SECTION 8: OPENAPI / DOCS"
echo "============================================================"

echo ""
echo "--- 8.1 /docs reachable ---"
CODE=$(req "$BASE/docs")
check "HTTP 200" "200" "$CODE"

echo ""
echo "--- 8.2 External API appears in OpenAPI schema ---"
CODE=$(req "$BASE/openapi.json")
BODY=$(cat /tmp/resp_body)
check "HTTP 200" "200" "$CODE"
check "v1/documents in schema" "v1/documents" "$BODY"
check "External API v1 tag" "External API v1" "$BODY"

echo ""
echo "============================================================"
echo " FINAL RESULTS"
echo "============================================================"
echo "  Passed: $pass"
echo "  Failed: $fail"
echo "  Total:  $((pass+fail))"
echo ""
if [ "$fail" -eq 0 ]; then
  echo "  ALL TESTS PASSED"
else
  echo "  $fail TEST(S) FAILED"
fi
