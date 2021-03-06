# Copyright 2015 Spotify AB. All rights reserved.
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

# import xmltodict
# used for XML output from the API

import tempfile
import re
from datetime import datetime


from base import NetworkDriver

from pycsco.nxos.device import Device as NXOSDevice
from pycsco.nxos.utils.file_copy import FileCopy
from pycsco.nxos.utils import install_config
from pycsco.nxos.utils import nxapi_lib
from pycsco.nxos.error import DiffError, FileTransferError, CLIError

from urllib2 import URLError

from exceptions import ConnectionException, MergeConfigException, ReplaceConfigException, CommandErrorException

def strip_trailing(string):
    lines = list(x.rstrip(' ') for x in string.splitlines())
    return '\n'.join(lines)


class NXOSDriver(NetworkDriver):

    def __init__(self, hostname, username, password, timeout=60, optional_args=None):
        if optional_args is None:
            optional_args = {}
        self.hostname = hostname
        self.username = username
        self.password = password
        self.timeout = timeout
        self.up = False
        try:
            self.device = NXOSDevice(username=username,
                                 password=password,
                                 ip=hostname,
                                 timeout=timeout)
            self.device.show('show version', fmat = 'json')
            # execute something easy
            # something easier with XML format?
            self.up = True
        except URLError:
            # unable to open connection
            pass
        self.replace = True
        self.loaded = False
        self.fc = None
        self.changed = False

    def open(self):
        if not self.up:
            raise ConnectionException('Cannot connect to {}'.format(self.hostname))

    def close(self):
        if self.changed:
            self._delete_file(self.backup_file)

    def _get_reply_body(self, result):
        # useful for debugging
        return result.get('ins_api', {}).get('outputs', {}).get('output', {}).get('body', {})

    def _get_reply_table(self, result, tablename, rowname):
        # still useful for debugging
        return self._get_reply_body(result).get(tablename, {}).get(rowname, [])

    def _get_command_table(self, command, tablename, rowname):

        result = {}

        try:
            # xml_result          = self.device.show(command)
            # json_output  = xmltodict.parse(xml_result[1])

            # or directly retrive JSON
            result = self.device.show(command, fmat = 'json')
            json_output = eval(result[1])
            # which will converted to a plain dictionary
        except Exception:
            return []

        return self._get_reply_table(json_output, tablename, rowname)

    def load_replace_candidate(self, filename=None, config=None):
        self.replace = True
        self.loaded = True

        if filename is None:
            temp_file = tempfile.NamedTemporaryFile()
            temp_file.write(config)
            temp_file.flush()
            cfg_file_path = temp_file.name
        else:
            cfg_file_path = filename

        self.fc = FileCopy(self.device, cfg_file_path)
        if not self.fc.file_already_exists():
            try:
                self.fc.transfer_file()
            except FileTransferError as fte:
                raise ReplaceConfigException(fte.message)

    def load_merge_candidate(self, filename=None, config=None):
        self.replace = False
        self.loaded = True
        if filename is not None:
            with open(filename, "r") as f:
                self.merge_candidate = f.read()
        else:
            self.merge_candidate = config

    def compare_config(self):
        if self.loaded:
            if not self.replace:
                return self.merge_candidate
            messy_diff = install_config.get_diff(self.device, self.fc.dst)
            clean_diff = strip_trailing(messy_diff)
            return clean_diff

        return ''

    def _commit_merge(self):
        commands = self.merge_candidate.splitlines()
        command_string = ';'.join(list(' %s ' % x.strip() for x in commands))
        self.device.config(command_string)

    def commit_config(self):
        if self.loaded:
            self.backup_file = 'config_' + str(datetime.now()).replace(' ', '_')
            install_config.save_config(self.device, self.backup_file)
            if self.replace:
                if install_config.rollback(self.device, self.fc.dst) is False:
                    raise ReplaceConfigException
            else:
                try:
                    self._commit_merge()
                except Exception as e:
                    raise MergeConfigException(str(e))

            self.changed = True
            self.loaded = False
        else:
            raise ReplaceConfigException('No config loaded.')

    def _delete_file(self, filename):
        self.device.show('terminal dont-ask', text=True)
        self.device.show('delete {}'.format(filename), text=True)
        self.device.show('no terminal dont-ask', text=True)

    def discard_config(self):
        if self.loaded and self.replace:
            try:
                self._delete_file(self.fc.dst)
            except CLIError:
                pass

        self.loaded = False

    def rollback(self):
        if self.changed:
            install_config.rollback(self.device, self.backup_file)
            self.changed = False

    def get_facts(self):
        results = {}
        facts_dict = nxapi_lib.get_facts(self.device)
        results['uptime'] = -1 # not implemented
        results['vendor'] = unicode('Cisco')
        results['os_version'] = facts_dict.get('os')
        results['serial_number'] = unicode('N/A')
        results['model'] = facts_dict.get('platform')
        results['hostname'] = facts_dict.get('hostname')
        results['fqdn'] = unicode('N/A')
        iface_list = results['interface_list'] = []

        intf_dict = nxapi_lib.get_interfaces_dict(self.device)
        for intf_list in intf_dict.values():
            for intf in intf_list:
                iface_list.append(intf)

        return results

    def get_interfaces(self):
        results = {}
        intf_dict = nxapi_lib.get_interfaces_dict(self.device)
        for intf_list in intf_dict.values():
            for intf in intf_list:
                intf_info = nxapi_lib.get_interface(self.device, intf)
                formatted_info = results[intf] = {}
                formatted_info['is_up'] = 'up' in intf_info.get('state', intf_info.get('admin_state', '')).lower()
                formatted_info['is_enabled'] = 'up' in intf_info.get('admin_state').lower()
                formatted_info['description'] = unicode(intf_info.get('description'))
                formatted_info['last_flapped'] = -1.0 #not implemented

                speed = intf_info.get('speed', '0')
                try:
                    speed = int(re.sub(r'[^\d]', '', speed).strip())
                except ValueError:
                    speed = -1

                formatted_info['speed'] = speed
                formatted_info['mac_address'] = unicode(intf_info.get('mac_address', 'N/A'))

        return results

    def get_lldp_neighbors(self):
        results = {}
        neighbor_list = nxapi_lib.get_neighbors(self.device, 'lldp')
        for neighbor in neighbor_list:
            local_iface = neighbor.get('local_interface')
            if neighbor.get(local_iface) is None:
                if local_iface not in results:
                    results[local_iface] = []

            neighbor_dict = {}
            neighbor_dict['hostname'] = unicode(neighbor.get('neighbor'))
            neighbor_dict['port'] = unicode(neighbor.get('neighbor_interface'))

            results[local_iface].append(neighbor_dict)

        return results

    def get_checkpoint_file(self):
        return install_config.get_checkpoint(self.device)

    def get_lldp_neighbors_detail(self, interface = ''):

        lldp_neighbors = dict()

        filter = ''
        if interface:
            filter = 'interface {name} '.format(
                name = interface
            )

        command = 'show lldp neighbors {filter}detail'.format(
            filter = filter
        ) # seems that show LLDP neighbors detail does not return JSON output...

        lldp_neighbors_table_str = self.cli([command]).get(command)
        # thus we need to take the raw text output

        lldp_neighbors_list = lldp_neighbors_table_str.splitlines()

        if not lldp_neighbors_list:
            return lldp_neighbors # empty dict

        CHASSIS_REGEX       = '^(Chassis id:)\s+([a-z0-9\.]+)$'
        PORT_REGEX          = '^(Port id:)\s+([0-9]+)$'
        LOCAL_PORT_ID_REGEX = '^(Local Port id:)\s+(.*)$'
        PORT_DESCR_REGEX    = '^(Port Description:)\s+(.*)$'
        SYSTEM_NAME_REGEX   = '^(System Name:)\s+(.*)$'
        SYSTEM_DESCR_REGEX  = '^(System Description:)\s+(.*)$'
        SYST_CAPAB_REEGX    = '^(System Capabilities:)\s+(.*)$'
        ENABL_CAPAB_REGEX   = '^(Enabled Capabilities:)\s+(.*)$'
        VLAN_ID_REGEX       = '^(Vlan ID:)\s+(.*)$'

        lldp_neighbor = {}
        interface_name = None

        for line in lldp_neighbors_list:
            chassis_rgx = re.search(CHASSIS_REGEX, line, re.I)
            if chassis_rgx:
                lldp_neighbor = {
                    'remote_chassis_id': unicode(chassis_rgx.groups()[1])
                }
                continue
            port_rgx = re.search(PORT_REGEX, line, re.I)
            if port_rgx:
                lldp_neighbor['parent_interface'] = unicode(port_rgx.groups()[1])
                continue # jump to next line
            local_port_rgx = re.search(LOCAL_PORT_ID_REGEX, line, re.I)
            if local_port_rgx:
                interface_name = local_port_rgx.groups()[1]
                continue
            port_descr_rgx = re.search(PORT_DESCR_REGEX, line, re.I)
            if port_descr_rgx:
                lldp_neighbor['remote_port'] = unicode(port_descr_rgx.groups()[1])
                lldp_neighbor['remote_port_description'] = unicode(port_descr_rgx.groups()[1])
                continue
            syst_name_rgx = re.search(SYSTEM_NAME_REGEX, line, re.I)
            if syst_name_rgx:
                lldp_neighbor['remote_system_name'] = unicode(syst_name_rgx.groups()[1])
                continue
            syst_descr_rgx = re.search(SYSTEM_DESCR_REGEX, line, re.I)
            if syst_descr_rgx:
                lldp_neighbor['remote_system_description'] = unicode(syst_descr_rgx.groups()[1])
                continue
            syst_capab_rgx = re.search(SYST_CAPAB_REEGX, line, re.I)
            if syst_capab_rgx:
                lldp_neighbor['remote_system_capab'] = unicode(syst_capab_rgx.groups()[1])
                continue
            syst_enabled_rgx = re.search(ENABL_CAPAB_REGEX, line, re.I)
            if syst_enabled_rgx:
                lldp_neighbor['remote_system_enable_capab'] = unicode(syst_enabled_rgx.groups()[1])
                continue
            vlan_rgx = re.search(VLAN_ID_REGEX, line, re.I)
            if vlan_rgx:
                # at the end of the loop
                if interface_name not in lldp_neighbors.keys():
                    lldp_neighbors[interface_name] = list()
                lldp_neighbors[interface_name].append(lldp_neighbor)

        return lldp_neighbors

    def cli(self, commands = None):

        cli_output = dict()

        if type(commands) is not list:
            raise TypeError('Please enter a valid list of commands!')

        for command in commands:
            try:
                string_output = self.device.show(command, fmat = 'json', text = True)[1]
                dict_output   = eval(string_output)
                cli_output[unicode(command)] = self._get_reply_body(dict_output)
            except Exception as e:
                cli_output[unicode(command)] = 'Unable to execute command "{cmd}": {err}'.format(
                    cmd = command,
                    err = e
                )
                raise CommandErrorException(str(cli_output))

        return cli_output

    def get_arp_table(self):

        arp_table = list()

        command = 'show ip arp'

        arp_table_raw = self._get_command_table(command, 'TABLE_vrf', 'ROW_vrf').get('TABLE_adj', {}).get('ROW_adj', [])

        if type(arp_table_raw) is dict:
            arp_table_raw = [arp_table_raw]

        for arp_table_entry in arp_table_raw:
            ip          = unicode(arp_table_entry.get('ip-addr-out'))
            mac_raw     = arp_table_entry.get('mac')
            mac_all     = mac_raw.replace('.', '').replace(':', '')
            mac_format  = unicode(':'.join([mac_all[i:i+2] for i in range(12)[::2]]))
            age         = arp_table_entry.get('time-stamp')
            age_time    = ''.join(age.split(':'))
            age_sec     = float(3600 * int(age_time[:2]) + 60 * int(age_time[2:4]) + int(age_time[4:]))
            interface   = unicode(arp_table_entry.get('intf-out'))
            arp_table.append(
                {
                    'interface' : interface,
                    'mac'       : mac_format,
                    'ip'        : ip,
                    'age'       : age_sec
                }
            )

        return arp_table

    def get_ntp_peers(self):

        ntp_peers = dict()

        command = 'show ntp peer-status'

        ntp_peers_table = self._get_command_table(command, 'TABLE_peersstatus', 'ROW_peersstatus')

        if type(ntp_peers_table) is dict:
            ntp_peers_table = [ntp_peers_table]

        for ntp_peer in ntp_peers_table:
            peer_address = unicode(ntp_peer.get('remote'))
            stratum      = int(ntp_peer.get('st'))
            hostpoll     = int(ntp_peer.get('poll'))
            reachability = int(ntp_peer.get('reach'))
            delay        = float(ntp_peer.get('delay'))
            ntp_peers[peer_address] = {
                'referenceid'   : peer_address,
                'stratum'       : stratum,
                'type'          : u'',
                'when'          : u'',
                'hostpoll'      : hostpoll,
                'reachability'  : reachability,
                'delay'         : delay,
                'offset'        : 0.0,
                'jitter'        : 0.0
            }

        return ntp_peers

    def get_interfaces_ip(self):

        interfaces_ip = dict()

        command_ipv4 = 'show ip interface'

        ipv4_interf_table_vrf = self._get_command_table(command_ipv4, 'TABLE_intf', 'ROW_intf')

        if type(ipv4_interf_table_vrf) is dict:
            # when there's one single entry, it is not returned as a list
            # with one single element
            # but as a simple dict
            ipv4_interf_table_vrf = [ipv4_interf_table_vrf]

        for interface in ipv4_interf_table_vrf:
            interface_name = unicode(interface.get('intf-name', ''))
            address = unicode(interface.get('prefix', ''))
            prefix  = int(interface.get('masklen', ''))
            if interface_name not in interfaces_ip.keys():
                interfaces_ip[interface_name] = dict()
            if u'ipv4' not in interfaces_ip[interface_name].keys():
                interfaces_ip[interface_name][u'ipv4'] = dict()
            if address not in interfaces_ip[interface_name].get(u'ipv4'):
                interfaces_ip[interface_name][u'ipv4'][address] = dict()
            interfaces_ip[interface_name][u'ipv4'][address].update({
                'prefix_length': prefix
            })
            secondary_addresses = interface.get('TABLE_secondary_address', {}).get('ROW_secondary_address', [])
            if type(secondary_addresses) is dict:
                secondary_addresses = [secondary_addresses]
            for secondary_address in secondary_addresses:
                secondary_address_ip        = unicode(secondary_address.get('prefix1', ''))
                secondary_address_prefix    = int(secondary_address.get('masklen1', ''))
                if u'ipv4' not in interfaces_ip[interface_name].keys():
                    interfaces_ip[interface_name][u'ipv4'] = dict()
                if secondary_address_ip not in interfaces_ip[interface_name].get(u'ipv4'):
                    interfaces_ip[interface_name][u'ipv4'][secondary_address_ip] = dict()
                interfaces_ip[interface_name][u'ipv4'][secondary_address_ip].update({
                    'prefix_length': secondary_address_prefix
                })

        command_ipv6 = 'show ipv6 interface'

        ipv6_interf_table_vrf = self._get_command_table(command_ipv6, 'TABLE_intf', 'ROW_intf')

        if type(ipv6_interf_table_vrf) is dict:
            ipv6_interf_table_vrf = [ipv6_interf_table_vrf]

        for interface in ipv6_interf_table_vrf:
            interface_name = unicode(interface.get('intf-name', ''))
            address = unicode(interface.get('addr', ''))
            prefix  = int(interface.get('prefix', '').split('/')[-1])
            if interface_name not in interfaces_ip.keys():
                interfaces_ip[interface_name] = dict()
            if u'ipv6' not in interfaces_ip[interface_name].keys():
                interfaces_ip[interface_name][u'ipv6'] = dict()
            if address not in interfaces_ip[interface_name].get('ipv6'):
                interfaces_ip[interface_name][u'ipv6'][address] = dict()
            interfaces_ip[interface_name][u'ipv6'][address].update({
                u'prefix_length': prefix
            })
            secondary_addresses = interface.get('TABLE_sec_addr', {}).get('ROW_sec_addr', [])
            if type(secondary_addresses) is dict:
                secondary_addresses = [secondary_addresses]
            for secondary_address in secondary_addresses:
                sec_prefix = secondary_address.get('sec-prefix', '').split('/')
                secondary_address_ip        = unicode(sec_prefix[0])
                secondary_address_prefix    = int(sec_prefix[-1])
                if u'ipv6' not in interfaces_ip[interface_name].keys():
                    interfaces_ip[interface_name][u'ipv6'] = dict()
                if secondary_address_ip not in interfaces_ip[interface_name].get(u'ipv6'):
                    interfaces_ip[interface_name][u'ipv6'][secondary_address_ip] = dict()
                interfaces_ip[interface_name][u'ipv6'][secondary_address_ip].update({
                    u'prefix_length': secondary_address_prefix
                })

        return interfaces_ip

    def get_mac_address_table(self):

        mac_table = list()

        command = 'show mac address-table'
        mac_table_raw = self._get_command_table(command, 'TABLE_mac_address', 'ROW_mac_address')

        if type(mac_table_raw) is dict:
            mac_table_raw = [mac_table_raw]

        for mac_entry in mac_table_raw:
            mac_raw     = mac_entry.get('disp_mac_addr')
            mac_str     = mac_raw.replace('.', '').replace(':', '')
            mac_format  = unicode(':'.join([ mac_str[i:i+2] for i in range(12)[::2] ]))
            interface   = unicode(mac_entry.get('disp_port'))
            age         = mac_entry.get('disp_age')
            vlan        = int(mac_entry.get('disp_vlan'))
            active      = True
            static      = (mac_entry.get('disp_is_static') != '0')
            moves       = 0
            last_move   = 0.0
            mac_table.append(
                {
                    'mac'       : mac_format,
                    'interface' : interface,
                    'vlan'      : vlan,
                    'active'    : active,
                    'static'    : static,
                    'moves'     : moves,
                    'last_move' : last_move
                }
            )

        return mac_table
