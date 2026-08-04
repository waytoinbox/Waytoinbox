def get_txt_record(domain):
    try:
        import dns.resolver
        answers = dns.resolver.resolve(domain, "TXT")
        return [
            "".join(part.decode() if isinstance(part, bytes) else part for part in rdata.strings)
            for rdata in answers
        ]
    except Exception as e:
        err = str(e)
        if "NXDOMAIN" in err or "does not exist" in err.lower():
            return [f"Error: NXDOMAIN — '{domain}' does not exist in DNS"]
        if "NoAnswer" in err or "no answer" in err.lower():
            return []   # domain exists but no TXT records
        return [f"Error: {e}"]


def _get_dkim_txt(dkim_domain):
    """TXT lookup with explicit CNAME follow (needed for SES Easy DKIM)."""
    import dns.resolver
    records = get_txt_record(dkim_domain)
    if records:
        return records, None
    # Domain exists but returned no TXT — check if there's a CNAME and follow it
    try:
        cname_answers = dns.resolver.resolve(dkim_domain, "CNAME")
        for rdata in cname_answers:
            cname_target = str(rdata.target).rstrip(".")
            cname_records = get_txt_record(cname_target)
            if cname_records:
                return cname_records, cname_target
    except Exception:
        pass
    return [], None


def check_spf(domain):
    records = get_txt_record(domain)
    spf = [r for r in records if r.startswith("v=spf1")]
    if not spf:
        return {"status": "fail", "reason": "No SPF record found"}
    record = spf[0]
    return {
        "status": "pass",
        "record": record,
        "includes": [part for part in record.split() if part.startswith("include:")],
        "all_mechanism": [part for part in record.split() if part in ["-all", "~all", "?all", "+all"]],
    }


def check_dmarc(domain):
    records = get_txt_record(f"_dmarc.{domain}")
    dmarc = [r for r in records if r.startswith("v=DMARC1")]
    if not dmarc:
        return {"status": "fail", "reason": "No DMARC record found"}
    record = dmarc[0]
    tags = dict(tag.split("=", 1) for tag in record.split(";") if "=" in tag)
    return {
        "status": "pass",
        "record": record,
        "policy": tags.get("p", "none"),
        "subdomain_policy": tags.get("sp", "inherit"),
        "reporting": {"rua": tags.get("rua"), "ruf": tags.get("ruf")},
    }


def _dkim_key_bits(b64_key):
    try:
        import base64 as _b64
        from cryptography.hazmat.primitives.serialization import load_der_public_key
        der = _b64.b64decode(b64_key + "==")
        return load_der_public_key(der).key_size
    except Exception:
        pass
    try:
        pad = b64_key.rstrip("=")
        bits = (len(pad) * 6) - (len(pad) % 4)
        if bits > 3500: return 4096
        if bits > 1800: return 2048
        if bits > 900:  return 1024
        return 512
    except Exception:
        return None


def check_dkim(domain, selector="default"):
    lookup_name = f"{selector}._domainkey.{domain}"
    records, cname_target = _get_dkim_txt(lookup_name)
    if records and records[0].startswith("Error:"):
        return {
            "status":   "fail",
            "selector": selector,
            "reason":   f"DNS lookup failed for '{lookup_name}' — {records[0]}",
        }
    def _is_dkim(r):
        u = r.upper()
        return "V=DKIM1" in u or (("K=RSA" in u or "K=ED25519" in u) and "P=" in u)

    dkim = [r for r in records if _is_dkim(r)]
    if not dkim:
        if not records:
            hint = (
                f"No TXT or CNAME record found at '{lookup_name}'. "
                "Verify the selector name in your DNS panel or email provider dashboard."
            )
        else:
            hint = (
                f"TXT record exists at '{lookup_name}' but does not contain 'v=DKIM1'. "
                f"Raw: {records[0][:120]}"
            )
        return {"status": "fail", "selector": selector, "reason": hint}
    record = dkim[0]
    tags = dict(tag.strip().split("=", 1) for tag in record.split(";") if "=" in tag)
    p_val = tags.get("p", "")
    return {
        "status":             "pass",
        "selector":           selector,
        "record":             record,
        "key_type":           tags.get("k", "rsa"),
        "hash_algorithm":     tags.get("h", "sha256"),
        "public_key_present": bool(p_val),
        "key_bits":           _dkim_key_bits(p_val) if p_val else None,
    }


def _detect_esp_selectors(domain):
    import dns.resolver
    from Email_validate_app.services.dkim_config import COMMON_SELECTORS, ESP_SELECTOR_MAP
    candidates = []
    seen = set()

    def _add(sels):
        for s in sels:
            if s not in seen:
                seen.add(s)
                candidates.append(s)

    try:
        mx_answers = dns.resolver.resolve(domain, "MX")
        for mx in mx_answers:
            host = str(mx.exchange).rstrip(".").lower()
            for fingerprint, selectors in ESP_SELECTOR_MAP.items():
                if fingerprint in host:
                    _add(selectors)
    except Exception:
        pass

    spf = check_spf(domain)
    if spf.get("status") == "pass":
        for inc in spf.get("includes", []):
            inc_domain = inc.replace("include:", "").strip()
            for fingerprint, selectors in ESP_SELECTOR_MAP.items():
                if fingerprint in inc_domain:
                    _add(selectors)

    for s in COMMON_SELECTORS:
        if s not in seen:
            seen.add(s)
            candidates.append(s)

    return candidates


def check_dkim_auto(domain):
    selectors_to_try = _detect_esp_selectors(domain)
    for sel in selectors_to_try:
        result = check_dkim(domain, sel)
        if result["status"] == "pass":
            return result
    tried_preview = ", ".join(selectors_to_try[:12])
    if len(selectors_to_try) > 12:
        tried_preview += "…"
    return {
        "status":   "fail",
        "selector": "",
        "reason": (
            f"No DKIM record found after checking {len(selectors_to_try)} selector(s): "
            f"{tried_preview}. "
            "DNS cannot enumerate DKIM selectors — enter your selector manually to check it directly."
        ),
    }
