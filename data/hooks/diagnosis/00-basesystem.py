#!/usr/bin/env python

import os
import json
import subprocess

from moulinette.utils.process import check_output
from moulinette.utils.filesystem import read_file, read_json, write_to_json
from yunohost.diagnosis import Diagnoser
from yunohost.utils.packages import ynh_packages_version


class BaseSystemDiagnoser(Diagnoser):

    id_ = os.path.splitext(os.path.basename(__file__))[0].split("-")[1]
    cache_duration = 600
    dependencies = []

    def run(self):

        # Detect virt technology (if not bare metal) and arch
        # Gotta have this "|| true" because it systemd-detect-virt return 'none'
        # with an error code on bare metal ~.~
        virt = check_output("systemd-detect-virt || true", shell=True).strip()
        if virt.lower() == "none":
            virt = "bare-metal"

        # Detect arch
        arch = check_output("dpkg --print-architecture").strip()
        hardware = dict(meta={"test": "hardware"},
                        status="INFO",
                        data={"virt": virt, "arch": arch},
                        summary="diagnosis_basesystem_hardware")

        # Also possibly the board name
        if os.path.exists("/proc/device-tree/model"):
            model = read_file('/proc/device-tree/model').strip().replace('\x00', '')
            hardware["data"]["model"] = model
            hardware["details"] = ["diagnosis_basesystem_hardware_board"]

        yield hardware

        # Kernel version
        kernel_version = read_file('/proc/sys/kernel/osrelease').strip()
        yield dict(meta={"test": "kernel"},
                   data={"kernel_version": kernel_version},
                   status="INFO",
                   summary="diagnosis_basesystem_kernel")

        # Debian release
        debian_version = read_file("/etc/debian_version").strip()
        yield dict(meta={"test": "host"},
                   data={"debian_version": debian_version},
                   status="INFO",
                   summary="diagnosis_basesystem_host")

        # Yunohost packages versions
        # We check if versions are consistent (e.g. all 3.6 and not 3 packages with 3.6 and the other with 3.5)
        # This is a classical issue for upgrades that failed in the middle
        # (or people upgrading half of the package because they did 'apt upgrade' instead of 'dist-upgrade')
        # Here, ynh_core_version is for example "3.5.4.12", so [:3] is "3.5" and we check it's the same for all packages
        ynh_packages = ynh_packages_version()
        ynh_core_version = ynh_packages["yunohost"]["version"]
        consistent_versions = all(infos["version"][:3] == ynh_core_version[:3] for infos in ynh_packages.values())
        ynh_version_details = [("diagnosis_basesystem_ynh_single_version",
                                {"package":package,
                                 "version": infos["version"],
                                 "repo": infos["repo"]}
                               )
                               for package, infos in ynh_packages.items()]

        yield dict(meta={"test": "ynh_versions"},
                   data={"main_version": ynh_core_version, "repo": ynh_packages["yunohost"]["repo"]},
                   status="INFO" if consistent_versions else "ERROR",
                   summary="diagnosis_basesystem_ynh_main_version" if consistent_versions else "diagnosis_basesystem_ynh_inconsistent_versions",
                   details=ynh_version_details)


        if self.is_vulnerable_to_meltdown():
            yield dict(meta={"test": "meltdown"},
                       status="ERROR",
                       summary="diagnosis_security_vulnerable_to_meltdown",
                       details=["diagnosis_security_vulnerable_to_meltdown_details"]
                       )

    def is_vulnerable_to_meltdown(self):
        # meltdown CVE: https://security-tracker.debian.org/tracker/CVE-2017-5754

        # We use a cache file to avoid re-running the script so many times,
        # which can be expensive (up to around 5 seconds on ARM)
        # and make the admin appear to be slow (c.f. the calls to diagnosis
        # from the webadmin)
        #
        # The cache is in /tmp and shall disappear upon reboot
        # *or* we compare it to dpkg.log modification time
        # such that it's re-ran if there was package upgrades
        # (e.g. from yunohost)
        cache_file = "/tmp/yunohost-meltdown-diagnosis"
        dpkg_log = "/var/log/dpkg.log"
        if os.path.exists(cache_file):
            if not os.path.exists(dpkg_log) or os.path.getmtime(cache_file) > os.path.getmtime(dpkg_log):
                self.logger_debug("Using cached results for meltdown checker, from %s" % cache_file)
                return read_json(cache_file)[0]["VULNERABLE"]

        # script taken from https://github.com/speed47/spectre-meltdown-checker
        # script commit id is store directly in the script
        SCRIPT_PATH = "/usr/lib/moulinette/yunohost/vendor/spectre-meltdown-checker/spectre-meltdown-checker.sh"

        # '--variant 3' corresponds to Meltdown
        # example output from the script:
        # [{"NAME":"MELTDOWN","CVE":"CVE-2017-5754","VULNERABLE":false,"INFOS":"PTI mitigates the vulnerability"}]
        try:
            self.logger_debug("Running meltdown vulnerability checker")
            call = subprocess.Popen("bash %s --batch json --variant 3" %
                                    SCRIPT_PATH, shell=True,
                                    stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE)

            # TODO / FIXME : here we are ignoring error messages ...
            # in particular on RPi2 and other hardware, the script complains about
            # "missing some kernel info (see -v), accuracy might be reduced"
            # Dunno what to do about that but we probably don't want to harass
            # users with this warning ...
            output, err = call.communicate()
            assert call.returncode in (0, 2, 3), "Return code: %s" % call.returncode

            # If there are multiple lines, sounds like there was some messages
            # in stdout that are not json >.> ... Try to get the actual json
            # stuff which should be the last line
            output = output.strip()
            if "\n" in output:
                self.logger_debug("Original meltdown checker output : %s" % output)
                output = output.split("\n")[-1]

            CVEs = json.loads(output)
            assert len(CVEs) == 1
            assert CVEs[0]["NAME"] == "MELTDOWN"
        except Exception as e:
            import traceback
            traceback.print_exc()
            self.logger_warning("Something wrong happened when trying to diagnose Meltdown vunerability, exception: %s" % e)
            raise Exception("Command output for failed meltdown check: '%s'" % output)

        self.logger_debug("Writing results from meltdown checker to cache file, %s" % cache_file)
        write_to_json(cache_file, CVEs)
        return CVEs[0]["VULNERABLE"]


def main(args, env, loggers):
    return BaseSystemDiagnoser(args, env, loggers).diagnose()
