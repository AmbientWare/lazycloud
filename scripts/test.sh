#!/bin/bash
# Test runner script for LazyCloud API

set -e

echo "🧪 Running LazyCloud API Tests"
echo "=============================="

# Function to run tests with specific options
run_tests() {
    local test_type="$1"
    local options="$2"
    
    echo ""
    echo "📋 Running $test_type tests..."
    echo "----------------------------------------"
    
    uv run pytest src/lazycloud_api/tests/ $options
}

# Parse command line arguments
case "${1:-all}" in
    "parser")
        run_tests "Compose Parser" "src/lazycloud_api/tests/test_compose_parser.py -v"
        ;;
    "generator") 
        run_tests "Helm Generator" "src/lazycloud_api/tests/test_helm_values_generator.py -v"
        ;;
    "integration")
        run_tests "Integration" "src/lazycloud_api/tests/test_integration.py -v"
        ;;
    "coverage")
        run_tests "Coverage" "--cov=src/lazycloud_api/services/compose --cov=src/lazycloud_api/services/k8s --cov-report=term-missing --cov-report=html:htmlcov"
        ;;
    "quick")
        run_tests "Quick" "-x"  # Stop on first failure
        ;;
    "all"|*)
        run_tests "All" "-v"
        ;;
esac

echo ""
echo "✅ Tests completed!"
echo ""
echo "💡 Usage: $0 [parser|generator|integration|coverage|quick|all]"