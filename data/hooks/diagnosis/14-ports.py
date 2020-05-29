#!/usr/bin/env python

import os

from yunohost.diagnosis import Diagnoser
from yunohost.utils.error import YunohostError
from yunohost.service import _get_services

class PortsDiagnoser(Diagnoser):

    id_ = os.path.splitext(os.path.basename(__file__))[0].split("-")[1]
    cache_duration = 600
    dependencies = ["ip"]

    def run(self):

        # TODO: report a warning if port 53 or 5353 is exposed to the outside world...

        # This dict is something like :
        #   {   80: "nginx",
        #       25: "postfix",
        #       443: "nginx"
        #       ... }
        ports = {}
        services = _get_services()
        for service, infos in services.items():
            for port in infos.get("needs_exposed_ports", []):
                ports[port] = service

        ipversions = []
        ipv4 = Diagnoser.get_cached_report("ip", item={"test": "ipv4"}) or {}
        if ipv4.get("status") == "SUCCESS":
            ipversions.append(4)

        # To be discussed: we could also make this check dependent on the
        # existence of an AAAA record...
        ipv6 = Diagnoser.get_cached_report("ip", item={"test": "ipv6"}) or {}
        if ipv6.get("status") == "SUCCESS":
            ipversions.append(6)

        # Fetch test result for each relevant IP version
        results = {}
        for ipversion in ipversions:
            try:
                r = Diagnoser.remote_diagnosis('check-ports',
                                               data={'ports': ports.keys()},
                                               ipversion=ipversion)
                results[ipversion] = r["ports"]
            except Exception as e:
                yield dict(meta={"reason": "remote_diagnosis_failed", "ipversion": ipversion},
                           data={"error": str(e)},
                           status="WARNING",
                           summary="diagnosis_ports_could_not_diagnose",
                           details=["diagnosis_ports_could_not_diagnose_details"])
                continue

        ipversions = results.keys()
        if not ipversions:
            return

        for port, service in sorted(ports.items()):
            port = str(port)
            category = services[service].get("category", "[?]")

            # If both IPv4 and IPv6 (if applicable) are good
            if all(results[ipversion].get(port) is True for ipversion in ipversions):
                yield dict(meta={"port": port},
                           data={"service": service, "category": category},
                           status="SUCCESS",
                           summary="diagnosis_ports_ok",
                           details=["diagnosis_ports_needed_by"])
            # If both IPv4 and IPv6 (if applicable) are failed
            elif all(results[ipversion].get(port) is not True for ipversion in ipversions):
                yield dict(meta={"port": port},
                           data={"service": service, "category": category},
                           status="ERROR",
                           summary="diagnosis_ports_unreachable",
                           details=["diagnosis_ports_needed_by", "diagnosis_ports_forwarding_tip"])
            # If only IPv4 is failed or only IPv6 is failed (if applicable)
            else:
                passed, failed = (4, 6) if results[4].get(port) is True else (6, 4)

                # Failing in ipv4 is critical.
                # If we failed in IPv6 but there's in fact no AAAA record
                # It's an acceptable situation and we shall not report an
                # error
                # If any AAAA record is set, IPv6 is important...
                def ipv6_is_important():
                    dnsrecords = Diagnoser.get_cached_report("dnsrecords") or {}
                    return any(record["data"].get("AAAA:@") in ["OK", "WRONG"] for record in dnsrecords.get("items", []))

                if failed == 4 or ipv6_is_important():
                    yield dict(meta={"port": port},
                               data={"service": service, "category": category, "passed": passed, "failed": failed},
                               status="ERROR",
                               summary="diagnosis_ports_partially_unreachable",
                               details=["diagnosis_ports_needed_by", "diagnosis_ports_forwarding_tip"])
                # So otherwise we report a success
                # And in addition we report an info about the failure in IPv6
                # *with a different meta* (important to avoid conflicts when
                # fetching the other info...)
                else:
                    yield dict(meta={"port": port},
                               data={"service": service, "category": category},
                               status="SUCCESS",
                               summary="diagnosis_ports_ok",
                               details=["diagnosis_ports_needed_by"])
                    yield dict(meta={"test": "ipv6", "port": port},
                               data={"service": service, "category": category, "passed": passed, "failed": failed},
                               status="INFO",
                               summary="diagnosis_ports_partially_unreachable",
                               details=["diagnosis_ports_needed_by", "diagnosis_ports_forwarding_tip"])


def main(args, env, loggers):
    return PortsDiagnoser(args, env, loggers).diagnose()
