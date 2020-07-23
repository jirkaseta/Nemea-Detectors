#!/usr/bin/env python3

import xml.etree.ElementTree as ET
import requests
import logging
import re
import sched
import time
import os
import subprocess
import argparse
import csv
import ipaddress
import json
from collections import OrderedDict
from contextlib import suppress

PORT_UNKNOWN = -1  # for blacklists that do not use port numbers; only used internally

SECONDS_IN_MINUTE = 60

cs = logging.StreamHandler()
formatter = logging.Formatter('[%(asctime)s] - %(levelname)s - %(message)s')
cs.setFormatter(formatter)
logger = logging.getLogger(__name__)
logger.addHandler(cs)

# IPv4 regex -- matches IPv4 + optional prefix
ip4_regex = re.compile('\\b(?:(?:2(?:5[0-5]|[0-4][0-9])|[01]?[0-9][0-9]?)\.){3}(?:2(?:5[0-5]|[0-4][0-9])|[01]?[0-9][0-9]?)(?:(?:/(3[012]|[12]?[0-9]))?)\\b')

# IPv6 regex -- matches only IPv6 (prefix not included)
ip6_regex = re.compile('\\b^[a-fA-F0-9:]+(/([0-9]|[1-9][0-9]|1[0-1][0-9]|12[0-8]))?\\b')

# Just a simple regex to eliminate commentaries
url_regex = re.compile('^[^#/].*\..+')

# FQDN regex for DNS
# taken from: https://github.com/guyhughes/fqdn/blob/develop/fqdn/__init__.py
fqdn_regex = re.compile(r'^((?!-)[-A-Z\d]{1,62}(?<!-)\.)+[A-Z]{1,62}\.?$', re.IGNORECASE)

# A heterogeneous list with all types of blacklists
blacklists = []

# Git repo used for versioning blacklists
repo_path = None


# Sorting comparator, splits the IP in format "A.B.C.D(/X),Y,Z"
# into tuple of IP (A, B, C, D), which is comparable by python (numerically)
def split_ip4(ip_dict_entry):
    # Extract only IP, without the prefix and indexes
    ip = ip_dict_entry[0]

    ip = ip.split('/')[0] if '/' in ip else ip.split(',')[0]

    try:
        tuple_ip = tuple(int(part) for part in ip.split('.'))
    except ValueError as e:
        logger.warning('Could not sort this IP addr: {}'.format(ip))
        logger.warning(e)
        tuple_ip = (0, 0, 0, 0)

    """Split a IP address given as string into a 4-tuple of integers."""
    return tuple_ip


def split_ip6(ip_dict_entry):
    ip = ip_dict_entry[0]
    ip = ip.split('/')[0] if '/' in ip else ip.split(',')[0]
    tuple_ip = tuple(int(part, 16) for part in ip.split(':'))

    return tuple_ip


class GeneralConfig:
    def __init__(self, general_config):
        self.socket_timeout = None
        self.download_interval = None

        for elem in general_config:
            with suppress(ValueError):
                setattr(self, elem.attrib['name'], int(elem.text))


class Blacklist:
    separator = None
    comparator = None

    def __init__(self, bl):
        self.entities = dict()
        self.last_download = 1

        # Generate variables from the XML config file
        for element in bl:
            setattr(self, element.attrib['name'], element.text)

    def __str__(self):
        return str(self.__dict__)

    def print_single_blacklist(self):
        # dictionary (OrderedDict) expected to be sorted
        output = ''

        for blacklist, ports in self.entities.items():
            output += blacklist + ':'
            for val in sorted(ports):
                if val != -1:
                    output += str(val) + ','
            output = output[:-1] + '\n'  # remove last comma
        return output

    @staticmethod
    def print_all_blacklists(entities_dict, bitfield_separator):
        # dictionary (OrderedDict) expected to be sorted
        # ports set NOT expected to be sorted
        output = ''
        separator_blacklists = ';'

        for ip in entities_dict:
            # IP
            output += ip + bitfield_separator

            # blacklists bitfield
            bitfield = 0

            # blacklist: ports
            all_blacklists = ''

            for blacklist in entities_dict[ip]:
                ignore_list = False
                bitfield |= 2 ** (blacklist - 1)
                single_blacklist = str(blacklist) + ':'

                for port in sorted(entities_dict[ip][blacklist]):
                    if port == PORT_UNKNOWN:
                        ignore_list = True
                    single_blacklist += str(port) + ','

                # replace last port separator by blacklist separator
                single_blacklist = single_blacklist[:-1] + separator_blacklists

                if not ignore_list:
                    all_blacklists += single_blacklist

            output += str(bitfield)
            if len(all_blacklists) > 0:
                output += separator_blacklists + all_blacklists[:-1]

            output += '\n'

        return output

    def write_to_repo(self):
        if isinstance(self, IPv4Blacklist):
            type_dir = 'ip4'
        elif isinstance(self, IPv6Blacklist):
            type_dir = 'ip6'
        elif isinstance(self, URLandDNSBlacklist):
            type_dir = 'url_dns'

        bl_file = '{}/{}/{}.blist'.format(repo_path, type_dir, self.name)
        with open(bl_file, 'w') as f:
            f.write(self.print_single_blacklist())

    def cut_csv(self, data):
        col_idx = int(self.csv_col) - 1
        rdr = csv.reader(data.decode().splitlines())
        return '\n'.join([row[col_idx] for row in rdr if len(row) > 0]).encode()

    @classmethod
    def process_entities(cls):
        def insert_entity(entities_dict, ip_str, bl_id, ports_set):

            # add IP to upper-level dict
            try:
                if entities_dict[ip_str] is not None:
                    pass
            except KeyError as e:
                entities_dict[ip_str] = dict()

            # add bl+ports to lower-level dict
            try:
                if ports_set == {PORT_UNKNOWN} and len(entities_dict[ip_str][bl_id]) != 0:
                    pass  # do not add -1 if there are other ports present
                else:
                    entities_dict[ip_str][bl_id].append(ports_set)
            except KeyError:
                entities_dict[ip_str][bl_id] = ports_set

        all_entities = dict()

        for bl in blacklists:
            if isinstance(bl, cls):
                for entity in bl.entities:
                    ports = bl.entities[entity]
                    insert_entity(all_entities, entity, int(bl.id), ports)

        return OrderedDict(sorted(all_entities.items(), key=cls.comparator))

    def download_and_update(self):
        """

        :return: OrderedDict where key=IP, value=(OrderedDict where key=Blacklist number, value=set of ports for given bl)
        """
        updated = False
        new_entities = None

        try:
            req = requests.get(self.source, timeout=g_conf.socket_timeout)

            if req.status_code == 200:
                data = req.content

                if self.file_format == 'JSON':
                    new_entities = self.extract_entities_json(data)

                else:
                    # csv or plaintext
                    if self.file_format == 'csv':
                        with suppress(AttributeError):
                            data = self.cut_csv(data)

                    # sorted ordered dictionary, with all values (ports) == {-1}, representing unknown/all ports
                    new_entities = OrderedDict.fromkeys(sorted(self.extract_entities(data), key=type(self).comparator),
                                                        {PORT_UNKNOWN})

                if not new_entities:
                    logger.warning('{}, {}: No valid entities found'.format(type(self).__name__, self.name))

                elif new_entities != self.entities:
                    # Blacklist entities changed since last download
                    self.entities = new_entities
                    if repo_path:
                        self.write_to_repo()
                    self.last_download = time.time()
                    updated = True

                    logger.info('Updated {}: {}'.format(type(self).__name__, self.name))

            else:  # req.status_code != 200
                logger.warning('Could not fetch blacklist: {} ({})\n'
                               'Status code: {}'.format(self.name, self.source, req.status_code))

        except requests.RequestException as e:
            logger.warning('Could not fetch blacklist: {}\n'
                           '{}'.format(self.source, e))

        return updated

    def extract_entities_json(self, data):
        try:
            data = json.loads(data)

        except (ValueError, TypeError) as e:
            logger.warning('Could not parse {}:\n"{}"'.format(self.name, e))
            return None

        data_dict = dict()  # (IP+prefix): [ports]
        flag_check_filter = False

        with suppress(AttributeError):
            if self.filter_key is not None \
                    and self.filter_value is not None:
                flag_check_filter = True

        flag_use_prefix = False

        with suppress(AttributeError):
            if self.json_prefix_key is not None:
                flag_use_prefix = True

        for i in data:
            # filter entities based on value of given field (e.g. server_status = up)
            if not flag_check_filter \
                    or str(i.get(self.filter_key)) != self.filter_value:
                continue

            ip = i.get(self.json_address_key)
            port = i.get(self.json_port_key)

            if flag_use_prefix:
                prefix = str(i.get(self.json_prefix_key))
                if prefix != '32':
                    ip += '/' + prefix

            try:
                data_dict[ip].add(port)
            except KeyError:
                data_dict[ip] = {port}

        # return data_dict
        return OrderedDict(sorted(data_dict.items(), key=type(self).comparator))


class IPv6Blacklist(Blacklist):
    separator = ','
    comparator = split_ip6

    def __init__(self, bl):
        super().__init__(bl)

    @staticmethod
    def extract_entities(data):
        extracted = []

        for line in data.decode('utf-8').splitlines():
            match = re.search(ip6_regex, line)
            if match:
                try:
                    ip, prefix = match.group(0).split('/')
                except ValueError:
                    # no prefix found, assume /128
                    ip = match.group(0)
                    prefix = '128'

                try:
                    exploded_ip = ipaddress.IPv6Address(ip).exploded
                    ip = exploded_ip + '/' + prefix
                    extracted.append(ip)
                except ipaddress.AddressValueError as e:
                    # invalid IPv6 format
                    logger.warning('Could not parse IPv6: {}\n{}'.format(line, e))

        return extracted

    @classmethod
    def create_detector_file(cls):
        entities = cls.process_entities()

        os.makedirs(os.path.dirname(cls.detector_file), exist_ok=True)

        try:
            with open(cls.detector_file, 'w') as f:
                f.write(super().print_all_blacklists(entities, cls.separator))

            logger.info('New IPv6 detector file created: {}'.format(cls.detector_file))

        except OSError as e:
            logger.critical('Failed to create detector file. {}. Exiting downloader'.format(e))
            exit(1)


class IPv4Blacklist(Blacklist):
    separator = ','
    comparator = split_ip4

    def __init__(self, bl):
        super().__init__(bl)

    @staticmethod
    def extract_entities(data):
        extracted = []

        for line in data.decode('utf-8').splitlines():
            # regex already matches the prefix - if present
            match = re.search(ip4_regex, line)
            if match and match.group(1) != '32':  # ignore default /32 prefix
                extracted.append(match.group(0))

        return extracted

    @classmethod
    def create_detector_file(cls):
        entities = cls.process_entities()

        os.makedirs(os.path.dirname(cls.detector_file), exist_ok=True)

        try:
            with open(cls.detector_file, 'w') as f:
                f.write(super().print_all_blacklists(entities, cls.separator))

            logger.info('New IPv4 detector file created: {}'.format(cls.detector_file))

        except OSError as e:
            logger.critical('Failed to create detector file. {}. Exiting downloader'.format(e))
            exit(1)


class URLandDNSBlacklist(Blacklist):
    dns_detector_file = None
    url_detector_file = None
    separator = '\\'
    comparator = str

    def __init__(self, bl):
        super().__init__(bl)

    @staticmethod
    def domain_to_idna(url):
        path = None
        if '/' in url:
            path = '/'.join(url.split('/')[1:])
            domain = url.split('/')[0]
        else:
            domain = url

        domain = domain.encode('idna').decode('ascii')

        return domain + '/' + path if path else domain

    @staticmethod
    def extract_entities(data):
        extracted = []

        for line in data.decode('utf-8').splitlines():
            match = re.search(url_regex, line)
            if match:
                url = match.group(0)
                url = url.replace('https://', '', 1)
                url = url.replace('http://', '', 1)
                url = url.replace('www.', '', 1)
                url = url.lower()
                while url[-1] == '/':
                    url = url[:-1]
                try:
                    url = URLandDNSBlacklist.domain_to_idna(url)
                except UnicodeError:
                    logger.warning('Could not normalize domain: {}'.format(url))

                try:
                    url.encode('ascii')
                    extracted.append(url)
                except UnicodeError:
                    logger.warning('Ignoring URL with non-ascii path: {}'.format(url))

        return extracted

    @classmethod
    def create_detector_file(cls):
        entities = cls.process_entities()

        os.makedirs(os.path.dirname(cls.url_detector_file), exist_ok=True)
        os.makedirs(os.path.dirname(cls.dns_detector_file), exist_ok=True)

        # not an OrderedDict but since its origin was sorted, this will keep the same order
        extracted_fqdns = {entity: vals for entity, vals in entities.items() if
                           re.search(fqdn_regex, entity[:entity.find(cls.separator)])}

        try:
            with open(cls.url_detector_file, 'w') as url_f, open(cls.dns_detector_file, 'w') as dns_f:
                url_f.write(super().print_all_blacklists(entities, cls.separator))
                dns_f.write(super().print_all_blacklists(extracted_fqdns, cls.separator))

            logger.info('New URL detector file created: {}'.format(cls.url_detector_file))
            logger.info('New DNS detector file created: {}'.format(cls.dns_detector_file))

        except OSError as e:
            logger.critical('Failed to create detector file. {}. Exiting downloader'.format(e))
            exit(1)


def parse_config(config_file):
    tree = ET.parse(config_file)

    r = list(tree.getroot())

    general_config = list(r[0])
    detector_files = list(r[1])
    blacklist_array = list(r[2])

    global g_conf
    g_conf = GeneralConfig(general_config)

    IPv4Blacklist.detector_file = [det_file.text for det_file in detector_files if det_file.attrib['name'] == 'IP4'][0]
    IPv6Blacklist.detector_file = [det_file.text for det_file in detector_files if det_file.attrib['name'] == 'IP6'][0]
    URLandDNSBlacklist.url_detector_file = \
        [det_file.text for det_file in detector_files if det_file.attrib['name'] == 'URL'][0]
    URLandDNSBlacklist.dns_detector_file = \
        [det_file.text for det_file in detector_files if det_file.attrib['name'] == 'DNS'][0]

    for bl_type_element in blacklist_array:
        bl_type = bl_type_element.attrib['type']
        for bl in bl_type_element:
            if bl_type == "IP":
                if int(bl.findall(".//*[@name='ip_version']")[0].text) == 6:
                    blacklists.append(IPv6Blacklist(bl))
                else:
                    blacklists.append(IPv4Blacklist(bl))
            elif bl_type == "URL/DNS":
                blacklists.append(URLandDNSBlacklist(bl))


def prepare_repo():
    if not os.path.isdir(repo_path + '/ip4'):
        os.makedirs(repo_path + '/ip4', exist_ok=True)
    if not os.path.isdir(repo_path + '/ip6'):
        os.makedirs(repo_path + '/ip6', exist_ok=True)
    if not os.path.isdir(repo_path + '/url_dns'):
        os.makedirs(repo_path + '/url_dns', exist_ok=True)

    if not os.path.isdir(repo_path + '/.git'):
        ret = subprocess.check_output(['git', 'init', '{}'.format(repo_path)])

        subprocess.check_call(['git', '--git-dir', '{}'.format(repo_path + '/.git'),
                               'config', 'user.name', 'bl_downloader'])

        subprocess.check_call(['git', '--git-dir', '{}'.format(repo_path + '/.git'),
                               'config', 'user.email', 'bl_downloader'], universal_newlines=True)

        logger.info(ret.decode().strip())


def commit_to_repo(bl_type):
    try:
        subprocess.check_call(['git', '--git-dir', '{}'.format(repo_path + '/.git'),
                               '--work-tree', '{}'.format(repo_path), 'add', '-A'], )

        subprocess.check_call(['git', '--git-dir', '{}'.format(repo_path + '/.git'),
                               'commit', '--allow-empty', '-m', '{}s updated'.format(bl_type.__name__)],
                              stdout=subprocess.DEVNULL)

        logger.info('Committed changes to GIT')

    except subprocess.CalledProcessError as e:
        logger.error("Could not add/commit to git repo: {}".format(e))


def run(s):
    # schedule next check immediately
    s.enter(g_conf.download_interval * SECONDS_IN_MINUTE, 1, run, (s,))

    for bl_type in [IPv4Blacklist, IPv6Blacklist, URLandDNSBlacklist]:
        updated = False

        for bl in blacklists:
            if isinstance(bl, bl_type):
                if bl.last_download and bl.last_download + SECONDS_IN_MINUTE * int(bl.download_interval) < time.time():
                    updated += bl.download_and_update()

        if updated:
            if repo_path:
                commit_to_repo(bl_type)
            bl_type.create_detector_file()
        else:
            logger.debug('Check for {} updates done, no changes'.format(bl_type.__name__))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--repo-path', '-r',
                        help="If set, blacklists will be saved in specified directory and versioned using git. ",
                        default=None)

    parser.add_argument('--log-level', '-l',
                        help="Logging level value (from standard Logging library, 10=DEBUG, 20=INFO etc.)",
                        type=int,
                        default=20)

    parser.add_argument('--config-file', '-c',
                        help="Configuration file for downloader (blacklists and metadata)",
                        type=str,
                        default='/etc/nemea/blacklistfilter/bl_downloader_config.xml')

    args = parser.parse_args()
    repo_path = args.repo_path
    logger.setLevel(args.log_level)

    parse_config(args.config_file)

    if repo_path:
        prepare_repo()

    s = sched.scheduler(time.time, time.sleep)

    s.enter(0, 1, run, (s,))
    s.run()
