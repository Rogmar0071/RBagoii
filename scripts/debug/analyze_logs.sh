#!/usr/bin/env bash
#
# Quick Log Analyzer - Extract and correlate recent errors
#
# Usage:
#   ./scripts/debug/analyze_logs.sh
#   ./scripts/debug/analyze_logs.sh --since "1 hour ago"
#   ./scripts/debug/analyze_logs.sh --errors-only

set -euo pipefail

# Configuration
LOG_PATHS=(
    "backend/logs/*.log"
    "/var/log/app/*.log"
    "logs/*.log"
    "/tmp/*.log"
)

SINCE="24 hours ago"
ERRORS_ONLY=false
VERBOSE=false

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --since)
            SINCE="$2"
            shift 2
            ;;
        --errors-only)
            ERRORS_ONLY=true
            shift
            ;;
        --verbose)
            VERBOSE=true
            shift
            ;;
        -h|--help)
            echo "Usage: $0 [OPTIONS]"
            echo ""
            echo "Options:"
            echo "  --since TIME       Show logs since TIME (default: 24 hours ago)"
            echo "  --errors-only      Only show ERROR level logs"
            echo "  --verbose          Show detailed output"
            echo "  -h, --help         Show this help"
            exit 0
            ;;
        *)
            echo "Unknown option: $1"
            exit 1
            ;;
    esac
done

echo "==================================================="
echo "RBagoii Log Analyzer"
echo "==================================================="
echo "Time range: Since $SINCE"
echo "Errors only: $ERRORS_ONLY"
echo ""

# Find log files
echo "📁 Searching for log files..."
FOUND_LOGS=()
for pattern in "${LOG_PATHS[@]}"; do
    # shellcheck disable=SC2086
    for file in $pattern 2>/dev/null; do
        if [[ -f "$file" ]]; then
            FOUND_LOGS+=("$file")
        fi
    done
done

if [[ ${#FOUND_LOGS[@]} -eq 0 ]]; then
    echo "⚠️  No log files found in standard locations"
    echo ""
    echo "Checking Docker containers..."
    if command -v docker &> /dev/null; then
        CONTAINERS=$(docker ps --format "{{.Names}}" 2>/dev/null || true)
        if [[ -n "$CONTAINERS" ]]; then
            echo "Found running containers:"
            echo "$CONTAINERS" | while read -r container; do
                echo "  - $container"
                echo "    To view logs: docker logs $container"
            done
        else
            echo "No running Docker containers"
        fi
    fi
    exit 1
fi

echo "Found ${#FOUND_LOGS[@]} log file(s):"
for log in "${FOUND_LOGS[@]}"; do
    echo "  - $log"
done
echo ""

# Analyze each log file
echo "==================================================="
echo "📊 Log Analysis"
echo "==================================================="

for log_file in "${FOUND_LOGS[@]}"; do
    echo ""
    echo "--- $log_file ---"
    
    # Get recent timestamp (Linux date)
    SINCE_TIMESTAMP=$(date -d "$SINCE" "+%Y-%m-%d %H:%M:%S" 2>/dev/null || \
                      date -v-24H "+%Y-%m-%d %H:%M:%S" 2>/dev/null || \
                      echo "")
    
    if [[ $ERRORS_ONLY == true ]]; then
        # Show only errors
        ERRORS=$(grep -i "error\|exception\|critical\|fatal" "$log_file" || true)
        if [[ -n "$ERRORS" ]]; then
            echo "$ERRORS" | tail -20
        else
            echo "✅ No errors found"
        fi
    else
        # Show all recent logs
        tail -50 "$log_file"
    fi
done

echo ""
echo "==================================================="
echo "🔍 Pattern Analysis"
echo "==================================================="

# Combine all logs for pattern detection
TEMP_LOG=$(mktemp)
trap 'rm -f "$TEMP_LOG"' EXIT

for log_file in "${FOUND_LOGS[@]}"; do
    cat "$log_file" >> "$TEMP_LOG" 2>/dev/null || true
done

# Count error types
echo ""
echo "Top error patterns:"
grep -ioh "error.*" "$TEMP_LOG" 2>/dev/null | \
    cut -d' ' -f1-5 | \
    sort | uniq -c | sort -rn | head -10 || \
    echo "No error patterns detected"

# Count exceptions
echo ""
echo "Exception types:"
grep -ioh "[a-zA-Z]*Exception\|[a-zA-Z]*Error" "$TEMP_LOG" 2>/dev/null | \
    sort | uniq -c | sort -rn | head -10 || \
    echo "No exceptions detected"

# HTTP error codes
echo ""
echo "HTTP error codes:"
grep -oh "HTTP/[0-9.]* [45][0-9][0-9]" "$TEMP_LOG" 2>/dev/null | \
    cut -d' ' -f2 | \
    sort | uniq -c | sort -rn || \
    echo "No HTTP errors detected"

echo ""
echo "==================================================="
echo "💡 Next Steps"
echo "==================================================="
echo ""
echo "1. Review error patterns above"
echo "2. Check timestamps for correlation"
echo "3. Cross-reference with git log:"
echo "   git log --oneline --since='$SINCE'"
echo ""
echo "4. For Docker logs:"
for log_file in "${FOUND_LOGS[@]}"; do
    if [[ "$log_file" == *"docker"* ]]; then
        container=$(basename "$log_file" .log)
        echo "   docker logs --tail 100 $container"
    fi
done
echo ""
echo "5. For more details, run with --verbose"
echo ""
