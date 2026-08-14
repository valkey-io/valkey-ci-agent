"""Parse CVE scanner JSON output into structured Finding objects.

Supports Trivy output format. All functions are pure (no I/O,
no side effects) and deterministic. Validation is strict: malformed
scanner output raises ParseError instead of silently yielding an
empty (fake all-clear) result. A document without 'Results' is a
valid clean scan (Trivy omits the key when nothing is found).
"""

from __future__ import annotations

from typing import Any

from scripts.cve_scan.models import Finding, Severity

#: Keys every Trivy vulnerability entry must carry as non-null strings.
_REQUIRED_VULN_KEYS = ("VulnerabilityID", "PkgName", "InstalledVersion", "Severity")


class ParseError(Exception):
    """Raised when scanner JSON output does not match the expected schema."""


def parse_trivy(json_obj: dict[str, Any], image: str, platform: str = "") -> list[Finding]:
    """Parse Trivy JSON output into Finding objects, validating the schema strictly.

    Reads Results[].Vulnerabilities[] (VulnerabilityID, PkgName,
    InstalledVersion, FixedVersion, Severity); FixedVersion may be
    absent or empty (mapped to None). 'Results' absent or an empty
    list is a valid clean scan and returns [].

    Raises:
        ParseError: If the document is not a dict with an integer
            'SchemaVersion', if 'Results' is present but not a list of
            dicts, if 'Vulnerabilities' is present but not a list of
            dicts, or if a vulnerability entry is missing (or mistypes)
            a required key.
    """
    if not isinstance(json_obj, dict):
        raise ParseError(
            f"Trivy output for {image}: top-level must be a JSON object, "
            f"got {type(json_obj).__name__}."
        )

    schema_version = json_obj.get("SchemaVersion")
    if isinstance(schema_version, bool) or not isinstance(schema_version, int):
        raise ParseError(
            f"Trivy output for {image}: missing or non-integer 'SchemaVersion' "
            f"(got {schema_version!r})."
        )

    findings: list[Finding] = []
    if "Results" not in json_obj:
        # Trivy omits 'Results' entirely on a clean scan.
        return findings

    results = json_obj["Results"]
    if not isinstance(results, list):
        raise ParseError(
            f"Trivy output for {image}: 'Results' must be a list, "
            f"got {type(results).__name__}."
        )

    for r_idx, result in enumerate(results):
        if not isinstance(result, dict):
            raise ParseError(
                f"Trivy output for {image}: Results[{r_idx}] must be an object, "
                f"got {type(result).__name__}."
            )

        vulns = result.get("Vulnerabilities")
        if vulns is None:
            # Absent or null: a result target with no vulnerabilities.
            continue
        if not isinstance(vulns, list):
            raise ParseError(
                f"Trivy output for {image}: Results[{r_idx}].Vulnerabilities "
                f"must be a list, got {type(vulns).__name__}."
            )

        for v_idx, vuln in enumerate(vulns):
            context = f"Results[{r_idx}].Vulnerabilities[{v_idx}]"
            if not isinstance(vuln, dict):
                raise ParseError(
                    f"Trivy output for {image}: {context} must be an object, "
                    f"got {type(vuln).__name__}."
                )

            for key in _REQUIRED_VULN_KEYS:
                value = vuln.get(key)
                if not isinstance(value, str):
                    raise ParseError(
                        f"Trivy output for {image}: {context} is missing "
                        f"required key {key!r} or it is not a string "
                        f"(got {value!r})."
                    )

            try:
                severity = Severity.from_str(vuln["Severity"])
            except ValueError as exc:
                raise ParseError(
                    f"Trivy output for {image}: {context}: {exc}"
                ) from exc

            fixed = vuln.get("FixedVersion", "")
            findings.append(
                Finding(
                    image=image,
                    package=vuln["PkgName"],
                    installed_version=vuln["InstalledVersion"],
                    cve_id=vuln["VulnerabilityID"],
                    severity=severity,
                    fixed_version=fixed if fixed else None,
                    platform=platform,
                )
            )
    return findings


def parse_findings(scanner: str, json_obj: dict[str, Any], image: str, platform: str = "") -> list[Finding]:
    """Dispatch to the correct parser based on scanner name; raises ValueError if unrecognized."""
    if scanner == "trivy":
        return parse_trivy(json_obj, image, platform=platform)
    raise ValueError(f"Unsupported scanner: {scanner!r}. Must be 'trivy'.")


def filter_by_threshold(findings: list[Finding], threshold: Severity) -> list[Finding]:
    """Return findings at or above the given severity threshold (inclusive)."""
    return [f for f in findings if f.severity >= threshold]
