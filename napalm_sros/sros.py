# -*- coding: utf-8 -*-
# © 2020 Nokia
# Licensed under the Apache License 2.0 License
# SPDX-License-Identifier: Apache-2.0

# Copyright 2016 Dravetech AB. All rights reserved.
#
# The contents of this file are licensed under the Apache License, Version 2.0
# (the "License"); you may not use this file except in compliance with the
# License. You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations under
# the License.

"""
Napalm driver for SROS.
"""
# import standard library
import json
import time
import re
import datetime
import traceback
import xmltodict
from dictdiffer import diff
import paramiko

# import NAPALM libraries

from lxml import etree

from napalm.base import NetworkDriver
from napalm.base.exceptions import (
    ConnectionException,
    SessionLockedException,
    MergeConfigException,
    ReplaceConfigException,
    CommitError,
    CommandErrorException,
)
from napalm.base.helpers import convert, ip, as_number
import napalm.base.constants as C

# import third party libraries
from ncclient import manager
from ncclient.xml_ import to_ele, to_xml

# import local modules
from napalm_sros.utils.parse_output_to_dict import parse_with_textfsm
from napalm_sros.nc_filters import GET_ARP_TABLE,GET_BGP_CONFIG,GET_ENVIRONMENT, \
     GET_FACTS,GET_INTERFACES,GET_INTERFACES_COUNTERS,GET_INTERFACES_IP, \
     GET_IPV6_NEIGHBORS_TABLE,GET_LLDP_NEIGHBORS,GET_LLDP_NEIGHBORS_DETAIL, \
     GET_NETWORK_INSTANCES,GET_NTP_PEERS,GET_NTP_SERVERS,GET_OPTICS, \
     GET_PROBES_CONFIG,GET_ROUTE_TO,GET_SNMP_INFORMATION,GET_USERS

from .api import get_bgp_neighbors, get_bgp_neighbors_detail
import logging

log = logging.getLogger(__file__)

class NokiaSROSDriver(NetworkDriver):
    """Napalm driver for Skeleton."""

    def __init__(self, hostname, username, password, timeout=60, optional_args=None):
        """Constructor."""
        self.manager = None
        self.hostname = hostname
        self.username = username
        self.password = password
        self.timeout = timeout
        self.conn = None
        self.conn_ssh = None
        self.ssh_channel = None
        self.fmt = None
        self.locked = False
        self.terminal_stdout_re = [
            re.compile(
                r"[\r\n]*\!?\*?(\((ex|gl|pr|ro)\))?\[.*\][\r\n]+[ABCD]\:\S+\@\S+\#\s$"
            ),
            re.compile(r"[\r\n]*\*?[ABCD]:[\w\-\.\,\>]+[#\$]\s"),
        ]
        self.terminal_stderr_re = [
            re.compile("Error: .*[\r\n]+"),
            re.compile("(MINOR|MAJOR|CRITICAL): .*[\r\n]+"),
        ]

        self.ipv4_address_re = re.compile(
            r"(([2][5][0-5]\.)|([2][0-4][0-9]\.)|([0-1]?[0-9]?[0-9]\.)){3}"
            + "(([2][5][0-5])|([2][0-4][0-9])|([0-1]?[0-9]?[0-9]))"
        )

        self.ipv6_address_re = re.compile(
          r"(([0-9a-fA-F]{1,4}:){7,7}[0-9a-fA-F]{1,4}|([0-9a-fA-F]{1,4}:){1,7}:|([0-9a-fA-F]{1,4}:){1,6}:[0-9a-fA-F]{1,4}|([0-9a-fA-F]{1,4}:){1,5}(:[0-9a-fA-F]{1,4}){1,2}|([0-9a-fA-F]{1,4}:){1,4}(:[0-9a-fA-F]{1,4}){1,3}|([0-9a-fA-F]{1,4}:){1,3}(:[0-9a-fA-F]{1,4}){1,4}|([0-9a-fA-F]{1,4}:){1,2}(:[0-9a-fA-F]{1,4}){1,5}|[0-9a-fA-F]{1,4}:((:[0-9a-fA-F]{1,4}){1,6})|:((:[0-9a-fA-F]{1,4}){1,7}|:)|fe80:(:[0-9a-fA-F]{0,4}){0,4}%[0-9a-zA-Z]{1,}|::(ffff(:0{1,4}){0,1}:){0,1}((25[0-5]|(2[0-4]|1{0,1}[0-9]){0,1}[0-9])\.){3,3}(25[0-5]|(2[0-4]|1{0,1}[0-9]){0,1}[0-9])|([0-9a-fA-F]{1,4}:){1,4}:((25[0-5]|(2[0-4]|1{0,1}[0-9]){0,1}[0-9])\.){3,3}(25[0-5]|(2[0-4]|1{0,1}[0-9]){0,1}[0-9]))"
        )
        self.cmd_line_pattern_re = re.compile(r"\*?(.*?)(>.*)*#.*?")

        if optional_args is None:
            optional_args = {}
        self.sros_get_format = optional_args.get("sros_get_format", "xml")
        self.sros_compare_format = optional_args.get("sros_compare_format", "json")
        self.port = optional_args.get("port", 830)
        self.conn_ssh = optional_args.get("ssh_conn", None)
        self.ssh_channel = optional_args.get("ssh_channel", None)

        # locking variables
        self.lock_disable = optional_args.get("lock_disable", False)
        self.session_config_lock = optional_args.get("config_lock", False)

        # namespace map
        self.nsmap = {
            "state_ns": "urn:nokia.com:sros:ns:yang:sr:state",
            "configure_ns": "urn:nokia.com:sros:ns:yang:sr:conf",
        }
        self.optional_args = None

    def open(self):
        """Implement the NAPALM method open (mandatory)"""
        # Create a NETCONF connection to the host
        try:
            if self.manager:
                self.conn = self.manager.connect()
            else:
                self.conn = manager.connect(
                    host=self.hostname,
                    port=self.port,
                    username=self.username,
                    password=self.password,
                    hostkey_verify=False,
                    timeout=self.timeout,
                )
            revision = re.compile( ".*&revision=(.*).*" )
            self.state_revisions = [ revision.match(i).groups()[0] for i in self.conn.server_capabilities if "nokia-state" in i ]
            log.info( self.state_revisions )
            self.R19 = '2016-07-06' in self.state_revisions # Older SR OS release e.g. '2022-10-19' or '2016-07-06'
        except ConnectionException as ce:
            print("Error in opening netconf connection : {}".format(ce))
            log.error(
                "Error in opening netconf connection : %s" % traceback.format_exc()
            )

        except Exception as e:
            print("Error in opening netconf connection : {}".format(e))
            log.error(
                "Error in opening netconf connection : %s" % traceback.format_exc()
            )

    def close(self):
        """Implement the NAPALM method close (mandatory)"""
        # Close the NETCONF connection with the host

        # netconf connection
        if self.conn is not None:
            self.conn.close_session()
        # ssh connection
        if self.conn_ssh is not None:
            self.conn_ssh.close()

    def _create_ssh(self):
        try:
            self.conn_ssh = paramiko.SSHClient()
            self.conn_ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            self.conn_ssh.connect(
                hostname=self.hostname,
                port=22,
                username=self.username,
                password=self.password,
                timeout = self.timeout
            )
            self.ssh_channel = self.conn_ssh.invoke_shell()
        except Exception as e:
            print("Error in opening a ssh connection: {}".format(e))
            log.error("Error in opening a ssh connection: %s" % traceback.format_exc())

    def _perform_cli_commands(self, commands, is_get, no_more=False):
        if no_more:
            # Disable paged responses, note that the '/' changes the filenames
            # for mocked data responses under test/unit/mocked_data
            # - they now require a leading '_'
            commands = ["/environment more false"] + commands
        try:
            is_alive = False
            if self.conn_ssh is not None:
                is_alive = self.conn_ssh.get_transport().is_active()
            if not is_alive:
                self._create_ssh()
            buff = ""
            if is_get:
                for command in commands:
                    if "\n" not in command:
                        command = command + "\n"
                    self.ssh_channel.send(command)
                    while True:
                        time.sleep(0.150)
                        resp = self.ssh_channel.recv(9999)
                        buff += resp.decode("ascii")
                        if re.search(self.terminal_stdout_re[0], buff):
                          break
            else:
                # chunk commands into lists of length 500
                # send all the 500 commands together
                # receive the output from the router
                # the last while loop is to ensure that we have received all the output 
                # from the router for the remaining commands

                command_list = []
                if len(commands) > 500:
                    n = 500
                    command_list = [
                        commands[i * n : (i + 1) * n]
                        for i in range((len(commands) + n - 1) // n)
                    ]
                else:
                    n = len(commands)
                    command_list = [
                        commands[i * n : (i + 1) * n]
                        for i in range((len(commands) + n - 1) // n)
                    ]
                if len(command_list) == 0:
                    pass
                for command_set in command_list:
                    for command in command_set:
                        if "\n" not in command:
                            command = command + "\n"
                        if self.ssh_channel.send_ready():
                            self.ssh_channel.send(command)
                    time.sleep(0.1)
                    while self.ssh_channel.recv_ready():
                        time.sleep(0.1)
                        resp = self.ssh_channel.recv(999)
                        buff += resp.decode("ascii")

                time.sleep(1.0)
                while self.ssh_channel.recv_ready():
                    time.sleep(0.1)
                    resp = self.ssh_channel.recv(999)
                    buff += resp.decode("ascii")

            return buff
        except Exception as e:
            print("Error in method perform cli commands : {}".format(e))
            log.error("Error in _perform_cli_commands : %s" % traceback.format_exc())
            return None

    def _lock_config(self):
        if not self.locked:
            try:
                self.conn.lock()
                self.locked = True
            except SessionLockedException as se:
                print("Error in locking the session: {}".format(se))
                log.error("Error in locking the session: %s" % traceback.format_exc())
            except Exception as e:
                print("Error in locking the session caused exception: {}".format(e))
                log.error(
                    "Error in locking the session caused exception: %s" %
                    traceback.format_exc(),
                )

    def _unlock_config(self):
        if self.locked:
            try:
                self.conn.unlock()
                self.locked = False
            except Exception as e:
                print("Error in unlocking the session: {}".format(e))
                log.error("Error in unlocking the session: %s" % traceback.format_exc())

    def _find_txt(self, xml_tree, path, default="", namespaces=None):
        """
        Extracts the text value from an XML tree, using XPath.
        In case of error, will return a default value.

        :param xml_tree:   the XML Tree object. Assumed is <type 'lxml.etree._Element'>.
        :param path:       XPath to be applied, in order to extract the desired data.
        :param default:    Value to be returned in case of error.
        :param namespaces: prefix-namespace mappings to process XPath
        :return: a str value.
        """
        value = ""
        try:
            xpath_applied = xml_tree.xpath(
                path, namespaces=namespaces
            )  # will consider the first match only
            xpath_length = len(xpath_applied)  # get a count of items in XML tree
            if xpath_length and xpath_applied[0] is not None:
                xpath_result = xpath_applied[0]
                if isinstance(xpath_result, type(xml_tree)):
                    if xpath_result.text is not None:
                        value = xpath_result.text.strip()
                else:
                    value = xpath_result
            else:
                if xpath_applied == "":
                    log.error(
                        "Unable to find the specified-text-element/XML path: %s in  \
                            the XML tree provided. Total Items in XML tree: %d "
                        % (path, xpath_length)
                    )
        except Exception as e:  # in case of any exception, returns default
            print("Error while finding text in xml: {}".format(e))
            log.error("Error while finding text in xml: %s" % traceback.format_exc())
            value = default
        return str(value)

    def is_alive(self):
        """
        Returns a flag with the connection state. Depends on the nature of API used by each driver.
        The state does not reflect only on the connection status (when SSH), it must also take into
        consideration other parameters,
        e.g.: NETCONF session might not be usable, although the underlying
        SSH session is still open etc.
        """
        try:
            is_alive_dict = {}
            if self.conn is not None and self.conn_ssh is not None:
                is_alive_dict.update({"is_alive": True})
            else:
                is_alive_dict.update({"is_alive": False})
            return is_alive_dict
        except Exception as e:  # in case of any exception, returns default
            print("Error occurred in is_alive method: {}".format(e))
            log.error("Error occurred in is_alive: %s" % traceback.format_exc())

    def discard_config(self):
        """
        Discards the configuration loaded into the candidate.
        """
        if self.fmt == "xml":
            self.conn.discard_changes()
        else:
            self._perform_cli_commands(["discard"], True)
        if not self.lock_disable and not self.session_config_lock:
            self._unlock_config()

    def commit_config(self, message="", revert_in=None):
        """
        Commits the changes requested by the method load_replace_candidate or load_merge_candidate.
        """
        if self.fmt == "text":
            buff = self._perform_cli_commands(["commit"], True)
            # If error while performing commit, return the error
            error = ""
            for item in buff.split("\n"):
                if any(match.search(item) for match in self.terminal_stderr_re):
                    row = item.strip()
                    row_list = row.split(": ")
                    error += row_list[2]
            if error:
                log.error(f"Error during commit: {error}")
                raise CommitError(error)
        elif self.fmt == "xml":
            self.conn.commit()
            if not self.lock_disable and not self.session_config_lock:
                self._unlock_config()

    def rollback(self):
        """
        If changes were made, revert changes to the original state.
        """
        cmd = ["/quit-config", "/configure exclusive", "rollback 1", "commit", "exit"]
        buff = self._perform_cli_commands(cmd, True)
        error = ""

        if buff is not None:
            for item in buff.split("\n"):
                if "MINOR: CLI #2069" in item:
                    continue
                elif any(match.search(item) for match in self.terminal_stderr_re):
                    row = item.strip()
                    row_list = row.split(": ")
                    error += row_list[2]
                if error:
                    log.error(f"Error during rollback: {error}")
                    raise CommandErrorException(error)

    def compare_config(self):
        """
        :return: A string showing the difference between the running configuration and the candidate
        configuration. The running_config is loaded automatically just before doing the comparison
        so there is no need for you to do it.
        """
        buff = ""
        if self.fmt == "text":
            buff = self._perform_cli_commands( ["compare"], True, no_more=True )
            # buff = self._perform_cli_commands(["/environment more false", "compare"])
        else:
            #  if format is xml we convert them into dict and perform a diff on configs to return the difference
            # running_dict = xmltodict.parse(running_config, process_namespaces=True)

            running_dict = xmltodict.parse(
                self.get_config(retrieve="running")["running"],
                process_namespaces=self.sros_compare_format != "json",
            )
            # candidate_dict = xmltodict.parse(candidate_config, process_namespaces=True)
            candidate_dict = xmltodict.parse(
                self.get_config(retrieve="candidate")["candidate"],
                process_namespaces=self.sros_compare_format != "json",
            )
            new_buff = ""
            result = diff(running_dict, candidate_dict)
            if self.sros_compare_format == "json":
                new_buff += "\n".join(
                    [json.dumps(e, sort_keys=True, indent=4) for e in result]
                )
            else:
                new_buff += " ".join([str(elem) for elem in result])

            return new_buff
        if buff is not None:
            new_buff = ""
            first_compare = False
            for item in buff.split("\n"):
                if any(match.search(item) for match in self.terminal_stderr_re):
                    row = item.strip()
                    new_buff += row
                    break
                if not first_compare and "compare" in item:
                    first_compare = True
                    continue
                elif re.search(r'\(ex\)\[\/?\]', item):
                    continue
                elif "/environment more false" in item:
                    continue
                elif self.cmd_line_pattern_re.search(item):
                    continue
                elif self.terminal_stdout_re[0].search(item):
                    continue
                else:
                    row = item.rstrip()
                    if row == "":
                        continue
                    if "configure" in row:
                        row = row.lstrip()
                    new_buff += row
                    new_buff += "\n"
            return new_buff.rstrip("\n")
        else:
            return ""

    def _determinne_config_format(self, config) -> str:
        if config.strip().startswith("<"):
            return "xml"
        return "text"

    def load_merge_candidate(self, filename=None, config=None):
        """
        Populates the candidate configuration. You can populate it from a file or from a string.
        If you send both a filename and a string containing the configuration, the file takes precedence.

        If you use this method the existing configuration will be merged with the candidate configuration
        once you commit the changes. This method will not change the configuration by itself.
        :param filename: Path to the file containing the desired configuration. By default is None.
        :param config: String containing the desired configuration.
        Raises: MergeConfigException – If there is an error on the configuration sent.
        """
        if filename is None:
            configuration = config
        else:
            with open(filename) as f:
                configuration = f.read()

        self.fmt = self._determinne_config_format(configuration)
        if self.fmt == "xml":
            if not self.lock_disable and not self.session_config_lock:
                self._lock_config()
            configuration = etree.XML(configuration)
            configuration_tree = etree.ElementTree(configuration)
            root = configuration_tree.getroot()
            if root.tag != "{urn:ietf:params:xml:ns:netconf:base:1.0}config":
                newroot = etree.Element(
                    "{urn:ietf:params:xml:ns:netconf:base:1.0}config"
                )
                newroot.insert(0, root)
                self.conn.edit_config(
                    config=newroot, target="candidate", default_operation="merge"
                )

            else:
                self.conn.edit_config(
                    config=configuration,
                    target="candidate",
                    default_operation="merge",
                )
            self.conn.validate(source="candidate")

        else:
            configuration = configuration.split("\n")
            configuration.insert(0, "edit-config exclusive")
            buff = self._perform_cli_commands(configuration, False)
            # error checking
            if buff is not None:
                for item in buff.split("\n"):
                    if any(match.search(item) for match in self.terminal_stderr_re):
                        log.error(f"Merge issue : {item}")
                        raise MergeConfigException("Merge issue: %s", item)
            else:
                raise MergeConfigException("Timeout during load_merge_candidate")

    def load_replace_candidate(self, filename=None, config=None):
        """
        Populates the candidate configuration. You can populate it from a file or from a string.
        If you send both a filename and a string containing the configuration, the file takes
        precedence.

        If you use this method the existing configuration will be merged with the candidate
        configuration once you commit the changes. This method will not change the configuration
        by itself.
        :param filename: Path to the file containing the desired configuration. By default is None.
        :param config: String containing the desired configuration.
        Raises: ReplaceConfigException – If there is an error on the configuration sent.
        """
        if filename is None:
            configuration = config
        else:
            with open(filename) as f:
                configuration = f.read()

        self.fmt = self._determinne_config_format(configuration)
        if self.fmt == "xml":
            if not self.lock_disable and not self.session_config_lock:
                self._lock_config()
            configuration = etree.XML(configuration)
            configuration_tree = etree.ElementTree(configuration)
            root = configuration_tree.getroot()
            if root.tag != "{urn:ietf:params:xml:ns:netconf:base:1.0}config":
                newroot = etree.Element(
                    "{urn:ietf:params:xml:ns:netconf:base:1.0}config"
                )
                newroot.insert(0, root)
                self.conn.edit_config(
                    config=newroot, target="candidate", default_operation="replace",
                )
            else:
                self.conn.edit_config(
                    config=configuration,
                    target="candidate",
                    default_operation="replace",
                )
            self.conn.validate(source="candidate")
        else:
            configuration = configuration.split("\n")
            configuration.insert(0, "edit-config exclusive")
            configuration.insert(1, "delete configure")
            buff = self._perform_cli_commands(configuration, False)
            # error checking
            if buff is not None:
                for item in buff.split("\n"):
                    if any(match.search(item) for match in self.terminal_stderr_re):
                        log.error( f"Replace issue: {item}" )
                        raise ReplaceConfigException("Replace issue: %s", item)
            else:
                raise ReplaceConfigException("Timeout during load_replace_candidate")

    def get_facts(self):
        """
            Returns a dictionary containing the following information:
                uptime - Uptime of the device in seconds.
                vendor - Manufacturer of the device.
                model - Device model.
                hostname - Hostname of the device
                fqdn - Fqdn of the device
                os_version - String with the OS version running on the device.
                serial_number - Serial number of the device
                interface_list - List of the interfaces of the device
        """
        try:
            interface_list = []
            result = to_ele(self.conn.get(filter=GET_FACTS["_"]).data_xml)

            hostname = self._find_txt(
                result,
                "state_ns:state/state_ns:system/state_ns:oper-name",
                default="",
                namespaces=self.nsmap,
            )
            fqdn = hostname
            uptime = self._find_txt(
                result,
                "state_ns:state/state_ns:system/state_ns:up-time",
                default="",
                namespaces=self.nsmap,
            )
            # In uptime, last three digits are milliseconds
            if uptime:
                uptime = uptime[:-3]+ "." + uptime[-3:]
                uptime = convert(float, uptime, default=0.0)
            else:
                uptime = -1.0
            interfaces = result.xpath(
                "state_ns:state/state_ns:router/state_ns:interface/state_ns:interface-name",
                namespaces=self.nsmap,
            )
            for i in interfaces:
                interface_list.append(i.text)

            return {
                "vendor": "Nokia",
                "model": self._find_txt(
                    result,
                    "state_ns:state/state_ns:system/state_ns:platform",
                    default="",
                    namespaces=self.nsmap,
                ),
                "serial_number": self._find_txt(
                    result,
                    "state_ns:state/state_ns:chassis/state_ns:hardware-data/state_ns:serial-number",
                    default="",
                    namespaces=self.nsmap,
                ),
                "os_version": self._find_txt(
                    result,
                    "state_ns:state/state_ns:system/state_ns:version/state_ns:version-number",
                    default="",
                    namespaces=self.nsmap,
                ),
                "hostname": hostname,
                "fqdn": fqdn,
                "uptime": uptime,
                "interface_list": interface_list,
            }
        except Exception as e:
            print("Error in method get facts : {}".format(e))
            log.error("Error in method get facts : %s" % traceback.format_exc())

    def get_interfaces(self):
        # All physical ports and interfaces
        # retrieval of management interface speed and mac is not implemented
        """
           Returns a dictionary of dictionaries.
           The keys for the first dictionary will be the interfaces in the devices.
           The inner dictionary will containing the following data for each interface:
               is_up (True/False)
               is_enabled (True/False)
               description (string)
               last_flapped (float in seconds)
               speed (float in Mbit)
               MTU (in Bytes)
               mac_address (string)
         """
        try:
            interfaces = {}
            result = to_ele(
                self.conn.get(
                    filter=GET_INTERFACES(R19=self.R19), with_defaults="report-all"
                ).data_xml
            )
            # get physical interfaces (ports) information
            for port in result.xpath("state_ns:state/state_ns:port", namespaces=self.nsmap):
                port_id = self._find_txt(
                    port, "state_ns:port-id", namespaces=self.nsmap
                )  # port name
                if port_id == "":
                    continue
                pd = {}  # port dict
                pd["mac_address"] = self._find_txt(
                    port, "state_ns:hardware-mac-address", namespaces=self.nsmap
                )
                pd["is_up"] = (
                    True
                    if self._find_txt(port, "state_ns:oper-state", namespaces=self.nsmap)
                    == "up"
                    else False
                )
                pd["speed"] = convert(
                    float,
                    self._find_txt(
                        port, "state_ns:ethernet/state_ns:oper-speed", namespaces=self.nsmap
                    ),
                )
                pd["last_flapped"] = -1.0  # flap information is not available in YANG yet
                pd["is_enabled"] = (
                    True
                    if self._find_txt(
                        result,
                        'configure_ns:configure/configure_ns:port[configure_ns:port-id="{}"]/configure_ns:admin-state'.format(
                            port_id
                        ),
                        namespaces=self.nsmap,
                    )
                    == "enable"
                    else False
                )
                pd["mtu"] = convert(
                    int,
                    self._find_txt(
                        result,
                        'configure_ns:configure/configure_ns:port[configure_ns:port-id="{}"]/configure_ns:ethernet/configure_ns:mtu'.format(
                            port_id
                        ),
                        namespaces=self.nsmap,
                    ),
                )
                pd["description"] = self._find_txt(
                    result,
                    'configure_ns:configure/configure_ns:port[configure_ns:port-id="{}"]/configure_ns:description'.format(
                        port_id
                    ),
                    namespaces=self.nsmap,
                )
                interfaces[port_id] = pd

            # get logical interfaces (interfaces) information
            for if_state in result.xpath(
                "state_ns:state/state_ns:router/state_ns:interface", namespaces=self.nsmap
            ):
                if_name = self._find_txt(
                    if_state, "state_ns:interface-name", namespaces=self.nsmap
                )
                if if_name == "":
                    continue
                ifd = {}  # interface dict
                if_mac = ""
                if_port = ""
                # configuration portion of the interface
                if_cfg_block = result.find(
                    f'configure_ns:configure/configure_ns:router/configure_ns:interface[configure_ns:interface-name="{if_name}"]',
                    self.nsmap,
                )
                if if_cfg_block is not None and len(if_cfg_block) > 0:
                    # description
                    ifd["description"] = self._find_txt(
                        if_cfg_block, "configure_ns:description", namespaces=self.nsmap
                    )
                    # MAC address
                    cfg_mac = self._find_txt(
                        if_cfg_block, "configure_ns:mac", namespaces=self.nsmap
                    )
                    # configured mac address
                    if cfg_mac != "":
                        if_mac = cfg_mac

                    # port info
                    _p = self._find_txt(
                        if_cfg_block, "configure_ns:port", namespaces=self.nsmap
                    )
                    if_port = (
                        _p.split(":")[0] if (_p != "" and ":" in _p) else ""
                    )  # port name without .1q tag

                    # configured admin-state
                    ifd["is_enabled"] = (
                        True
                        if self._find_txt(
                            if_cfg_block, "configure_ns:admin-state", namespaces=self.nsmap
                        )
                        == "enable"
                        else False
                    )

                # state portion of the port associated with interface
                if_port_state_block = []
                if if_port != "":
                    if_port_state_block = result.find(
                        f'state_ns:state/state_ns:port[state_ns:port-id="{if_port}"]',
                        self.nsmap,
                    )

                if if_mac == "":
                    if if_name != "system":
                        # take port's MAC for non system interfaces
                        if if_port_state_block is not None and len(if_port_state_block) > 0:
                            if_mac = self._find_txt(
                                if_port_state_block,
                                "state_ns:hardware-mac-address",
                                namespaces=self.nsmap,
                            )
                    else:
                        # system interface gets chassis MAC
                        if_mac = self._find_txt(
                            result,
                            "state_ns:state/state_ns:chassis/state_ns:hardware-data/state_ns:base-mac-address",
                            namespaces=self.nsmap,
                        )
                ifd["mac_address"] = if_mac

                # speed is a port inherited value
                if_speed = -1.0  # default value for system/loopback interface
                if if_port:
                    if if_port_state_block is not None and len(if_port_state_block) > 0:
                        if_speed = convert(
                            float,
                            self._find_txt(
                                if_port_state_block,
                                "state_ns:ethernet/state_ns:oper-speed",
                                namespaces=self.nsmap,
                            ),
                        )
                ifd["speed"] = if_speed

                ifd["is_up"] = self._find_txt(
                        if_state, "state_ns:if-oper-status" if self.R19 else "state_ns:oper-state", 
                        namespaces=self.nsmap
                    ) == "up"

                flap_time = self._find_txt(
                    if_state, "state_ns:last-oper-change", namespaces=self.nsmap
                )
                ifd["last_flapped"] = (
                    datetime.datetime.strptime(
                        flap_time, "%Y-%m-%dT%H:%M:%S.%fZ"
                    ).timestamp()
                    if flap_time != ""
                    else -1.0
                )

                ifd["mtu"] = convert(
                    int,
                    self._find_txt(if_state, "state_ns:oper-ip-mtu", namespaces=self.nsmap),
                )
                interfaces[if_name] = ifd

            return interfaces
        except Exception as e:
            print("Error in method get interfaces : {}".format(e))
            log.error("Error in method get interfaces : %s" % traceback.format_exc())

    def get_interfaces_counters(self):
        # (Statistics of all ports and router/interface is taken)
        """
            Returns a dictionary of dictionaries where the first key is an interface name
            and the inner dictionary contains the following keys:
                tx_errors (int)
                rx_errors (int)
                tx_discards (int)
                rx_discards (int)
                tx_octets (int)
                rx_octets (int)
                tx_unicast_packets (int)
                rx_unicast_packets (int)
                tx_multicast_packets (int)
                rx_multicast_packets (int)
                tx_broadcast_packets (int)
                rx_broadcast_packets (int)
        """
        try:
            interface_counters = {}
            result = to_ele(
                self.conn.get(
                    filter=GET_INTERFACES_COUNTERS["_"], with_defaults="report-all"
                ).data_xml
            )
            # Looping through port-list to get statistics of individual port
            for port in result.xpath("state_ns:state/state_ns:port", namespaces=self.nsmap):
                port_id = self._find_txt(port, "state_ns:port-id", namespaces=self.nsmap)
                if port_id == "":
                    continue
                interface_counters[port_id] = {
                    "tx_errors": convert(
                        int,
                        self._find_txt(
                            port,
                            "state_ns:statistics/state_ns:out-errors",
                            namespaces=self.nsmap,
                        ),
                        default=-1,
                    ),
                    "rx_errors": convert(
                        int,
                        self._find_txt(
                            port,
                            "state_ns:statistics/state_ns:in-errors",
                            namespaces=self.nsmap,
                        ),
                        default=-1,
                    ),
                    "tx_discards": convert(
                        int,
                        self._find_txt(
                            port,
                            "state_ns:statistics/state_ns:out-discards",
                            namespaces=self.nsmap,
                        ),
                        default=-1,
                    ),
                    "rx_discards": convert(
                        int,
                        self._find_txt(
                            port,
                            "state_ns:statistics/state_ns:in-discards",
                            namespaces=self.nsmap,
                        ),
                        default=-1,
                    ),
                    "tx_octets": convert(
                        int,
                        self._find_txt(
                            port,
                            "state_ns:statistics/state_ns:out-octets",
                            namespaces=self.nsmap,
                        ),
                        default=-1,
                    ),
                    "rx_octets": convert(
                        int,
                        self._find_txt(
                            port,
                            "state_ns:statistics/state_ns:in-octets",
                            namespaces=self.nsmap,
                        ),
                        default=-1,
                    ),
                    "tx_unicast_packets": convert(
                        int,
                        self._find_txt(
                            port,
                            "state_ns:statistics/state_ns:out-unicast-packets",
                            namespaces=self.nsmap,
                        ),
                        default=-1,
                    ),
                    "rx_unicast_packets": convert(
                        int,
                        self._find_txt(
                            port,
                            "state_ns:statistics/state_ns:in-unicast-packets",
                            namespaces=self.nsmap,
                        ),
                        default=-1,
                    ),
                    "tx_multicast_packets": convert(
                        int,
                        self._find_txt(
                            port,
                            "state_ns:statistics/state_ns:out-multicast-packets",
                            namespaces=self.nsmap,
                        ),
                        default=-1,
                    ),
                    "rx_multicast_packets": convert(
                        int,
                        self._find_txt(
                            port,
                            "state_ns:statistics/state_ns:in-multicast-packets",
                            namespaces=self.nsmap,
                        ),
                        default=-1,
                    ),
                    "tx_broadcast_packets": convert(
                        int,
                        self._find_txt(
                            port,
                            "state_ns:statistics/state_ns:out-broadcast-packets",
                            namespaces=self.nsmap,
                        ),
                        default=-1,
                    ),
                    "rx_broadcast_packets": convert(
                        int,
                        self._find_txt(
                            port,
                            "state_ns:statistics/state_ns:in-broadcast-packets",
                            namespaces=self.nsmap,
                        ),
                        default=-1,
                    ),
                }
            # Looping through interfaces-list to get statistics of interfaces port
            for iface in result.xpath(
                "state_ns:state/state_ns:router/state_ns:interface", namespaces=self.nsmap
            ):
                if_name = self._find_txt(
                    iface, "state_ns:interface-name", namespaces=self.nsmap
                )
                if if_name == "":
                    continue
                interface_counters[if_name] = {
                    "tx_errors": -1,
                    "rx_errors": -1,
                    "tx_discards": convert(
                        int,
                        self._find_txt(
                            iface,
                            "state_ns:statistics/state_ns:ip/state_ns:out-discard-packets",
                            namespaces=self.nsmap,
                        ),
                        default=-1,
                    ),
                    "rx_discards": -1,
                    "tx_octets": convert(
                        int,
                        self._find_txt(
                            iface,
                            "state_ns:statistics/state_ns:ip/state_ns:out-octets",
                            namespaces=self.nsmap,
                        ),
                        default=-1,
                    ),
                    "rx_octets": convert(
                        int,
                        self._find_txt(
                            iface,
                            "state_ns:statistics/state_ns:ip/state_ns:in-octets",
                            namespaces=self.nsmap,
                        ),
                        default=-1,
                    ),
                    "tx_unicast_packets": -1,
                    "rx_unicast_packets": -1,
                    "tx_multicast_packets": -1,
                    "rx_multicast_packets": -1,
                    "tx_broadcast_packets": -1,
                    "rx_broadcast_packets": -1,
                }
            return interface_counters
        except Exception as e:
            print("Error in method get interfaces counters : {}".format(e))
            log.error("Error in method get interfaces counters : %s" % traceback.format_exc())

    def get_network_instances(self, name=""):
        """
           Return a dictionary of network instances (VRFs) configured, including default/global
               Parameters:	name (string) –
               Returns:
                   name (dict)
                       name (unicode)
                       type (unicode)
                       state (dict)
                           route_distinguisher (unicode)
                       interfaces (dict)
                           interface (dict)
                               interface name: (dict)
        """
        try:
            network_instances = {}

            result = to_ele(
                self.conn.get(
                    filter=GET_NETWORK_INSTANCES["_"].format(instance_name=name),
                    with_defaults="report-all",
                ).data_xml
            )

            # helper
            def _get_interfaces_list(instance):
                network_instances[instance_name].update(
                    {
                        "name": instance_name,
                        "state": {
                            "route_distinguisher": self._find_txt(
                                instance,
                                "state_ns:oper-route-distinguisher",
                                namespaces=self.nsmap,
                            )
                        },
                        "interfaces": {"interface": {}},
                    }
                )
                for interface in instance.xpath(
                    "state_ns:interface", namespaces=self.nsmap
                ):
                    interface_name = self._find_txt(
                        interface, "state_ns:interface-name", namespaces=self.nsmap
                    )
                    network_instances[instance_name]["interfaces"]["interface"].update(
                        {interface_name: {}}
                    )

            for router in result.xpath(
                "state_ns:state/state_ns:router", namespaces=self.nsmap
            ):
                instance_name = self._find_txt(
                    router, "state_ns:router-name", namespaces=self.nsmap
                )
                if instance_name == "":
                    continue
                if instance_name == "Base":
                    network_instances.update({instance_name: {"type": "DEFAULT_INSTANCE"}})
                if instance_name == "management":
                    network_instances.update({instance_name: {"type": "MGMT"}})
                _get_interfaces_list(router)

            for vprn_service in result.xpath(
                "state_ns:state/state_ns:service/state_ns:vprn", namespaces=self.nsmap
            ):
                instance_name = self._find_txt(
                    vprn_service, "state_ns:service-name", namespaces=self.nsmap
                )
                if instance_name == "":
                    continue
                network_instances.update({instance_name: {"type": "L3VRF"}})
                _get_interfaces_list(vprn_service)

            for vpls_service in result.xpath(
                "state_ns:state/state_ns:service/state_ns:vpls", namespaces=self.nsmap
            ):
                instance_name = self._find_txt(
                    vpls_service, "state_ns:service-name", namespaces=self.nsmap
                )
                if instance_name == "":
                    continue
                network_instances.update({instance_name: {"type": "VPLS"}})
                _get_interfaces_list(vpls_service)
            return network_instances
        except Exception as e:
            print("Error in method get network instances : {}".format(e))
            log.error("Error in method get network instances : %s", traceback.format_exc())

    def get_config(
        self,
        retrieve="all",
        full=False,
        sanitized=False,
        format="text",
    ):
        """
            Return the configuration of a device.
            Parameters:
                retrieve (string) – Which configuration type you want to populate, default is all of
                them.
                The rest will be set to “”.
                full (bool) – Retrieve all the configuration. For instance, on ios, “sh run all”.
                sanitized(bool) - Remove secret data . Default is false
                optional_args - To define the format
            Returns:
                running(string) - Representation of the native running configuration
                candidate(string) - Representation of the native candidate configuration.
                If the device doesn't differentiate between running and startup configuration this
                will an empty string
                startup(string) - Representation of the native startup configuration.
                If the device doesn't differentiate between running and startup configuration this
                will an empty string
            Return type:
            The object returned is a dictionary with a key for each configuration store
        """
        try:
            configuration = {"running": "", "candidate": "", "startup": ""}
            if self.sros_get_format == "cli" or format == "cli":
                # Getting output in MD-CLI format
                # retrieving config using md-cli
                cmd_running = "admin show configuration | no-more"
                cmd_candidate = ["edit-config read-only", "info | no-more", "quit-config"]

                # helper method
                def _update_buff(buff):
                    if "@nokia.com" in buff:
                        buff = buff.split("@nokia.com.")
                        updated_buff = [buff[1]]
                    else:
                        updated_buff = [buff]
                    new_buff = ""
                    match_strings = [
                        cmd_candidate[0],
                        cmd_candidate[1],
                        cmd_candidate[2],
                        cmd_running,
                    ]
                    count = 1
                    for item in updated_buff[0].split("\n"):
                        row = item.rstrip()
                        if any(match in item for match in match_strings):
                            continue
                        if "[]" in item:
                            continue
                        elif self.cmd_line_pattern_re.search(item) or not row:
                            continue
                        elif "persistent-indices" in item:
                            break
                        else:
                            if "configure" in row and len(row) == 11:
                                new_buff += row.strip() + "\n"
                                count = count + 1
                            else:
                                if count == 1:
                                    continue
                                new_buff += row + "\n"
                    return new_buff[: new_buff.rfind("\n")]

                if retrieve == "running":
                    buff_running = self._perform_cli_commands([cmd_running], True)
                    configuration["running"] = _update_buff(buff_running)
                    return configuration
                elif retrieve == "startup":
                    buff_running = self._perform_cli_commands([cmd_running], True)
                    configuration["startup"] = _update_buff(buff_running)
                    return configuration
                elif retrieve == "candidate":
                    buff_candidate = self._perform_cli_commands(cmd_candidate, True)
                    configuration["candidate"] = _update_buff(buff_candidate)
                    return configuration
                elif retrieve == "all":
                    buff_running = self._perform_cli_commands([cmd_running], True)
                    buff_candidate = self._perform_cli_commands(cmd_candidate, True)
                    configuration["running"] = _update_buff(buff_running)
                    configuration["startup"] = _update_buff(buff_running)
                    configuration["candidate"] = _update_buff(buff_candidate)
                    return configuration

            # returning the config in xml format
            elif self.sros_get_format == "xml" or format == "xml":
                config_data_running_xml = ""
                if retrieve == "running" or retrieve == "all":
                    config_data_running = to_ele(
                        self.conn.get_config(source="running").data_xml
                    )
                    config_data_running_xml = to_xml(
                        config_data_running.xpath(
                            "configure_ns:configure", namespaces=self.nsmap
                        )[0]
                    )
                    # remove xml declaration
                    config_data_running_xml = re.sub(
                        r"<\?xml.*\?>", "", config_data_running_xml
                    )
                    configuration["running"] = config_data_running_xml

                if retrieve == "startup" or retrieve == "all":
                    configuration["startup"] = config_data_running_xml

                if retrieve == "candidate" or retrieve == "all":
                    config_data_candidate = to_ele(
                        self.conn.get_config(source="candidate").data_xml
                    )
                    config_data_candidate_xml = to_xml(
                        config_data_candidate.xpath(
                            "configure_ns:configure", namespaces=self.nsmap
                        )[0]
                    )
                    config_data_candidate_xml = re.sub(
                        r"<\?xml.*\?>", "", config_data_candidate_xml
                    )
                    configuration["candidate"] = config_data_candidate_xml
                return configuration
                
            if self.sros_get_format == "flat" or format == "flat":
                # Getting output in MD-CLI format
                # retrieving config using md-cli
                #cmd_running = "admin show configuration flat | no-more"
                cmd_running = ["edit-config read-only", "configure", "info flat | no-more", "quit-config"]
                cmd_candidate = ["edit-config read-only", "info flat | no-more", "quit-config"]

                # helper method
                def _update_buff(buff):
                    if "@nokia.com" in buff:
                        buff = buff.split("@nokia.com.")
                        updated_buff = [buff[1]]
                    else:
                        updated_buff = [buff]
                    new_buff = ""
                    match_strings = [
                        cmd_candidate[0],
                        cmd_candidate[1],
                        cmd_candidate[2],
                        #cmd_running,
                        cmd_running[0],
                        cmd_running[1],
                        cmd_running[2],
                        cmd_running[3],
                    ]
                    count = 1
                    for item in updated_buff[0].split("\n"):
                        row = item.rstrip()
                        if any(match in item for match in match_strings):
                            continue
                        if "[]" in item:
                            continue
                        elif self.cmd_line_pattern_re.search(item) or not row:
                            continue
                        elif "*(ro)[/configure]" in row:
                            continue
                        else:
                            new_buff += row.strip() + "\n"
                    return new_buff[: new_buff.rfind("\n")]

                if retrieve == "running":
                    buff_running = self._perform_cli_commands(cmd_running, True)
                    configuration["running"] = _update_buff(buff_running)
                    return configuration
                elif retrieve == "startup":
                    buff_running = self._perform_cli_commands(cmd_running, True)
                    configuration["startup"] = _update_buff(buff_running)
                    return configuration
                elif retrieve == "candidate":
                    buff_candidate = self._perform_cli_commands(cmd_candidate, True)
                    configuration["candidate"] = _update_buff(buff_candidate)
                    return configuration
                elif retrieve == "all":
                    buff_running = self._perform_cli_commands(cmd_running, True)
                    buff_candidate = self._perform_cli_commands(cmd_candidate, True)
                    configuration["running"] = _update_buff(buff_running)
                    configuration["startup"] = _update_buff(buff_running)
                    configuration["candidate"] = _update_buff(buff_candidate)
                    return configuration
        
        except Exception as e:
            print("Error in method get config : {}".format(e))
            log.error("Error in method get config : %s" % traceback.format_exc())

    def get_optics(self):
        """
            Fetches the power usage on the various transceivers installed on the switch (in dbm),
            and returns a view that conforms with the openconfig model
            openconfig-platform-transceiver.yang
                Returns a dictionary where the keys are as listed below:
                intf_name (unicode)
                    physical_channels
                        channels (list of dicts)
                            index (int)
                            state
                                input_power
                                    instant (float)
                                    avg (float)
                                    min (float)
                                    max (float)
                                output_power
                                    instant (float)
                                    avg (float)
                                    min (float)
                                    max (float)
                                laser_bias_current
                                    instant (float)
                                    avg (float)
                                    min (float)
                                    max (float)
        """
        try:
            optics_dict = {}

            result = to_ele(
                self.conn.get(filter=GET_OPTICS["_"], with_defaults="report-all").data_xml
            )

            for port in result.xpath("state_ns:state/state_ns:port", namespaces=self.nsmap):
                port_id = self._find_txt(
                    port, "state_ns:port-id", namespaces=self.nsmap
                )  # port-name
                optics_dict[port_id] = {"physical_channels": {"channel": []}}

                for lane in port.xpath(
                    "state_ns:transceiver/state_ns:digital-diagnostic-monitoring/state_ns:lane",
                    namespaces=self.nsmap,
                ):
                    optics_dict[port_id]["physical_channels"]["channel"].append(
                        {
                            "index": convert(
                                int,
                                self._find_txt(
                                    lane, "state_ns:lane-id", namespaces=self.nsmap
                                ),
                                default=-1,
                            ),
                            "state": {
                                "input_power": {
                                    "instant": convert(
                                        float,
                                        self._find_txt(
                                            lane,
                                            "state_ns:received-optical-power/state_ns:current",
                                            namespaces=self.nsmap,
                                        ),
                                        default=-1.0,
                                    ),
                                    "avg": -1.0,  # default value as avg information in YANG
                                    "min": -1.0,  # default value as min information in YANG
                                    "max": -1.0,  # default value as max information in YANG
                                },
                                "output_power": {
                                    "instant": convert(
                                        float,
                                        self._find_txt(
                                            lane,
                                            "state_ns:transmit-output-power/state_ns:current",
                                            namespaces=self.nsmap,
                                        ),
                                        default=-1.0,
                                    ),
                                    "avg": -1.0,  # default value as avg information in YANG
                                    "min": -1.0,  # default value as min information in YANG
                                    "max": -1.0,  # default value as max information in YANG
                                },
                                "laser_bias_current": {
                                    "instant": convert(
                                        float,
                                        self._find_txt(
                                            lane,
                                            "state_ns:transmit-bias-current/state_ns:current",
                                            namespaces=self.nsmap,
                                        ),
                                        default=-1.0,
                                    ),
                                    "avg": -1.0,  # default value as avg information in YANG
                                    "min": -1.0,  # default value as min information in YANG
                                    "max": -1.0,  # default value as max information in YANG
                                },
                            },
                        }
                    )
            return optics_dict
        except Exception as e:
            print("Error in method get optics : {}".format(e))
            log.error("Error in method get optics : %s" % traceback.format_exc())

    def get_arp_table(self, vrf=""):
        """
            Returns a list of dictionaries having the following set of keys:
                interface (string)
                mac (string)
                ip (string)
                age (float)
            ‘vrf’ of null-string will default to all VRFs.
            Specific ‘vrf’ will return the ARP table entries for that VRFs
             (including potentially ‘default’ or ‘global’).

            In all cases the same data structure is returned and no reference to the VRF that was
            used is included in the output.
        """
        try:
            arp_table = []

            # helper function

            def _get_arp_table(neighbor_discovered):

                arp_table.append(
                    {
                        "interface": interface_name,
                        "mac": self._find_txt(
                            neighbor_discovered,
                            "state_ns:mac-address",
                            namespaces=self.nsmap,
                        ),
                        "ip": self._find_txt(
                            neighbor_discovered,
                            "state_ns:ipv4-address",
                            namespaces=self.nsmap,
                        ),
                        "age": convert(
                            float,
                            self._find_txt(
                                neighbor_discovered,
                                "state_ns:timer",
                                namespaces=self.nsmap,
                            ),
                        ),
                    }
                )

            result = to_ele(
                self.conn.get(
                    filter=GET_ARP_TABLE["_"].format(vrf=vrf), with_defaults="report-all",
                ).data_xml
            )

            for interface in result.xpath(
                "state_ns:state/state_ns:router/state_ns:interface", namespaces=self.nsmap
            ):
                interface_name = self._find_txt(
                    interface, "state_ns:interface-name", namespaces=self.nsmap
                )

                for neighbor in interface.xpath(
                    "state_ns:ipv4/state_ns:neighbor-discovery/state_ns:neighbor",
                    namespaces=self.nsmap,
                ):

                    discovered_nei_ip = self._find_txt(
                        neighbor, "state_ns:ipv4-address", namespaces=self.nsmap,
                    )
                    if discovered_nei_ip == "":
                        continue
                    _get_arp_table(neighbor)

            for interface in result.xpath(
                "state_ns:state/state_ns:service/state_ns:vprn/state_ns:interface",
                namespaces=self.nsmap,
            ):
                for neighbor in interface.xpath(
                    "state_ns:ipv4/state_ns:neighbor-discovery/state_ns:neighbor",
                    namespaces=self.nsmap,
                ):
                    discovered_nei_ip = self._find_txt(
                        interface, "state_ns:ipv4-address", namespaces=self.nsmap,
                    )
                    if discovered_nei_ip == "":
                        continue
                    _get_arp_table(neighbor)
            return arp_table
        except Exception as e:
            print("Error in method get arp table : {}".format(e))
            log.error("Error in method get arp table : %s" % traceback.format_exc())

    def get_interfaces_ip(self):
        # per router/interface and service/vprn/interface
        """
            Returns all configured IP addresses on all interfaces as a dictionary of dictionaries.
            of the main dictionary represent the name of the interface.
            Values of the main dictionary represent are dictionaries that may consist of two keys
            ‘ipv4’ and ‘ipv6’ (one, both or none) which are themselves dictionaries with the IP addresses as keys.
            Each IP Address dictionary has the following keys:
                prefix_length (int)
        """
        try:
            interfaces_ip = {}

            result = to_ele(
                self.conn.get(
                    filter=GET_INTERFACES_IP["_"], with_defaults="report-all"
                ).data_xml
            )

            xpath_iface_filter = "configure_ns:configure/configure_ns:router/configure_ns:interface | \
                            configure_ns:configure/configure_ns:service/configure_ns:vprn/configure_ns:interface"

            for interface in result.xpath(xpath_iface_filter, namespaces=self.nsmap):
                interface_name = self._find_txt(
                    interface, "configure_ns:interface-name", namespaces=self.nsmap
                )
                if interface_name == "":
                    continue
                interfaces_ip[interface_name] = {}
                ipv4_primary_address = self._find_txt(
                    interface,
                    "configure_ns:ipv4/configure_ns:primary/configure_ns:address",
                    namespaces=self.nsmap,
                )
                if ipv4_primary_address != "":
                    interfaces_ip[interface_name]["ipv4"] = {
                        ipv4_primary_address: {
                            "prefix_length": convert(
                                int,
                                self._find_txt(
                                    interface,
                                    "configure_ns:ipv4/configure_ns:primary/configure_ns:prefix-length",
                                    namespaces=self.nsmap,
                                ),
                                default="N/A",
                            )
                        }
                    }
                ipv4_secondary_address = self._find_txt(
                    interface,
                    "configure_ns:ipv4/configure_ns:secondary/configure_ns:address",
                    namespaces=self.nsmap,
                )
                if ipv4_secondary_address != "":
                    interfaces_ip[interface_name]["ipv4"] = {
                        ipv4_secondary_address: {
                            "prefix_length": convert(
                                int,
                                self._find_txt(
                                    interface,
                                    "configure_ns:ipv4/configure_ns:secondary/configure_ns:prefix-length",
                                    namespaces=self.nsmap,
                                ),
                                default="N/A",
                            )
                        }
                    }
                ipv6_address = self._find_txt(
                    interface,
                    "configure_ns:ipv6/configure_ns:address/configure_ns:ipv6-address",
                    namespaces=self.nsmap,
                )
                if ipv6_address != "":
                    interfaces_ip[interface_name]["ipv6"] = {
                        ipv6_address: {
                            "prefix_length": convert(
                                int,
                                self._find_txt(
                                    interface,
                                    "configure_ns:ipv6/configure_ns:address/configure_ns:prefix-length",
                                    namespaces=self.nsmap,
                                ),
                                default="N/A",
                            )
                        }
                    }

            return interfaces_ip
        except Exception as e:
            print("Error in method get interfaces ip : {}".format(e))
            log.error("Error in method get interfaces ip : %s" % traceback.format_exc())

    def get_ntp_peers(self):
        """
            Returns the NTP peers configuration as dictionary.
            The keys of the dictionary represent the IP Addresses of the peers.
            Inner dictionaries do not have yet any available keys.
        """
        try:
            ntp_peers = {}
            result = to_ele(
                self.conn.get(
                    filter=GET_NTP_PEERS["_"], with_defaults="report-all"
                ).data_xml
            )

            for peer in result.xpath(
                "state_ns:state/state_ns:system/state_ns:time/state_ns:ntp/state_ns:peer",
                namespaces=self.nsmap,
            ):
                ntp_peers.update(
                    {
                        ip(
                            self._find_txt(
                                peer, "state_ns:ip-address", namespaces=self.nsmap
                            )
                        ): {}
                    }
                )
            return ntp_peers
        except Exception as e:
            print("Error in method get ntp peers : {}".format(e))
            log.error("Error in method get ntp peers : %s" % traceback.format_exc())

    def get_ntp_servers(self):
        """
            Returns the NTP servers configuration as dictionary.
            The keys of the dictionary represent the IP Addresses of the servers.
            Inner dictionaries do not have yet any available keys.
        """
        try:
            ntp_servers = {}
            result = to_ele(
                self.conn.get(
                    filter=GET_NTP_SERVERS["_"], with_defaults="report-all"
                ).data_xml
            )

            for server in result.xpath(
                "state_ns:state/state_ns:system/state_ns:time/state_ns:ntp/state_ns:server",
                namespaces=self.nsmap,
            ):
                ntp_servers.update(
                    {
                        ip(
                            self._find_txt(
                                server, "state_ns:ip-address", namespaces=self.nsmap
                            )
                        ): {}
                    }
                )
            return ntp_servers
        except Exception as e:
            print("Error in method get ntp servers : {}".format(e))
            log.error("Error in method get ntp servers : %s" % traceback.format_exc())

    def get_ntp_stats(self):
        """
        Returns a list of NTP synchronization statistics.
            remote (string)
            referenceid (string)
            synchronized (True/False)
            stratum (int)
            type (string)
            when (string)
            hostpoll (int)
            reachability (int)
            delay (float)
            offset (float)
            jitter (float)
        """
        try:
            ntp_stats_list = []

            # helper method
            def _get_ntp_stats_data(buff):
                dashed_row = False
                temp_dict = {}
                for item in buff.split("\n"):
                    if "---" in item:
                        dashed_row = True
                        continue
                    if self.ipv4_address_re.search(item) or dashed_row:
                        row = item.strip()
                        row_list = row.split()
                        if len(row_list) == 8:
                            temp_dict = {
                                "referenceid": row_list[1],
                                "synchronized": True if row_list[0] == "chosen" else False,
                                "stratum": convert(int, row_list[2]),
                                "type": row_list[3],
                                "hostpoll": convert(int, row_list[5]),
                                "offset": convert(float, row_list[7]),
                            }

                        if len(row_list) == 2:
                            dashed_row = False
                            temp_dict.update(
                                {
                                    "remote": row_list[1],
                                    "when": "",
                                    "reachability": -1,
                                    "delay": -1.0,
                                    "jitter": -1.0,
                                }
                            )
                ntp_stats_list.append(temp_dict)
                return ntp_stats_list

            cmd = ["/show system ntp servers"]
            buff_servers = self._perform_cli_commands(cmd, True, no_more=True)
            ntp_stats_list = _get_ntp_stats_data(buff_servers)
            cmd = ["/show system ntp peers"]
            buff_peers = self._perform_cli_commands(cmd, True, no_more=True)
            ntp_stats_list = _get_ntp_stats_data(buff_peers)

            return ntp_stats_list
        except Exception as e:
            print("Error in method get ntp stats : {}".format(e))
            log.error("Error in method get ntp stats : %s" % traceback.format_exc())

    def get_snmp_information(self):

        """
            Returns a dict of dicts containing SNMP configuration. Each inner dictionary contains these fields
                chassis_id (string)
                community (dictionary)
                contact (string)
                location (string)
                ‘community’ is a dictionary with community string specific information, as follows:
                    acl (string) # acl number or name
                    mode (string) # read-write (rw), read-only (ro)
        """
        try:
            snmp_information = {}
            result = to_ele(
                self.conn.get(
                    filter=GET_SNMP_INFORMATION["_"], with_defaults="report-all"
                ).data_xml
            )

            for system in result.xpath(
                "configure_ns:configure/configure_ns:system", namespaces=self.nsmap
            ):
                snmp_information["chassis_id"] = self._find_txt(
                    system, "configure_ns:name", namespaces=self.nsmap
                )
                snmp_information["contact"] = self._find_txt(
                    system, "configure_ns:contact", namespaces=self.nsmap
                )
                snmp_information["location"] = self._find_txt(
                    system, "configure_ns:location", namespaces=self.nsmap
                )
                snmp_information["community"] = {}

                for community in system.xpath(
                    "configure_ns:security/configure_ns:snmp/configure_ns:community",
                    namespaces=self.nsmap,
                ):
                    community_string = self._find_txt(
                        community, "configure_ns:community-string", namespaces=self.nsmap
                    )
                    if community_string == "":
                        continue
                    if community_string not in snmp_information["community"].keys():
                        snmp_information["community"].update({community_string: {}})
                    snmp_information["community"][community_string].update(
                        {
                            "acl": self._find_txt(
                                community,
                                "configure_ns:source-access-list",
                                namespaces=self.nsmap,
                            ),
                            "mode": self._find_txt(
                                community,
                                "configure_ns:access-permissions",
                                namespaces=self.nsmap,
                            ),
                        }
                    )

            return snmp_information
        except Exception as e:
            print("Error in method get snmp information : {}".format(e))
            log.error("Error in method get snmp information : %s" % traceback.format_exc())

    def get_users(self):
        """
            Returns a dictionary with the configured users.
            The keys of the main dictionary represents the username.
            The values represent the details of the user, represented by the following keys:
                level (int)
                password (str)
                sshkeys (list)
            The level is an integer between 0 and 15, where 0 is the lowest access
            and 15 represents full access to the device.
        """
        try:
            users_dict = {}
            profile_dict = {}
            result = to_ele(
                self.conn.get(filter=GET_USERS["_"], with_defaults="report-all").data_xml
            )

            for profile in result.xpath(
                "configure_ns:configure/configure_ns:system/configure_ns:security/configure_ns:aaa/configure_ns:local-profiles/configure_ns:profile",
                namespaces=self.nsmap,
            ):
                profile_name = self._find_txt(
                    profile, "configure_ns:user-profile-name", namespaces=self.nsmap
                )
                if profile_name == "":
                    continue
                number = ""
                if any(i.isdigit() for i in profile_name):
                    number = int("".join(filter(str.isdigit, profile_name)))
                profile_dict.update({profile_name: number})

            for user in result.xpath(
                "configure_ns:configure/configure_ns:system/configure_ns:security/configure_ns:user-params/configure_ns:local-user/configure_ns:user",
                namespaces=self.nsmap,
            ):
                user_name = self._find_txt(
                    user, "configure_ns:user-name", namespaces=self.nsmap
                )
                password = self._find_txt(
                    user, "configure_ns:password", namespaces=self.nsmap
                )
                member = self._find_txt(
                    user, "configure_ns:console/configure_ns:member", namespaces=self.nsmap
                )
                level = profile_dict.get(member)
                keys_list = []

                for key in user.xpath(
                    "configure_ns:public-keys/configure_ns:rsa/configure_ns:rsa-key",
                    namespaces=self.nsmap,
                ):
                    keys_list.append(
                        self._find_txt(key, "configure_ns:key-value", namespaces=self.nsmap)
                    )
                users_dict[user_name] = {
                    "level": convert(int, level, default=0),
                    "password": password,
                    "sshkeys": keys_list,
                }
            return users_dict
        except Exception as e:
            print("Error in method get users : {}".format(e))
            log.error("Error in method get users : %s" % traceback.format_exc())

    def get_route_to(self, destination="", protocol="", longer=False):
        """
        Returns a dictionary of dictionaries containing details of all available routes to a destination.

        Parameters:
        destination – The destination prefix to be used when filtering the routes.
        (optional) (protocol) – Retrieve the routes only for a specific protocol.
        (optional) – Retrieve more specific routes as well.
        Each inner dictionary contains the following fields:

            protocol (string)
            current_active (True/False)
            last_active (True/False)
            age (int)
            next_hop (string)
            outgoing_interface (string)
            selected_next_hop (True/False)
            preference (int)
            inactive_reason (string)
            routing_table (string)
            protocol_attributes (dictionary)
            protocol_attributes is a dictionary with protocol-specific information, as follows:
            BGP
                local_as (int)
                remote_as (int)
                peer_id (string)
                as_path (string)
                communities (list)
                local_preference (int)
                preference2 (int)
                metric (int)
                metric2 (int)
            ISIS:
                level (int)
        """

        # helper functions
        try:
            route_to_dict = {}

            def _get_protocol_attributes(router_name, local_protocol):
                # destination needs to be with prefix
                command = f"/show router {router_name} route-table {destination} protocol {local_protocol} extensive all"
                output = self._perform_cli_commands([command], True, no_more=True)
                destination_address_with_prefix = ""
                next_hop_once = False
                next_hop = ""
                age = ""
                preference = ""
                for item_1 in re.split("\n|\r", output):
                    if "Dest Prefix" in item_1:
                        row_1 = item_1.strip()
                        row_1_list = row_1.split(": ")
                        destination_address_with_prefix = row_1_list[1]
                        route_to_dict.update(
                            {
                                row_1_list[1]: [
                                    {
                                        "routing_table": router_name,
                                        "protocol": local_protocol,
                                        "last_active": False,
                                        "inactive_reason": "",
                                    }
                                ]
                            }
                        )
                    elif "Age" in item_1:
                        row_1 = item_1.strip()
                        row_1_list = row_1.split(": ")
                        if "d" in row_1_list[1]:
                            time_string = re.split("d|h|m", row_1_list[1])
                        else:
                            time_string = re.split("h|m|s", row_1_list[1])
                        age = (
                            (int(time_string[0]) * 86400)
                            + (int(time_string[1]) * 60 * 60)
                            + (int(time_string[2]) * 60)
                        )
                        for d in route_to_dict[destination_address_with_prefix]:
                            d.update({"age": age})
                    elif "Preference" in item_1:
                        row_1 = item_1.strip()
                        row_1_list = row_1.split(": ")
                        preference = row_1_list[1]
                        for d in route_to_dict[destination_address_with_prefix]:
                            d.update({"preference": convert(int, preference, default=-1)})
                    elif "Active" in item_1:
                        row_1 = item_1.strip()
                        row_1_list = row_1.split(": ")
                        for d in route_to_dict[destination_address_with_prefix]:
                            if next_hop_once:
                                if d.get("next_hop") == next_hop:
                                    d.update(
                                        {
                                            "current_active": True
                                            if row_1_list[1] is True
                                            else False
                                        }
                                    )
                            else:
                                d.update(
                                    {
                                        "current_active": True
                                        if row_1_list[1] is True
                                        else False
                                    }
                                )
                    elif "Next-Hop" in item_1:
                        row_1 = item_1.strip()
                        row_1_list = row_1.split(": ")
                        _sel = self.ipv4_address_re.search(row_1_list[1])
                        temp_2_dict = {"selected_next_hop": bool(_sel)}
                        if "Indirect" in item_1:
                            if next_hop_once:
                                next_hop = row_1_list[1]
                                route_to_dict[destination_address_with_prefix].append(
                                    {
                                        "routing_table": router_name,
                                        "protocol": protocol,
                                        "next_hop": row_1_list[1],
                                        "age": age,
                                        "preference": convert(int, preference, default=-1),
                                        "last_active": False,  # default value as SROS does not have this value
                                        "inactive_reason": "",
                                    }
                                )
                                for d in route_to_dict[destination_address_with_prefix]:
                                    if d.get("next_hop") == next_hop:
                                        d.update(temp_2_dict)
                            else:
                                for d in route_to_dict[destination_address_with_prefix]:
                                    d.update({"next_hop": row_1_list[1]})
                                    d.update(temp_2_dict)
                                next_hop_once = True
                                next_hop = row_1_list[1]
                        elif "Resolving" in item_1:
                            for d in route_to_dict[destination_address_with_prefix]:
                                if d.get("next_hop") == next_hop:
                                    d.update(temp_2_dict)
                                    d.update({"next_hop": row_1_list[1]})
                            next_hop = row_1_list[1]
                        else:
                            for d in route_to_dict[destination_address_with_prefix]:
                                d.update({"next_hop": row_1_list[1]})
                                d.update(temp_2_dict)
                            next_hop_once = True
                            next_hop = row_1_list[1]
                    elif "Interface" in item_1:
                        row_1 = item_1.strip()
                        row_1_list = row_1.split(": ")
                        for d in route_to_dict[destination_address_with_prefix]:
                            if d.get("next_hop") == next_hop:
                                d.update({"outgoing_interface": row_1_list[1]})
                    elif "Metric" in item_1:
                        if local_protocol == "bgp":
                            row_1 = item_1.strip()
                            row_1_list = row_1.split(": ")
                            for d in route_to_dict[destination_address_with_prefix]:
                                if d.get("next_hop") == next_hop:
                                    # Update BGP protocol attributes dictionary
                                    d.update(
                                        {
                                            "protocol_attributes": {
                                                "metric": convert(
                                                    int, row_1_list[1], default=-1
                                                ),
                                                "metric2": -1,  # default value as SROS does not have this
                                                "preference2": convert(
                                                    int, preference, default=-1
                                                ),
                                            }
                                        }
                                    )

            # Method for extracting BGP protocol attributes from router
            def _get_bgp_protocol_attributes(router_name):
                destination_address_with_prefix = ""

                for k, v in route_to_dict.items():
                    destination_address_with_prefix = k
                if destination_address_with_prefix:
                    # protocol attributes local_as, as_path, local_preference
                    cmd = f"/show router {router_name} bgp routes {destination_address_with_prefix} detail"
                    buff_1 = self._perform_cli_commands( [cmd], True, no_more=True )

                    for d in route_to_dict[destination_address_with_prefix]:
                        next_hop = d.get("next_hop")

                        # protocol attributes peer_id and remote_as
                        match_router = False
                        for bgp_neighbor in result.xpath(
                            "state_ns:state/state_ns:router/state_ns:bgp/state_ns:neighbor",
                            namespaces=self.nsmap,
                        ):
                            ip_address = self._find_txt(
                                bgp_neighbor, "state_ns:ip-address", namespaces=self.nsmap
                            )
                            if ip_address == next_hop:
                                match_router = True
                                d["protocol_attributes"].update(
                                    {
                                        "peer_id": self._find_txt(
                                            bgp_neighbor,
                                            "state_ns:statistics/state_ns:peer-identifier",
                                            namespaces=self.nsmap,
                                        ),
                                        "remote_as": convert(
                                            int,
                                            self._find_txt(
                                                bgp_neighbor,
                                                "state_ns:statistics/state_ns:peer-as",
                                                namespaces=self.nsmap,
                                            ),
                                            default=-1,
                                        ),
                                    }
                                )
                                # update bgp protocol for protocol attributes local_as, as_path, local_preference
                                _update_bgp_protocol_attributes(buff_1, d)
                                break
                        if not match_router:
                            for vprn_bgp_neighbor in result.xpath(
                                "state_ns:state/state_ns:service/state_ns:vprn/state_ns:bgp/state_ns:neighbor",
                                namespaces=self.nsmap,
                            ):
                                ip_address = self._find_txt(
                                    vprn_bgp_neighbor,
                                    "state_ns:ip-address",
                                    namespaces=self.nsmap,
                                )
                                if ip_address == next_hop:
                                    d["protocol_attributes"].update(
                                        {
                                            "peer_id": self._find_txt(
                                                vprn_bgp_neighbor,
                                                "state_ns:statistics/state_ns:peer-identifier",
                                                namespaces=self.nsmap,
                                            ),
                                            "remote_as": self._find_txt(
                                                vprn_bgp_neighbor,
                                                "state_ns:statistics/state_ns:peer-as",
                                                namespaces=self.nsmap,
                                            ),
                                        }
                                    )
                                    # update bgp protocol for protocol attributes local_as, as_path, local_preference
                                    _update_bgp_protocol_attributes(buff_1, d)
                                    break

            def _update_bgp_protocol_attributes(buff_1, d):
                modified_attributes = False
                for item_1 in buff_1.split("\n"):
                    if "Modified Attributes" in item_1:
                        modified_attributes = True
                        continue
                    if "Local AS" in item_1:
                        row_1 = item_1.strip()
                        row_1_list = row_1.split(":")
                        d["protocol_attributes"].update(
                            {"local_as": convert(int, row_1_list[3], default=-1)}
                        )
                    elif "AS-Path" in item_1 and modified_attributes:
                        row_1 = item_1.strip()
                        row_1_list = row_1.split(": ")
                        d["protocol_attributes"].update({"as_path": row_1_list[1]})
                        modified_attributes = False
                    elif "Local Pref." in item_1 and modified_attributes:
                        row_1 = item_1.strip()
                        row_1_list = row_1.split(": ")
                        d["protocol_attributes"].update(
                            {
                                "local_preference": convert(
                                    int, row_1_list[1].split(" ")[0], default=-1
                                )
                            }
                        )
                    elif "Community" in item_1 and modified_attributes:
                        row_1 = item_1.strip()
                        row_1_list = row_1.split(": ")
                        multiple_community = row_1_list[1].split(" ")
                        d["protocol_attributes"].update({"communities": multiple_community})

            # Method for extracting ISIS protocol attributes from router
            def _get_isis_protocol_attributes(router_name):
                destination_address_with_prefix = ""
                for k, v in route_to_dict.items():
                    destination_address_with_prefix = k
                if destination_address_with_prefix:
                    for d in route_to_dict[destination_address_with_prefix]:
                        d.update({"protocol_attributes": {}})
                    command = f"/show router {router_name} isis routes ip-prefix-prefix-length {destination_address_with_prefix}"
                    buff_1 = self._perform_cli_commands([command], True, no_more=True)
                    prev_row = ""
                    for item_1 in buff_1.split("\n"):
                        if destination_address_with_prefix in item_1 or prev_row:
                            if "# show" in item_1:
                                continue
                            row_1 = item_1.strip()
                            row_1_list = row_1.split()
                            if len(row_1_list) > 3:
                                prev_row = row
                                temp_list = row_1_list[2].split("/")
                            else:
                                next_hop = row_1_list[0]
                                prev_row = ""
                                for d in route_to_dict[destination_address_with_prefix]:
                                    if d.get("next_hop") == next_hop:
                                        d["protocol_attributes"].update(
                                            {"level": temp_list[0]}
                                        )

            # Method for extracting OSPF protocol attributes from router
            def _get_ospf_protocol_attributes(router_name):
                destination_address_with_prefix = ""
                for k, v in route_to_dict.items():
                    destination_address_with_prefix = k
                if destination_address_with_prefix:
                    for d in route_to_dict[destination_address_with_prefix]:
                        d.update({"protocol_attributes": {}})
                    command = f"/show router {router_name} ospf routes {destination_address_with_prefix}"
                    buff_1 = self._perform_cli_commands([command], True, no_more=True)
                    first_row = False
                    for item_1 in buff_1.split("\n"):
                        if destination_address_with_prefix in item_1 or first_row:
                            if "# show" in item_1:
                                continue
                            if not first_row:
                                first_row = True
                                continue
                            row_1 = item_1.strip()
                            row_1_list = row_1.split()
                            next_hop = row_1_list[0]
                            first_row = False
                            for d in route_to_dict[destination_address_with_prefix]:
                                if d.get("next_hop") == next_hop:
                                    d["protocol_attributes"].update({"cost": row_1_list[2]})

            result = to_ele(
                self.conn.get(filter=GET_ROUTE_TO["_"], with_defaults="report-all").data_xml
            )

            name_list = []
            for router in result.xpath(
                "state_ns:state/state_ns:router", namespaces=self.nsmap
            ):
                name_list.append(
                    self._find_txt(router, "state_ns:router-name", namespaces=self.nsmap)
                )
            for vprn in result.xpath(
                "state_ns:state/state_ns:service/state_ns:vprn", namespaces=self.nsmap
            ):
                name_list.append(
                    self._find_txt(vprn, "state_ns:oper-service-id", namespaces=self.nsmap)
                )

            for name in name_list:

                bgp_once = False
                isis_once = False
                local_once = False
                ospf_once = False
                static_once = False

                if longer:
                    if "/" not in destination:
                        destination_address_with_prefix = destination + "/32"
                    else:
                        destination_address_with_prefix = destination
                    cmd = f"/show router {name} route-table {destination_address_with_prefix} longer\n"
                else:
                    cmd = f"/show router {name} route-table {destination} \n"

                buff = self._perform_cli_commands([cmd], True, no_more=True)
                for item in buff.split("\n"):
                    if self.ipv4_address_re.search(item):
                        if "# show" in item:
                            continue
                        row = item.strip()
                        row_list = row.split()
                        if len(row_list) > 2:
                            local_protocol = row_list[2].lower()
                            if local_protocol == "bgp":
                                if not bgp_once:
                                    _get_protocol_attributes(name, local_protocol)
                                    bgp_once = True
                                    _get_bgp_protocol_attributes(name)
                            if local_protocol == "isis":
                                if not isis_once:
                                    _get_protocol_attributes(name, local_protocol)
                                    isis_once = True
                                    _get_isis_protocol_attributes(name)
                            elif local_protocol == "local":
                                if not local_once:
                                    _get_protocol_attributes(name, local_protocol)
                                    local_once = True
                            elif local_protocol == "ospf":
                                if not ospf_once:
                                    _get_protocol_attributes(name, local_protocol)
                                    ospf_once = True
                                    _get_ospf_protocol_attributes(name)
                            elif local_protocol == "static":
                                if not static_once:
                                    _get_protocol_attributes(name, local_protocol)
                                    static_once = True
            return route_to_dict
        except Exception as e:
            print("Error in method get route to : {}".format(e))
            log.error("Error in method get route to : %s" % traceback.format_exc())

    def get_probes_results(self):
        # for base router
        """
        Returns a dictionary with the results of the probes. The keys of the main dictionary represent
        the name of the probes. Each probe consists on multiple tests, each test name being a key
        in the probe dictionary. A test has the following keys:
            target (str)
            source (str)
            probe_type (str)
            probe_count (int)
            rtt (float)
            round_trip_jitter (float)
            current_test_loss (float)
            current_test_min_delay (float)
            current_test_max_delay (float)
            current_test_avg_delay (float)
            last_test_min_delay (float)
            last_test_max_delay (float)
            last_test_avg_delay (float)
            global_test_min_delay (float)
            global_test_max_delay (float)
            global_test_avg_delay (float)
        """
        try:
            probes_results = {}

            result = to_ele(
                self.conn.get(
                    filter=GET_PROBES_CONFIG["_"], with_defaults="report-all"
                ).data_xml,
            )
            for probe in result.xpath(
                "configure_ns:configure/configure_ns:saa/configure_ns:owner",
                namespaces=self.nsmap,
            ):
                probe_name = self._find_txt(
                    probe, "configure_ns:owner-name", namespaces=self.nsmap
                )
                if probe_name == "":
                    continue
                test_name = self._find_txt(
                    probe, "configure_ns:test", namespaces=self.nsmap
                )
                if test_name == "":
                    continue
                if probe_name not in probes_results.keys():
                    probes_results.update({probe_name: {}})
                probes_results[probe_name].update({test_name: {}})
                path = "configure_ns:type/configure_ns:icmp-ping"
                cmd = f"/show saa {test_name}"
                buff = self._perform_cli_commands([cmd], True, no_more=True)
                test_number_1 = ""
                test_number_2 = 0
                found_first_test = False
                found_second_test = False
                last_test_min_delay = ""
                last_test_max_delay = ""
                last_test_avg_delay = ""
                current_test_min_delay = ""
                current_test_max_delay = ""
                current_test_avg_delay = ""
                roundtrip_jitter = ""
                for item in buff.split("\n"):
                    if "Test runs since last clear" in item:
                        row = item.strip()
                        row_list = row.split(": ")
                        if int(row_list[1]) > 0:
                            test_number_1 = row_list[1]
                            test_number_2 = int(test_number_1) - 1
                            continue
                        else:
                            break
                    if test_number_2 > 0:
                        if str(test_number_2) in item:
                            found_second_test = True
                            total_number_of_attempts = 0
                        if found_second_test:
                            if "Total number of attempts" in item:
                                row_1 = item.strip()
                                row_1_list = row_1.split(": ")
                                total_number_of_attempts = int(row_1_list[1])
                            elif "failed to be sent out" in item:
                                row_1 = item.strip()
                                row_1_list = row_1.split(": ")
                                requests_failed_to_be_sent_out = int(row_1_list[1])
                                if total_number_of_attempts > 0:
                                    last_test_loss = float(
                                        requests_failed_to_be_sent_out
                                        / total_number_of_attempts
                                    )
                            if "Roundtrip" in item:
                                test_number_2 = 0
                                row_1 = item.strip()
                                row_1_list = row_1.split()
                                last_test_min_delay = float(row_1_list[2])
                                last_test_max_delay = row_1_list[3]
                                last_test_avg_delay = row_1_list[4]

                    if test_number_1:
                        if test_number_1 in item:
                            found_first_test = True
                        if found_first_test:
                            if "Roundtrip" in item:
                                row_1 = item.strip()
                                row_1_list = row_1.split()
                                roundtrip_jitter = row_1_list[5]
                                current_test_avg_delay = row_1_list[4]
                                current_test_max_delay = row_1_list[3]
                                current_test_min_delay = row_1_list[2]
                probes_results[probe_name][test_name].update(
                    {
                        "probe_type": "icmp-ping",
                        "target": self._find_txt(
                            probe,
                            f"{path}/configure_ns:destination-address",
                            namespaces=self.nsmap,
                        ),
                        "source": self._find_txt(
                            probe,
                            f"{path}/configure_ns:source-address",
                            namespaces=self.nsmap,
                        ),
                        "probe_count": convert(
                            int,
                            self._find_txt(
                                probe, f"{path}/configure_ns:count", namespaces=self.nsmap,
                            ),
                        ),
                        "rtt": convert(float, current_test_avg_delay, default=-1.0),
                        "round_trip_jitter": convert(float, roundtrip_jitter, default=-1.0),
                        "current_test_min_delay": convert(
                            float, current_test_min_delay, default=-1.0
                        ),
                        "current_test_max_delay": convert(
                            float, current_test_max_delay, default=-1.0
                        ),
                        "current_test_avg_delay": convert(
                            float, current_test_avg_delay, default=-1.0
                        ),
                        "last_test_min_delay": convert(
                            float, last_test_min_delay, default=-1.0
                        ),
                        "last_test_max_delay": convert(
                            float, last_test_max_delay, default=-1.0
                        ),
                        "last_test_avg_delay": convert(
                            float, last_test_avg_delay, default=-1.0
                        ),
                        "last_test_loss": convert(int, last_test_loss, default=-1),
                        "global_test_min_delay": -1.0,  # default value as SROS does not have global_test
                        "global_test_max_delay": -1.0,  # default value as SROS does not have global_test
                        "global_test_avg_delay": -1.0,  # default value as SROS does not have global_test
                    }
                )

            return probes_results
        except Exception as e:
            print("Error in method get probes results : {}".format(e))
            log.error("Error in method get probes results : %s" % traceback.format_exc())

    def get_probes_config(self):
        # for base router
        """
        Returns a dictionary with the probes configured on the device. Probes can be either RPM on
        JunOS devices, either SLA on IOS-XR. Other vendors do not support probes.
        The keys of the main dictionary represent the name of the probes.
        Each probe consists on multiple tests, each test name being a key in the probe dictionary.
        A test has the following keys:
            probe_type (str)
            target (str)
            source (str)
            probe_count (int)
            test_interval (int)
        """
        try:
            probes_config = {}

            result = to_ele(
                self.conn.get(
                    filter=GET_PROBES_CONFIG["_"], with_defaults="report-all"
                ).data_xml
            )

            for probe in result.xpath(
                "configure_ns:configure/configure_ns:saa/configure_ns:owner",
                namespaces=self.nsmap,
            ):
                probe_name = self._find_txt(
                    probe, "configure_ns:owner-name", namespaces=self.nsmap
                )
                if probe_name == "":
                    continue
                test_name = self._find_txt(
                    probe, "configure_ns:test", namespaces=self.nsmap
                )
                if test_name == "":
                    continue
                path = "configure_ns:type/configure_ns:icmp-ping"
                if probe_name not in probes_config.keys():
                    probes_config = {probe_name: {test_name: {}}}
                else:
                    probes_config[probe_name].update({test_name: {}})
                probes_config[probe_name][test_name].update(
                    {
                        "probe_type": "icmp-ping",
                        "target": self._find_txt(
                            probe,
                            f"{path}/configure_ns:destination-address",
                            namespaces=self.nsmap,
                        ),
                        "source": self._find_txt(
                            probe,
                            f"{path}/configure_ns:source-address",
                            namespaces=self.nsmap,
                        ),
                        "probe_count": convert(
                            int,
                            self._find_txt(
                                probe, f"{path}/configure_ns:count", namespaces=self.nsmap,
                            ),
                            default=-1,
                        ),
                        "test_interval": convert(
                            int,
                            self._find_txt(
                                probe,
                                f"{path}/configure_ns:interval",
                                namespaces=self.nsmap,
                            ),
                            default=-1,
                        ),
                    }
                )
            return probes_config
        except Exception as e:
            print("Error in method get probes config : {}".format(e))
            log.error("Error in method get probes config : %s" % traceback.format_exc())

    def get_mac_address_table(self):
        """
        Returns a lists of dictionaries. Each dictionary represents an entry in the MAC Address Table,
        having the following keys:

            mac (string)
            interface (string)
            vlan (int)
            active (boolean)
            static (boolean)
            moves (int)
            last_move (float)
        """
        try:
            mac_address_list = []

            cmd = "/show service fdb-mac"
            buff = self._perform_cli_commands([cmd], True, no_more=True)
            template = "textfsm_templates//nokia_sros_show_service_fdb_mac.tpl"
            # template = "textfsm_templates\\nokia_sros_show_service_fdb_mac.tpl"
            output_list = parse_with_textfsm(template, buff)
            new_records = []
            for record in output_list:
                new_dict = {}
                for k, v in record.items():
                    if k.endswith("_"):
                        new_dict[k.replace("__", "")] = new_dict[k.replace("__", "")] + v
                    else:
                        new_dict[k] = v
                new_records.append(new_dict)

            for record in new_records:
                source_identifier = record.get("Source_Identifier")
                temp_list = []
                if ":" in source_identifier:
                    temp_list = source_identifier.split(":")
                static = False
                if (
                    record.get("Type").lower().find("static") > -1
                    or record.get("Type").find("S") > -1
                ):
                    static = True

                mac_address_list.append(
                    {
                        "mac": record.get("MAC"),
                        "interface": source_identifier
                        if len(temp_list) == 0
                        else temp_list[0] + ":" + temp_list[1],
                        "vlan": -1 if len(temp_list) == 0 else convert(int, temp_list[2]),
                        "static": static,
                        "active": False,
                        "moves": -1,
                        "last_move": -1.0,
                    }
                )

            return mac_address_list
        except Exception as e:
            print("Error in method get mac address : {}".format(e))
            log.error("Error in method get mac address : %s" % traceback.format_exc())

    def get_bgp_neighbors(self):
        """
            Returns a dictionary of dictionaries. The keys for the first dictionary will be the vrf
            (global if no vrf). The inner dictionary will contain the following data for each vrf:

                router_id
                peers - another dictionary of dictionaries. Outer keys are the IPs of the neighbors.
                The inner keys are:
                    local_as (int)
                    remote_as (int)
                    remote_id - peer router id
                    is_up (True/False)
                    is_enabled (True/False)
                    description (string)
                    uptime (int in seconds)
                    address_family (dictionary) - A dictionary of address families available for
                    the neighbor.
                    So far it can be ‘ipv4’ or ‘ipv6’
                        received_prefixes (int)
                        accepted_prefixes (int)
                        sent_prefixes (int)
                Note, if is_up is False and uptime has a positive value then this indicates the
                uptime of the last active BGP session.
        """
        try:
          return get_bgp_neighbors(self.conn)
        except Exception as e:
          print(e)
          log.error("Error in method get bgp neighbors : %s" % traceback.format_exc())
          return {}

    def get_bgp_neighbors_detail(self, neighbor_address=""):
        """
        :param neighbor_address:
        :return:
            Returns a dictionary of dictionaries. The keys for the first dictionary will be the vrf (global if no vrf).
            The keys of the inner dictionary represent the AS number of the neighbors.
            Leaf dictionaries contain the following fields:
                up (True/False)
                local_as (int)
                remote_as (int)
                router_id (string)
                local_address (string)
                routing_table (string)
                local_address_configured (True/False)
                local_port (int)
                remote_address (string)
                remote_port (int)
                multihop (True/False)
                multipath (True/False)
                remove_private_as (True/False)
                import_policy (string)
                export_policy (string)
                input_messages (int)
                output_messages (int)
                input_updates (int)
                output_updates (int)
                messages_queued_out (int)
                connection_state (string)
                previous_connection_state (string)
                last_event (string)
                suppress_4byte_as (True/False)
                local_as_prepend (True/False)
                holdtime (int)
                configured_holdtime (int)
                keepalive (int)
                configured_keepalive (int)
                active_prefix_count (int)
                received_prefix_count (int)
                accepted_prefix_count (int)
                suppressed_prefix_count (int)
                advertised_prefix_count (int)
                flap_count (int)

        """
        try:
          return get_bgp_neighbors_detail(self.conn,neighbor_address)
        except Exception as e:
          print(e)
          log.error("Error in method get bgp neighbors detail : %s" % traceback.format_exc())
          return {}

    def get_bgp_config(self, group="", neighbor=""):
        """
        Returns a dictionary containing the BGP configuration.
        Can return either the whole config, either the config only for a group or neighbor.

        :param group: Returns the configuration of a specific BGP group.
        :param neighbor: Returns the configuration of a specific BGP neighbor.

        Main dictionary keys represent the group name and the values represent a dictionary having
        the keys below. Neighbors which aren't members of a group will be stored in a key named "_":

            * type (string)
            * description (string)
            * apply_groups (string list)
            * multihop_ttl (int)
            * multipath (True/False)
            * local_address (string)
            * local_as (int)
            * remote_as (int)
            * import_policy (string)
            * export_policy (string)
            * remove_private_as (True/False)
            * prefix_limit (dictionary)
            * neighbors (dictionary)

        Neighbors is a dictionary of dictionaries with the following keys:

            * description (string)
            * import_policy (string)
            * export_policy (string)
            * local_address (string)
            * local_as (int)
            * remote_as (int)
            * authentication_key (string)
            * prefix_limit (dictionary)
            * route_reflector_client (True/False)
            * nhs (True/False)

        The inner dictionary prefix_limit has the same structure for both layers::

            {
                [FAMILY_NAME]: {
                    [FAMILY_TYPE]: {
                        'limit': [LIMIT],
                        ... other options
                    }
                }
            }
        """
        #
        # Note: returns all bgp neighbors across all VRFs, merging groups with the same name
        # Does not (cannot) report dynamic neighbors, only static ones
        #
        try:
            bgp_config = {}

            # helpers

            def _build_prefix_limit(peer_xml):
                prefix_limit = {}
                for pl in peer_xml.xpath(
                    "configure_ns:prefix-limit", namespaces=self.nsmap
                ):
                    af = self._find_txt(
                        pl, "configure_ns:family", namespaces=self.nsmap
                    ).lower()
                    if "ipv6" in af:
                        prefix_type = "inet6"
                    else:
                        prefix_type = "inet"

                    prefix_limit.update(
                        {
                            prefix_type: {
                                af: {
                                    "limit": self._find_txt(
                                        pl, "configure_ns:maximum", namespaces=self.nsmap
                                    ),
                                    "teardown": {
                                        "threshold": self._find_txt(
                                            pl,
                                            "configure_ns:threshold",
                                            namespaces=self.nsmap,
                                        ),
                                        "timeout": self._find_txt(
                                            pl,
                                            "configure_ns:idle-timeout",
                                            namespaces=self.nsmap,
                                        ),
                                    },
                                }
                            }
                        }
                    )
                return prefix_limit

            def _get_policies(policies_xml):
                policies = [ele.text for ele in policies_xml]
                return ", ".join(policies)

            def _route_reflect(xml):
              _cluster_id = self._find_txt(xml,"configure_ns:cluster/configure_ns:cluster-id",namespaces=self.nsmap)
              _client_reflect = self._find_txt(xml,"configure_ns:client-reflect", namespaces=self.nsmap)
              return (bool(_cluster_id),_client_reflect) # keep client_reflect as string to distinguish between not set and 'false'

            def _get_bgp_neighbor_group(bgp_neighbors,global_autonomous):
                for bgp_neighbor in bgp_neighbors:
                    group_name = self._find_txt(
                        bgp_neighbor, "configure_ns:group", namespaces=self.nsmap
                    )

                    def _group_attr(attr):
                      return bgp_groups[group_name][attr] if group_name in bgp_groups and attr in bgp_groups[group_name] else None

                    peer = ip(
                        self._find_txt(
                            bgp_neighbor, "configure_ns:ip-address", namespaces=self.nsmap
                        )
                    )

                    if neighbor != "" and peer != neighbor:
                        continue

                    # JvB note: 'type' configuration allows implicit peer AS configuration for iBGP
                    type_ = self._find_txt(
                        bgp_neighbor, "configure_ns:type", namespaces=self.nsmap
                    ) or _group_attr('type')

                    _nhs = self._find_txt(
                        bgp_neighbor,"configure_ns:next-hop-self",namespaces=self.nsmap,
                    )
                    _next_hop_self = (_nhs != "false") if _nhs else _group_attr('_nhs')

                    _cluster_id,_client_reflect = _route_reflect(bgp_neighbor)
                    route_reflector = (_cluster_id or _group_attr('_cluster_id')) \
                                  and (_group_attr('_client_reflect') and _client_reflect=="")

                    explicit_local_as = self._find_txt(
                        bgp_neighbor,
                        "configure_ns:local-as/configure_ns:as-number",
                        namespaces=self.nsmap,
                    )

                    # Order of priority:
                    # 1. Neighbor level local-as
                    # 2. Group level local-as
                    # 3. Global AS
                    local_as = explicit_local_as or _group_attr('local_as') or global_autonomous

                    explicit_peer_as = self._find_txt(
                        bgp_neighbor, "configure_ns:peer-as", namespaces=self.nsmap
                    )

                    if explicit_peer_as:
                      peer_as = explicit_peer_as
                    else:
                      group_remote_as = _group_attr('remote_as')
                      if group_remote_as:
                        peer_as = group_remote_as
                      elif type_=="internal": # implicit peer_as configuration
                        peer_as = local_as
                      else:
                        peer_as = 0 # Not configured

                    if group_name not in bgp_group_neighbors.keys():
                        bgp_group_neighbors[group_name] = {}
                    bgp_group_neighbors[group_name][peer] = {
                        "description": self._find_txt(
                            bgp_neighbor, "configure_ns:description", namespaces=self.nsmap
                        ),
                        "local_as": as_number(local_as),
                        "remote_as": as_number(peer_as),
                        "prefix_limit": _build_prefix_limit(bgp_neighbor),
                        "import_policy": _get_policies(
                            bgp_neighbor.xpath(
                                "configure_ns:import/configure_ns:policy",
                                namespaces=self.nsmap,
                            )
                        ),
                        "export_policy": _get_policies(
                            bgp_neighbor.xpath(
                                "configure_ns:export/configure_ns:policy",
                                namespaces=self.nsmap,
                            )
                        ),
                        "local_address": convert(
                            ip,
                            self._find_txt(
                                bgp_neighbor,
                                "configure_ns:local-address",
                                namespaces=self.nsmap,
                            ),
                        ),
                        # Note: ignoring any group level authentication key here
                        "authentication_key": self._find_txt(
                            bgp_neighbor,
                            "configure_ns:authentication-key",
                            namespaces=self.nsmap,
                        ),
                        "nhs": bool(_next_hop_self),
                        "route_reflector_client": route_reflector,
                    }
                    if neighbor != "" and peer == neighbor:
                        break

            def _get_bgp_group_data(bgp_groups_list,local_as_number,g_cluster_id,g_client_reflect):
                for bgp_group in bgp_groups_list:
                    group_name = self._find_txt(
                        bgp_group, "configure_ns:group-name", namespaces=self.nsmap
                    )
                    if group != "" and group != group_name:
                        continue

                    remove_private = (
                        True
                        if self._find_txt(
                            bgp_group,
                            "configure_ns:remove-private/configure_ns:limited",
                            namespaces=self.nsmap,
                        )
                        == "true"
                        else False
                    )
                    type_ = self._find_txt(
                        bgp_group, "configure_ns:type", namespaces=self.nsmap
                    )
                    explicit_local_as = self._find_txt(
                        bgp_group,
                        "configure_ns:local-as/configure_ns:as-number",
                        namespaces=self.nsmap,
                    )
                    local_as = int(explicit_local_as or local_as_number)

                    explicit_peer_as = self._find_txt(
                        bgp_group, "configure_ns:peer-as", namespaces=self.nsmap
                    )
                    if explicit_peer_as:
                      peer_as = int(explicit_peer_as)
                      type_ = "internal" if peer_as==local_as else "external"
                    elif type_=="internal":
                      peer_as = local_as # Implicitly configured
                    else:
                      peer_as = 0 # Not configured, type_ may be 'no-type'

                    xbgp = "ibgp" if type_=="internal" else "ebgp"
                    max_path = self._find_txt(
                        bgp_group,
                        f"../configure_ns:multipath/configure_ns:{xbgp}",
                        namespaces=self.nsmap,
                    )
                    multipath = bool( peer_as and max_path and int(max_path)>1 )

                    _nhs = self._find_txt(bgp_group, "configure_ns:next-hop-self", namespaces=self.nsmap)
                    # Can only set client_reflect to 'false' at group level
                    _cluster_id,_client_reflect = _route_reflect(bgp_group)

                    apply_groups_list = []
                    for apply_group in bgp_group.xpath(
                        "configure_ns:apply-groups", namespaces=self.nsmap
                    ):
                        apply_groups_list.append(apply_group)

                    bgp_groups[group_name] = {
                        "type": type_,
                        "description": self._find_txt(
                            bgp_group, "configure_ns:description", namespaces=self.nsmap
                        ),
                        "apply_groups": apply_groups_list,
                        "local_as": as_number(local_as),
                        "remote_as": as_number(peer_as),
                        "remove_private_as": remove_private,
                        "import_policy": _get_policies(
                            bgp_group.xpath(
                                "configure_ns:import/configure_ns:policy",
                                namespaces=self.nsmap,
                            )
                        ),
                        "export_policy": _get_policies(
                            bgp_group.xpath(
                                "configure_ns:export/configure_ns:policy",
                                namespaces=self.nsmap,
                            )
                        ),
                        "local_address": convert(
                            ip,
                            self._find_txt(
                                bgp_group,
                                "configure_ns:local-address",
                                namespaces=self.nsmap,
                            ),
                        ),
                        "multipath": multipath,
                        "multihop_ttl": convert(
                            int,
                            self._find_txt(
                                bgp_group, "configure_ns:multihop", namespaces=self.nsmap
                            ),
                            default=-1,
                        ),
                        "prefix_limit": _build_prefix_limit(bgp_group),
                        "_nhs": bool(_nhs != "false"),
                        "_cluster_id": g_cluster_id or bool(_cluster_id),
                        "_client_reflect": g_client_reflect and _client_reflect=="",
                        "neighbors": {},
                    }
                    if group != "" and group == group_name:
                        break

            bgp_running_config = to_ele(
                self.conn.get(
                    filter=GET_BGP_CONFIG["_"].format(group_name=group, neighbor=neighbor),
                    with_defaults="report-all",
                ).data_xml
            )
            # print( to_xml(bgp_running_config, pretty_print=True) )

            bgp_group_neighbors = {}
            bgp_groups = {}
            global_as = self._find_txt(
                bgp_running_config,
                "configure_ns:configure/configure_ns:router/configure_ns:autonomous-system",
                namespaces=self.nsmap,
            )

            for router in bgp_running_config.xpath(
                "configure_ns:configure/configure_ns:router/configure_ns:bgp",
                namespaces=self.nsmap,
            ):
                _cluster_id,_client_reflect = _route_reflect(router)
                _get_bgp_group_data(
                    router.xpath("configure_ns:group", namespaces=self.nsmap),
                    local_as_number=int(global_as),
                    g_cluster_id=_cluster_id,g_client_reflect=_client_reflect
                )
                _get_bgp_neighbor_group(
                    router.xpath("configure_ns:neighbor", namespaces=self.nsmap),
                    global_as,
                )

            for vprn in bgp_running_config.xpath(
                "configure_ns:configure/configure_ns:service/configure_ns:vprn/configure_ns:bgp",
                namespaces=self.nsmap,
            ):
                vprn_as = self._find_txt(
                    vprn,"../configure_ns:autonomous-system",
                    namespaces=self.nsmap,
                )
                _cluster_id,_client_reflect = _route_reflect(vprn)
                _get_bgp_group_data(
                    vprn.xpath("configure_ns:group", namespaces=self.nsmap),
                    local_as_number=int(vprn_as),
                    g_cluster_id=_cluster_id,g_client_reflect=_client_reflect
                )
                _get_bgp_neighbor_group(
                    vprn.xpath("configure_ns:neighbor",namespaces=self.nsmap),
                    vprn_as,
                )

            # Assemble groups and neighbors
            for grp_name, grp_data in bgp_groups.items():
                neighbors = bgp_group_neighbors.get(grp_name, {})

                grp_data.pop("_nhs")  # remove temporary keys
                grp_data.pop("_cluster_id")
                grp_data.pop("_client_reflect")

                grp_data["neighbors"] = neighbors  # Add updated neighbors to group

                bgp_config[grp_name] = grp_data  # Add group with neighbors to output dict

            if "" in bgp_group_neighbors.keys():
                bgp_config["_"] = {
                    "apply_groups": [],
                    "description": "",
                    "local_as": 0,
                    "type": "",
                    "import_policy": "",
                    "export_policy": "",
                    "local_address": "",
                    "multipath": False,
                    "multihop_ttl": 0,
                    "remote_as": 0,
                    "remove_private_as": False,
                    "prefix_limit": {},
                    "neighbors": bgp_group_neighbors.get("", {}),
                }

            return bgp_config

        except Exception as e:
            print("Error in method get bgp config : {}".format(e))
            log.error("Error in method get bgp config : %s" % traceback.format_exc())

    def get_lldp_neighbors(self):
        """
        Returns a dictionary where the keys are local ports and the value is a list of dictionaries
        with the following information:

                port
        """
        try:
            lldp_neighbors = {}

            root = to_ele(self.conn.get(filter=GET_LLDP_NEIGHBORS["_"]).data_xml)

            path = (
                "state_ns:ethernet/state_ns:lldp/state_ns:dest-mac/state_ns:remote-system/"
            )
            for port in root.xpath("state_ns:state/state_ns:port", namespaces=self.nsmap):
                port_id = self._find_txt(
                    port, "state_ns:port-id", namespaces=self.nsmap
                )  # port name
                port_op_state = self._find_txt(
                    port, "state_ns:oper-state", namespaces=self.nsmap
                ).lower()
                if port_op_state != "up" or port_id == "":
                    continue
                # if no remote_chassis_id is present (mandatory TLV),
                # then no LLDP neighbor is behind the port
                remote_chassis_id = self._find_txt(
                    port, f"{path}state_ns:chassis-id", namespaces=self.nsmap
                )
                if remote_chassis_id == "":
                    continue
                remote_system_name = self._find_txt(
                    port, f"{path}state_ns:system-name", namespaces=self.nsmap
                )
                remote_port_id = self._find_txt(
                    port, f"{path}state_ns:remote-port-id", namespaces=self.nsmap
                )
                if port_id not in lldp_neighbors.keys():
                    lldp_neighbors[port_id] = [
                        {"hostname": remote_system_name, "port": remote_port_id}
                    ]
                else:
                    lldp_neighbors[port_id].append(
                        {"hostname": remote_system_name, "port": remote_port_id}
                    )

            return lldp_neighbors
        except Exception as e:
            print("Error in method get lldp neighbors : {}".format(e))
            log.error("Error in method get lldp neighbors : %s" % traceback.format_exc())

    def get_lldp_neighbors_detail(self, interface=""):
        """
        Returns a detailed view of the LLDP neighbors as a dictionary containing lists
        of dictionaries for each interface.

        Empty entries are returned as an empty string (e.g. ‘’) or list where applicable.

        Inner dictionaries contain fields:
            parent_interface (string)
            remote_port (string)
            remote_port_description (string)
            remote_chassis_id (string)
            remote_system_name (string)
            remote_system_description (string)
            remote_system_capab (list) with any of these values
                other
                repeater
                bridge
                wlan-access-point
                router
                telephone
                docsis-cable-device
                station
            remote_system_enabled_capab (list)
        """
        try:
            lldp_neighbors_details = {}

            root = to_ele(
                self.conn.get(
                    filter=GET_LLDP_NEIGHBORS_DETAIL["_"].format(port_id=interface)
                ).data_xml
            )
            for port in root.xpath("state_ns:state/state_ns:port", namespaces=self.nsmap):
                port_id = self._find_txt(
                    port, "state_ns:port-id", namespaces=self.nsmap
                )  # port name
                port_op_state = self._find_txt(
                    port, "state_ns:oper-state", namespaces=self.nsmap
                ).lower()
                if port_id == "" or port_op_state != "up":
                    continue
                path = "state_ns:ethernet/state_ns:lldp/state_ns:dest-mac/state_ns:remote-system/"
                remote_chassis_id = self._find_txt(
                    port, f"{path}state_ns:chassis-id", namespaces=self.nsmap
                )
                # if no remote_chassis_id is present (mandatory TLV),
                # then no LLDP neighbor is behind the port
                if remote_chassis_id == "":
                    continue
                remote_system_name = self._find_txt(
                    port, f"{path}state_ns:system-name", namespaces=self.nsmap
                )
                remote_port_id = self._find_txt(
                    port, f"{path}state_ns:remote-port-id", namespaces=self.nsmap
                )
                remote_port_desc = self._find_txt(
                    port, f"{path}state_ns:port-description", namespaces=self.nsmap
                )
                remote_system_description = self._find_txt(
                    port, f"{path}state_ns:system-description", namespaces=self.nsmap
                )
                remote_system_capab = self._find_txt(
                    port,
                    f"{path}state_ns:system-supported-capabilities",
                    namespaces=self.nsmap,
                )
                remote_system_enable_capab = self._find_txt(
                    port,
                    f"{path}state_ns:system-enabled-capabilities",
                    namespaces=self.nsmap,
                )
                if port_id not in lldp_neighbors_details.keys():
                    lldp_neighbors_details[port_id] = []
                lldp_neighbors_details[port_id].append(
                    {
                        "parent_interface": "",
                        "remote_chassis_id": remote_chassis_id,
                        "remote_system_name": remote_system_name,
                        "remote_port": remote_port_id,
                        "remote_port_description": remote_port_desc,
                        "remote_system_description": remote_system_description,
                        "remote_system_capab": remote_system_capab.split(),
                        "remote_system_enable_capab": remote_system_enable_capab.split(),
                    }
                )
            return lldp_neighbors_details
        except Exception as e:
            print("Error in method get lldp neighbors detail : {}".format(e))
            log.error("Error in method get lldp neighbors detail : %s" % traceback.format_exc())

    def get_environment(self):
        """
            Returns a dictionary where:

                fans is a dictionary of dictionaries where the key is the location and the values:
                    status (True/False) - True if it’s ok, false if it’s broken
                temperature is a dict of dictionaries where the key is the location and the values:
                    temperature (float) - Temperature in celsius the sensor is reporting.
                is_alert (True/False) - True if the temperature is above the alert threshold
                is_critical (True/False) - True if the temp is above the critical threshold
                power is a dictionary of dictionaries where the key is the PSU id and the values:
                    status (True/False) - True if it’s ok, false if it’s broken
                    capacity (float) - Capacity in W that the power supply can support
                    output (float) - Watts drawn by the system
                cpu is a dictionary of dictionaries where the key is the ID and the values
                    %usage
                memory is a dictionary with:
                    available_ram (int) - Total amount of RAM installed in the device
                    used_ram (int) - RAM in use in the device
        """
        try:
            environment_data = {
                "fans": {},
                "power": {},
                "temperature": {},
                "memory": {},
            }

            # helpers functions
            def _build_temperature_dict(instance, choice=1):
                temp = convert(
                    float,
                    self._find_txt(
                        instance,
                        "state_ns:hardware-data/state_ns:temperature",
                        namespaces=self.nsmap,
                    ),
                )
                if temp == "":
                    return
                temp_thresh = convert(
                    float,
                    self._find_txt(
                        instance,
                        "state_ns:hardware-data/state_ns:temperature-threshold",
                        namespaces=self.nsmap,
                    ),
                )
                if temp_thresh == "":
                    return

                # Assume warning temperature is 80% of the threshold tempearature
                temp_warn = 0.8 * temp_thresh

                data = {
                    "temperature": temp,
                    "is_alert": True if temp >= temp_warn else False,
                    "is_critical": True if temp >= temp_thresh else False,
                }

                if choice == 1:
                    environment_data["temperature"].update({"cpm": {}})
                    environment_data["temperature"]["cpm"].update(data)
                elif choice == 2:
                    environment_data["temperature"].update({"card": {}})
                    environment_data["temperature"]["card"].update(data)
                elif choice == 3:
                    environment_data["temperature"].update({"mda": {}})
                    environment_data["temperature"]["mda"].update(data)

            result = to_ele(
                self.conn.get(
                    filter=GET_ENVIRONMENT["_"], with_defaults="report-all"
                ).data_xml
            )

            for fan in result.xpath(
                "state_ns:state/state_ns:chassis/state_ns:fan", namespaces=self.nsmap
            ):
                fan_slot = self._find_txt(fan, "state_ns:fan-slot", namespaces=self.nsmap)

                oper_state = (
                    True
                    if self._find_txt(
                        fan,
                        "state_ns:hardware-data/state_ns:oper-state",
                        namespaces=self.nsmap,
                    )
                    == "in-service"
                    else False
                )
                environment_data["fans"].update({fan_slot: {"status": oper_state}})

            # get the output of each power-module using MD-CLI
            buff = self._perform_cli_commands(
                [
                    "/show chassis power-management utilization detail",
                    # JvB: on VSR this is 'power-supply'
                ],
                True,
                no_more=True
            )
            total_power_modules = 0
            output = 0.0
            for item in buff.split("\n"):
                if "Power Module" in item:
                    total_power_modules = total_power_modules + 1
                if "Current Util." in item:
                    item.strip()
                    watts = re.match(r"^.*:\s*(\d+[.]\d+) Watts.*$", item)
                    if watts:
                        output = float(watts.groups()[0])

            for power_module in result.xpath(
                "state_ns:state/state_ns:chassis/state_ns:power-shelf/state_ns:power-module",
                namespaces=self.nsmap,
            ):
                power_module_id = convert(
                    int,
                    self._find_txt(
                        power_module, "state_ns:power-module-id", namespaces=self.nsmap
                    ),
                )
                oper_state = (
                    True
                    if self._find_txt(
                        power_module,
                        "state_ns:hardware-data/state_ns:oper-state",
                        namespaces=self.nsmap,
                    )
                    == "in-service"
                    else False
                )
                capacity = convert(
                    float,
                    self._find_txt(
                        power_module, "state_ns:available-wattage", namespaces=self.nsmap
                    ),
                )
                environment_data["power"].update(
                    {
                        str(power_module_id): {
                            "status": oper_state,
                            "capacity": capacity,
                            "output": output / total_power_modules,
                        }
                    }
                )

            for cpm in result.xpath("state_ns:state/state_ns:cpm", namespaces=self.nsmap):
                _build_temperature_dict(cpm, choice=1)

            for card in result.xpath("state_ns:state/state_ns:card", namespaces=self.nsmap):
                _build_temperature_dict(card, choice=2)
                for mda in card.xpath("state_ns:mda", namespaces=self.nsmap):
                    _build_temperature_dict(mda, choice=3)

            for system in result.xpath(
                "state_ns:state/state_ns:system", namespaces=self.nsmap
            ):
                available_ram = convert(
                    int,
                    self._find_txt(
                        system,
                        "state_ns:memory-pools/state_ns:summary/state_ns:available-memory",
                        namespaces=self.nsmap,
                    ),
                )
                used_ram = convert(
                    int,
                    self._find_txt(
                        system,
                        "state_ns:memory-pools/state_ns:summary/state_ns:total-in-use",
                        namespaces=self.nsmap,
                    ),
                )
                environment_data.update({"cpu": {}})
                for cpu in result.xpath(
                    "state_ns:state/state_ns:system/state_ns:cpu", namespaces=self.nsmap
                ):
                    sample_period = convert(
                        int,
                        self._find_txt(
                            cpu, "state_ns:sample-period", namespaces=self.nsmap
                        ),
                    )
                    cpu_usage = convert(
                        float,
                        self._find_txt(
                            cpu,
                            "state_ns:summary/state_ns:usage/state_ns:cpu-usage",
                            namespaces=self.nsmap,
                        ),
                        default=-1,
                    )
                    environment_data["cpu"].update({str(sample_period): {"%usage": cpu_usage}})

                environment_data["memory"].update(
                    {"available_ram": available_ram + used_ram, "used_ram": used_ram}
                )
            return environment_data
        except Exception as e:
            print("Error in method get environment data : {}".format(e))
            log.error("Error in method get environment data : %s" % traceback.format_exc())


    def get_ipv6_neighbors_table(self):
        """
        Get IPv6 neighbors table information.

        Return a list of dictionaries having the following set of keys:

            interface (string)
            mac (string)
            ip (string)
            age (float) in seconds
            state (string)
        """
        try:
            result = to_ele(
                self.conn.get(
                    filter=GET_IPV6_NEIGHBORS_TABLE["_"], with_defaults="report-all"
                ).data_xml
            )
            name_list = []
            for router in result.xpath(
                "state_ns:state/state_ns:router", namespaces=self.nsmap
            ):
                name_list.append(
                    self._find_txt(router, "state_ns:router-name", namespaces=self.nsmap)
                )
            for vprn in result.xpath(
                "state_ns:state/state_ns:service/state_ns:vprn", namespaces=self.nsmap
            ):
                name_list.append(
                    self._find_txt(vprn, "state_ns:oper-service-id", namespaces=self.nsmap)
                )

            ipv6_neighbor_list = []

            for name in name_list:
                cmd = [f"/show router {name} neighbor"]
                buff = self._perform_cli_commands(cmd, True, no_more=True)
                prev_row = ""
                ip_address = ""
                for item in buff.split("\n"):
                    if self.ipv6_address_re.search(item) or prev_row:
                        row = item.strip()
                        prev_row = row
                        row_list = row.split()
                        if len(row_list) == 2:
                            ip_address = row_list[0]
                            temp_dict = {"ip": row_list[0], "interface": row_list[1]}
                            ipv6_neighbor_list.append(temp_dict)
                        if len(row_list) > 2:
                            time_string = re.split("h|m|s", row_list[2])
                            seconds = (
                                (int(time_string[0]) * 3600)
                                + (int(time_string[1]) * 60)
                                + (int(time_string[2]))
                            )
                            temp_dict_1 = {
                                "mac": row_list[0],
                                "state": row_list[1].lower(),
                                "age": convert(float, seconds, default=-1),
                            }

                            for dictionary in ipv6_neighbor_list:
                                if dictionary.get("ip") == ip_address:
                                    dictionary.update(temp_dict_1)
                            prev_row = ""
                            ip_address = ""

            return ipv6_neighbor_list
        except Exception as e:
            print("Error in method get ipv6 neighbors : {}".format(e))
            log.error("Error in method get ipv6 neighbors : %s" % traceback.format_exc())

    def ping(
        self,
        destination,
        source=C.PING_SOURCE,
        ttl=C.PING_TTL,
        timeout=C.PING_TIMEOUT,
        size=C.PING_SIZE,
        count=C.PING_COUNT,
        vrf=C.PING_VRF,
        source_interface=C.PING_SOURCE_INTERFACE,
    ):
        """
        ttl should be in the range 1..128
        """
        try:
            ping = {}
            if ttl > 128:
                ttl = 128
            results = []
            command = ""
            if source and vrf:
                command = (
                    "ping {d1} timeout {d2} ttl {d3} source-address {d4} size {d5} "
                    "count {d6} router-instance {d7}"
                )
            elif not source and not vrf:
                command = "ping {d1} timeout {d2} ttl {d3} size {d5} count {d6}"
            elif source:
                command = "ping {d1} timeout {d2} ttl {d3} source-address {d4} size {d5} count {d6}"
            elif vrf:
                command = "ping {d1} timeout {d2} ttl {d3} size {d5} count {d6} router-instance {d7}"
            command = command.format(
                d1=destination,
                d2=str(timeout),
                d3=str(ttl),
                d4=source,
                d5=str(size),
                d6=str(count),
                d7=vrf,
            )
            buff = self._perform_cli_commands([command], True, no_more=True)
            for item in buff.split("\n"):
                if "No route to destination" in item:
                    value = "unknown host " + destination
                    ping.update({"error": value})
                    return ping
                elif "icmp_seq" in item:
                    row = item.strip()
                    if "\b" in row:
                        row = row.replace(".\b", "")
                        row = row.replace("\b", "")
                    row_list = row.split()
                    rtt = row_list[6].split("=")
                    results.append(
                        {
                            "ip_address": row_list[3].split(":")[0],
                            "rtt": convert(float, rtt[1].split("m")[0]),
                        }
                    )
                elif "packets" in item:
                    row = item.strip()
                    row_list = row.split()
                    ping.update(
                        {
                            "success": {
                                "probes_sent": convert(int, row_list[0]),
                                "packet_loss": convert(int, row_list[0])
                                - convert(int, row_list[3]),
                            }
                        }
                    )
                elif "round-trip" in item:
                    row = item.strip()
                    row_list = row.split()
                    ping["success"].update(
                        {
                            "rtt_min": convert(float, row_list[3].split("m")[0]),
                            "rtt_avg": convert(float, row_list[6].split("m")[0]),
                            "rtt_max": convert(float, row_list[9].split("m")[0]),
                            "rtt_stddev": convert(float, row_list[12].split("m")[0]),
                        }
                    )
                    ping["success"].update({"results": results})
            return ping
        except Exception as e:
            print("Error in method ping : {}".format(e))
            log.error("Error in method ping : %s" % traceback.format_exc())


    def traceroute(
        self,
        destination,
        source=C.TRACEROUTE_SOURCE,
        ttl=C.TRACEROUTE_TTL,
        timeout=C.TRACEROUTE_TIMEOUT,
        vrf=C.TRACEROUTE_VRF,
    ):
        """
        timeout should be in the range 10..60000
        """
        try:
            traceroute = {}
            if timeout < 10 :
                timeout = 10
            cmd = ""
            if source and vrf:
                cmd = "traceroute {d1} wait {d2} ttl {d3} source-address {d4} router-instance {d5}"
            elif not source and not vrf:
                cmd = "traceroute {d1} wait {d2} ttl {d3}"
            elif source:
                cmd = "traceroute {d1} wait {d2} ttl {d3} source-address {d4}"
            elif vrf:
                cmd = "traceroute {d1} wait {d2} ttl {d3} router-instance {d5}"
            cmd = cmd.format(
                d1=destination, d2=str(timeout), d3=str(ttl), d4=source, d5=vrf,
            )
            command = [
                "/environment progress-indicator admin-state disable",
                cmd,
            ]
            buff = self._perform_cli_commands(command, True, no_more=True)
            for item in buff.split("\n"):
                if "* * *" in item:
                    value = "unknown host " + destination
                    traceroute.update({"error": value})
                    return traceroute
                elif "ms" in item:
                    traceroute.update({"success": {}})
                    row = item.strip()
                    row_list = row.split()
                    traceroute["success"].update(
                        {
                            row_list[0]: {
                                "probes": {
                                    "1": {
                                        "rtt": convert(float, row_list[3]),
                                        "ip_address": row_list[2]
                                        .split("(")[1]
                                        .split(")")[0],
                                        "host_name": row_list[1],
                                    },
                                    "2": {
                                        "rtt": convert(float, row_list[5]),
                                        "ip_address": row_list[2]
                                        .split("(")[1]
                                        .split(")")[0],
                                        "host_name": row_list[1],
                                    },
                                    "3": {
                                        "rtt": convert(float, row_list[7]),
                                        "ip_address": row_list[2]
                                        .split("(")[1]
                                        .split(")")[0],
                                        "host_name": row_list[1],
                                    },
                                }
                            }
                        }
                    )

            return traceroute
        except Exception as e:
            print("Error in method traceroute : {}".format(e))
            log.error("Error in method traceroute : %s" % traceback.format_exc())

    def cli(self, commands, encoding="text"):
        """
        Will execute a list of commands and return the output in a dictionary format.
        """
        if encoding not in ("text",):
            raise NotImplementedError("%s is not a supported encoding" % encoding)
        try:
            cli_output = {}
            for cmd in commands:
                buff = self._perform_cli_commands([cmd], True)
                new_buff = ""
                for item in buff.split("\n"):
                    if "[]" in item:
                        continue
                    elif self.cmd_line_pattern_re.search(item):
                        continue
                    else:
                        row = item.strip()
                        if row == cmd:
                            continue
                        new_buff += row
                        new_buff += "\n"
                cli_output.update({cmd: new_buff})
            return cli_output
        except Exception as e:
            print("Error in method cli : {}".format(e))
            log.error("Error in method cli : %s" % traceback.format_exc())
