# -*- coding: utf-8 -*-

""" License

    Copyright (C) 2013 YunoHost

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

""" yunohost_domain.py

    Manage domains
"""
import os
import re

from moulinette import m18n, msettings
from moulinette.core import MoulinetteError
from yunohost.utils.error import YunohostError
from moulinette.utils.log import getActionLogger

from yunohost.app import app_ssowatconf, _installed_apps, _get_app_settings
from yunohost.regenconf import regen_conf, _force_clear_hashes, _process_regen_conf
from yunohost.utils.network import get_public_ip
from yunohost.log import is_unit_operation
from yunohost.hook import hook_callback

logger = getActionLogger('yunohost.domain')


def domain_list(exclude_subdomains=False):
    """
    List domains

    Keyword argument:
        exclude_subdomains -- Filter out domains that are subdomains of other declared domains

    """
    from yunohost.utils.ldap import _get_ldap_interface

    ldap = _get_ldap_interface()
    result = [entry['virtualdomain'][0] for entry in ldap.search('ou=domains,dc=yunohost,dc=org', 'virtualdomain=*', ['virtualdomain'])]

    result_list = []
    for domain in result:
        if exclude_subdomains:
            parent_domain = domain.split(".", 1)[1]
            if parent_domain in result:
                continue
        result_list.append(domain)

    return {'domains': result_list}


@is_unit_operation()
def domain_add(operation_logger, domain, dyndns=False):
    """
    Create a custom domain

    Keyword argument:
        domain -- Domain name to add
        dyndns -- Subscribe to DynDNS

    """
    from yunohost.hook import hook_callback
    from yunohost.app import app_ssowatconf
    from yunohost.utils.ldap import _get_ldap_interface

    if domain.startswith("xmpp-upload."):
        raise YunohostError("domain_cannot_add_xmpp_upload")

    ldap = _get_ldap_interface()

    try:
        ldap.validate_uniqueness({'virtualdomain': domain})
    except MoulinetteError:
        raise YunohostError('domain_exists')

    operation_logger.start()

    # DynDNS domain
    if dyndns:

        # Do not allow to subscribe to multiple dyndns domains...
        if os.path.exists('/etc/cron.d/yunohost-dyndns'):
            raise YunohostError('domain_dyndns_already_subscribed')

        from yunohost.dyndns import dyndns_subscribe, _dyndns_provides

        # Check that this domain can effectively be provided by
        # dyndns.yunohost.org. (i.e. is it a nohost.me / noho.st)
        if not _dyndns_provides("dyndns.yunohost.org", domain):
            raise YunohostError('domain_dyndns_root_unknown')

        # Actually subscribe
        dyndns_subscribe(domain=domain)

    try:
        import yunohost.certificate
        yunohost.certificate._certificate_install_selfsigned([domain], False)

        attr_dict = {
            'objectClass': ['mailDomain', 'top'],
            'virtualdomain': domain,
        }

        try:
            ldap.add('virtualdomain=%s,ou=domains' % domain, attr_dict)
        except Exception as e:
            raise YunohostError('domain_creation_failed', domain=domain, error=e)

        # Don't regen these conf if we're still in postinstall
        if os.path.exists('/etc/yunohost/installed'):
            # Sometime we have weird issues with the regenconf where some files
            # appears as manually modified even though they weren't touched ...
            # There are a few ideas why this happens (like backup/restore nginx
            # conf ... which we shouldnt do ...). This in turns creates funky
            # situation where the regenconf may refuse to re-create the conf
            # (when re-creating a domain..)
            # So here we force-clear the has out of the regenconf if it exists.
            # This is a pretty ad hoc solution and only applied to nginx
            # because it's one of the major service, but in the long term we
            # should identify the root of this bug...
            _force_clear_hashes(["/etc/nginx/conf.d/%s.conf" % domain])
            regen_conf(names=['nginx', 'metronome', 'dnsmasq', 'postfix', 'rspamd'])
            app_ssowatconf()

    except Exception:
        # Force domain removal silently
        try:
            domain_remove(domain, True)
        except:
            pass
        raise

    hook_callback('post_domain_add', args=[domain])

    logger.success(m18n.n('domain_created'))


@is_unit_operation()
def domain_remove(operation_logger, domain, force=False):
    """
    Delete domains

    Keyword argument:
        domain -- Domain to delete
        force -- Force the domain removal

    """
    from yunohost.hook import hook_callback
    from yunohost.app import app_ssowatconf
    from yunohost.utils.ldap import _get_ldap_interface

    if not force and domain not in domain_list()['domains']:
        raise YunohostError('domain_unknown')

    # Check domain is not the main domain
    if domain == _get_maindomain():
        other_domains = domain_list()["domains"]
        other_domains.remove(domain)

        if other_domains:
            raise YunohostError('domain_cannot_remove_main',
                                domain=domain, other_domains="\n * " + ("\n * ".join(other_domains)))
        else:
            raise YunohostError('domain_cannot_remove_main_add_new_one', domain=domain)

    # Check if apps are installed on the domain
    apps_on_that_domain = []

    for app in _installed_apps():
        settings = _get_app_settings(app)
        if settings.get("domain") == domain:
            apps_on_that_domain.append("%s (on https://%s%s)" % (app, domain, settings["path"]) if "path" in settings else app)

    if apps_on_that_domain:
        raise YunohostError('domain_uninstall_app_first', apps=", ".join(apps_on_that_domain))

    operation_logger.start()
    ldap = _get_ldap_interface()
    try:
        ldap.remove('virtualdomain=' + domain + ',ou=domains')
    except Exception as e:
        raise YunohostError('domain_deletion_failed', domain=domain, error=e)

    os.system('rm -rf /etc/yunohost/certs/%s' % domain)

    # Sometime we have weird issues with the regenconf where some files
    # appears as manually modified even though they weren't touched ...
    # There are a few ideas why this happens (like backup/restore nginx
    # conf ... which we shouldnt do ...). This in turns creates funky
    # situation where the regenconf may refuse to re-create the conf
    # (when re-creating a domain..)
    #
    # So here we force-clear the has out of the regenconf if it exists.
    # This is a pretty ad hoc solution and only applied to nginx
    # because it's one of the major service, but in the long term we
    # should identify the root of this bug...
    _force_clear_hashes(["/etc/nginx/conf.d/%s.conf" % domain])
    # And in addition we even force-delete the file Otherwise, if the file was
    # manually modified, it may not get removed by the regenconf which leads to
    # catastrophic consequences of nginx breaking because it can't load the
    # cert file which disappeared etc..
    if os.path.exists("/etc/nginx/conf.d/%s.conf" % domain):
        _process_regen_conf("/etc/nginx/conf.d/%s.conf" % domain, new_conf=None, save=True)

    regen_conf(names=['nginx', 'metronome', 'dnsmasq', 'postfix'])
    app_ssowatconf()

    hook_callback('post_domain_remove', args=[domain])

    logger.success(m18n.n('domain_deleted'))


def domain_dns_conf(domain, ttl=None):
    """
    Generate DNS configuration for a domain

    Keyword argument:
        domain -- Domain name
        ttl -- Time to live

    """

    ttl = 3600 if ttl is None else ttl

    dns_conf = _build_dns_conf(domain, ttl)

    result = ""

    result += "; Basic ipv4/ipv6 records"
    for record in dns_conf["basic"]:
        result += "\n{name} {ttl} IN {type} {value}".format(**record)

    result += "\n\n"
    result += "; XMPP"
    for record in dns_conf["xmpp"]:
        result += "\n{name} {ttl} IN {type} {value}".format(**record)

    result += "\n\n"
    result += "; Mail"
    for record in dns_conf["mail"]:
        result += "\n{name} {ttl} IN {type} {value}".format(**record)
    result += "\n\n"

    result += "; Extra"
    for record in dns_conf["extra"]:
        result += "\n{name} {ttl} IN {type} {value}".format(**record)

    for name, record_list in dns_conf.items():
        if name not in ("basic", "xmpp", "mail", "extra") and record_list:
            result += "\n\n"
            result += "; " + name
            for record in record_list:
                result += "\n{name} {ttl} IN {type} {value}".format(**record)

    if msettings.get('interface') == 'cli':
        logger.info(m18n.n("domain_dns_conf_is_just_a_recommendation"))

    return result


@is_unit_operation()
def domain_main_domain(operation_logger, new_main_domain=None):
    """
    Check the current main domain, or change it

    Keyword argument:
        new_main_domain -- The new domain to be set as the main domain

    """
    from yunohost.tools import _set_hostname

    # If no new domain specified, we return the current main domain
    if not new_main_domain:
        return {'current_main_domain': _get_maindomain()}

    # Check domain exists
    if new_main_domain not in domain_list()['domains']:
        raise YunohostError('domain_unknown')

    operation_logger.related_to.append(('domain', new_main_domain))
    operation_logger.start()

    # Apply changes to ssl certs
    ssl_key = "/etc/ssl/private/yunohost_key.pem"
    ssl_crt = "/etc/ssl/private/yunohost_crt.pem"
    new_ssl_key = "/etc/yunohost/certs/%s/key.pem" % new_main_domain
    new_ssl_crt = "/etc/yunohost/certs/%s/crt.pem" % new_main_domain

    try:
        if os.path.exists(ssl_key) or os.path.lexists(ssl_key):
            os.remove(ssl_key)
        if os.path.exists(ssl_crt) or os.path.lexists(ssl_crt):
            os.remove(ssl_crt)

        os.symlink(new_ssl_key, ssl_key)
        os.symlink(new_ssl_crt, ssl_crt)

        _set_maindomain(new_main_domain)
    except Exception as e:
        logger.warning("%s" % e, exc_info=1)
        raise YunohostError('main_domain_change_failed')

    _set_hostname(new_main_domain)

    # Generate SSOwat configuration file
    app_ssowatconf()

    # Regen configurations
    try:
        with open('/etc/yunohost/installed', 'r'):
            regen_conf()
    except IOError:
        pass

    logger.success(m18n.n('main_domain_changed'))


def domain_cert_status(domain_list, full=False):
    import yunohost.certificate
    return yunohost.certificate.certificate_status(domain_list, full)


def domain_cert_install(domain_list, force=False, no_checks=False, self_signed=False, staging=False):
    import yunohost.certificate
    return yunohost.certificate.certificate_install(domain_list, force, no_checks, self_signed, staging)


def domain_cert_renew(domain_list, force=False, no_checks=False, email=False, staging=False):
    import yunohost.certificate
    return yunohost.certificate.certificate_renew(domain_list, force, no_checks, email, staging)


def _get_conflicting_apps(domain, path, ignore_app=None):
    """
    Return a list of all conflicting apps with a domain/path (it can be empty)

    Keyword argument:
        domain -- The domain for the web path (e.g. your.domain.tld)
        path -- The path to check (e.g. /coffee)
        ignore_app -- An optional app id to ignore (c.f. the change_url usecase)
    """

    domain, path = _normalize_domain_path(domain, path)

    # Abort if domain is unknown
    if domain not in domain_list()['domains']:
        raise YunohostError('domain_unknown')

    # This import cannot be put on top of file because it would create a
    # recursive import...
    from yunohost.app import app_map

    # Fetch apps map
    apps_map = app_map(raw=True)

    # Loop through all apps to check if path is taken by one of them
    conflicts = []
    if domain in apps_map:
        # Loop through apps
        for p, a in apps_map[domain].items():
            if a["id"] == ignore_app:
                continue
            if path == p:
                conflicts.append((p, a["id"], a["label"]))
            # We also don't want conflicts with other apps starting with
            # same name
            elif path.startswith(p) or p.startswith(path):
                conflicts.append((p, a["id"], a["label"]))

    return conflicts


def domain_url_available(domain, path):
    """
    Check availability of a web path

    Keyword argument:
        domain -- The domain for the web path (e.g. your.domain.tld)
        path -- The path to check (e.g. /coffee)
    """

    return len(_get_conflicting_apps(domain, path)) == 0


def _get_maindomain():
    with open('/etc/yunohost/current_host', 'r') as f:
        maindomain = f.readline().rstrip()
    return maindomain


def _set_maindomain(domain):
    with open('/etc/yunohost/current_host', 'w') as f:
        f.write(domain)


def _normalize_domain_path(domain, path):

    # We want url to be of the format :
    #  some.domain.tld/foo

    # Remove http/https prefix if it's there
    if domain.startswith("https://"):
        domain = domain[len("https://"):]
    elif domain.startswith("http://"):
        domain = domain[len("http://"):]

    # Remove trailing slashes
    domain = domain.rstrip("/").lower()
    path = "/" + path.strip("/")

    return domain, path


def _build_dns_conf(domain, ttl=3600, include_empty_AAAA_if_no_ipv6=False):
    """
    Internal function that will returns a data structure containing the needed
    information to generate/adapt the dns configuration

    The returned datastructure will have the following form:
    {
        "basic": [
            # if ipv4 available
            {"type": "A", "name": "@", "value": "123.123.123.123", "ttl": 3600},
            # if ipv6 available
            {"type": "AAAA", "name": "@", "value": "valid-ipv6", "ttl": 3600},
        ],
        "xmpp": [
            {"type": "SRV", "name": "_xmpp-client._tcp", "value": "0 5 5222 domain.tld.", "ttl": 3600},
            {"type": "SRV", "name": "_xmpp-server._tcp", "value": "0 5 5269 domain.tld.", "ttl": 3600},
            {"type": "CNAME", "name": "muc", "value": "@", "ttl": 3600},
            {"type": "CNAME", "name": "pubsub", "value": "@", "ttl": 3600},
            {"type": "CNAME", "name": "vjud", "value": "@", "ttl": 3600}
            {"type": "CNAME", "name": "xmpp-upload", "value": "@", "ttl": 3600}
        ],
        "mail": [
            {"type": "MX", "name": "@", "value": "10 domain.tld.", "ttl": 3600},
            {"type": "TXT", "name": "@", "value": "\"v=spf1 a mx ip4:123.123.123.123 ipv6:valid-ipv6 -all\"", "ttl": 3600 },
            {"type": "TXT", "name": "mail._domainkey", "value": "\"v=DKIM1; k=rsa; p=some-super-long-key\"", "ttl": 3600},
            {"type": "TXT", "name": "_dmarc", "value": "\"v=DMARC1; p=none\"", "ttl": 3600}
        ],
        "extra": [
            # if ipv4 available
            {"type": "A", "name": "*", "value": "123.123.123.123", "ttl": 3600},
            # if ipv6 available
            {"type": "AAAA", "name": "*", "value": "valid-ipv6", "ttl": 3600},
            {"type": "CAA", "name": "@", "value": "128 issue \"letsencrypt.org\"", "ttl": 3600},
        ],
        "example_of_a_custom_rule": [
            {"type": "SRV", "name": "_matrix", "value": "domain.tld.", "ttl": 3600}
        ],
    }
    """

    ipv4 = get_public_ip()
    ipv6 = get_public_ip(6)

    ###########################
    # Basic ipv4/ipv6 records #
    ###########################

    basic = []
    if ipv4:
        basic.append(["@", ttl, "A", ipv4])

    if ipv6:
        basic.append(["@", ttl, "AAAA", ipv6])
    elif include_empty_AAAA_if_no_ipv6:
        basic.append(["@", ttl, "AAAA", None])

    #########
    # Email #
    #########

    mail = [
        ["@", ttl, "MX", "10 %s." % domain],
        ["@", ttl, "TXT", '"v=spf1 a mx -all"'],
    ]

    # DKIM/DMARC record
    dkim_host, dkim_publickey = _get_DKIM(domain)

    if dkim_host:
        mail += [
            [dkim_host, ttl, "TXT", dkim_publickey],
            ["_dmarc", ttl, "TXT", '"v=DMARC1; p=none"'],
        ]

    ########
    # XMPP #
    ########

    xmpp = [
        ["_xmpp-client._tcp", ttl, "SRV", "0 5 5222 %s." % domain],
        ["_xmpp-server._tcp", ttl, "SRV", "0 5 5269 %s." % domain],
        ["muc", ttl, "CNAME", "@"],
        ["pubsub", ttl, "CNAME", "@"],
        ["vjud", ttl, "CNAME", "@"],
        ["xmpp-upload", ttl, "CNAME", "@"],
    ]

    #########
    # Extra #
    #########

    extra = []

    if ipv4:
        extra.append(["*", ttl, "A", ipv4])

    if ipv6:
        extra.append(["*", ttl, "AAAA", ipv6])
    elif include_empty_AAAA_if_no_ipv6:
        extra.append(["*", ttl, "AAAA", None])

    extra.append(["@", ttl, "CAA", '128 issue "letsencrypt.org"'])

    ####################
    # Standard records #
    ####################

    records = {
        "basic": [{"name": name, "ttl": ttl_, "type": type_, "value": value} for name, ttl_, type_, value in basic],
        "xmpp": [{"name": name, "ttl": ttl_, "type": type_, "value": value} for name, ttl_, type_, value in xmpp],
        "mail": [{"name": name, "ttl": ttl_, "type": type_, "value": value} for name, ttl_, type_, value in mail],
        "extra": [{"name": name, "ttl": ttl_, "type": type_, "value": value} for name, ttl_, type_, value in extra],
    }

    ##################
    # Custom records #
    ##################

    # Defined by custom hooks ships in apps for example ...

    hook_results = hook_callback('custom_dns_rules', args=[domain])
    for hook_name, results in hook_results.items():
        #
        # There can be multiple results per hook name, so results look like
        # {'/some/path/to/hook1':
        #       { 'state': 'succeed',
        #         'stdreturn': [{'type': 'SRV',
        #                        'name': 'stuff.foo.bar.',
        #                        'value': 'yoloswag',
        #                        'ttl': 3600}]
        #       },
        #  '/some/path/to/hook2':
        #       { ... },
        #  [...]
        #
        # Loop over the sub-results
        custom_records = [v['stdreturn'] for v in results.values()
                          if v and v['stdreturn']]

        records[hook_name] = []
        for record_list in custom_records:
            # Check that record_list is indeed a list of dict
            # with the required keys
            if not isinstance(record_list, list) \
               or any(not isinstance(record, dict) for record in record_list) \
               or any(key not in record for record in record_list for key in ["name", "ttl", "type", "value"]):
                # Display an error, mainly for app packagers trying to implement a hook
                logger.warning("Ignored custom record from hook '%s' because the data is not a *list* of dict with keys name, ttl, type and value. Raw data : %s" % (hook_name, record_list))
                continue

            records[hook_name].extend(record_list)

    return records


def _get_DKIM(domain):
    DKIM_file = '/etc/dkim/{domain}.mail.txt'.format(domain=domain)

    if not os.path.isfile(DKIM_file):
        return (None, None)

    with open(DKIM_file) as f:
        dkim_content = f.read()

    # Gotta manage two formats :
    #
    # Legacy
    # -----
    #
    # mail._domainkey IN      TXT     ( "v=DKIM1; k=rsa; "
    #           "p=<theDKIMpublicKey>" )
    #
    # New
    # ------
    #
    # mail._domainkey IN  TXT ( "v=DKIM1; h=sha256; k=rsa; "
    #           "p=<theDKIMpublicKey>" )

    is_legacy_format = " h=sha256; " not in dkim_content

    # Legacy DKIM format
    if is_legacy_format:
        dkim = re.match((
            r'^(?P<host>[a-z_\-\.]+)[\s]+([0-9]+[\s]+)?IN[\s]+TXT[\s]+'
             '[^"]*"v=(?P<v>[^";]+);'
             '[\s"]*k=(?P<k>[^";]+);'
             '[\s"]*p=(?P<p>[^";]+)'), dkim_content, re.M | re.S
        )
    else:
        dkim = re.match((
            r'^(?P<host>[a-z_\-\.]+)[\s]+([0-9]+[\s]+)?IN[\s]+TXT[\s]+'
             '[^"]*"v=(?P<v>[^";]+);'
             '[\s"]*h=(?P<h>[^";]+);'
             '[\s"]*k=(?P<k>[^";]+);'
             '[\s"]*p=(?P<p>[^";]+)'), dkim_content, re.M | re.S
        )

    if not dkim:
        return (None, None)

    if is_legacy_format:
        return (
            dkim.group('host'),
            '"v={v}; k={k}; p={p}"'.format(v=dkim.group('v'),
                                           k=dkim.group('k'),
                                           p=dkim.group('p'))
        )
    else:
        return (
            dkim.group('host'),
            '"v={v}; h={h}; k={k}; p={p}"'.format(v=dkim.group('v'),
                                                  h=dkim.group('h'),
                                                  k=dkim.group('k'),
                                                  p=dkim.group('p'))
        )
