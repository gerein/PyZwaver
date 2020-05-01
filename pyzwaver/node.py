#!/usr/bin/python3
# Copyright 2016 Robert Muth <robert@muth.org>
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; version 3
# of the License.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 59 Temple Place - Suite 330, Boston, MA  02111-1307, USA.

"""
node.py provides the Node and NodeSet abstraction
"""

import logging
import struct
import time
from typing import List, Set, Mapping
from enum import Enum

from pyzwaver import command_helper as ch
from pyzwaver import zwave as z
from pyzwaver import command
from pyzwaver.value import GetSensorMeta, GetMeterMeta, SENSOR_KIND_BATTERY, SENSOR_KIND_SWITCH_MULTILEVEL, \
    SENSOR_KIND_SWITCH_BINARY, TEMPERATURE_MODES

from pyzwaver.command import NodeCommand
from pyzwaver.driver import Driver
from pyzwaver.transaction import Transaction

SECURE_MODE = False

if SECURE_MODE:
    from pyzwaver import security

NODE_STATE_NONE        = "00_None"
NODE_STATE_INCLUDED    = "10_Included"   # discovered means we have the command classes
NODE_STATE_DISCOVERED  = "20_Discovered" # interviewed means we have received product info (including most static info an versions)
NODE_STATE_INTERVIEWED = "30_Interviewed"
NODE_STATE_KEX_GET     = "21_KexGet"
NODE_STATE_KEX_REPORT  = "22_KexReport"
NODE_STATE_KEX_SET     = "23_KexSet"
NODE_STATE_PUBLIC_KEY_REPORT_OTHER = "24_PublicKeyReportOther"
NODE_STATE_PUBLIC_KEY_REPORT_SELF  = "25_PublicKeyReportSelf"

_NO_VERSION = -1
_BAD_VERSION = 0


def _ExtractMeter(v):
    value = v["value"]
    key = (value["type"], value["unit"])
    return [(key, value)]


def _ExtractSensor(v):
    value = v["value"]
    key = (v["type"], value["unit"])
    return [(key, value)]


def _ExtractAssociationInfo(v):
    out = []
    for t in v["groups"]:
        out.append((t[0], t))
    return out


_COMMANDS_WITH_MAP_VALUES = {
    z.Version_CommandClassReport:             lambda v: [(v["class"], v["version"])],
    z.Meter_Report:                           _ExtractMeter,
    z.Configuration_Report:                   lambda v: [(v["parameter"], v["value"])],
    z.SensorMultilevel_Report:                _ExtractSensor,
    z.ThermostatSetpoint_Report:              lambda v: [(v["thermo"], v["value"])],
    z.Association_Report:                     lambda v: [(v["group"], v)],
    z.AssociationGroupInformation_NameReport: lambda v: [(v["group"], v["name"])],
    z.AssociationGroupInformation_InfoReport: _ExtractAssociationInfo,
    z.AssociationGroupInformation_ListReport: lambda v: [(v["group"], v["commands"])],
    z.SceneActuatorConf_Report:               lambda v: [(v["scene"], v)],
    z.UserCode_Report:                        lambda v: [(v["user"], v)],
    z.MultiChannel_CapabilityReport:          lambda v: [(v["endpoint"], v)],
}

_COMMANDS_WITH_SPECIAL_ACTIONS = {
    z.ManufacturerSpecific_Report: lambda node, _values: node.MaybeChangeState(NODE_STATE_INTERVIEWED),
    z.ZwavePlusInfo_Report:        lambda node, _values: node.MaybeChangeState(NODE_STATE_INTERVIEWED),
    z.SceneActuatorConf_Report:    lambda node,  values: node.values.Set(command.CUSTOM_COMMAND_ACTIVE_SCENE, values),
    z.Security2_KexReport:         lambda node, _values: node.MaybeChangeState(NODE_STATE_KEX_REPORT),
    z.Security2_PublicKeyReport:   lambda node, _values: node.MaybeChangeState(NODE_STATE_PUBLIC_KEY_REPORT_OTHER),
    z.Security2_NonceGet:          lambda node,  values: node.SendNonce(values["seq"]),
}

def BitsToSetWithOffset(x: int, offset: int) -> Set[int]:
    out = set()
    pos = 0
    while x:
        if (x & 1) == 1: out.add(pos + offset)
        pos += 1
        x >>= 1
    return out


class NodeValues:
    """
    NodeValues is a cache of all recently received commands sent to a Node.

    The commands are usually of kind "XXXReport".

    The command kinds fall in two categories:
    1. We only cache one recent message
       The corresponding  "XXXGet" command does not take an argument.
    2. We cache several recent messages
       The corresponding  "XXXGet" command takes an argument.
    """

    def __init__(self):
        self._values = {}
        self._maps = {}

    def HasValue(self, key: tuple):
        return key in self._values

    def Set(self, key: tuple, v: Mapping):
        if v is None:
            return
        self._values[key] = time.time(), v

    def SetMapEntry(self, key: tuple, subkey, v):
        if v is None:
            return
        m = self._maps.get(key)
        if m is None:
            m = {}
            self._maps[key] = m
        m[subkey] = time.time(), v

    def Get(self, key: tuple):
        v = self._values.get(key)
        if v is not None:
            return v[1]
        return None

    def GetMap(self, key: tuple) -> map:
        return self._maps.get(key, {})

    def ColorSwitchSupported(self):
        v = self.Get(z.ColorSwitch_SupportedReport)
        if not v:
            return set()
        # TODO - double check
        return BitsToSetWithOffset(v["bits"]["value"], 0)

    def SensorSupported(self):
        v = self.Get(z.SensorMultilevel_SupportedReport)
        if not v:
            return set()
        return BitsToSetWithOffset(v["bits"]["value"], 1)

    def MultiChannelEndPointIds(self):
        v = self.Get(z.MultiChannel_EndPointReport)
        if not v:
            return []
        return range(1, v["count"] + 1)

    def MeterSupported(self):
        v = self.Get(z.Meter_SupportedReport)
        if not v:
            return set()
        return BitsToSetWithOffset(v["scale"], 0)

    def MeterFlags(self):
        v = self.Get(z.Meter_SupportedReport)
        if not v:
            return None
        return v["type"]

    def GetMultilevelSwitchLevel(self):
        v = self.Get(z.SwitchMultilevel_Report)
        if not v:
            return 0
        return v["level"]

    def ProductInfo(self):
        v = self.Get(z.ManufacturerSpecific_Report)
        if not v:
            return 0, 0, 0
        return v.get("manufacturer", 0), v.get("type", 0), v.get("product", 0)

    def DeviceType(self):
        v = self.Get(command.CUSTOM_COMMAND_PROTOCOL_INFO)
        if not v:
            return 0, 0, 0
        return v["device_type"]

    def AssociationGroupIds(self):
        m = self.GetMap(z.Association_Report)
        if m:
            return m.keys()
        v = self.Get(z.Association_GroupingsReport)
        if not v or v["count"] in [0, 255]:
            n = 4
        else:
            n = v["count"]
        return list(range(1, n + 1)) + [255]

    def HasCommandClass(self, cls):
        m = self.GetMap(z.Version_CommandClassReport)
        e = m.get(cls)
        if not e:
            return False
        return e != 0

    def NumCommands(self):
        m = self.GetMap(z.Version_CommandClassReport)
        return len(m)

    def HasAlternaticeForBasicCommand(self):
        m = self.GetMap(z.Version_CommandClassReport)
        return z.SwitchBinary in m or z.SwitchMultilevel in m

    def Classes(self):
        m = self.GetMap(z.Version_CommandClassReport)
        return m.keys()

    def CommandVersions(self):
        m = self.GetMap(z.Version_CommandClassReport)
        return [(cls, command.StringifyCommandClass(cls), val)
                for cls, (_, val) in m.items() if val != 0]

    def Configuration(self):
        m = self.GetMap(z.Configuration_Report)
        return [(no, val["size"], val["value"])
                for no, (_, val) in m.items()]

    def SceneActuatorConfiguration(self):
        m = self.GetMap(z.SceneActuatorConf_Report)
        return [(no, val["level"], val["delay"])
                for no, (_, val) in m.items()]

    def Values(self):
        return [(key, command.StringifyCommand(key), val)
                for key, (_, val) in self._values.items()]

    def Sensors(self):
        m = self.GetMap(z.SensorMultilevel_Report)
        return [(key, *GetSensorMeta(*key), val["_value"])
                for key, (_, val) in m.items()]

    def Meters(self):
        m = self.GetMap(z.Meter_Report)
        return [(key, *GetMeterMeta(*key), val["_value"])
                for key, (_, val) in m.items()]

    def ThermostatMode(self):
        v = self.Get(z.ThermostatMode_Report)
        if v is not None:
            return (z.ThermostatMode_Report, v["thermo"], *TEMPERATURE_MODES[v["thermo"]])
        return None

    def ThermostatSetpoints(self):
        m = self.GetMap(z.ThermostatSetpoint_Report)
        return [(key, val['unit'], val["_value"])
                for key, (_, val) in m.items()]

    def MiscSensors(self):
        out = []
        v = self.Get(z.SwitchMultilevel_Report)
        if v is not None:
            out.append((z.SwitchMultilevel_Report, SENSOR_KIND_SWITCH_MULTILEVEL,
                        "% (dimmer)", v["level"]))
        v = self.Get(z.SwitchBinary_Report)
        if v is not None:
            out.append((z.SwitchBinary_Report, SENSOR_KIND_SWITCH_BINARY,
                        "on/off", v["level"]))
        v = self.Get(z.Battery_Report)
        if v is not None:
            out.append((z.Battery_Report, SENSOR_KIND_BATTERY,
                        "% (battery)", v["level"]))
        return out

    def Associations(self):
        groups = self.GetMap(z.Association_Report)
        names = self.GetMap(z.AssociationGroupInformation_NameReport)
        infos = self.GetMap(z.AssociationGroupInformation_InfoReport)
        lists = self.GetMap(z.AssociationGroupInformation_ListReport)
        assocs: Set[int] = set(groups.keys())
        assocs |= infos.keys()
        assocs |= lists.keys()
        assocs |= names.keys()

        def foo(m, k):
            e = m.get(k)
            if e is None:
                return None
            return e[1]

        out = []
        for n in assocs:
            out.append((n, foo(groups, n), foo(names, n),
                        foo(infos, n), foo(lists, n)))
        return out

    def Versions(self):
        v = self.Get(z.Version_Report)
        if not v:
            return 0, 0, 0, 0
        return v.get("library", 0), v.get("protocol", 0), v.get("firmware", 0), v.get("hardware", 0)

    def __str__(self):
        out = ["  values:"]
        for x in sorted(self.Values()):
            out.append("    " + str(x))
        out.append("  configuration:")
        for x in sorted(self.Configuration()):
            out.append("    " + str(x))
        out.append("  commands:")
        for x in sorted(self.CommandVersions()):
            out.append("    " + str(x))
        out.append("  associations:")
        for x in sorted(self.Associations()):
            out.append("    " + str(x))
        if self.MeterSupported():
            out.append("  meters:")
            for x in sorted(self.Meters()):
                out.append("    " + str(x))
        if self.SensorSupported():
            out.append("  sensors:")
            for x in sorted(self.Sensors()):
                out.append("    " + str(x))
        return "\n".join(out)


class NetworkElement:
    def __init__(self, name=None):
        self.name = name
        self.listeners = []

    def addListener(self, listener):
        self.listeners.append(listener)

    def getName(self):
        return self.name if self.name is not None else ""

    def SendCommand(self, nodeCommand:NodeCommand, highPriority=True, txOptions:tuple=None):
        pass

    def put(self, nodeCommand):
        for listener in self.listeners:
            listener.put(nodeCommand)


class Node(NetworkElement):
    """A Node represents a single node in a network.

    Incoming commands are passed to it from Nodeset via put()
    Outgoing commands are send to the CommandTranslator.
    """

    def __init__(self, n, driver:Driver, name=None, is_controller=False):
        super().__init__(name)
        self.n = n
        self.driver = driver
        self.is_controller = is_controller
        self.state = NODE_STATE_NONE
        self._controls = set()

        self.values: NodeValues = NodeValues()
        self.is_controller = is_controller
        self.last_contact = 0
        self.secure_pair = SECURE_MODE
        self._tmp_key_ccm = None
        self._tmp_personalization_string = None

    def getName(self):
        if self.name is None: return str(self.n)
        return self.name + " (" + str(self.n) + ")"

    def IsInterviewed(self):
        return self.state == NODE_STATE_INTERVIEWED

    def IsFailed(self):
        values = self.values.Get(command.CUSTOM_COMMAND_FAILED_NODE)
        return values and values["failed"]

    def __lt__(self, other):
        return self.n < other.n

    def InitializeUnversioned(self, cmd: List[int], controls: List[int], std_cmd: List[int], std_controls: List[int]):
        self._controls |= set(controls)
        self._controls |= set(std_controls)

        ts = 0.0
        for k in cmd:
            if not self.values.HasCommandClass(k):
                self.values.SetMapEntry(z.Version_CommandClassReport, k, _NO_VERSION)
        for k in self._controls:
            if not self.values.HasCommandClass(k):
                self.values.SetMapEntry(z.Version_CommandClassReport, k, _NO_VERSION)
        for k in std_cmd:
            if not self.values.HasCommandClass(k):
                self.values.SetMapEntry(z.Version_CommandClassReport, k, _NO_VERSION)

    def BasicString(self):
        return "NODE: %s " % self.getName() +  \
               "state: %s " % self.state[3:] if not self.IsFailed() else "FAILED" + \
               "version: %d:%d:%d:%d " % self.values.Versions() + \
               "product: %04x:%04x:%04x " % self.values.ProductInfo() + \
               "groups: %d" % len(self.values.AssociationGroupIds())

    def __str__(self):
        return self.BasicString() + "\n" + str(self.values)

    def BatchCommandSubmitFiltered(self, commands: List[tuple], highPriority=True):
        for key, values in commands:
            if not self.values.HasCommandClass(key[0]):
                continue

            # if self._IsSecureCommand(cmd[0], cmd[1]):
            #    self._secure_messaging.Send(cmd)
            #    continue

            self.SendCommand(NodeCommand(key, values), highPriority=highPriority)


    TXoptions = Enum('TXoptions', {'ACK': z.TRANSMIT_OPTION_ACK, 'AUTO_ROUTE': z.TRANSMIT_OPTION_AUTO_ROUTE, 'EXPLORE': z.TRANSMIT_OPTION_EXPLORE, 'LOW_POWER': z.TRANSMIT_OPTION_EXPLORE, 'NO_ROUTE': z.TRANSMIT_OPTION_NO_ROUTE})

    def SendCommand(self, nodeCommand:NodeCommand, highPriority=True, txOptions:tuple=None):
        nodeCommandData = nodeCommand.toDeviceData()
        if nodeCommandData is None:
            commandIndex = (nodeCommand.command[0] << 8) + nodeCommand.command[1]
            if commandIndex in z.SUBCMD_TO_STRING:
                logging.error("XXX: Trying to send command with incomplete values - ignored: %s, %s",
                              z.SUBCMD_TO_STRING[commandIndex], nodeCommand.commandValues)
            else:
                logging.error("Trying to send unknown command - ignored: %s", nodeCommand.command)
            return

        if txOptions is None: txOptions = (Node.TXoptions.ACK, Node.TXoptions.AUTO_ROUTE, Node.TXoptions.EXPLORE)
        txData = 0
        for tx in txOptions: txData |= tx.value

        serialCommandData = [self.n, len(nodeCommandData)] + nodeCommandData + [txData]
        priority = (Driver.RequestPriority.HIGH_FAIR, self.n) if highPriority else (Driver.RequestPriority.LOW_FAIR, self.n)

        self.driver.sendRequest(z.API_ZW_SEND_DATA, serialCommandData, priority, timeout=5.0)


    def GetNodeProtocolInfo(self):
        def handler(callbackReason, data):
            if callbackReason == Transaction.CallbackReason.TIMED_OUT:
                logging.error("==X: GetNodeProtocolInfo (node: %s) timed-out", self.getName())
                return
            if len(data) < 5:
                logging.error("==X: GetNodeProtocolInfo (node: %s): bad payload: %s", self.getName(), data)
                return

            _BAUD = [
                "unknown_baud",
                "9600_baud",
                "40000_baud",
                "100000_baud",
                "unknown_baud",
                "unknown_baud",
                "unknown_baud",
                "unknown_baud",
            ]

            a, b, _, basic, generic, specific = struct.unpack(">BBBBBB", bytes(data))
            flags = set()
            if a & 0x80: flags.add("listening")
            if a & 0x40: flags.add("routing")

            flags.add(_BAUD[(a & 0x38) >> 3])

            if b & 0x01: flags.add("security")
            if b & 0x02: flags.add("controller")
            if b & 0x04: flags.add("specific_device")
            if b & 0x08: flags.add("routing_slave")
            if b & 0x10: flags.add("beam_capable")
            if b & 0x20: flags.add("sensor_250ms")
            if b & 0x40: flags.add("sensor_1000ms")
            if b & 0x80: flags.add("optional_functionality")
            out = {
                "protocol_version": 1 + (a & 0x7),
                "flags": flags,
                "device_type": (basic, generic, specific),
            }
            self.values.Set(command.CUSTOM_COMMAND_PROTOCOL_INFO, out)

        logging.info("===: GetNodeProtocolInfo (node: %s)", self.getName())
        self.driver.sendRequest(z.API_ZW_GET_NODE_PROTOCOL_INFO, [self.n], Driver.RequestPriority.HIGHEST, callback=handler)


    def _RequestNodeInfo(self, retries):
        """This usually triggers send "API_ZW_APPLICATION_UPDATE:"""

        def handler(callbackReason, data):
            if callbackReason == Transaction.CallbackReason.TIMED_OUT or data[0] == 0:
                logging.warning("==X: RequestNodeInfo (node: %s) failed: %s", self.getName(), data)
                self._RequestNodeInfo(retries - 1)

        if retries == 0:
            logging.error("==X: RequestNodeInfo (node: %s) failed permanently", self.getName())
            return

        logging.warning("===: RequestNodeInfo (node: %s, retries left: %d)", self.getName(), retries)
        self.driver.sendRequest(z.API_ZW_REQUEST_NODE_INFO, [self.n], Driver.RequestPriority.HIGHEST, callback=handler)


    def Ping(self, retries, reason):
        logging.info("===: Ping (node: %s): reason %s, retries %d", self.getName(), reason, retries)
        self.GetNodeProtocolInfo()

        def handler(callbackReason, data):
            if callbackReason == Transaction.CallbackReason.TIMED_OUT: return
            failed = data[0] != 0
            logging.info("===: Pong (node: %s) is-failed: %s, %s", self.getName(), failed, data)
            self.values.Set(command.CUSTOM_COMMAND_FAILED_NODE, {"failed": failed})
            if not failed: self._RequestNodeInfo(retries)

        self.driver.sendRequest(z.API_ZW_IS_FAILED_NODE_ID, [self.n], Driver.RequestPriority.HIGHEST, callback=handler)


    def ProbeNode(self):
        self.BatchCommandSubmitFiltered([(z.NoOperation_Set, {})])

    #        cmd = zwave_cmd.MakeWakeUpIntervalCapabilitiesGet(
    #            self.n, xmit, driver.GetCallbackId())
    #        driver.Send(cmd, handler, "WakeUpIntervalCapabilitiesGet")

    def RefreshAllCommandVersions(self):
        self.BatchCommandSubmitFiltered(ch.CommandVersionQueries(range(255)), highPriority=False)

    def RefreshAllSceneActuatorConfigurations(self):
        # append 0 to set current scene at very end
        self.BatchCommandSubmitFiltered(ch.SceneActuatorConfiguration(list(range(1, 256)) + [0]), highPriority=False)

    def RefreshAllParameters(self):
        logging.warning("[%d] RefreshAllParameter", self.n)
        self.BatchCommandSubmitFiltered(ch.ParameterQueries(range(255)), highPriority=False)

    def RefreshDynamicValues(self):
        logging.warning("[%d] RefreshDynamic", self.n)
        c = (ch.DYNAMIC_PROPERTY_QUERIES + ch.SensorMultiLevelQueries(self.values.SensorSupported()) +
             ch.MeterQueries(self.values.MeterSupported()) + ch.ColorQueries(self.values.ColorSwitchSupported()))
        self.BatchCommandSubmitFiltered(c, highPriority=False)

    def RefreshStaticValues(self):
        logging.warning("[%d] RefreshStatic", self.n)
        c = (ch.STATIC_PROPERTY_QUERIES + ch.CommandVersionQueries(self.values.Classes()) + ch.STATIC_PROPERTY_QUERIES_LAST)
        self.BatchCommandSubmitFiltered(c, highPriority=False)

    def RefreshSemiStaticValues(self):
        logging.warning("[%d] RefreshSemiStatic", self.n)
        c = (ch.AssociationQueries(self.values.AssociationGroupIds()) + ch.MultiChannelEndpointQueries(self.values.MultiChannelEndPointIds()))
        self.BatchCommandSubmitFiltered(c, highPriority=False)

    def SmartRefresh(self):
        if   self.state == NODE_STATE_NONE: return
        elif self.state == NODE_STATE_DISCOVERED: self.RefreshStaticValues()
        elif self.state == NODE_STATE_INTERVIEWED:
            self.RefreshSemiStaticValues()
            self.RefreshDynamicValues()

    def SendNonce(self, seq):
        # TODO: using a fixed nonce is a total hack - fix this
        args = {"seq": seq, "mode": 1, "nonce": [0] * 16}
        logging.warning("Sending Nonce: %s", str(args))
        self.BatchCommandSubmitFiltered([(z.Security2_NonceReport, args)])

    def MaybeChangeState(self, new_state: str):
        old_state = self.state
        if old_state >= new_state: return

        logging.warning("[%s] state transition %s -- %s", self.getName(), old_state, new_state)
        self.state = new_state

        if new_state == NODE_STATE_DISCOVERED:
            # if self.values.HasCommandClass(z.MultiChannel):
            #    self.BatchCommandSubmitFilteredFast(
            #            [(z.MultiChannel_Get, {})])
            if old_state < NODE_STATE_DISCOVERED:
                if self.secure_pair and (self.values.HasCommandClass(z.Security) or self.values.HasCommandClass(z.Security2)):
                    self.state = NODE_STATE_KEX_GET
                    logging.error("[%d] Sending KEX_GET", self.n)
                    self.BatchCommandSubmitFiltered([(z.Security2_KexGet, {})])
                else:
                    self.RefreshStaticValues()
        elif new_state == NODE_STATE_KEX_REPORT:
            v = self.values.Get(z.Security2_KexReport)
            # we currently only support S2 Unauthenticated Class
            assert v["keys"] & 1 == 1
            logging.error("[%d] Sending KEX_SET", self.n)
            args = {'mode': 0, 'schemes': 2, 'profiles': 1, 'keys': v["keys"] & 1}
            self.BatchCommandSubmitFiltered([(z.Security2_KexSet, args)])
            self.state = NODE_STATE_KEX_SET
        elif new_state == NODE_STATE_PUBLIC_KEY_REPORT_OTHER:
            v = self.values.Get(z.Security2_PublicKeyReport)
            other_public_key = bytes(v["key"])
            self._tmp_key_ccm, self._tmp_personalization_string, this_public_key = security.CKFD_SharedKey(other_public_key)

            print("@@@@@@", len(self._tmp_key_ccm), self._tmp_key_ccm, len(self._tmp_personalization_string), self._tmp_personalization_string)
            args = {"mode": 1, "key": [int(x) for x in this_public_key]}
            self.BatchCommandSubmitFiltered([(z.Security2_PublicKeyReport, args)])
            self.state = NODE_STATE_PUBLIC_KEY_REPORT_SELF

        elif new_state == NODE_STATE_INTERVIEWED:
            self.RefreshDynamicValues()
            self.RefreshSemiStaticValues()


    def handleApplicationUpdate(self, values):
        # Must be z.UPDATE_STATE_NODE_INFO_RECEIVED: the node is awake now and/or has changed values

        # maybe update generic+specific device
        if self.values.Get(command.CUSTOM_COMMAND_PROTOCOL_INFO) is None:
            self.values.Set(command.CUSTOM_COMMAND_PROTOCOL_INFO, {"device_type": (0, values["generic"], values["specific"])})

        v = z.GENERIC_SPECIFIC_DB.get((values["generic"] << 8) + values["specific"])
        if v is None:
            logging.error("[%d] unknown generic device : %s", self.n, repr(values))
            return

        self.InitializeUnversioned(values["commands"], values["controls"], v[1], v[2])
        self.MaybeChangeState(NODE_STATE_DISCOVERED)
        if self.state >= NODE_STATE_INTERVIEWED:
            self.RefreshDynamicValues()
            self.RefreshSemiStaticValues()


    def put(self, nodeCommand:NodeCommand):
        """A Node receives new commands via this function"""
        self.last_contact = time.time()

        if self.state < NODE_STATE_DISCOVERED: self.Ping(3, "undiscovered")

        items_extractor = _COMMANDS_WITH_MAP_VALUES.get(nodeCommand.command)
        if items_extractor:
            for k, v in items_extractor(nodeCommand.commandValues):
                self.values.SetMapEntry(nodeCommand.command, k, v)
        else:
            self.values.Set(nodeCommand.command, nodeCommand.commandValues)

        special = _COMMANDS_WITH_SPECIAL_ACTIONS.get(nodeCommand.command)
        if special:
            special(self, nodeCommand.commandValues)

        super().put(nodeCommand)


        # elif a == command.ACTION_STORE_SCENE:
        #    if value[0] == 0:
        #        # TODO
        #        #self._values[command.VALUE_ACTIVE_SCENE] = -1
        #        pass
        #    else:
        #        self.scenes[value[0]] = value[1:]
        # elif a == command.SECURITY_SCHEME:
        #    assert len(value) == 1
        #    if value[0] == 0:
        #        # not paired yet start key exchange
        #        self.SecurityChangeKey(self._shared,security_key)
        #    else:
        #        # already paired
        #        self.SecurityRequestClasses()


class Endpoint(NetworkElement):
    def __init__(self, node:Node, endpoint, name=None):
        super().__init__(name)
        self.node = node
        self.endpoint = endpoint


    def getName(self):
        id = "%d.%d" % (self.node.n, self.endpoint)
        if self.name is None and self.node.name is None: return id
        return self.name if self.name is not Node else self.node.name + " (" + id + ")"


    def SendCommand(self, nodeCommand:NodeCommand, highPriority=True, txOptions:tuple=None):
        nodeCommand.endPoint = self.endpoint
        self.node.SendCommand(nodeCommand, highPriority, txOptions)


    def Ping(self, retries, reason):
        logging.info("===: Ping (node: %s): reason %s, retries %d", self.getName(), reason, retries)

        self.SendCommand(NodeCommand(z.MultiChannel_CapabilityGet, {"endpoint": self.endpoint})) #FIXME: does a ping on endpoint make sense?


    def put(self, nodeCommand):
        super().put(nodeCommand)


class Nodeset:
    """NodeSet represents the collection of all nodes in the network.

    It handles incoming commands from the CommandTranslators and dispatches
    them to the corresponding node - creating new nodes as necessary.

    It is not involved in outgoing messages which have to be sent directly to the
    CommandTranslator.
    """

    def __init__(self, driver:Driver, controller_n):
        self._controller_n = controller_n
        self.driver = driver
        self.nodes = {}
        self.endpoints = {}
        self.driver.addListener(self)

    def getNode(self, n: int) -> Node:
        node = self.nodes.get(n)
        if node is None:
            node = Node(n, self.driver, is_controller=n==self._controller_n)
            self.nodes[n] = node
        return node

    def getEndpoint(self, nodeId:int, endpointId:int) -> Endpoint:
        index = (nodeId << 8) + endpointId
        endpoint = self.endpoints.get(index)
        if endpoint is None:
            endpoint = Endpoint(self.getNode(nodeId), endpointId)
            self.endpoints[index] = endpoint
        return endpoint

    def getNetworkElement(self, id) -> NetworkElement:
        if type(id) == tuple: return self.getEndpoint(id[0], id[1])
        return self.getNode(id)


    def put(self, serialCommand, serialCommandParameters):
        if serialCommand == z.API_APPLICATION_COMMAND_HANDLER:
            srcNode, nodeCommand = serialCommandParameters
            if nodeCommand.command == z.MultiChannel_CmdEncap:
                self.getEndpoint(srcNode, nodeCommand.commandValues['src']).put(nodeCommand.endpointCommand)
                return

            if nodeCommand.command == z.MultiChannel_CapabilityReport:
                # FIXME - can we do this more elegantly?
                nodeCommand.commandValues["commands"] = nodeCommand.commandValues["classes"]
                nodeCommand.commandValues["controls"] = []

                self.getEndpoint(srcNode, nodeCommand.commandValues['endpoint']).put(nodeCommand)
                return

            self.getNode(srcNode).put(nodeCommand)

        elif serialCommand == z.API_ZW_APPLICATION_UPDATE:
            if serialCommandParameters[0] == z.UPDATE_STATE_NODE_INFO_REQ_FAILED:
                if serialCommandParameters[1] != 0:
                    logging.error("XXX: Application update request failed")

            elif serialCommandParameters[0] == z.UPDATE_STATE_SUC_ID:
                logging.warning("===: Application updated: succ id updated: needs work")

            elif serialCommandParameters[0] == z.UPDATE_STATE_NODE_INFO_RECEIVED:
                # the node is awake now and/or has changed values
                srcNode = serialCommandParameters[1]
                length = serialCommandParameters[2]
                data = serialCommandParameters[3: 3 + length]
                commands = []
                controls = []
                seen_marker = False
                for i in data[3:]:
                    if i == z.Mark: seen_marker = True
                    elif seen_marker: controls.append(i)
                    else: commands.append(i)

                values = {
                    "basic": data[0],
                    "generic": data[1],
                    "specific": data[2],
                    "commands": commands,
                    "controls": controls,
                }
                self.getNode(srcNode).handleApplicationUpdate(values)  # FIXME: do we need to reintroduce timestamp

            else:
                logging.error("XXX: Application update: unknown type (%x) - ignore", serialCommandParameters[0])

        else:
            logging.error("XXX: unhandled message - ignore: %s %s", command, serialCommandParameters)
