Equipment renewal decisions. Python 3.11+, standard library only.
Analyze: python3 renewal.py analyze --input fixtures/equipment.json --as-of 2026-11-12 --known-at 2026-11-13
Demo: python3 renewal.py demo --fixture fixtures/equipment.json
Tests: python3 -m unittest discover -s tests -v
Build: python3 -m compileall -q equipment_domain risk_model funding decisions renewal.py
CLI: python3 renewal.py --help
All demonstration inputs are synthetic. Risk indices are ordinal, not failure probabilities.
