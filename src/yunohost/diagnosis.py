# -*- coding: utf-8 -*-

""" License

    Copyright (C) 2018 YunoHost

    This program is free software; you can redistribute it and/or modify
    it under the terms of the GNU Affero General Public License as published
    by the Free Software Foundation, either version 3 of the License, or
    (at your option) any later version.

    This program is distributed in the hope that it will be useful,
    but WITHOUT ANY WARRANTY; without even the implied warranty of
    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
    GNU Affero General Public License for more details.

    You should have received a copy of the GNU Affero General Public License
    along with this program; if not, see http://www.gnu.org/licenses

"""

""" diagnosis.py

    Look for possible issues on the server
"""

import re
import os
import time
import smtplib

from moulinette import m18n, msettings
from moulinette.utils import log
from moulinette.utils.filesystem import read_json, write_to_json, read_yaml, write_to_yaml

from yunohost.utils.error import YunohostError
from yunohost.hook import hook_list, hook_exec

logger = log.getActionLogger('yunohost.diagnosis')

DIAGNOSIS_CACHE = "/var/cache/yunohost/diagnosis/"
DIAGNOSIS_CONFIG_FILE = '/etc/yunohost/diagnosis.yml'
DIAGNOSIS_SERVER = "diagnosis.yunohost.org"


def diagnosis_list():
    all_categories_names = [h for h, _ in _list_diagnosis_categories()]
    return {"categories": all_categories_names}


def diagnosis_get(category, item):

    # Get all the categories
    all_categories = _list_diagnosis_categories()
    all_categories_names = [c for c, _ in all_categories]

    if category not in all_categories_names:
        raise YunohostError('diagnosis_unknown_categories', categories=category)

    if isinstance(item, list):
        if any("=" not in criteria for criteria in item):
            raise YunohostError("Criterias should be of the form key=value (e.g. domain=yolo.test)")

        # Convert the provided criteria into a nice dict
        item = {c.split("=")[0]: c.split("=")[1] for c in item}

    return Diagnoser.get_cached_report(category, item=item)


def diagnosis_show(categories=[], issues=False, full=False, share=False, human_readable=False):

    if not os.path.exists(DIAGNOSIS_CACHE):
        logger.warning(m18n.n("diagnosis_never_ran_yet"))
        return

    # Get all the categories
    all_categories = _list_diagnosis_categories()
    all_categories_names = [category for category, _ in all_categories]

    # Check the requested category makes sense
    if categories == []:
        categories = all_categories_names
    else:
        unknown_categories = [c for c in categories if c not in all_categories_names]
        if unknown_categories:
            raise YunohostError('diagnosis_unknown_categories', categories=", ".join(unknown_categories))

    # Fetch all reports
    all_reports = []
    for category in categories:

        try:
            report = Diagnoser.get_cached_report(category)
        except Exception as e:
            logger.error(m18n.n("diagnosis_failed", category=category, error=str(e)))
            continue

        Diagnoser.i18n(report, force_remove_html_tags=share or human_readable)

        add_ignore_flag_to_issues(report)
        if not full:
            del report["timestamp"]
            del report["cached_for"]
            report["items"] = [item for item in report["items"] if not item["ignored"]]
            for item in report["items"]:
                del item["meta"]
                del item["ignored"]
                if "data" in item:
                    del item["data"]
        if issues:
            report["items"] = [item for item in report["items"] if item["status"] in ["WARNING", "ERROR"]]
            # Ignore this category if no issue was found
            if not report["items"]:
                continue

        all_reports.append(report)

    if share:
        from yunohost.utils.yunopaste import yunopaste
        content = _dump_human_readable_reports(all_reports)
        url = yunopaste(content)

        logger.info(m18n.n("log_available_on_yunopaste", url=url))
        if msettings.get('interface') == 'api':
            return {"url": url}
        else:
            return
    elif human_readable:
        print(_dump_human_readable_reports(all_reports))
    else:
        return {"reports": all_reports}


def _dump_human_readable_reports(reports):

    output = ""

    for report in reports:
        output += "=================================\n"
        output += "{description} ({id})\n".format(**report)
        output += "=================================\n\n"
        for item in report["items"]:
            output += "[{status}] {summary}\n".format(**item)
            for detail in item.get("details", []):
                output += "  - " + detail.replace("\n", "\n    ") + "\n"
            output += "\n"
        output += "\n\n"

    return(output)


def diagnosis_run(categories=[], force=False, except_if_never_ran_yet=False, email=False):

    if (email or except_if_never_ran_yet) and not os.path.exists(DIAGNOSIS_CACHE):
        return

    # Get all the categories
    all_categories = _list_diagnosis_categories()
    all_categories_names = [category for category, _ in all_categories]

    # Check the requested category makes sense
    if categories == []:
        categories = all_categories_names
    else:
        unknown_categories = [c for c in categories if c not in all_categories_names]
        if unknown_categories:
            raise YunohostError('diagnosis_unknown_categories', categories=", ".join(unknown_categories))

    issues = []
    # Call the hook ...
    diagnosed_categories = []
    for category in categories:
        logger.debug("Running diagnosis for %s ..." % category)
        path = [p for n, p in all_categories if n == category][0]

        try:
            code, report = hook_exec(path, args={"force": force}, env=None)
        except Exception:
            import traceback
            logger.error(m18n.n("diagnosis_failed_for_category", category=category, error='\n'+traceback.format_exc()))
        else:
            diagnosed_categories.append(category)
            if report != {}:
                issues.extend([item for item in report["items"] if item["status"] in ["WARNING", "ERROR"]])

    if email:
        _email_diagnosis_issues()
    if issues and msettings.get("interface") == "cli":
        logger.warning(m18n.n("diagnosis_display_tip"))


def diagnosis_ignore(add_filter=None, remove_filter=None, list=False):
    """
    This action is meant for the admin to ignore issues reported by the
    diagnosis system if they are known and understood by the admin.  For
    example, the lack of ipv6 on an instance, or badly configured XMPP dns
    records if the admin doesn't care so much about XMPP. The point being that
    the diagnosis shouldn't keep complaining about those known and "expected"
    issues, and instead focus on new unexpected issues that could arise.

    For example, to ignore badly XMPP dnsrecords for domain yolo.test:

        yunohost diagnosis ignore --add-filter dnsrecords domain=yolo.test category=xmpp
                                                  ^              ^             ^
                                            the general    additional       other
                                            diagnosis       criterias       criteria
                                            category to    to target        to target
                                            act on           specific       specific
                                                             reports        reports
    Or to ignore all dnsrecords issues:

        yunohost diagnosis ignore --add-filter dnsrecords

    The filters are stored in the diagnosis configuration in a data structure like:

    ignore_filters: {
        "ip": [
           {"version": 6}     # Ignore all issues related to ipv6
        ],
        "dnsrecords": [
           {"domain": "yolo.test", "category": "xmpp"}, # Ignore all issues related to DNS xmpp records for yolo.test
           {}                                           # Ignore all issues about dnsrecords
        ]
    }
    """

    # Ignore filters are stored in
    configuration = _diagnosis_read_configuration()

    if list:
        return {"ignore_filters": configuration.get("ignore_filters", {})}

    def validate_filter_criterias(filter_):

        # Get all the categories
        all_categories = _list_diagnosis_categories()
        all_categories_names = [category for category, _ in all_categories]

        # Sanity checks for the provided arguments
        if len(filter_) == 0:
            raise YunohostError("You should provide at least one criteria being the diagnosis category to ignore")
        category = filter_[0]
        if category not in all_categories_names:
            raise YunohostError("%s is not a diagnosis category" % category)
        if any("=" not in criteria for criteria in filter_[1:]):
            raise YunohostError("Criterias should be of the form key=value (e.g. domain=yolo.test)")

        # Convert the provided criteria into a nice dict
        criterias = {c.split("=")[0]: c.split("=")[1] for c in filter_[1:]}

        return category, criterias

    if add_filter:

        category, criterias = validate_filter_criterias(add_filter)

        # Fetch current issues for the requested category
        current_issues_for_this_category = diagnosis_show(categories=[category], issues=True, full=True)
        current_issues_for_this_category = current_issues_for_this_category["reports"][0].get("items", {})

        # Accept the given filter only if the criteria effectively match an existing issue
        if not any(issue_matches_criterias(i, criterias) for i in current_issues_for_this_category):
            raise YunohostError("No issues was found matching the given criteria.")

        # Make sure the subdicts/lists exists
        if "ignore_filters" not in configuration:
            configuration["ignore_filters"] = {}
        if category not in configuration["ignore_filters"]:
            configuration["ignore_filters"][category] = []

        if criterias in configuration["ignore_filters"][category]:
            logger.warning("This filter already exists.")
            return

        configuration["ignore_filters"][category].append(criterias)
        _diagnosis_write_configuration(configuration)
        logger.success("Filter added")
        return

    if remove_filter:

        category, criterias = validate_filter_criterias(remove_filter)

        # Make sure the subdicts/lists exists
        if "ignore_filters" not in configuration:
            configuration["ignore_filters"] = {}
        if category not in configuration["ignore_filters"]:
            configuration["ignore_filters"][category] = []

        if criterias not in configuration["ignore_filters"][category]:
            raise YunohostError("This filter does not exists.")

        configuration["ignore_filters"][category].remove(criterias)
        _diagnosis_write_configuration(configuration)
        logger.success("Filter removed")
        return


def _diagnosis_read_configuration():
    if not os.path.exists(DIAGNOSIS_CONFIG_FILE):
        return {}

    return read_yaml(DIAGNOSIS_CONFIG_FILE)


def _diagnosis_write_configuration(conf):
    write_to_yaml(DIAGNOSIS_CONFIG_FILE, conf)


def issue_matches_criterias(issue, criterias):
    """
    e.g. an issue with:
       meta:
          domain: yolo.test
          category: xmpp

    matches the criterias {"domain": "yolo.test"}
    """
    for key, value in criterias.items():
        if key not in issue["meta"]:
            return False
        if str(issue["meta"][key]) != value:
            return False
    return True


def add_ignore_flag_to_issues(report):
    """
    Iterate over issues in a report, and flag them as ignored if they match an
    ignored filter from the configuration

    N.B. : for convenience. we want to make sure the "ignored" key is set for
    every item in the report
    """

    ignore_filters = _diagnosis_read_configuration().get("ignore_filters", {}).get(report["id"], [])

    for report_item in report["items"]:
        report_item["ignored"] = False
        if report_item["status"] not in ["WARNING", "ERROR"]:
            continue
        for criterias in ignore_filters:
            if issue_matches_criterias(report_item, criterias):
                report_item["ignored"] = True
                break


############################################################


class Diagnoser():

    def __init__(self, args, env, loggers):

        # FIXME ? That stuff with custom loggers is weird ... (mainly inherited from the bash hooks, idk)
        self.logger_debug, self.logger_warning, self.logger_info = loggers
        self.env = env
        self.args = args or {}
        self.cache_file = Diagnoser.cache_file(self.id_)
        self.description = Diagnoser.get_description(self.id_)

    def cached_time_ago(self):

        if not os.path.exists(self.cache_file):
            return 99999999
        return time.time() - os.path.getmtime(self.cache_file)

    def write_cache(self, report):
        if not os.path.exists(DIAGNOSIS_CACHE):
            os.makedirs(DIAGNOSIS_CACHE)
        return write_to_json(self.cache_file, report)

    def diagnose(self):

        if not self.args.get("force", False) and self.cached_time_ago() < self.cache_duration:
            self.logger_debug("Cache still valid : %s" % self.cache_file)
            logger.info(m18n.n("diagnosis_cache_still_valid", category=self.description))
            return 0, {}

        for dependency in self.dependencies:
            dep_report = Diagnoser.get_cached_report(dependency)

            if dep_report["timestamp"] == -1:  # No cache yet for this dep
                dep_errors = True
            else:
                dep_errors = [item for item in dep_report["items"] if item["status"] == "ERROR"]

            if dep_errors:
                logger.error(m18n.n("diagnosis_cant_run_because_of_dep", category=self.description, dep=Diagnoser.get_description(dependency)))
                return 1, {}

        items = list(self.run())

        for item in items:
            if "details" in item and not item["details"]:
                del item["details"]

        new_report = {"id": self.id_,
                      "cached_for": self.cache_duration,
                      "items": items}

        self.logger_debug("Updating cache %s" % self.cache_file)
        self.write_cache(new_report)
        Diagnoser.i18n(new_report)
        add_ignore_flag_to_issues(new_report)

        errors   = [item for item in new_report["items"] if item["status"] == "ERROR" and not item["ignored"]]
        warnings = [item for item in new_report["items"] if item["status"] == "WARNING" and not item["ignored"]]
        errors_ignored = [item for item in new_report["items"] if item["status"] == "ERROR" and item["ignored"]]
        warning_ignored = [item for item in new_report["items"] if item["status"] == "WARNING" and item["ignored"]]
        ignored_msg = " " + m18n.n("diagnosis_ignored_issues", nb_ignored=len(errors_ignored+warning_ignored)) if errors_ignored or warning_ignored else ""

        if errors and warnings:
            logger.error(m18n.n("diagnosis_found_errors_and_warnings", errors=len(errors), warnings=len(warnings), category=new_report["description"]) + ignored_msg)
        elif errors:
            logger.error(m18n.n("diagnosis_found_errors", errors=len(errors), category=new_report["description"]) + ignored_msg)
        elif warnings:
            logger.warning(m18n.n("diagnosis_found_warnings", warnings=len(warnings), category=new_report["description"]) + ignored_msg)
        else:
            logger.success(m18n.n("diagnosis_everything_ok", category=new_report["description"]) + ignored_msg)

        return 0, new_report

    @staticmethod
    def cache_file(id_):
        return os.path.join(DIAGNOSIS_CACHE, "%s.json" % id_)

    @staticmethod
    def get_cached_report(id_, item=None, warn_if_no_cache=True):
        cache_file = Diagnoser.cache_file(id_)
        if not os.path.exists(cache_file):
            if warn_if_no_cache:
                logger.warning(m18n.n("diagnosis_no_cache", category=id_))
            report = {"id": id_,
                      "cached_for": -1,
                      "timestamp": -1,
                      "items": []}
        else:
            report = read_json(cache_file)
            report["timestamp"] = int(os.path.getmtime(cache_file))

        if item:
            for report_item in report["items"]:
                if report_item.get("meta") == item:
                    return report_item
            return {}
        else:
            return report

    @staticmethod
    def get_description(id_):
        key = "diagnosis_description_" + id_
        descr = m18n.n(key)
        # If no description available, fallback to id
        return descr if descr.decode('utf-8') != key else id_

    @staticmethod
    def i18n(report, force_remove_html_tags=False):

        # "Render" the strings with m18n.n
        # N.B. : we do those m18n.n right now instead of saving the already-translated report
        # because we can't be sure we'll redisplay the infos with the same locale as it
        # was generated ... e.g. if the diagnosing happened inside a cron job with locale EN
        # instead of FR used by the actual admin...

        report["description"] = Diagnoser.get_description(report["id"])

        for item in report["items"]:

            # For the summary and each details, we want to call
            # m18n() on the string, with the appropriate data for string
            # formatting which can come from :
            # - infos super-specific to the summary/details (if it's a tuple(key,dict_with_info) and not just a string)
            # - 'meta' info = parameters of the test (e.g. which domain/category for DNS conf record)
            # - actual 'data' retrieved from the test (e.g. actual global IP, ...)

            meta_data = item.get("meta", {}).copy()
            meta_data.update(item.get("data", {}))

            html_tags = re.compile(r'<[^>]+>')
            def m18n_(info):
                if not isinstance(info, tuple) and not isinstance(info, list):
                    info = (info, {})
                info[1].update(meta_data)
                s = m18n.n(info[0], **(info[1]))
                # In cli, we remove the html tags
                if msettings.get("interface") != "api" or force_remove_html_tags:
                    s = s.replace("<cmd>", "'").replace("</cmd>", "'")
                    s = html_tags.sub('', s.replace("<br>","\n"))
                else:
                    s = s.replace("<cmd>", "<code class='cmd'>").replace("</cmd>", "</code>")
                    # Make it so that links open in new tabs
                    s = s.replace("<a href=", "<a target='_blank' rel='noopener noreferrer' href=")
                return s

            item["summary"] = m18n_(item["summary"])

            if "details" in item:
                item["details"] = [m18n_(info) for info in item["details"]]

    @staticmethod
    def remote_diagnosis(uri, data, ipversion, timeout=30):

        # Lazy loading for performance
        import requests
        import socket

        # Monkey patch socket.getaddrinfo to force request() to happen in ipv4
        # or 6 ...
        # Inspired by https://stackoverflow.com/a/50044152
        old_getaddrinfo = socket.getaddrinfo

        def getaddrinfo_ipv4_only(*args, **kwargs):
            responses = old_getaddrinfo(*args, **kwargs)
            return [response
                    for response in responses
                    if response[0] == socket.AF_INET]

        def getaddrinfo_ipv6_only(*args, **kwargs):
            responses = old_getaddrinfo(*args, **kwargs)
            return [response
                    for response in responses
                    if response[0] == socket.AF_INET6]

        if ipversion == 4:
            socket.getaddrinfo = getaddrinfo_ipv4_only
        elif ipversion == 6:
            socket.getaddrinfo = getaddrinfo_ipv6_only

        url = 'https://%s/%s' % (DIAGNOSIS_SERVER, uri)
        try:
            r = requests.post(url, json=data, timeout=timeout)
        finally:
            socket.getaddrinfo = old_getaddrinfo

        if r.status_code not in [200, 400]:
            raise Exception("The remote diagnosis server failed miserably while trying to diagnose your server. This is most likely an error on Yunohost's infrastructure and not on your side. Please contact the YunoHost team an provide them with the following information.<br>URL: <code>%s</code><br>Status code: <code>%s</code>" % (url, r.status_code))
        if r.status_code == 400:
            raise Exception("Diagnosis request was refused: %s" % r.content)

        try:
            r = r.json()
        except Exception as e:
            raise Exception("Failed to parse json from diagnosis server response.\nError: %s\nOriginal content: %s" % (e, r.content))

        return r


def _list_diagnosis_categories():
    hooks_raw = hook_list("diagnosis", list_by="priority", show_info=True)["hooks"]
    hooks = []
    for _, some_hooks in sorted(hooks_raw.items(), key=lambda h: int(h[0])):
        for name, info in some_hooks.items():
            hooks.append((name, info["path"]))

    return hooks


def _email_diagnosis_issues():
    from yunohost.domain import _get_maindomain
    maindomain = _get_maindomain()
    from_ = "diagnosis@%s (Automatic diagnosis on %s)" % (maindomain, maindomain)
    to_ = "root"
    subject_ = "Issues found by automatic diagnosis on %s" % maindomain

    disclaimer = "The automatic diagnosis on your YunoHost server identified some issues on your server. You will find a description of the issues below. You can manage those issues in the 'Diagnosis' section in your webadmin."

    issues = diagnosis_show(issues=True)["reports"]
    if not issues:
        return

    content = _dump_human_readable_reports(issues)

    message = """\
From: %s
To: %s
Subject: %s

%s

---

%s
""" % (from_, to_, subject_, disclaimer, content)

    smtp = smtplib.SMTP("localhost")
    smtp.sendmail(from_, [to_], message)
    smtp.quit()
