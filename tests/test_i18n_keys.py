# -*- coding: utf-8 -*-

import os
import re
import glob
import json
import yaml
import subprocess

ignore = ["password_too_simple_",
          "password_listed",
          "backup_method_",
          "backup_applying_method_",
          "confirm_app_install_"]

###############################################################################
#   Find used keys in python code                                             #
###############################################################################


def find_expected_string_keys():

    # Try to find :
    #    m18n.n(   "foo"
    #    YunohostError("foo"
    p1 = re.compile(r'm18n\.n\(\s*[\"\'](\w+)[\"\']')
    p2 = re.compile(r'YunohostError\([\'\"](\w+)[\'\"]')

    python_files = glob.glob("src/yunohost/*.py")
    python_files.extend(glob.glob("src/yunohost/utils/*.py"))
    python_files.extend(glob.glob("src/yunohost/data_migrations/*.py"))
    python_files.extend(glob.glob("data/hooks/diagnosis/*.py"))
    python_files.append("bin/yunohost")

    for python_file in python_files:
        content = open(python_file).read()
        for m in p1.findall(content):
            if m.endswith("_"):
                continue
            yield m
        for m in p2.findall(content):
            if m.endswith("_"):
                continue
            yield m

    # For each diagnosis, try to find strings like "diagnosis_stuff_foo" (c.f. diagnosis summaries)
    # Also we expect to have "diagnosis_description_<name>" for each diagnosis
    p3 = re.compile(r'[\"\'](diagnosis_[a-z]+_\w+)[\"\']')
    for python_file in glob.glob("data/hooks/diagnosis/*.py"):
        content = open(python_file).read()
        for m in p3.findall(content):
            if m.endswith("_"):
                # Ignore some name fragments which are actually concatenated with other stuff..
                continue
            yield m
        yield "diagnosis_description_" + os.path.basename(python_file)[:-3].split("-")[-1]

    # For each migration, expect to find "migration_description_<name>"
    for path in glob.glob("src/yunohost/data_migrations/*.py"):
        if "__init__" in path:
            continue
        yield "migration_description_" + os.path.basename(path)[:-3]

    # For each default service, expect to find "service_description_<name>"
    for service, info in yaml.safe_load(open("data/templates/yunohost/services.yml")).items():
        if info is None:
            continue
        yield "service_description_" + service

    # For all unit operations, expect to find "log_<name>"
    # A unit operation is created either using the @is_unit_operation decorator
    # or using OperationLogger(
    cmd = "grep -hr '@is_unit_operation' src/yunohost/ -A3 2>/dev/null | grep '^def' | sed -E 's@^def (\\w+)\\(.*@\\1@g'"
    for funcname in subprocess.check_output(cmd, shell=True).decode("utf-8").strip().split("\n"):
        yield "log_" + funcname

    p4 = re.compile(r"OperationLogger\([\"\'](\w+)[\"\']")
    for python_file in python_files:
        content = open(python_file).read()
        for m in ("log_" + match for match in p4.findall(content)):
            yield m

    # Global settings descriptions
    # Will be on a line like : ("service.ssh.allow_deprecated_dsa_hostkey", {"type": "bool", ...
    p5 = re.compile(r" \([\"\'](\w[\w\.]+)[\"\'],")
    content = open("src/yunohost/settings.py").read()
    for m in ("global_settings_setting_" + s.replace(".", "_") for s in p5.findall(content)):
        yield m

    # Keys for the actionmap ...
    for category in yaml.load(open("data/actionsmap/yunohost.yml")).values():
        if "actions" not in category.keys():
            continue
        for action in category["actions"].values():
            if "arguments" not in action.keys():
                continue
            for argument in action["arguments"].values():
                extra = argument.get("extra")
                if not extra:
                    continue
                if "password" in extra:
                    yield extra["password"]
                if "ask" in extra:
                    yield extra["ask"]
                if "comment" in extra:
                    yield extra["comment"]
                if "pattern" in extra:
                    yield extra["pattern"][1]
                if "help" in extra:
                    yield extra["help"]

    # Hardcoded expected keys ...
    yield "admin_password"  # Not sure that's actually used nowadays...

    for method in ["tar", "copy", "borg", "custom"]:
        yield "backup_applying_method_%s" % method
        yield "backup_method_%s_finished" % method

    for level in ["danger", "thirdparty", "warning"]:
        yield "confirm_app_install_%s" % level

    for errortype in ["not_found", "error", "warning", "success", "not_found_details"]:
        yield "diagnosis_domain_expiration_%s" % errortype
    yield "diagnosis_domain_not_found_details"

    for errortype in ["bad_status_code", "connection_error", "timeout"]:
        yield "diagnosis_http_%s" % errortype

    yield "password_listed"
    for i in [1, 2, 3, 4]:
        yield "password_too_simple_%s" % i

    checks = ["outgoing_port_25_ok", "ehlo_ok", "fcrdns_ok",
              "blacklist_ok", "queue_ok", "ehlo_bad_answer",
              "ehlo_unreachable", "ehlo_bad_answer_details",
              "ehlo_unreachable_details", ]
    for check in checks:
        yield "diagnosis_mail_%s" % check

###############################################################################
#   Load en locale json keys                                                  #
###############################################################################


def keys_defined_for_en():
    return json.loads(open("locales/en.json").read()).keys()

###############################################################################
#   Compare keys used and keys defined                                        #
###############################################################################


expected_string_keys = set(find_expected_string_keys())
keys_defined = set(keys_defined_for_en())


def test_undefined_i18n_keys():
    undefined_keys = expected_string_keys.difference(keys_defined)
    undefined_keys = sorted(undefined_keys)

    if undefined_keys:
        raise Exception("Those i18n keys should be defined in en.json:\n"
                        "    - " + "\n    - ".join(undefined_keys))


def test_unused_i18n_keys():

    unused_keys = keys_defined.difference(expected_string_keys)
    unused_keys = sorted(unused_keys)

    if unused_keys:
        raise Exception("Those i18n keys appears unused:\n"
                        "    - " + "\n    - ".join(unused_keys))
