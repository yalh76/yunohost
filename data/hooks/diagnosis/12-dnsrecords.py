#!/usr/bin/env python

import os
import re

from datetime import datetime, timedelta
from publicsuffix import PublicSuffixList

from moulinette.utils.process import check_output

from yunohost.utils.network import dig
from yunohost.diagnosis import Diagnoser
from yunohost.domain import domain_list, _build_dns_conf, _get_maindomain

YNH_DYNDNS_DOMAINS = ['nohost.me', 'noho.st', 'ynh.fr']


class DNSRecordsDiagnoser(Diagnoser):

    id_ = os.path.splitext(os.path.basename(__file__))[0].split("-")[1]
    cache_duration = 600
    dependencies = ["ip"]

    def run(self):

        main_domain = _get_maindomain()

        all_domains = domain_list()["domains"]
        for domain in all_domains:
            self.logger_debug("Diagnosing DNS conf for %s" % domain)
            is_subdomain = domain.split(".", 1)[1] in all_domains
            for report in self.check_domain(domain, domain == main_domain, is_subdomain=is_subdomain):
                yield report

        # Check if a domain buy by the user will expire soon
        psl = PublicSuffixList()
        domains_from_registrar = [psl.get_public_suffix(domain) for domain in all_domains]
        domains_from_registrar = [domain for domain in domains_from_registrar if "." in domain]
        domains_from_registrar = set(domains_from_registrar) - set(YNH_DYNDNS_DOMAINS + ["netlib.re"])
        for report in self.check_expiration_date(domains_from_registrar):
            yield report

    def check_domain(self, domain, is_main_domain, is_subdomain):

        expected_configuration = _build_dns_conf(domain, include_empty_AAAA_if_no_ipv6=True)

        categories = ["basic", "mail", "xmpp", "extra"]
        # For subdomains, we only diagnosis A and AAAA records
        if is_subdomain:
            categories = ["basic"]

        for category in categories:

            records = expected_configuration[category]
            discrepancies = []
            results = {}

            for r in records:
                id_ = r["type"] + ":" + r["name"]
                r["current"] = self.get_current_record(domain, r["name"], r["type"])
                if r["value"] == "@":
                    r["value"] = domain + "."

                if self.current_record_match_expected(r):
                    results[id_] = "OK"
                else:
                    if r["current"] is None:
                        results[id_] = "MISSING"
                        discrepancies.append(("diagnosis_dns_missing_record", r))
                    else:
                        results[id_] = "WRONG"
                        discrepancies.append(("diagnosis_dns_discrepancy", r))

            def its_important():
                # Every mail DNS records are important for main domain
                # For other domain, we only report it as a warning for now...
                if is_main_domain and category == "mail":
                    return True
                elif category == "basic":
                    # A bad or missing A record is critical ...
                    # And so is a wrong AAAA record
                    # (However, a missing AAAA record is acceptable)
                    if results["A:@"] != "OK" or results["AAAA:@"] == "WRONG":
                        return True

                return False

            if discrepancies:
                status = "ERROR" if its_important() else "WARNING"
                summary = "diagnosis_dns_bad_conf"
            else:
                status = "SUCCESS"
                summary = "diagnosis_dns_good_conf"

            output = dict(meta={"domain": domain, "category": category},
                          data=results,
                          status=status,
                          summary=summary)

            if discrepancies:
                # For ynh-managed domains (nohost.me etc...), tell people to try to "yunohost dyndns update --force"
                if any(domain.endswith(ynh_dyndns_domain) for ynh_dyndns_domain in YNH_DYNDNS_DOMAINS):
                    output["details"] = ["diagnosis_dns_try_dyndns_update_force"]
                # Otherwise point to the documentation
                else:
                    output["details"] = ["diagnosis_dns_point_to_doc"]
                output["details"] += discrepancies

            yield output

    def get_current_record(self, domain, name, type_):

        query = "%s.%s" % (name, domain) if name != "@" else domain
        success, answers = dig(query, type_, resolvers="force_external")

        if success != "ok":
            return None
        else:
            return answers[0] if len(answers) == 1 else answers

    def current_record_match_expected(self, r):
        if r["value"] is not None and r["current"] is None:
            return False
        if r["value"] is None and r["current"] is not None:
            return False
        elif isinstance(r["current"], list):
            return False

        if r["type"] == "TXT":
            # Split expected/current
            #  from  "v=DKIM1; k=rsa; p=hugekey;"
            #  to a set like {'v=DKIM1', 'k=rsa', 'p=...'}
            expected = set(r["value"].strip(';" ').replace(";", " ").split())
            current = set(r["current"].strip(';" ').replace(";", " ").split())

            # For SPF, ignore parts starting by ip4: or ip6:
            if r["name"] == "@":
                current = {part for part in current if not part.startswith("ip4:") and not part.startswith("ip6:")}
            return expected == current
        elif r["type"] == "MX":
            # For MX, we want to ignore the priority
            expected = r["value"].split()[-1]
            current = r["current"].split()[-1]
            return expected == current
        else:
            return r["current"] == r["value"]

    def check_expiration_date(self, domains):
        """
        Alert if expiration date of a domain is soon
        """

        details = {
            "not_found": [],
            "error": [],
            "warning": [],
            "success": []
        }

        for domain in domains:
            expire_date = self.get_domain_expiration(domain)

            if isinstance(expire_date, str):
                status_ns, _ = dig(domain, "NS", resolvers="force_external")
                status_a, _ = dig(domain, "A", resolvers="force_external")
                if "ok" not in [status_ns, status_a]:
                    details["not_found"].append((
                        "diagnosis_domain_%s_details" % (expire_date),
                        {"domain": domain}))
                else:
                    self.logger_debug("Dyndns domain: %s" % (domain))
                continue

            expire_in = expire_date - datetime.now()

            alert_type = "success"
            if expire_in <= timedelta(15):
                alert_type = "error"
            elif expire_in <= timedelta(45):
                alert_type = "warning"

            args = {
                "domain": domain,
                "days": expire_in.days - 1,
                "expire_date": str(expire_date)
            }
            details[alert_type].append(("diagnosis_domain_expires_in", args))

        for alert_type in ["success", "error", "warning", "not_found"]:
            if details[alert_type]:
                if alert_type == "not_found":
                    meta = {"test": "domain_not_found"}
                else:
                    meta = {"test": "domain_expiration"}
                # Allow to ignore specifically a single domain
                if len(details[alert_type]) == 1:
                    meta["domain"] = details[alert_type][0][1]["domain"]
                yield dict(meta=meta,
                           data={},
                           status=alert_type.upper() if alert_type != "not_found" else "WARNING",
                           summary="diagnosis_domain_expiration_" + alert_type,
                           details=details[alert_type])

    def get_domain_expiration(self, domain):
        """
        Return the expiration datetime of a domain or None
        """
        command = "whois -H %s || echo failed" % (domain)
        out = check_output(command).strip().split("\n")

        # Reduce output to determine if whois answer is equivalent to NOT FOUND
        filtered_out = [line for line in out
               if re.search(r'^[a-zA-Z0-9 ]{4,25}:', line, re.IGNORECASE) and
               not re.match(r'>>> Last update of whois', line, re.IGNORECASE) and
               not re.match(r'^NOTICE:', line, re.IGNORECASE) and
               not re.match(r'^%%', line, re.IGNORECASE) and
               not re.match(r'"https?:"', line, re.IGNORECASE)]

        # If there is less than 7 lines, it's NOT FOUND response
        if len(filtered_out) <= 6:
            return "not_found"

        for line in out:
            match = re.search(r'Expir.+(\d{4}-\d{2}-\d{2})', line, re.IGNORECASE)
            if match is not None:
                return datetime.strptime(match.group(1), '%Y-%m-%d')

            match = re.search(r'Expir.+(\d{2}-\w{3}-\d{4})', line, re.IGNORECASE)
            if match is not None:
                return datetime.strptime(match.group(1), '%d-%b-%Y')

        return "expiration_not_found"


def main(args, env, loggers):
    return DNSRecordsDiagnoser(args, env, loggers).diagnose()
