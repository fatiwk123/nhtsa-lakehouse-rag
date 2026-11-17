"""Run Soda checks against the DuckDB gold layer."""

from __future__ import annotations

from pathlib import Path

from soda.scan import Scan

QUALITY_DIR = Path(__file__).resolve().parents[1] / "quality"


def run_soda_scan() -> tuple[bool, dict, str]:
    scan = Scan()
    scan.set_data_source_name("lakehouse")
    scan.set_scan_definition_name("nhtsa_gold")
    scan.add_configuration_yaml_file(str(QUALITY_DIR / "configuration.yml"))
    scan.add_sodacl_yaml_file(str(QUALITY_DIR / "checks.yml"))
    scan.execute()
    results = scan.get_scan_results()
    ok = not (scan.has_check_fails() or scan.has_error_logs())
    return ok, results, scan.get_logs_text()
