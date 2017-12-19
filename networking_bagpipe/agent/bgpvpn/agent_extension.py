# Copyright (c) 2015 Orange.
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

"""
L2 Agent extension to support bagpipe networking-bgpvpn driver RPCs in the
OpenVSwitch agent
"""

import itertools
import netaddr

from oslo_concurrency import lockutils
from oslo_config import cfg
from oslo_log import helpers as log_helpers
from oslo_log import log as logging

from networking_bagpipe._i18n import _
from networking_bagpipe.agent import agent_base_info
from networking_bagpipe.agent import bagpipe_bgp_agent
from networking_bagpipe.agent.bgpvpn import constants as bgpvpn_const
from networking_bagpipe.bagpipe_bgp import constants as bbgp_const
from networking_bagpipe.driver import type_route_target
from networking_bagpipe.objects import bgpvpn as objects

from neutron.agent.common import ovs_lib
from neutron.api.rpc.callbacks.consumer import registry as rpc_registry
from neutron.api.rpc.callbacks import events as rpc_events
from neutron.api.rpc.handlers import resources_rpc
from neutron.conf.agent import common as config
from neutron.conf.plugins.ml2.drivers import ovs_conf
from neutron.debug import debug_agent
from neutron.plugins.ml2.drivers.linuxbridge.agent.common \
    import constants as lnxbridge_agt_constants
from neutron.plugins.ml2.drivers.linuxbridge.agent.linuxbridge_neutron_agent \
    import LinuxBridgeManager
from neutron.plugins.ml2.drivers.openvswitch.agent.common \
    import constants as ovs_agt_constants
from neutron.plugins.ml2.drivers.openvswitch.agent import vlanmanager

from neutron_lib.agent import l2_extension
from neutron_lib.api.definitions import bgpvpn
from neutron_lib.callbacks import events
from neutron_lib.callbacks import registry
from neutron_lib.callbacks import resources
from neutron_lib import constants as n_const

LOG = logging.getLogger(__name__)

bagpipe_bgpvpn_opts = [
    cfg.StrOpt('mpls_bridge', default='br-mpls',
               help=_("OVS MPLS bridge to use")),
]

# these options are for internal use only (fullstack tests), and hence
# better kept in a separate table not looked at by oslo gen confi hooks
internal_opts = [
    cfg.StrOpt('tun_to_mpls_peer_patch_port', default='patch-to-mpls',
               help=_("OVS Peer patch port in tunnel bridge to MPLS bridge ")),
    cfg.StrOpt('mpls_to_tun_peer_patch_port', default='patch-from-tun',
               help=_("OVS Peer patch port in MPLS bridge to tunnel bridge ")),
    cfg.StrOpt('mpls_to_int_peer_patch_port', default='patch-mpls-to-int',
               help=_("OVS Peer patch port in MPLS bridge to int bridge ")),
    cfg.StrOpt('int_to_mpls_peer_patch_port', default='patch-int-from-mpls',
               help=_("OVS Peer patch port in int bridge to MPLS bridge ")),
]

cfg.CONF.register_opts(bagpipe_bgpvpn_opts, "BAGPIPE")
cfg.CONF.register_opts(internal_opts, "BAGPIPE")
ovs_conf.register_ovs_agent_opts()
config.register_agent_state_opts_helper(cfg.CONF)


NO_NEED_FOR_VNI = -1


def has_attachement(bgpvpn_info, vpn_type):
    return (vpn_type in bgpvpn_info and (
            bgpvpn_info[vpn_type].get(bbgp_const.RT_IMPORT) or
            bgpvpn_info[vpn_type].get(bbgp_const.RT_EXPORT))
            )


class BagpipeBgpvpnAgentExtension(l2_extension.L2AgentExtension,
                                  agent_base_info.BaseInfoManager):

    def __init__(self):
        super(BagpipeBgpvpnAgentExtension, self).__init__()
        self.ports = set()

    @log_helpers.log_method_call
    def consume_api(self, agent_api):
        self.agent_api = agent_api

    @log_helpers.log_method_call
    def initialize(self, connection, driver_type):
        self.driver_type = driver_type
        if self._is_ovs_extension():
            self.int_br = self.agent_api.request_int_br()
            self.tun_br = self.agent_api.request_tun_br()

            if self.tun_br is None:
                raise Exception("tunneling is not enabled in OVS agent, "
                                "however bagpipe_bgpvpn extensions needs it")

            self.bagpipe_bgp_agent = (
                bagpipe_bgp_agent.BaGPipeBGPAgent.get_instance(
                    n_const.AGENT_TYPE_OVS)
            )

            self._setup_mpls_br()

            self.vlan_manager = vlanmanager.LocalVlanManager()

            registry.subscribe(self.ovs_restarted,
                               resources.AGENT,
                               events.OVS_RESTARTED)

        elif self._is_linuxbridge_extension():
            self.bagpipe_bgp_agent = (
                bagpipe_bgp_agent.BaGPipeBGPAgent.get_instance(
                    n_const.AGENT_TYPE_LINUXBRIDGE)
            )
        else:
            raise Exception("driver type not supported: %s", driver_type)

        self.bagpipe_bgp_agent.register_build_callback(
            bgpvpn_const.BGPVPN_SERVICE,
            self.build_bgpvpn_attach_info)

        # NOTE(tmorin): replace by callback, so that info can be derived
        # from self.ports_info.keys() instead of being duplicated into
        # self.ports
        self.bagpipe_bgp_agent.register_port_list(bgpvpn_const.BGPVPN_SERVICE,
                                                  self.ports)
        # OVO-based BGPVPN RPCs
        self._setup_rpc(connection)

    def _is_ovs_extension(self):
        return self.driver_type == ovs_agt_constants.EXTENSION_DRIVER_TYPE

    def _is_linuxbridge_extension(self):
        return (
            self.driver_type == lnxbridge_agt_constants.EXTENSION_DRIVER_TYPE)

    def _setup_rpc(self, connection):
        self.rpc_pull_api = resources_rpc.ResourcesPullRpcApi()

        rpc_registry.register(self.handle_notification_net_assocs,
                              objects.BGPVPNNetAssociation.obj_name())
        rpc_registry.register(self.handle_notification_router_assocs,
                              objects.BGPVPNRouterAssociation.obj_name())
        endpoints = [resources_rpc.ResourcesPushRpcCallback()]
        topic_net_assoc = resources_rpc.resource_type_versioned_topic(
            objects.BGPVPNNetAssociation.obj_name())
        topic_router_assoc = resources_rpc.resource_type_versioned_topic(
            objects.BGPVPNRouterAssociation.obj_name())
        connection.create_consumer(topic_net_assoc, endpoints, fanout=True)
        connection.create_consumer(topic_router_assoc, endpoints, fanout=True)

    @log_helpers.log_method_call
    @lockutils.synchronized('bagpipe-bgpvpn')
    def handle_port(self, context, data):
        port_id = data['port_id']
        network_id = data['network_id']

        if self._ignore_port(context, data):
            return

        self.ports.add(port_id)

        net_info, port_info = (
            self._get_network_port_infos(network_id, port_id)
        )

        if data['network_type'] == n_const.TYPE_VXLAN:
            net_info.segmentation_id = data['segmentation_id']
        # for type driver 'ROUTE_TARGET' we need to track the fact
        # that we don't need a VNI (using -1 special value)
        if data['network_type'] == type_route_target.TYPE_ROUTE_TARGET:
            net_info.segmentation_id = NO_NEED_FOR_VNI

        port_info.set_ip_mac_infos(data['fixed_ips'][0]['ip_address'],
                                   data['mac_address'])

        assocs = self.rpc_pull_api.pull(context,
                                        objects.BGPVPNAssociations.obj_name(),
                                        network_id)
        for assoc in itertools.chain(assocs.network_associations,
                                     assocs.router_associations):
            # replug_ports=False because we will call do_port_plug
            # once for all associations, and only for this port, out of
            # the loop
            self._add_association_for_net(network_id, assoc,
                                          replug_ports=False)

        if port_info.associations:
            self.bagpipe_bgp_agent.do_port_plug(port_id)

    @log_helpers.log_method_call
    @lockutils.synchronized('bagpipe-bgpvpn')
    def delete_port(self, context, data):
        port_id = data['port_id']
        port_info = self.ports_info.get(port_id)

        if port_info and port_info.associations:
            if len(port_info.network.ports) == 1:
                # last port on network...
                self._check_arp_voodoo_unplug(port_info.network,
                                              last_port=True)

            detach_info = (
                self._build_bgpvpn_detach_info(port_info)
            )

            # here if this was the last port for its network
            # we clean our cache for this port
            self._remove_network_port_infos(port_info.network.id, port_id)
            self.ports.remove(port_id)

            self.bagpipe_bgp_agent.do_port_plug_refresh(port_id,
                                                        detach_info)

    @log_helpers.log_method_call
    @lockutils.synchronized('bagpipe-bgpvpn')
    def handle_notification_net_assocs(self, context, resource_type,
                                       net_assocs, event_type):
        for net_assoc in net_assocs:
            if event_type in (rpc_events.CREATED, rpc_events.UPDATED):
                self._add_association_for_net(net_assoc.network_id,
                                              net_assoc)
            elif event_type == rpc_events.DELETED:
                self._remove_association_for_net(net_assoc.network_id,
                                                 net_assoc)
            else:
                LOG.warning("unsupported event: %s", event_type)

    @log_helpers.log_method_call
    @lockutils.synchronized('bagpipe-bgpvpn')
    def handle_notification_router_assocs(self, context, resource_type,
                                          router_assocs, event_type):
        for router_assoc in router_assocs:
            if event_type in (rpc_events.CREATED, rpc_events.UPDATED):
                for connected_net in router_assoc.connected_networks:
                    self._add_association_for_net(connected_net['network_id'],
                                                  router_assoc)
            elif event_type == rpc_events.DELETED:
                for connected_net in router_assoc.connected_networks:
                    self._remove_association_for_net(
                        connected_net['network_id'],
                        router_assoc)
            else:
                LOG.warning("unsupported event: %s", event_type)

    @log_helpers.log_method_call
    def _add_association_for_net(self, network_id, assoc, replug_ports=True):
        if not any([assoc.bgpvpn.route_targets,
                    assoc.bgpvpn.import_targets,
                    assoc.bgpvpn.export_targets]):
            LOG.debug("ignoring association %s because empty RT params")
            return

        LOG.debug("add association with bgpvpn %s", assoc.bgpvpn)

        net_info = self.networks_info[network_id]

        # for now we only support a single IPv4 subnet
        for subnet in assoc.all_subnets(network_id):
            if subnet['ip_version'] == 4:
                gateway_info = agent_base_info.GatewayInfo(
                    subnet['gateway_mac'],
                    subnet['gateway_ip']
                )
                if assoc.bgpvpn.type == bgpvpn.BGPVPN_L3:
                    self._check_arp_voodoo_plug(net_info, gateway_info)
                net_info.set_gateway_info(gateway_info)
                break

        if not net_info:
            LOG.debug("no net_info for network %s, skipping", network_id)
            return

        if not net_info.ports:
            LOG.debug("no port on network %s, skipping", network_id)
            return

        net_info.add_association(assoc)

        if replug_ports:
            for port_info in net_info.ports:
                self.bagpipe_bgp_agent.do_port_plug(port_info.id)

    @log_helpers.log_method_call
    def _remove_association_for_net(self, network_id, assoc):
        net_info = self.networks_info.get(network_id)

        if not net_info:
            LOG.debug("no net_info for network %s, skipping", network_id)
            return

        if not net_info.ports:
            LOG.debug("no port on network %s, skipping", network_id)
            return

        # is there an association of same BGPVPN type that remains ?
        remaining = len([1 for a in net_info.associations
                         if a.bgpvpn.type == assoc.bgpvpn.type])

        # we need to build port detach_information before we update
        # net_info.associations:
        if remaining <= 1:
            detach_info = {}
            for port_info in net_info.ports:
                detach_info[port_info.id] = (
                    self._build_bgpvpn_detach_info(port_info,
                                                   assoc.bgpvpn.type)
                )

        net_info.remove_association(assoc)

        if remaining > 1:
            LOG.debug("some association of type %s remain, updating all ports",
                      assoc.bgpvpn.type)
            for port_info in net_info.ports:
                self.bagpipe_bgp_agent.do_port_plug(port_info.id)
        else:
            LOG.debug("no association of type %s remains, detaching it for "
                      "all ports", assoc.bgpvpn.type)
            for port_info in net_info.ports:
                self.bagpipe_bgp_agent.do_port_plug_refresh(
                    port_info.id,
                    detach_info[port_info.id]
                )

            if assoc.bgpvpn.type == bgpvpn.BGPVPN_L3:
                self._check_arp_voodoo_unplug(net_info, last_assoc=True)

    def _format_associations_route_targets(self, assocs):
        bgpvpn_rts = {}
        for assoc in assocs:
            bgpvpn = assoc.bgpvpn
            vpn_type = bgpvpn_const.BGPVPN_2_BAGPIPE[bgpvpn.type]

            # Add necessary keys to BGP VPN route targets dictionary
            if vpn_type not in bgpvpn_rts:
                bgpvpn_rts[vpn_type] = {bbgp_const.RT_IMPORT: [],
                                        bbgp_const.RT_EXPORT: []}

            bgpvpn_rts[vpn_type][bbgp_const.RT_IMPORT] += bgpvpn.route_targets
            bgpvpn_rts[vpn_type][bbgp_const.RT_IMPORT] += bgpvpn.import_targets

            bgpvpn_rts[vpn_type][bbgp_const.RT_EXPORT] += bgpvpn.route_targets
            bgpvpn_rts[vpn_type][bbgp_const.RT_EXPORT] += bgpvpn.export_targets

        for rts in bgpvpn_rts.values():
            for i_or_e in [bbgp_const.RT_IMPORT, bbgp_const.RT_EXPORT]:
                if i_or_e in rts:
                    rts[i_or_e] = list(set(rts[i_or_e]))

        return bgpvpn_rts

    def _ignore_port(self, context, data):
        if data['port_id'] is None:
            return True

        if (data['device_owner'].startswith(
            n_const.DEVICE_OWNER_NETWORK_PREFIX) and not (data['device_owner']
            in (debug_agent.DEVICE_OWNER_COMPUTE_PROBE,
                debug_agent.DEVICE_OWNER_NETWORK_PROBE))):
            LOG.info("Port %s owner is network:*, we'll do nothing",
                     data['port_id'])
            return True

        return False

    def _setup_mpls_br(self):
        '''Setup the MPLS bridge for bagpipe-bgp.

        Creates MPLS bridge, and links it to the integration and tunnel
        bridges using patch ports.

        :param mpls_br: the name of the MPLS bridge.
        '''
        mpls_br = cfg.CONF.BAGPIPE.mpls_bridge
        self.mpls_br = ovs_lib.OVSBridge(mpls_br)

        if not self.mpls_br.bridge_exists(mpls_br):
            LOG.error("Unable to enable MPLS on this agent, MPLS bridge "
                      "%(mpls_br)s doesn't exist. Agent terminated!",
                      {"mpls_br": mpls_br})
            exit(1)

        # patch ports for traffic from tun bridge to mpls bridge
        self.patch_tun_to_mpls_ofport = self.tun_br.add_patch_port(
            cfg.CONF.BAGPIPE.tun_to_mpls_peer_patch_port,
            cfg.CONF.BAGPIPE.mpls_to_tun_peer_patch_port)
        self.patch_mpls_to_tun_ofport = self.mpls_br.add_patch_port(
            cfg.CONF.BAGPIPE.mpls_to_tun_peer_patch_port,
            cfg.CONF.BAGPIPE.tun_to_mpls_peer_patch_port)

        # patch ports for traffic from mpls bridge to int bridge
        self.patch_mpls_to_int_ofport = self.mpls_br.add_patch_port(
            cfg.CONF.BAGPIPE.mpls_to_int_peer_patch_port,
            cfg.CONF.BAGPIPE.int_to_mpls_peer_patch_port)
        self.patch_int_to_mpls_ofport = self.int_br.add_patch_port(
            cfg.CONF.BAGPIPE.int_to_mpls_peer_patch_port,
            cfg.CONF.BAGPIPE.mpls_to_int_peer_patch_port)

        if (int(self.patch_tun_to_mpls_ofport) < 0 or
                int(self.patch_mpls_to_tun_ofport) < 0 or
                int(self.patch_int_to_mpls_ofport) < 0 or
                int(self.patch_mpls_to_int_ofport) < 0):
            LOG.error("Failed to create OVS patch port. Cannot have "
                      "MPLS enabled on this agent, since this version "
                      "of OVS does not support patch ports. "
                      "Agent terminated!")
            exit(1)

        patch_int_ofport = self.tun_br.get_port_ofport(
            cfg.CONF.OVS.tun_peer_patch_port)

        # In br-tun, redirect all traffic from VMs toward a BGPVPN
        # default gateway MAC address to the MPLS bridge.
        #
        # (priority >0 is needed or we hit the rule redirecting unicast to
        # the UCAST_TO_TUN table)
        self.tun_br.add_flow(
            table=ovs_agt_constants.PATCH_LV_TO_TUN,
            priority=1,
            in_port=patch_int_ofport,
            dl_dst=bgpvpn_const.DEFAULT_GATEWAY_MAC,
            actions="output:%s" % self.patch_tun_to_mpls_ofport
        )

        # Redirect traffic from the MPLS bridge to br-int
        self.tun_br.add_flow(in_port=self.patch_tun_to_mpls_ofport,
                             actions="output:%s" % patch_int_ofport)

    @log_helpers.log_method_call
    @lockutils.synchronized('bagpipe-bgpvpn')
    def ovs_restarted(self, resources, event, trigger):
        self._setup_mpls_br()
        for net_info in self.networks_info.values():
            if (net_info.ports and
                    net_info.gateway_info != agent_base_info.NO_GW_INFO and
                    any([assoc.bgpvpn.type == bgpvpn.BGPVPN_L3
                        for assoc in net_info.associations])):
                self._check_arp_voodoo_plug(net_info, net_info.gateway_info)
        # TODO(tmorin): need to handle restart on bagpipe-bgp side, in the
        # meantime after an OVS restart, restarting bagpipe-bgp is required

    @log_helpers.log_method_call
    def _enable_gw_redirect(self, vlan, gateway_ip):
        # Add ARP responder entry for default gateway in br-tun

        # We may compete with the ARP responder entry for the real MAC
        # if the router is on a network node and we are a compute node,
        # so we must add our rule with a higher priority. Using a different
        # priority also means that arp_responder will not remove our ARP
        # responding flows and we won't remove theirs.

        # NOTE(tmorin): consider adding priority to install_arp_responder
        # and then use it here

        # (mostly copy-pasted ovs_ofctl....install_arp_responder)
        actions = ovs_agt_constants.ARP_RESPONDER_ACTIONS % {
            'mac': netaddr.EUI(bgpvpn_const.DEFAULT_GATEWAY_MAC,
                               dialect=netaddr.mac_unix),
            'ip': netaddr.IPAddress(gateway_ip),
        }
        self.tun_br.add_flow(table=ovs_agt_constants.ARP_RESPONDER,
                             priority=2,  # see above
                             dl_vlan=vlan,
                             proto='arp',
                             arp_op=0x01,
                             arp_tpa='%s' % gateway_ip,
                             actions=actions)

    @log_helpers.log_method_call
    def _disable_gw_redirect(self, vlan, gateway_ip):
        # Remove ARP responder entry for default gateway in br-tun
        self.tun_br.delete_flows(
            strict=True,
            table=ovs_agt_constants.ARP_RESPONDER,
            priority=2,
            dl_vlan=vlan,
            proto='arp',
            arp_op=0x01,
            arp_tpa='%s' % gateway_ip)

    @log_helpers.log_method_call
    def _hide_real_gw_arp(self, vlan, gateway_info):
        # Kill ARP replies for the gateway IP coming on br-int from the real
        # router, if any.
        #
        # NOTE(tmorin): we assume that the router MAC exists only in this vlan.
        # Doing filtering based on the local vlan would be better, but
        # we can't do this in br-int because this bridge does tagging based
        # on ovs-vsctl port tags.
        self.int_br.add_flow(table=ovs_agt_constants.LOCAL_SWITCHING,
                             priority=2,
                             proto='arp',
                             arp_op=0x2,
                             dl_src=gateway_info.mac,
                             arp_sha=gateway_info.mac,
                             arp_spa=gateway_info.ip,
                             actions="drop")

        # ARP requests from the real gateway need to
        # have their IP address changed to hide the gateway
        # address or the VMs will use it to update their
        # ARP cache implicitly. Below we overwrite it with 0.0.0.0.
        self.int_br.add_flow(table=ovs_agt_constants.LOCAL_SWITCHING,
                             priority=2,
                             proto='arp',
                             arp_op=0x01,
                             dl_src=gateway_info.mac,
                             arp_spa=gateway_info.ip,
                             arp_sha=gateway_info.mac,
                             actions="load:0x0->NXM_OF_ARP_SPA[],NORMAL")

    @log_helpers.log_method_call
    def _unhide_real_gw_arp(self, vlan, gateway_mac):
        LOG.debug("unblocking ARP from real gateway for vlan %d (%s)",
                  vlan, gateway_mac)
        self.int_br.delete_flows(table=ovs_agt_constants.LOCAL_SWITCHING,
                                 proto='arp',
                                 dl_src=gateway_mac,
                                 arp_sha=gateway_mac)

    @log_helpers.log_method_call
    def _check_arp_voodoo_plug(self, net_info, new_gateway_info):

        if not self._is_ovs_extension():
            return

        try:
            vlan = self.vlan_manager.get(net_info.id).vlan

            # NOTE(tmorin): can be improved, only needed on first plug...
            self._enable_gw_redirect(vlan, new_gateway_info.ip)

            # update real gateway ARP blocking...
            # remove old ARP blocking ?
            if net_info.gateway_info.mac is not None:
                self._unhide_real_gw_arp(vlan, net_info.gateway_info.mac)
            # add new ARP blocking ?
            if new_gateway_info.mac:
                self._hide_real_gw_arp(vlan, new_gateway_info)
        except vlanmanager.MappingNotFound:
            LOG.warning("no VLAN mapping for net %s no ARP voodoo in place",
                        net_info.id)

    @log_helpers.log_method_call
    def _check_arp_voodoo_unplug(self, net_info,
                                 last_port=False, last_assoc=False):

        if not self._is_ovs_extension():
            return

        if not net_info:
            return

        # if we are unplugging the last_port, we don't need to do
        # anything if there is no l3vpn for this network
        if last_port and not any([assoc.bgpvpn.type == bgpvpn.BGPVPN_L3
                                  for assoc in net_info.associations]):
            return

        # if we have just removed the last l3vpn association for the network
        # then we don't need to do anything if there is no port on this network
        if last_assoc and len(net_info.ports) == 0:
            return

        LOG.debug("undoing voodoo ARP for net %s", net_info.id)
        # NOTE(tmorin): vlan lookup might break if port is already
        # unplugged from bridge ?
        vlan = self.vlan_manager.get(net_info.id).vlan
        self._disable_gw_redirect(vlan, net_info.gateway_info.ip)
        if net_info.gateway_info.mac is not None:
            self._unhide_real_gw_arp(vlan, net_info.gateway_info.mac)

    def _base_attach_info(self, port_info):
        i = {
            'network_id': port_info.network.id,
            'ip_address': port_info.ip_address,
            'mac_address': port_info.mac_address,
            'local_port': {}
        }

        if self._is_ovs_extension():
            vlan = self.vlan_manager.get(port_info.network.id).vlan
            i['local_port']['linuxif'] = (
                '%s:%s' % (bgpvpn_const.LINUXIF_PREFIX, vlan))
        else:
            i['local_port']['linuxif'] = (
                LinuxBridgeManager.get_tap_device_name(port_info.id))

        return i

    @log_helpers.log_method_call
    def build_bgpvpn_attach_info(self, port_id):
        if port_id not in self.ports_info:
            LOG.debug("%s service has no PortInfo for port %s",
                      bgpvpn_const.BGPVPN_SERVICE, port_id)
            return {}

        port_info = self.ports_info[port_id]
        net_info = port_info.network

        attach_info = self._base_attach_info(port_info)
        attach_info['gateway_ip'] = net_info.gateway_info.ip

        attach_info.update(
            self._format_associations_route_targets(port_info.associations)
        )

        if self._is_ovs_extension():
            # Add OVS VLAN information
            vlan = self.vlan_manager.get(net_info.id).vlan

            # no OVS driver yet for EVPN
            if bbgp_const.EVPN in attach_info:
                LOG.warning("BGPVPN type L2 (EVPN) is not supported with "
                            "OVS yet")

            if has_attachement(attach_info, bbgp_const.IPVPN):
                attach_info[bbgp_const.IPVPN].update({
                    'local_port': {
                        'ovs': {
                            'plugged': True,
                            'port_number': self.patch_mpls_to_tun_ofport,
                            'vlan': vlan
                        }
                    }
                })

                # Add fallback information if needed as well
                if net_info.gateway_info.mac:
                    attach_info[bbgp_const.IPVPN].update({
                        'fallback': {
                            'dst_mac': net_info.gateway_info.mac,
                            'src_mac': bgpvpn_const.FALLBACK_SRC_MAC,
                            'ovs_port_number': self.patch_mpls_to_int_ofport
                        }
                    })
        else:  # linuxbridge
            if has_attachement(attach_info, bbgp_const.EVPN):
                attach_info[bbgp_const.EVPN]['linuxbr'] = (
                    LinuxBridgeManager.get_bridge_name(net_info.id)
                )
            if has_attachement(attach_info, bbgp_const.IPVPN):
                # the interface we need to pass to bagpipe is the
                # bridge
                attach_info[bbgp_const.IPVPN]['local_port'] = {
                    'linuxif': LinuxBridgeManager.get_bridge_name(net_info.id)
                }
                # NOTE(tmorin): fallback support still missing

        if has_attachement(attach_info, bbgp_const.EVPN):
            # if the network is a VXLAN network, then reuse same VNI
            # in bagpipe-bgp
            vni = net_info.segmentation_id
            if vni == NO_NEED_FOR_VNI:
                LOG.debug("no VNI reuse, because 'route_target' type driver "
                          "in use")
            else:
                LOG.debug("vni %s found for net %s, reusing for E-VPN",
                          vni, net_info.id)
                attach_info[bbgp_const.EVPN]['vni'] = vni

        if not (has_attachement(attach_info, bbgp_const.EVPN) or
                has_attachement(attach_info, bbgp_const.IPVPN)):
            LOG.warning("no attachment for port %s: %s", port_id, attach_info)
            return {}

        return attach_info

    @log_helpers.log_method_call
    def _build_bgpvpn_detach_info(self, port_info, detach_bgpvpn_type=None):
        detach_infos = {}
        for assoc in port_info.associations:
            if detach_bgpvpn_type and assoc.bgpvpn.type != detach_bgpvpn_type:
                LOG.debug("Skip assoc %s, because only detaching %s",
                          assoc.id, detach_bgpvpn_type)
                continue

            service_type = bgpvpn_const.BGPVPN_2_BAGPIPE[assoc.bgpvpn.type]
            detach_infos.update({
                service_type: self._base_attach_info(port_info)
            })

        return detach_infos
