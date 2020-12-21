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
import time
from typing import List, Set, Mapping
from enum import Enum

from . import commandQuerySets as cqs
from . import zwave as z
from . import command
from .value import GetSensorMeta, GetMeterMeta, SENSOR_KIND_BATTERY, SENSOR_KIND_SWITCH_MULTILEVEL, SENSOR_KIND_SWITCH_BINARY, TEMPERATURE_MODES
from .command import SerialRequest, NodeCommand
from .driver import Driver
from .transactionProcessor import TransactionProcessor

SECURE_MODE = False
if SECURE_MODE:
    from pyzwaver import security


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

    def Get(self, key: tuple):
        if key not in self._values: return None
        return self._values[key][1]

    def Set(self, key: tuple, v: Mapping):
        if v is None: return
        self._values[key] = time.time(), v

    def GetMap(self, key: tuple) -> map:
        return self._maps.get(key, {})

    def SetMapEntry(self, key: tuple, subkey, v):
        if v is None: return
        if key not in self._maps: self._maps[key] = {}
        self._maps[key][subkey] = time.time(), v

    @staticmethod
    def BitsToSet(x: int) -> Set:
        out = set()
        pos = 0
        while x:
            if (x & 1) == 1: out.add(pos)
            pos += 1
            x >>= 1
        return out

    def ColorSwitchSupported(self):  # TODO - double check
        if z.ColorSwitch_SupportedReport not in self._values: return set()
        return NodeValues.BitsToSet(self._values[z.ColorSwitch_SupportedReport][1]["bits"]["value"])

    def SensorSupported(self):
        if z.SensorMultilevel_SupportedReport not in self._values: return set()
        return NodeValues.BitsToSet(self._values[z.SensorMultilevel_SupportedReport][1]["bits"]["value"] << 1) # one offset

    def MultiChannelEndPointIds(self):
        if z.MultiChannel_EndPointReport not in self._values: return []
        return range(1, self._values[z.MultiChannel_EndPointReport][1]["count"] + 1)

    def MeterSupported(self):
        if z.Meter_SupportedReport not in self._values: return set()
        return NodeValues.BitsToSet(self._values[z.Meter_SupportedReport][1]["scale"])

    def MeterFlags(self):
        if z.Meter_SupportedReport not in self._values: return None
        return self._values[z.Meter_SupportedReport][1]["type"]

    def GetMultilevelSwitchLevel(self):
        if z.SwitchMultilevel_Report not in self._values: return 0
        return self._values[z.SwitchMultilevel_Report][1]["level"]

    def ProductInfo(self):
        v = self.Get(z.ManufacturerSpecific_Report)
        if not v: return 0, 0, 0
        return v.get("manufacturer", 0), v.get("type", 0), v.get("product", 0)

    def DeviceType(self):
        if command.CUSTOM_COMMAND_PROTOCOL_INFO not in self._values: return 0, 0, 0
        return self._values[command.CUSTOM_COMMAND_PROTOCOL_INFO]["device_type"]

    def AssociationGroupIds(self):
        if z.Association_Report in self._maps: return self._maps[z.Association_Report].keys()

        v = self.Get(z.Association_GroupingsReport)
        n = 4 if not v or v["count"] in [0, 255] else v["count"]
        return list(range(1, n + 1)) + [255]

    def HasCommandClass(self, cls):
        e = self.GetMap(z.Version_CommandClassReport).get(cls)
        return e != 0 if e else False

    def HasAlternaticeForBasicCommand(self):
        m = self.GetMap(z.Version_CommandClassReport)
        return z.SwitchBinary in m or z.SwitchMultilevel in m

    def NumCommands(self):                return len(self.GetMap(z.Version_CommandClassReport))
    def Classes(self):                    return self.GetMap(z.Version_CommandClassReport).keys()
    def CommandVersions(self):            return [(cls, z.CMD_TO_STRING.get(cls, "UNKNOWN:%d" % cls), val) for cls, (_, val) in self.GetMap(z.Version_CommandClassReport).items() if val != 0]
    def Configuration(self):              return [(no, val["size"], val["value"]) for no, (_, val) in self.GetMap(z.Configuration_Report).items()]
    def SceneActuatorConfiguration(self): return [(no, val["level"], val["delay"]) for no, (_, val) in self.GetMap(z.SceneActuatorConf_Report).items()]
    def Values(self):                     return [(key, command.StringifyCommand(key), val)  for key, (_, val) in self._values.items()]
    def Sensors(self):                    return [(key, *GetSensorMeta(*key), val["_value"]) for key, (_, val) in self.GetMap(z.SensorMultilevel_Report).items()]
    def Meters(self):                     return [(key, *GetMeterMeta(*key) , val["_value"]) for key, (_, val) in self.GetMap(z.Meter_Report).items()]
    def ThermostatSetpoints(self):        return [(key, val['unit'], val["_value"]) for key, (_, val) in self.GetMap(z.ThermostatSetpoint_Report).items()]

    def ThermostatMode(self):
        v = self.Get(z.ThermostatMode_Report)
        return (z.ThermostatMode_Report, v["thermo"], *TEMPERATURE_MODES[v["thermo"]]) if v is not None else None

    def MiscSensors(self):
        out = []
        v = self.Get(z.SwitchMultilevel_Report)
        if v is not None: out.append((z.SwitchMultilevel_Report, SENSOR_KIND_SWITCH_MULTILEVEL, "% (dimmer)", v["level"]))
        v = self.Get(z.SwitchBinary_Report)
        if v is not None: out.append((z.SwitchBinary_Report, SENSOR_KIND_SWITCH_BINARY, "on/off", v["level"]))
        v = self.Get(z.Battery_Report)
        if v is not None: out.append((z.Battery_Report, SENSOR_KIND_BATTERY, "% (battery)", v["level"]))
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
            return e[1] if e is not None else None

        out = []
        for n in assocs:
            out.append((n, foo(groups, n), foo(names, n), foo(infos, n), foo(lists, n)))
        return out

    def Versions(self):
        v = self.Get(z.Version_Report)
        return (v.get("library", 0), v.get("protocol", 0), v.get("firmware", 0), v.get("hardware", 0)) if v else (0, 0, 0, 0)

    def __str__(self):
        return "Values:        " + " ".join([str(x) for x in sorted(self.Values()         )]) + "\n" + \
               "Configuration: " + " ".join([str(x) for x in sorted(self.Configuration()  )]) + "\n" + \
               "Commands:      " + " ".join([str(x) for x in sorted(self.CommandVersions())]) + "\n" + \
               "Associations:  " + " ".join([str(x) for x in sorted(self.Associations()   )]) + "\n" + \
              ("Meters:        " + " ".join([str(x) for x in sorted(self.Meters()         )]) + "\n" if self.MeterSupported()  else "") + \
              ("Sensors:       " + " ".join([str(x) for x in sorted(self.Sensors()        )])        if self.SensorSupported() else "")


class NetworkElement:
    def __init__(self, name=None):
        self.name = name
        self.listeners = []
        self.values: NodeValues = NodeValues()

    def addListener(self, listener):
        self.listeners.append(listener)

    def getName(self):
        return self.name if self.name is not None else ""

    def setName(self, name):
        self.name = name

    def sendCommand(self, nodeCommand:NodeCommand, highPriority=True, txOptions:tuple=None):
        pass

    def put(self, nodeCommand):
        for listener in self.listeners:
            listener.put(nodeCommand)


class Node(NetworkElement):
    """A Node represents a single node in a network.

    Incoming commands are passed to it from Nodeset via put()
    Outgoing commands are send to the CommandTranslator.
    """

    NodeStatus = Enum('NodeStatus', "NONE DISCOVERED INTERVIEWED KEX_GET KEX_REPORT KEX_SET PUBLIC_KEY_REPORT_OTHER PUBLIC_KEY_REPORT_SELF")
    # discovered means we have the command classes, interviewed means we have the product info (including most static info and versions)

    def __init__(self, nodeId, driver:Driver, name=None):
        super().__init__(name)

        self.nodeId = nodeId
        self.driver = driver

        self._controls = set()

        self.status = Node.NodeStatus.NONE
        self.last_contact = 0

        self.secure_pair = SECURE_MODE
        self._tmp_key_ccm = None
        self._tmp_personalization_string = None

    def getName(self):
        if self.name is None: return str(self.nodeId)
        return self.name + " (" + str(self.nodeId) + ")"

    def IsFailed(self):
        values = self.values.Get(command.CUSTOM_COMMAND_FAILED_NODE)
        return values and values["failed"]

    def InitializeUnversioned(self, cmd: List[int], controls: List[int], std_cmd: List[int], std_controls: List[int]):
        self._controls |= set(controls) | set(std_controls)
        for k in cmd + list(self._controls) + std_cmd:
            if not self.values.HasCommandClass(k):
                self.values.SetMapEntry(z.Version_CommandClassReport, k, -1)

    def __str__(self):
        return "NODE: %s "                % self.getName()                                         + \
              ("state: %s "               % self.status.name if not self.IsFailed() else "FAILED") + \
               "version: %d:%d:%d:%d "    % self.values.Versions()                                 + \
               "product: %04x:%04x:%04x " % self.values.ProductInfo()                              + \
               "groups: %d"               % len(self.values.AssociationGroupIds()) + "\n"          + \
               str(self.values)

    def sendFilteredCommandBatch(self, commands: List[tuple], highPriority=True):
        for key, values in commands:
            if not self.values.HasCommandClass(key[0]): continue

            # if self._IsSecureCommand(cmd[0], cmd[1]):
            #    self._secure_messaging.Send(cmd)
            #    continue

            self.sendCommand(NodeCommand(key, values), highPriority=highPriority)

    TXoptions = Enum('TXoptions', {'ACK'     : z.TRANSMIT_OPTION_ACK,     'AUTO_ROUTE': z.TRANSMIT_OPTION_AUTO_ROUTE,
                                   'EXPLORE' : z.TRANSMIT_OPTION_EXPLORE, 'LOW_POWER' : z.TRANSMIT_OPTION_EXPLORE,
                                   'NO_ROUTE': z.TRANSMIT_OPTION_NO_ROUTE})

    def sendCommand(self, nodeCommand:NodeCommand, highPriority=True, txOptions:tuple=None):
        if txOptions is None: txOptions = (Node.TXoptions.ACK, Node.TXoptions.AUTO_ROUTE, Node.TXoptions.EXPLORE)
        txData = 0
        for tx in txOptions: txData |= tx.value

        serialRequest = SerialRequest(z.API_ZW_SEND_DATA, {"node": self.nodeId, "txOptions": txData, "command": nodeCommand})
        priority = (Driver.RequestPriority.HIGH_FAIR, self.nodeId) if highPriority else (Driver.RequestPriority.LOW_FAIR, self.nodeId)

        self.driver.sendRequest(serialRequest, priority, timeout=10.0)


    def GetNodeProtocolInfo(self):
        def handler(callbackReason, serialCommandValues):
            if callbackReason == TransactionProcessor.CallbackReason.TIMED_OUT:
                logging.error("==X: GetNodeProtocolInfo (node: %s) timed-out", self.getName())
                return

            flags = set()

            bitmap = ["unknown_baud", "9600_baud", "40000_baud", "100000_baud", "unknown_baud", "unknown_baud", "unknown_baud", "unknown_baud",]
            flags.add(bitmap[(serialCommandValues["capability"] & 0b00111000) >> 3])

            bitmap = ["listening", "routing"]
            for i in range(2, 4):
                if serialCommandValues["capability"] & (1 << i): flags.add(bitmap[i])

            bitmap = ["security", "controller", "specific_device", "routing_slave", "beam_capable", "sensor_250ms", "sensor_1000ms", "optional_functionality"]
            for i in range(0, 8):
                if serialCommandValues["security"] & (1 << i): flags.add(bitmap[i])

            out = {"protocol_version": 1 + (serialCommandValues["capability"] & 0b00000111),
                   "flags": flags,
                   "device_type": (serialCommandValues["basic"], serialCommandValues["generic"], serialCommandValues["specific"])}
            self.values.Set(command.CUSTOM_COMMAND_PROTOCOL_INFO, out)

        logging.info("===: GetNodeProtocolInfo (node: %s)", self.getName())
        self.driver.sendRequest(SerialRequest(z.API_ZW_GET_NODE_PROTOCOL_INFO, {"node": self.nodeId}),
                                requestPriority=Driver.RequestPriority.HIGHEST, callback=handler)

    def RequestNodeInfo(self, triesLeft=3):
        """Force the generation of a API_ZW_APPLICATION_UPDATE event"""

        def nodeInfoHandler(serialCommandValues):
            if serialCommandValues["retval"] == 0:
                if triesLeft > 1:
                    logging.warning("==X: RequestNodeInfo (node: %s) failed - trying again", self.getName())
                    self.RequestNodeInfo(triesLeft - 1)
                else:
                    logging.error("==X: RequestNodeInfo (node: %s) failed permanently", self.getName())

        logging.info("===: RequestNodeInfo (node: %s, tries left: %d)", self.getName(), triesLeft)
        self.driver.sendRequest(SerialRequest(z.API_ZW_REQUEST_NODE_INFO, {"node": self.nodeId}),
                                requestPriority=Driver.RequestPriority.HIGHEST, callback=Driver.ignoreTimeoutHandler(nodeInfoHandler))

    def Ping(self):
        logging.info("===: Ping (node: %s)", self.getName())

        def failedNodeHandler(serialCommandValues):
            failed = serialCommandValues["retval"] != 0
            logging.info("===: Pong (node: %s) is-failed: %s, %s", self.getName(), failed, serialCommandValues["retval"])
            self.values.Set(command.CUSTOM_COMMAND_FAILED_NODE, {"failed": failed})
            if not failed: self.RequestNodeInfo()

        self.GetNodeProtocolInfo()
        logging.info("===: IsFailedNode (node: %s)", self.getName())
        self.driver.sendRequest(SerialRequest(z.API_ZW_IS_FAILED_NODE_ID, {"node": self.nodeId}),
                                requestPriority=Driver.RequestPriority.HIGHEST,
                                callback=Driver.ignoreTimeoutHandler(failedNodeHandler))

    def RefreshAllCommandVersions(self):
        self.sendFilteredCommandBatch(cqs.CommandVersionQueries(range(255)), highPriority=False)

    def RefreshAllSceneActuatorConfigurations(self):
        # append 0 to set current scene at very end
        self.sendFilteredCommandBatch(cqs.SceneActuatorConfiguration(list(range(1, 256)) + [0]), highPriority=False)

    def RefreshAllParameters(self):
        logging.info("[%s] RefreshAllParameter", self.getName())
        self.sendFilteredCommandBatch(cqs.ParameterQueries(range(255)), highPriority=False)

    def RefreshStaticValues(self):
        logging.info("[%s] RefreshStatic", self.getName())
        self.sendFilteredCommandBatch(cqs.getStaticCommands(self.values), highPriority=False)

    def RefreshDynamicValues(self):
        logging.info("[%s] RefreshDynamic", self.getName())
        self.sendFilteredCommandBatch(cqs.getDynamicCommands(self.values), highPriority=False)

    def ChangeStatus(self, new_state):
        if self.status.value >= new_state.value: return

        self.status = new_state
        logging.info("Node %s: Setting status to %s", self.getName(), self.status.name)
        logging.info(str(self))

        if self.status == Node.NodeStatus.DISCOVERED:
            # if self.values.HasCommandClass(z.MultiChannel):
            #    self.BatchCommandSubmitFilteredFast([(z.MultiChannel_Get, {})])
            #FIXME: do we need to send multichannel_get (maybe as part of refreshstatic? command classes are filtered anyway)
            if self.secure_pair and (self.values.HasCommandClass(z.Security) or self.values.HasCommandClass(z.Security2)):
                self.status = Node.NodeStatus.KEX_GET
                logging.error("[%d] Sending KEX_GET", self.nodeId)
                self.sendFilteredCommandBatch([(z.Security2_KexGet, {})])
                return

            self.RefreshStaticValues()

        elif self.status == Node.NodeStatus.INTERVIEWED:
            self.RefreshDynamicValues()

        elif self.status == Node.NodeStatus.KEX_REPORT:
            v = self.values.Get(z.Security2_KexReport)
            # we currently only support S2 Unauthenticated Class
            assert v["keys"] & 1 == 1
            logging.error("[%d] Sending KEX_SET", self.nodeId)
            self.sendFilteredCommandBatch([(z.Security2_KexSet, {'mode': 0, 'schemes': 2, 'profiles': 1, 'keys': v["keys"] & 1})])
            self.status = Node.NodeStatus.KEX_SET

        elif self.status == Node.NodeStatus.PUBLIC_KEY_REPORT_OTHER:
            v = self.values.Get(z.Security2_PublicKeyReport)
            self._tmp_key_ccm, self._tmp_personalization_string, this_public_key = security.CKFD_SharedKey(bytes(v["key"]))

            #print("@@@@@@", len(self._tmp_key_ccm), self._tmp_key_ccm, len(self._tmp_personalization_string), self._tmp_personalization_string)
            self.sendFilteredCommandBatch([(z.Security2_PublicKeyReport, {"mode": 1, "key": [int(x) for x in this_public_key]})])
            self.status = Node.NodeStatus.PUBLIC_KEY_REPORT_SELF


    def handleApplicationUpdate(self, values):
        # Must be z.UPDATE_STATE_NODE_INFO_RECEIVED: the node is awake now and/or has changed values

        logging.info("===: Node update (%s): %s", self.getName(), values) #FIXME: print properly

        # maybe update generic+specific device
        if self.values.Get(command.CUSTOM_COMMAND_PROTOCOL_INFO) is None:
            self.values.Set(command.CUSTOM_COMMAND_PROTOCOL_INFO, {"device_type": (0, values["generic"], values["specific"])})

        v = z.GENERIC_SPECIFIC_DB.get((values["generic"] << 8) + values["specific"])
        if v is None:
            logging.error("[%d] unknown generic device : %s", self.nodeId, repr(values))
            return

        self.InitializeUnversioned(values["commands"], values["controls"], v[1], v[2])
        self.ChangeStatus(Node.NodeStatus.DISCOVERED)

        if self.status.value >= Node.NodeStatus.INTERVIEWED.value:
            self.RefreshDynamicValues()


    def put(self, nodeCommand:NodeCommand):
        """A Node receives new commands via this function"""
        self.last_contact = time.time()

        if self.status.value < Node.NodeStatus.DISCOVERED.value: self.Ping()

        c, v = nodeCommand.command, nodeCommand.commandValues

        if   c == z.Version_CommandClassReport:             self.values.SetMapEntry(c, v["class"], v["version"])
        elif c == z.Meter_Report:                           self.values.SetMapEntry(c, (v["value"]["type"], v["value"]["unit"]), v["value"])
        elif c == z.Configuration_Report:                   self.values.SetMapEntry(c, v["parameter"], v["value"])
        elif c == z.SensorMultilevel_Report:                self.values.SetMapEntry(c, (v["type"], v["value"]["unit"]), v["value"])
        elif c == z.ThermostatSetpoint_Report:              self.values.SetMapEntry(c, v["thermo"], v["value"])
        elif c == z.AssociationGroupInformation_ListReport: self.values.SetMapEntry(c, v["group"], v["commands"])
        elif c == z.SceneActuatorConf_Report:               self.values.SetMapEntry(c, v["scene"], v)
        elif c == z.UserCode_Report:                        self.values.SetMapEntry(c, v["user"], v)
        elif c == z.Association_Report:                     self.values.SetMapEntry(c, v["group"], v)
        elif c == z.AssociationGroupInformation_NameReport: self.values.SetMapEntry(c, v["group"], v["name"])
        elif c == z.AssociationGroupInformation_InfoReport:
            for t in v["groups"]:
                self.values.SetMapEntry(c, t[0], t)
        elif c == z.MultiChannel_CapabilityReport:          self.values.SetMapEntry(c, v["endpoint"], v)
        else: self.values.Set(c, v)

        if   c == z.ManufacturerSpecific_Report or c == z.ZwavePlusInfo_Report: self.ChangeStatus(Node.NodeStatus.INTERVIEWED)   # these are the last of the static queries triggered by DISCOVERED
        elif c == z.Security2_KexReport:         self.ChangeStatus(Node.NodeStatus.KEX_REPORT)
        elif c == z.Security2_PublicKeyReport:   self.ChangeStatus(Node.NodeStatus.PUBLIC_KEY_REPORT_OTHER)

        if c == z.SceneActuatorConf_Report:    self.values.Set(command.CUSTOM_COMMAND_ACTIVE_SCENE, v)

        if c == z.Security2_NonceGet:
            # TODO: using a fixed nonce is a total hack - fix this
            args = {"seq": v["seq"], "mode": 1, "nonce": [0] * 16}
            logging.warning("Sending Nonce: %s", str(args))
            self.sendFilteredCommandBatch([(z.Security2_NonceReport, args)])

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
    def __init__(self, node:Node, endpoint:int, name=None):
        super().__init__(name)
        self.node = node
        self.endpoint = endpoint


    def getName(self):
        id = "%d.%d" % (self.node.nodeId, self.endpoint)
        if self.name is None and self.node.name is None: return id
        return self.name if self.name is not Node else self.node.name + " (" + id + ")"


    def sendCommand(self, nodeCommand:NodeCommand, highPriority=True, txOptions:tuple=None):
        nodeCommand = NodeCommand(z.MultiChannel_CmdEncap, {"src": 0, "dst": self.endpoint, "command": nodeCommand})
        self.node.sendCommand(nodeCommand, highPriority, txOptions)


    def Ping(self, retries, reason):
        logging.info("===: Ping (node: %s): reason %s, retries %d", self.getName(), reason, retries)

        self.sendCommand(NodeCommand(z.MultiChannel_CapabilityGet, {"endpoint": self.endpoint})) #FIXME: does a ping on endpoint make sense?


    def put(self, nodeCommand):
        c, v = nodeCommand.command, nodeCommand.commandValues

        if   c == z.Version_CommandClassReport:             self.values.SetMapEntry(c, v["class"], v["version"])
        elif c == z.Meter_Report:                           self.values.SetMapEntry(c, (v["value"]["type"], v["value"]["unit"]), v["value"])
        elif c == z.Configuration_Report:                   self.values.SetMapEntry(c, v["parameter"], v["value"])
        elif c == z.SensorMultilevel_Report:                self.values.SetMapEntry(c, (v["type"], v["value"]["unit"]), v["value"])
        elif c == z.ThermostatSetpoint_Report:              self.values.SetMapEntry(c, v["thermo"], v["value"])
        elif c == z.AssociationGroupInformation_ListReport: self.values.SetMapEntry(c, v["group"], v["commands"])
        elif c == z.SceneActuatorConf_Report:               self.values.SetMapEntry(c, v["scene"], v)
        elif c == z.UserCode_Report:                        self.values.SetMapEntry(c, v["user"], v)
        elif c == z.MultiChannel_CapabilityReport:          self.values.SetMapEntry(c, v["endpoint"], v)
        elif c == z.Association_Report:                     self.values.SetMapEntry(c, v["group"], v)
        elif c == z.AssociationGroupInformation_NameReport: self.values.SetMapEntry(c, v["group"], v["name"])
        elif c == z.AssociationGroupInformation_InfoReport:
            for t in v["groups"]:
                self.values.SetMapEntry(c,t[0], t)
        else: self.values.Set(c, v)

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
            node = Node(n, self.driver)
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


    def put(self, serialRequest:SerialRequest):
        if serialRequest.serialCommand == z.API_APPLICATION_COMMAND_HANDLER:
            srcNode, nodeCommand = serialRequest.serialCommandValues["node"], serialRequest.serialCommandValues["command"]

            if nodeCommand.command == z.MultiChannel_CmdEncap:
                self.getEndpoint(srcNode, nodeCommand.commandValues["src"]).put(nodeCommand.commandValues["command"])
                return

            if nodeCommand.command == z.MultiChannel_CapabilityReport:
                # FIXME - can we do this more elegantly? also, this is not right
                nodeCommand.commandValues["commands"] = nodeCommand.commandValues["classes"]
                nodeCommand.commandValues["controls"] = []

                self.getEndpoint(srcNode, nodeCommand.commandValues["endpoint"]).put(nodeCommand)
                self.getNode(srcNode).put(nodeCommand)
                return

            self.getNode(srcNode).put(nodeCommand)

        elif serialRequest.serialCommand == z.API_ZW_APPLICATION_UPDATE:
            if serialRequest.serialCommandValues["status"] == z.UPDATE_STATE_NODE_INFO_REQ_FAILED:
                if serialRequest.serialCommandValues["data"][0] != 0:
                    logging.error("XXX: Application update request failed")

            elif serialRequest.serialCommandValues["status"] == z.UPDATE_STATE_SUC_ID:
                logging.warning("===: Application updated: suc id updated: needs work")

            elif serialRequest.serialCommandValues["status"] == z.UPDATE_STATE_NODE_INFO_RECEIVED:
                # the node is awake now and/or has changed values
                data = serialRequest.serialCommandValues["data"]
                values = {}
                srcNode            = data[0]
                values["basic"]    = data[2]
                values["generic"]  = data[3]
                values["specific"] = data[4]
                values["commands"] = []
                values["controls"] = []
                seen_marker = False
                for i in data[5: 2 + data[1]]:
                    if i == z.Mark:
                        seen_marker = True
                        continue
                    values["controls" if seen_marker else "commands"].append(i)

                self.getNode(srcNode).handleApplicationUpdate(values)

            else:
                logging.error("XXX: Application update: unknown type (%x) - ignore", serialRequest.serialCommandValues["status"])

        else:
            logging.error("XXX: unhandled message - ignore: %s", serialRequest.toString())
