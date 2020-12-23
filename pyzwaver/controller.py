# Copyright 2016 Robert Muth <robert@muth.org>
# Copyright 2020 Gerein
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
controller.py contains code for dealing with the controller node in a zwave network.
"""

import logging
import struct
import time

from . import zwave as z
from .driver import Driver
from .command import SerialRequest
from .transactionProcessor import TransactionProcessor
from .node import Nodeset

_APPLICATION_NODEINFO_LISTENING = 1
_NUM_NODE_BITFIELD_BYTES = 29

MESSAGE_TIMEOUT = 100
MESSAGE_NOT_DELIVERED = 101

ACTIVITY_ADD_NODE          = "AddNode"
ACTIVITY_STOP_ADD_NODE     = "StopAddNode"
ACTIVITY_REMOVE_NODE       = "RemoveNode"
ACTIVITY_SET_LEARN_MODE    = "SetLearnMode"
ACTIVITY_CHANGE_CONTROLLER = "ChangeController"
ACTIVITY_CONTROLLER_UPDATE = "ControllerUpdate"

EVENT_PAIRING_ABORTED  = "Aborted"
EVENT_PAIRING_CONTINUE = "InProgress"
EVENT_PAIRING_FAILED   = "Failed"
EVENT_PAIRING_SUCCESS  = "Success"
EVENT_PAIRING_STARTED  = "Started"

EVENT_UPDATE_STARTED   = "Started"
EVENT_UPDATE_COMPLETE  = "Complete"

PAIRING_ACTION_CONTINUE    = 1
PAIRING_ACTION_DONE        = 2
PAIRING_ACTION_FAILED      = 3
PAIRING_ACTION_DONE_UPDATE = 4

HANDLER_TYPE_ADD_NODE = (z.ADD_NODE_STATUS_TO_STRING, {
    z.ADD_NODE_STATUS_LEARN_READY:                 PAIRING_ACTION_CONTINUE,
    z.ADD_NODE_STATUS_ADDING_SLAVE:                PAIRING_ACTION_CONTINUE,
    z.ADD_NODE_STATUS_ADDING_CONTROLLER:           PAIRING_ACTION_CONTINUE,
    z.ADD_NODE_STATUS_NODE_FOUND:                  PAIRING_ACTION_CONTINUE,
    z.ADD_NODE_STATUS_FAILED:                      PAIRING_ACTION_FAILED,
    z.REMOVE_NODE_STATUS_NOT_INCLUSION_CONTROLLER: PAIRING_ACTION_FAILED,
    z.ADD_NODE_STATUS_DONE:                        PAIRING_ACTION_DONE_UPDATE,
    z.ADD_NODE_STATUS_PROTOCOL_DONE:               PAIRING_ACTION_DONE_UPDATE,
})

HANDLER_TYPE_STOP = (z.ADD_NODE_STATUS_TO_STRING, {
    z.ADD_NODE_STATUS_LEARN_READY:                 PAIRING_ACTION_CONTINUE,
    z.ADD_NODE_STATUS_ADDING_SLAVE:                PAIRING_ACTION_CONTINUE,
    z.ADD_NODE_STATUS_ADDING_CONTROLLER:           PAIRING_ACTION_CONTINUE,
    z.ADD_NODE_STATUS_NODE_FOUND:                  PAIRING_ACTION_CONTINUE,
    z.ADD_NODE_STATUS_FAILED:                      PAIRING_ACTION_DONE,
    z.REMOVE_NODE_STATUS_NOT_INCLUSION_CONTROLLER: PAIRING_ACTION_DONE,
    z.ADD_NODE_STATUS_DONE:                        PAIRING_ACTION_DONE,
    z.ADD_NODE_STATUS_PROTOCOL_DONE:               PAIRING_ACTION_DONE,
})

HANDLER_TYPE_REMOVE_NODE = (z.REMOVE_NODE_STATUS_TO_STRING, {
    z.REMOVE_NODE_STATUS_LEARN_READY:              PAIRING_ACTION_CONTINUE,
    z.REMOVE_NODE_STATUS_REMOVING_SLAVE:           PAIRING_ACTION_CONTINUE,
    z.REMOVE_NODE_STATUS_NODE_FOUND:               PAIRING_ACTION_CONTINUE,
    z.REMOVE_NODE_STATUS_NOT_INCLUSION_CONTROLLER: PAIRING_ACTION_FAILED,
    z.REMOVE_NODE_STATUS_FAILED:                   PAIRING_ACTION_FAILED,
    z.REMOVE_NODE_STATUS_DONE:                     PAIRING_ACTION_DONE_UPDATE,
    z.REMOVE_NODE_STATUS_REMOVING_CONTROLLER:      PAIRING_ACTION_CONTINUE,
})

HANDLER_TYPE_SET_LEARN_MODE = (z.LEARN_MODE_STATUS_TO_STRING, {
    z.LEARN_MODE_STATUS_STARTED: PAIRING_ACTION_CONTINUE,
    z.LEARN_MODE_STATUS_FAILED:  PAIRING_ACTION_FAILED,
    z.LEARN_MODE_STATUS_DONE:    PAIRING_ACTION_DONE_UPDATE,
})


class ControllerProperties:

    def __init__(self):
        self.home_id = None
        self.node_id = None
        self.product = [0, 0, 0]
        self.chip_type = None
        self.version = None
        self.version_str = None
        self.serial_api_version = None
        self.serial_version = None
        self.library_type = None
        self._api_mask = None
        self.attrs = set()

    def StringApis(self):
        out = []
        for func, name in z.API_TO_STRING.items():
            if self._api_mask[(func - 1) // 8] & (1 << ((func - 1) % 8)):   # previously .HasApi(func)
                out.append("%s[%d]" % (name, func))
        return "\n".join(out)

    def __str__(self):
        return "home: %08x  node: %02x" % (self.home_id, self.node_id) + "\n" + \
               "versions: %s %x %x (%s)" % (self.version, self.serial_api_version, self.serial_version, self.version_str) + "\n" + \
               "chip: %x.%02x" % (self.chip_type, self.version) + "\n" + \
               "product: %04x %04x %04x  %x" % (self.product[0], self.product[1], self.product[2], self.library_type) + "\n" + \
               "attrs: %s" % repr(self.attrs)


def ExtractNodes(bits):
    assert len(bits) == _NUM_NODE_BITFIELD_BYTES
    r = set()
    for i in range(8 * _NUM_NODE_BITFIELD_BYTES):
        if (bits[i // 8] & (1 << (i % 8))) == 0: continue
        node_id = i + 1
        r.add(node_id)
    return r


class Controller:
    """
    Represents the controller node in a Zwave network
    The message_queue is used to send messages to the physical controller and
    the other nodes in the network.
    """

    def __init__(self, driver: Driver, pairing_timeout_secs=15.0):
        self.driver = driver
        self._pairing_timeout_sec = pairing_timeout_secs
        self.isInitialised = False
        self.nodes = set()
        self.failed_nodes = set()
        self.props = ControllerProperties()
        self.routes = {}

    def __str__(self):
        return self.StringBasic() + "\n\n" + \
               "routes: " + self.StringRoutes()

    def StringBasic(self):
        return str(self.props) + "\n" + \
               "nodes: %s" % repr(self.nodes) + "\n" + \
               "failed_nodes: %s" % repr(self.failed_nodes)

    def StringRoutes(self):
        out = []
        nodes = sorted(self.nodes)
        for n in nodes:
            routes = self.routes.get(n, set())
            line = "%2d: " % n
            for m in nodes: line += "#" if m in routes else " "
            out.append(line)
        return "\n".join(out)

    def UpdateVersion(self):
        def handler(callbackReason, serialCommandValues):
            if callbackReason == TransactionProcessor.CallbackReason.TIMED_OUT:
                # logging.error("Cannot read controller version. Check serial device.")
                raise ValueError("Cannot read controller version. Check serial device.")

            logging.debug("Handling version response: %s", serialCommandValues["rest"])
            self.props.version_str, self.props.library_type = struct.unpack(">12sB", bytes(serialCommandValues["rest"]))
            if self.props.version_str[-1] == 0: self.props.version_str = self.props.version_str[:-1]
            assert self.props.library_type <= 8
            if self.props.library_type == 7: self.props.attrs.add("bridge")
            logging.info("===: GET_VERSION result: library: %s, type: %s", self.props.version_str, self.props.library_type)

        self.driver.sendRequest(SerialRequest(z.API_ZW_GET_VERSION), requestPriority=Driver.RequestPriority.HIGHEST, callback=handler)

    def UpdateId(self):
        def handler(serialCommandValues):
            self.props.home_id = (serialCommandValues["homeidMSW"] << 16) + serialCommandValues["homeidLSW"]
            self.props.node_id = serialCommandValues["node"]
            logging.info("===: MEMORY_GET_ID result: home-id: 0x%x, node-id: %d", self.props.home_id, self.props.node_id)

        self.driver.sendRequest(SerialRequest(z.API_ZW_MEMORY_GET_ID),
                                requestPriority=Driver.RequestPriority.HIGHEST,
                                callback=Driver.ignoreTimeoutHandler(handler))

    def UpdateControllerCapabilities(self):
        def handler(serialCommandValues):
            props = set()
            if serialCommandValues["retval"] & z.CAP_CONTROLLER_SECONDARY:    props.add("secondary")
            if serialCommandValues["retval"] & z.CAP_CONTROLLER_SUC:          props.add("suc")
            if serialCommandValues["retval"] & z.CAP_CONTROLLER_SIS:          props.add("sis")
            if serialCommandValues["retval"] & z.CAP_CONTROLLER_REAL_PRIMARY: props.add("real_primary")
            logging.info("===: GET_CONTROLLER_CAPABILITIES results: capabilities: %x = (%s)", serialCommandValues["retval"], ", ".join([i for i in props]))
            for p in props: self.props.attrs.add(p)

        self.driver.sendRequest(SerialRequest(z.API_ZW_GET_CONTROLLER_CAPABILITIES),
                                requestPriority=Driver.RequestPriority.HIGHEST,
                                callback=Driver.ignoreTimeoutHandler(handler))

    def UpdateSerialApiGetCapabilities(self):
        def handler(serialCommandValues):
            self.props.serial_api_version = serialCommandValues["version"]
            self.props.product = (serialCommandValues["manufacturer"], serialCommandValues["productType"], serialCommandValues["productId"])
            self.props._api_mask = serialCommandValues["functionBitmask"]
            logging.info("===: GET_CAPABILITIES results: product: %s", self.props.product)  #FIXME: add functionBitmask output

        self.driver.sendRequest(SerialRequest(z.API_SERIAL_API_GET_CAPABILITIES),
                                requestPriority=Driver.RequestPriority.HIGHEST,
                                callback=Driver.ignoreTimeoutHandler(handler))

    def UpdateSerialApiGetInitData(self):
        """This get all the node numbers"""

        def handler(serialCommandValues):
            self.props.serial_version = serialCommandValues["version"]
            self.props.chip_type = serialCommandValues["chipType"]
            self.props.version = serialCommandValues["chipVersion"]

            props = set()
            if serialCommandValues["capabilities"] & z.SERIAL_CAP_SLAVE:         props.add("serial_slave")
            if serialCommandValues["capabilities"] & z.SERIAL_CAP_TIMER_SUPPORT: props.add("serial_timer")
            if serialCommandValues["capabilities"] & z.SERIAL_CAP_SECONDARY:     props.add("serial_secondary")
            logging.info("===: GET_INIT_DATA results: capabilities: %x = (%s)", serialCommandValues["capabilities"], ", ".join([i for i in props]))
            for p in props: self.props.attrs.add(p)

            self.nodes = ExtractNodes(serialCommandValues["nodemask"])

        self.driver.sendRequest(SerialRequest(z.API_SERIAL_API_GET_INIT_DATA),
                                requestPriority=Driver.RequestPriority.HIGHEST,
                                callback=Driver.ignoreTimeoutHandler(handler))

    def SetTimeouts(self, ack_timeout_msec, byte_timeout_msec):
        def handler(serialCommandValues):
            logging.info("===: SET_TIMEOUTS results: previous_timeouts: %d %d",
                         serialCommandValues["prevrxacktimeout"] * 10, serialCommandValues["prevrxbytetimeout"] * 10)

        self.driver.sendRequest(SerialRequest(z.API_SERIAL_API_SET_TIMEOUTS,
                                              {"rxacktimeout": ack_timeout_msec // 10, "rxbytetimeout": byte_timeout_msec // 10}),
                                requestPriority=Driver.RequestPriority.HIGHEST,
                                callback=Driver.ignoreTimeoutHandler(handler))

    def UpdateSucNodeId(self):
        def handler(serialCommandValues):
            logging.info("===: GET_SUC_NODE results: suc_node_id: %s", serialCommandValues["sucnodeid"])

        self.driver.sendRequest(SerialRequest(z.API_ZW_GET_SUC_NODE_ID),
                                requestPriority=Driver.RequestPriority.HIGHEST,
                                callback=Driver.ignoreTimeoutHandler(handler))

    def GetRandom(self, _, cb):
        def handler(serialCommandValues):
            cb(serialCommandValues["randomGenerationSuccess"], serialCommandValues["randomBytes"])

        self.driver.sendRequest(SerialRequest(z.API_ZW_GET_RANDOM),
                                requestPriority=Driver.RequestPriority.HIGHEST,
                                callback=Driver.ignoreTimeoutHandler(handler))

    def UpdateFailedNode(self, node: int):
        def handler(serialCommandValues):
            if serialCommandValues["retval"]:
                self.failed_nodes.add(node)
            else:
                self.failed_nodes.discard(node)

        self.driver.sendRequest(SerialRequest(z.API_ZW_IS_FAILED_NODE_ID, {"node": node}),
                                requestPriority=Driver.RequestPriority.HIGHEST,
                                callback=Driver.ignoreTimeoutHandler(handler))

    def ReadMemory(self, offset: int, length: int, cb):
        self.driver.sendRequest(SerialRequest(z.API_ZW_READ_MEMORY, {"offset": offset, "length": length}),
                                requestPriority=Driver.RequestPriority.HIGHEST,
                                callback=Driver.ignoreTimeoutHandler(cb))

    def SetPromiscuousMode(self, state):
        self.driver.sendRequest(SerialRequest(z.API_ZW_SET_PROMISCUOUS_MODE, {"state": state}),
                                requestPriority=Driver.RequestPriority.HIGHEST)

    def RequestNodeInfo(self, node: int, cb=None):
        """Force the generation of a zwave.API_ZW_APPLICATION_UPDATE event"""
        logging.warning("===: REQUEST_NODE_INFO results: node: %d)", node)

        def handler(data):
            if cb: cb(data[0])

        self.driver.sendRequest(SerialRequest(z.API_ZW_REQUEST_NODE_INFO, {"node": node}),
                                requestPriority=Driver.RequestPriority.HIGHEST,
                                callback=Driver.ignoreTimeoutHandler(handler))

    def RemoveFailedNode(self, node: int, cb):  # FIXME: figure out flow and if it still works correctly, currently not used
        def handler(callbackReason, data):
            if   callbackReason == TransactionProcessor.CallbackReason.TIMED_OUT:         cb(MESSAGE_TIMEOUT)
            elif callbackReason == TransactionProcessor.CallbackReason.RESPONSE_RECEIVED: cb(MESSAGE_NOT_DELIVERED)
            else:
                return cb(data[1])

        self.driver.sendRequest(SerialRequest(z.API_ZW_REMOVE_FAILED_NODE_ID, {"node": node}),
                                requestPriority=Driver.RequestPriority.HIGHEST, callback=handler)

    # ============================================================
    # Routing
    # ============================================================
    def UpdateRoutingInfo(self):
        def nodeWrappedHandler(node):
            def handler(callbackReason, serialCommandValues):
                if callbackReason == TransactionProcessor.CallbackReason.TIMED_OUT: return
                self.routes[node] = serialCommandValues["nodelist"]
                logging.info("===: UpdateRoutingInfo results: setting routing info for node %d to: %s",
                             node, self.routes[node])
            return handler

        for n in self.nodes:
            self.driver.sendRequest(SerialRequest(z.API_ZW_GET_ROUTING_INFO,
                                                  {"node": n, "removebad": False, "removenonreps": False, "_mustbezero": 0}),
                                    requestPriority=Driver.RequestPriority.HIGHEST, callback=nodeWrappedHandler(n))

    # ============================================================
    # Pairing
    # ============================================================
    def MakeFancyReceiver(self, activity: str, receiver_type, event_cb):
        stringMap, actions = receiver_type

        def Handler(callbackReason, serialCommandValues):
            if callbackReason == TransactionProcessor.CallbackReason.TIMED_OUT:
                logging.error("XXX: Timed-out %s", activity)
                event_cb(activity, EVENT_PAIRING_ABORTED, None)
                return True
            if callbackReason == TransactionProcessor.CallbackReason.REQUEST_SENT:
                event_cb(activity, EVENT_PAIRING_STARTED, None)
                return True

            status = serialCommandValues["status"]
            node = serialCommandValues["node"]
            name = stringMap[status]
            a = actions[status]
            logging.warning("===: Pairing status update: %s", a)

            if a == PAIRING_ACTION_CONTINUE:
                logging.warning("===: Pairing: [%s] Continue - %s [%d]", activity, name, node)
                event_cb(activity, EVENT_PAIRING_CONTINUE, node)
                return False

            elif a == PAIRING_ACTION_DONE:
                logging.warning("===: Pairing: [%s] Success", node)
                event_cb(activity, EVENT_PAIRING_SUCCESS, node)
                return True

            elif a == PAIRING_ACTION_DONE_UPDATE:
                logging.warning("===: Pairing: [%s] Success - updating nodes %s [%d]", activity, name, node)
                event_cb(activity, EVENT_PAIRING_SUCCESS, node)
                # This does not make much sense for node removals but does not hurt either
                self.RequestNodeInfo(node)
                self.Update()
                return True

            elif a == PAIRING_ACTION_FAILED:
                logging.warning("===: Pairing: [%s] Failure - %s [%d]", activity, name, node)
                event_cb(activity, EVENT_PAIRING_FAILED, node)
                return True

            else:
                logging.error("XXX: Pairing: activity unexpected: ${name}")
                return False

        return Handler

    def NeighborUpdate(self, node: int, event_cb):
        activity = "NeighborUpdate",

        def handler(callbackReason, serialCommandValues):
            if callbackReason == TransactionProcessor.CallbackReason.TIMED_OUT:
                logging.error("XXX: NeighborUpdate (%d) aborted/timed-out", node)
                event_cb(activity, EVENT_PAIRING_ABORTED, node)
                return True
            if callbackReason == TransactionProcessor.CallbackReason.REQUEST_SENT:
                event_cb(activity, EVENT_PAIRING_STARTED, node)
                return False

            if serialCommandValues["status"] == z.REQUEST_NEIGHBOR_UPDATE_STARTED:
                event_cb(activity, EVENT_PAIRING_CONTINUE, node)
                return False
            if serialCommandValues["status"] == z.REQUEST_NEIGHBOR_UPDATE_DONE:
                event_cb(activity, EVENT_PAIRING_SUCCESS, node)
                return True
            if serialCommandValues["status"] == z.REQUEST_NEIGHBOR_UPDATE_FAIL:
                event_cb(activity, EVENT_PAIRING_FAILED, node)
                return True

            logging.error("XXX: NeighborUpdate (%d) unknown status %s", node, serialCommandValues["status"])
            return True

        logging.warning("===: NeighborUpdate(%d)", node)
        self.driver.sendRequest(SerialRequest(z.API_ZW_REQUEST_NODE_NEIGHBOR_UPDATE, {"node": node}),
                                requestPriority=Driver.RequestPriority.HIGHEST, timeout=self._pairing_timeout_sec, callback=handler)

    def AddNodeToNetwork(self, event_cb):
        logging.warning("===: AddNodeToNetwork")
        self.driver.sendRequest(SerialRequest(z.API_ZW_ADD_NODE_TO_NETWORK, {"mode": z.ADD_NODE_ANY}),
                                requestPriority=Driver.RequestPriority.HIGHEST, timeout=self._pairing_timeout_sec,
                                callback=self.MakeFancyReceiver(ACTIVITY_ADD_NODE, HANDLER_TYPE_ADD_NODE, event_cb))

    def StopAddNodeToNetwork(self, event_cb):
        logging.warning("===: StopAddNodeToNetwork")
        self.driver.sendRequest(SerialRequest(z.API_ZW_ADD_NODE_TO_NETWORK, {"mode": z.ADD_NODE_STOP}),
                                requestPriority=Driver.RequestPriority.HIGHEST, timeout=5,
                                callback=self.MakeFancyReceiver(ACTIVITY_STOP_ADD_NODE, HANDLER_TYPE_STOP, event_cb))

    def RemoveNodeFromNetwork(self, event_cb):
        logging.warning("===: RemoveNodeFromNetwork")
        self.driver.sendRequest(SerialRequest(z.API_ZW_REMOVE_NODE_FROM_NETWORK, {"mode": z.REMOVE_NODE_ANY}),
                                requestPriority=Driver.RequestPriority.HIGHEST, timeout=self._pairing_timeout_sec,
                                callback=self.MakeFancyReceiver(ACTIVITY_REMOVE_NODE, HANDLER_TYPE_REMOVE_NODE, event_cb))

    def StopRemoveNodeFromNetwork(self):
        # NOTE: this will sometimes result in a "stray request" being sent back:
        #  SOF len:07 REQU API_ZW_REMOVE_NODE_FROM_NETWORK:4b cb:64 status:06 00 00 chk:d1
        # We just drop this message on the floor
        self.driver.sendRequest(SerialRequest(z.API_ZW_REMOVE_NODE_FROM_NETWORK, {"mode": z.REMOVE_NODE_STOP}))

    def SetLearnMode(self, event_cb):
        self.driver.sendRequest(SerialRequest(z.API_ZW_SET_LEARN_MODE, {"mode": z.LEARN_MODE_NWI}),
                                requestPriority=Driver.RequestPriority.HIGHEST, timeout=self._pairing_timeout_sec,
                                callback=self.MakeFancyReceiver(ACTIVITY_SET_LEARN_MODE, HANDLER_TYPE_SET_LEARN_MODE, event_cb))

    def StopSetLearnMode(self):
        self.driver.sendRequest(SerialRequest(z.API_ZW_SET_LEARN_MODE, {"mode": z.LEARN_MODE_DISABLE}))

    def ChangeController(self, event_cb):
        self.driver.sendRequest(SerialRequest(z.API_ZW_CONTROLLER_CHANGE, {"mode": z.CONTROLLER_CHANGE_START}),
                                requestPriority=Driver.RequestPriority.HIGHEST, timeout=self._pairing_timeout_sec,
                                callback=self.MakeFancyReceiver(ACTIVITY_CHANGE_CONTROLLER, HANDLER_TYPE_ADD_NODE, event_cb))

    def StopChangeController(self):
        self.driver.sendRequest(SerialRequest(z.API_ZW_CONTROLLER_CHANGE, {"mode": z.CONTROLLER_CHANGE_STOP}),
                                requestPriority=Driver.RequestPriority.HIGHEST)

    # ============================================================

    def ApplNodeInformation(self):
        """Advertise/change the features of this node"""

        def handler(serialCommandValues):
            logging.warning("===: Controller is now initialized")
            self.isInitialised = True

        self.driver.sendRequest(SerialRequest(z.API_SERIAL_API_APPL_NODE_INFORMATION,
                                              {"deviceoptions": _APPLICATION_NODEINFO_LISTENING, "generic": 2, "specific": 1,"nodeparm": ""}),
                                requestPriority=Driver.RequestPriority.HIGHEST,
                                callback=Driver.ignoreTimeoutHandler(handler))

    def SendNodeInformation(self, dst_node: int, txOptions: int, cb):
        self.driver.sendRequest(SerialRequest(z.API_ZW_SEND_NODE_INFORMATION, {"node": dst_node, "txOptions": txOptions}),
                                requestPriority=Driver.RequestPriority.HIGHEST,
                                callback=Driver.ignoreTimeoutHandler(cb))

    def SetDefault(self):
        """Factory reset the controller"""

        def handler(serialCommandValues):
            logging.warning("===: SET_DEFAULT completed")

        self.driver.sendRequest(SerialRequest(z.API_ZW_SET_DEFAULT),
                                requestPriority=Driver.RequestPriority.HIGHEST,
                                callback=Driver.ignoreTimeoutHandler(handler))

    def SoftReset(self):
        def handler(callbackReason, serialCommandValues):
            if callbackReason == TransactionProcessor.CallbackReason.REQUEST_SENT:
                logging.warning("===: SOFT_RESET initiated - waiting 1.5 seconds")
                time.sleep(1.5)

        self.driver.sendRequest(SerialRequest(z.API_SERIAL_API_SOFT_RESET),
                                requestPriority=Driver.RequestPriority.HIGHEST, timeout=2.0, callback=handler)


    def Initialize(self, timeout=5):
        logging.info("Initialising the controller...")
        timestamp = time.time()

        self.UpdateVersion()
        self.UpdateId()
        self.UpdateControllerCapabilities()
        self.UpdateSerialApiGetCapabilities()
        self.UpdateSerialApiGetInitData()
        self.SetTimeouts(1000, 150)
        self.UpdateSucNodeId()
        self.ApplNodeInformation()  # sets isInitialised

        while not self.isInitialised:
            if time.time() - timestamp > timeout:
                logging.error("...timeout! Controller didn't initialise in time. State undefined!")
                return None
            time.sleep(0.5)

        logging.info("...done! Controller is initialised")

        return Nodeset(self.driver, self.props.node_id)


    def Update(self):
        logging.warning("Update")
        # self._event_cb(ACTIVITY_CONTROLLER_UPDATE, EVENT_UPDATE_STARTED)
        self.UpdateId()
        self.UpdateControllerCapabilities()
        self.UpdateSerialApiGetCapabilities()
        self.UpdateSerialApiGetInitData()
        for n in self.nodes:
            self.UpdateFailedNode(n)
