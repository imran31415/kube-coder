#!/bin/bash
set -e

echo "=== Generating Test Coverage Reports ==="
echo ""

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Function to print section header
print_header() {
    echo -e "${BLUE}=== $1 ===${NC}"
}

# Function to extract coverage percentage from vitest output
extract_vitest_coverage() {
    local output_file="$1"
    # Look for the "All files" line and extract the first percentage
    local coverage_line=$(grep "All files" "$output_file")
    if [ -n "$coverage_line" ]; then
        echo "$coverage_line" | awk -F'|' '{print $2}' | tr -d ' ' | sed 's/%//'
    else
        echo "0"
    fi
}

# Run frontend tests with coverage
print_header "Frontend Coverage (Vitest)"
cd charts/workspace/web
FRONTEND_COVERAGE_OUTPUT=$(mktemp)
yarn test:coverage 2>&1 | tee "$FRONTEND_COVERAGE_OUTPUT" || true
FRONTEND_COVERAGE=$(extract_vitest_coverage "$FRONTEND_COVERAGE_OUTPUT")
rm "$FRONTEND_COVERAGE_OUTPUT"
cd -

echo ""
print_header "Backend Coverage (Python)"
cd charts/workspace
BACKEND_COVERAGE_OUTPUT=$(mktemp)
coverage run -m unittest discover -s tests -p '*_test.py' -v > /dev/null 2>&1
coverage report | tee "$BACKEND_COVERAGE_OUTPUT"
BACKEND_COVERAGE=$(grep "TOTAL" "$BACKEND_COVERAGE_OUTPUT" | awk '{print $NF}' | sed 's/%//')
rm "$BACKEND_COVERAGE_OUTPUT"
cd -

echo ""
print_header "Coverage Summary"
echo "Frontend Coverage: ${FRONTEND_COVERAGE}%"
echo "Backend Coverage:  ${BACKEND_COVERAGE}%"

# Convert to integer by removing decimal
FRONTEND_COVERAGE_INT=${FRONTEND_COVERAGE%.*}
BACKEND_COVERAGE_INT=${BACKEND_COVERAGE%.*}

# Calculate weighted average based on lines of code
FRONTEND_LOC=$(find charts/workspace/web/src -name "*.ts" -o -name "*.tsx" | xargs wc -l 2>/dev/null | tail -1 | awk '{print $1}')
BACKEND_LOC=$(find charts/workspace -name "*.py" ! -path "*/tests/*" ! -path "*/__pycache__/*" | xargs wc -l 2>/dev/null | tail -1 | awk '{print $1}')
TOTAL_LOC=$((FRONTEND_LOC + BACKEND_LOC))

if [ $TOTAL_LOC -gt 0 ]; then
    # Calculate percentages using integer arithmetic
    FRONTEND_WEIGHT=$((FRONTEND_LOC * 100 / TOTAL_LOC))
    BACKEND_WEIGHT=$((BACKEND_LOC * 100 / TOTAL_LOC))
    
    # Calculate overall coverage using integer arithmetic
    FRONTEND_CONTRIBUTION=$((FRONTEND_COVERAGE_INT * FRONTEND_LOC / 100))
    BACKEND_CONTRIBUTION=$((BACKEND_COVERAGE_INT * BACKEND_LOC / 100))
    OVERALL_COVERAGE=$(((FRONTEND_CONTRIBUTION + BACKEND_CONTRIBUTION) * 100 / TOTAL_LOC))
    
    echo ""
    echo "Lines of Code:"
    echo "  Frontend: $FRONTEND_LOC lines ($FRONTEND_WEIGHT%)"
    echo "  Backend:  $BACKEND_LOC lines ($BACKEND_WEIGHT%)"
    echo "  Total:    $TOTAL_LOC lines"
    echo ""
    
    # Color code the overall coverage
    if [ $OVERALL_COVERAGE -ge 80 ]; then
        echo -e "${GREEN}Overall Coverage: ${OVERALL_COVERAGE}%${NC}"
    elif [ $OVERALL_COVERAGE -ge 60 ]; then
        echo -e "${YELLOW}Overall Coverage: ${OVERALL_COVERAGE}%${NC}"
    else
        echo -e "${RED}Overall Coverage: ${OVERALL_COVERAGE}%${NC}"
    fi
else
    echo "Could not calculate overall coverage (no lines of code found)"
fi

echo ""
echo "Detailed reports:"
echo "  Frontend: charts/workspace/web/coverage/index.html"
echo "  Backend:  charts/workspace/htmlcov/index.html"