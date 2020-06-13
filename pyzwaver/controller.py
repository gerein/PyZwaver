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
controller.py contains code for dealing with the controller node in a zwave network.
"""

import logging
import struct
import time

from pyzwaver import zwave as z
from pyzwaver.driver import Driver
from pyzwaver.driver import MessageQueueOut
from pyzwaver.transaction import Transaction

_APPLICATION_NODEINFO_LISTENING = 1
_NUM_NODE_BITFIELD_BYTES = 29

MESSAGE_TIMEOUT = 100
MESSAGE_NOT_DELIVERED = 101

CONTROLLER_STATE_NONE = 0
CONTROLLER_STATE_INITIALIZED = 1

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

def ExtractNodes(bits):
    assert len(bits) == _NUM_NODE_BITFIELD_BYTES
    r = set()
    for i in range(8 * _NUM_NODE_BITFIELD_BYTES):
        if (bits[i // 8] & (1 << (i % 8))) == 0:
            continue
        node_id = i + 1
        r.add(node_id)
    return r
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

    def SetVersion(self, version_str, library_type):
        if version_str[-1] == 0:
            version_str = version_str[:-1]

        self.library_type = library_type
        self.version_str = version_str
        assert self.library_type <= 8
        if self.library_type == 7:
            self.attrs.add("bridge")
        logging.info("library_type: %s", self.library_type)

    def SetId(self, home_id, node_id):
        self.home_id = home_id
        self.node_id = node_id
        logging.info("home-id: 0x%x node-id: %d", self.home_id, self.node_id)

    def SetControllerCapabilites(self, caps):
        logging.info("capabilities: %x", caps)
        if caps & z.CAP_CONTROLLER_SECONDARY:
            self.attrs.add("secondary")
        if caps & z.CAP_CONTROLLER_SUC:
            self.attrs.add("suc")
        if caps & z.CAP_CONTROLLER_SIS:
            self.attrs.add("sis")
        if caps & z.CAP_CONTROLLER_REAL_PRIMARY:
            self.attrs.add("real_primary")

    def HasApi(self, func):
        fid = func - 1
        return self._api_mask[fid // 8] & (1 << (fid % 8))

    def SetSerialCapabilities(self, serial_api_version, manu_id, type_id, prod_id, api_mask):
        self.serial_api_version = serial_api_version
        self.product = (manu_id, type_id, prod_id)
        self._api_mask = api_mask

    def SetInitAndReturnBits(self, serial_version, caps, num_bytes, bits, chip_type, version):
        assert num_bytes == _NUM_NODE_BITFIELD_BYTES
        self.serial_version = serial_version
        self.chip_type = chip_type
        self.version = version
        logging.info("serial caps: %x", caps)
        if caps & z.SERIAL_CAP_SLAVE:
            self.attrs.add("serial_slave")
        if caps & z.SERIAL_CAP_TIMER_SUPPORT:
            self.attrs.add("serial_timer")
        if caps & z.SERIAL_CAP_SECONDARY:
            self.attrs.add("serial_secondary")
        return bits

    def StringApis(self):
        out = []
        for func, name in z.API_TO_STRING.items():
            if self.HasApi(func):
                out.append("%s[%d]" % (name, func))
        return "\n".join(out)

    def __str__(self):
        return "home: %08x  node: %02x" % (self.home_id, self.node_id) + "\n" + \
               "versions: %s %x %x (%s)" % (self.version, self.serial_api_version, self.serial_version, self.version_str) + "\n" + \
               "chip: %x.%02x" % (self.chip_type, self.version) + "\n" + \
               "product: %04x %04x %04x  %x" % (self.product[0], self.product[1], self.product[2], self.library_type) + "\n" + \
               "attrs: %s" % repr(self.attrs)




class Controller:
    """
    Represents the controller node in a Zwave network
    The message_queue is used to send messages to the physical controller and
    the other nodes in the network.
    """

    def __init__(self, driver: Driver, pairing_timeout_secs=15.0):
        """
        :param message_queue:  is used to send commands to the controller and other zwave nodes.
                               The other end of the queue must be handled by the driver.
        :param pairing_timeout_secs:
        """
        # self._event_cb = event_cb
        self._pairing_timeout_sec = pairing_timeout_secs
        self._state = CONTROLLER_STATE_NONE
        self.driver = driver
        self.nodes = set()
        self.failed_nodes = set()
        self.props = ControllerProperties()
        self.routes = {}

    def __str__(self):
        return self.StringBasic() + "\n\n" + self.StringRoutes()

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
        def handler(callbackReason, data):
            if callbackReason == Transaction.CallbackReason.TIMED_OUT:
                # logging.error("Cannot read controller version. Check serial device.")
                raise ValueError("Cannot read controller version. Check serial device.")
            else:
                logging.debug("Handling version response: %s", data)
                self.props.SetVersion(*struct.unpack(">12sB", bytes(data)))

        self.driver.sendRequest(z.API_ZW_GET_VERSION, [], callback=handler)

    def UpdateId(self):
        def handler(callbackReason, data):
            if callbackReason == Transaction.CallbackReason.TIMED_OUT: return
            self.props.SetId(*struct.unpack(">IB",bytes(data)))

        self.driver.sendRequest(z.API_ZW_MEMORY_GET_ID, [], callback=handler)

    def UpdateControllerCapabilities(self):
        def handler(callbackReason, data):
            if callbackReason == Transaction.CallbackReason.TIMED_OUT: return
            self.props.SetControllerCapabilites(data[0])

        self.driver.sendRequest(z.API_ZW_GET_CONTROLLER_CAPABILITIES, [], callback=handler)

    def UpdateSerialApiGetCapabilities(self):
        """
        """

        def handler(callbackReason, data):
            if callbackReason == Transaction.CallbackReason.TIMED_OUT: return
            self.props.SetSerialCapabilities(*struct.unpack(">HHHH32s", bytes(data)))

        self.driver.sendRequest(z.API_SERIAL_API_GET_CAPABILITIES, [], callback=handler)

    def UpdateSerialApiGetInitData(self):
        """This get all the node numbers"""

        def handler(callbackReason, data):
            if callbackReason == Transaction.CallbackReason.TIMED_OUT: return
            bits = self.props.SetInitAndReturnBits(*struct.unpack(">BBB29sBB", bytes(data)))
            self.nodes = ExtractNodes(bits)

        self.driver.sendRequest(z.API_SERIAL_API_GET_INIT_DATA, [], callback=handler)

    def SetTimeouts(self, ack_timeout_msec, byte_timeout_msec):
        def handler(callbackReason, data):
            if callbackReason == Transaction.CallbackReason.TIMED_OUT: return
            logging.info("previous timeouts: %d %d", data[0] * 10, data[1] * 10)

        self.driver.sendRequest(z.API_SERIAL_API_SET_TIMEOUTS,
                         [ack_timeout_msec // 10, byte_timeout_msec // 10],
                         callback=handler)

    def UpdateSucNodeId(self):
        def handler(callbackReason, data):
            if callbackReason == Transaction.CallbackReason.TIMED_OUT: return
            succ_node = data[0]
            logging.info("suc node id: %s", succ_node)

        self.driver.sendRequest(z.API_ZW_GET_SUC_NODE_ID, [], callback=handler)

    def GetRandom(self, _, cb):
        def handler(callbackReason, data):
            if callbackReason == Transaction.CallbackReason.TIMED_OUT: return
            success = data[0]
            size = data[1]
            data = data[2:2 + size]
            cb(success, data)

        self.driver.sendRequest(z.API_ZW_GET_RANDOM, [], callback=handler)

    def UpdateFailedNode(self, node: int):
        def handler(callbackReason, data):
            if callbackReason == Transaction.CallbackReason.TIMED_OUT: return
            if data[0]:
                self.failed_nodes.add(node)
            else:
                self.failed_nodes.discard(node)

        self.driver.sendRequest(z.API_ZW_IS_FAILED_NODE_ID, [node], callback=handler)

    def ReadMemory(self, offset: int, length: int, cb):
        def handler(callbackReason, data):
            if callbackReason == Transaction.CallbackReason.TIMED_OUT: return
            logging.info("received %x bytes", len(data))
            cb(data)

        self.driver.sendRequest(z.API_ZW_READ_MEMORY,
                         [offset >> 8, offset & 0xff, length],
                         callback=handler)

    def GetRoutingInfo(self, node: int, rem_bad, rem_non_repeaters, cb):
        def handler(callbackReason, data):
            if callbackReason == Transaction.CallbackReason.TIMED_OUT: return
            cb(node, ExtractNodes(data))

        self.driver.sendRequest(z.API_ZW_GET_ROUTING_INFO,
                         [node, rem_bad, rem_non_repeaters, 3],
                         callback=handler)

    def SetPromiscuousMode(self, state):
        def handler(callbackReason, data):
            pass

        self.driver.sendRequest(z.API_ZW_SET_PROMISCUOUS_MODE, [state], callback=handler)

    def RequestNodeInfo(self, node: int, cb=None):
        """Force the generation of a zwave.API_ZW_APPLICATION_UPDATE event
        """
        logging.warning("requesting node info for %d", node)

        def handler(callbackReason, data):
            if callbackReason == Transaction.CallbackReason.TIMED_OUT: return
            if cb:
                cb(data[0])

        self.driver.sendRequest(z.API_ZW_REQUEST_NODE_INFO, [node], callback=handler)

    def RemoveFailedNode(self, node: int, cb):  # FIXME: figure out flow and if it still works correctly, currently not used
        def handler(callbackReason, data):
            if callbackReason == Transaction.CallbackReason.TIMED_OUT:
                cb(MESSAGE_TIMEOUT)
            elif callbackReason == Transaction.CallbackReason.RESPONSE_RECEIVED:
                cb(MESSAGE_NOT_DELIVERED)
            else:
                return cb(data[1])

        self.driver.sendRequest(z.API_ZW_REMOVE_FAILED_NODE_ID, [node], callback=handler)

    # ============================================================
    # Routing
    # ============================================================
    def UpdateRoutingInfo(self):
        def handler(node, neighbors):
            logging.info("[%d] setting routing info to: %s", node, neighbors)
            self.routes[node] = set(neighbors)

        for n in self.nodes:
            self.GetRoutingInfo(n, False, False, handler)

    # ============================================================
    # Pairing
    # ============================================================
    def MakeFancyReceiver(self, activity: str, receiver_type, event_cb):
        stringMap, actions = receiver_type

        def Handler(callbackReason, data):
            if callbackReason == Transaction.CallbackReason.TIMED_OUT:
                logging.error("XXX: Aborted %s", activity)
                event_cb(activity, EVENT_PAIRING_ABORTED, None)
                return True
            if not data:
                event_cb(activity, EVENT_PAIRING_STARTED, None)
                return True

            status = data[1]
            node = data[2]
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
                # This not make much sense for node removals but does not hurt either
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

        def handler(callbackReason, data):
            if callbackReason == Transaction.CallbackReason.TIMED_OUT:
                logging.error("[%s] Aborted", activity)
                event_cb(activity, EVENT_PAIRING_ABORTED, node)
                return True
            if not data:
                event_cb(activity, EVENT_PAIRING_STARTED, node)
                return False

            status = data[1]
            if status == z.REQUEST_NEIGHBOR_UPDATE_STARTED:
                event_cb(activity, EVENT_PAIRING_CONTINUE, node)
                return False
            elif status == z.REQUEST_NEIGHBOR_UPDATE_DONE:
                event_cb(activity, EVENT_PAIRING_SUCCESS, node)
                return True
            elif status == z.REQUEST_NEIGHBOR_UPDATE_FAIL:
                event_cb(activity, EVENT_PAIRING_FAILED, node)
                return True
            else:
                logging.error("[%s] unknown status %d %s", activity, status, " ".join(["%02x" % i for i in data]))
                return True

        self.driver.sendRequest(z.API_ZW_REQUEST_NODE_NEIGHBOR_UPDATE, [node], timeout=self._pairing_timeout_sec, callback=handler)
        logging.warning("===: NeighborUpdate(%d)", node)

    def AddNodeToNetwork(self, event_cb):
        cb = self.MakeFancyReceiver(ACTIVITY_ADD_NODE, HANDLER_TYPE_ADD_NODE, event_cb)
        self.driver.sendRequest(z.API_ZW_ADD_NODE_TO_NETWORK, [z.ADD_NODE_ANY], timeout=self._pairing_timeout_sec, callback=cb)
        logging.warning("===: AddNodeToNetwork")

    def StopAddNodeToNetwork(self, event_cb):
        cb = self.MakeFancyReceiver(ACTIVITY_STOP_ADD_NODE, HANDLER_TYPE_STOP, event_cb)
        self.driver.sendRequest(z.API_ZW_ADD_NODE_TO_NETWORK, [z.ADD_NODE_STOP], timeout=5, callback=cb)
        logging.warning("===: StopAddNodeToNetwork")

    def RemoveNodeFromNetwork(self, event_cb):
        cb = self.MakeFancyReceiver(ACTIVITY_REMOVE_NODE, HANDLER_TYPE_REMOVE_NODE, event_cb)
        self.driver.sendRequest(z.API_ZW_REMOVE_NODE_FROM_NETWORK, [z.REMOVE_NODE_ANY], cb, timeout=self._pairing_timeout_sec)
        logging.warning("===: RemoveNodeFromNetwork")

    def StopRemoveNodeFromNetwork(self, _):
        # NOTE: this will sometimes result in a "stray request" being sent back:
        #  SOF len:07 REQU API_ZW_REMOVE_NODE_FROM_NETWORK:4b cb:64 status:06 00 00 chk:d1
        # We just drop this message on the floor
        self.driver.sendRequest(z.API_ZW_REMOVE_NODE_FROM_NETWORK, [z.REMOVE_NODE_STOP])

    def SetLearnMode(self, event_cb):
        cb = self.MakeFancyReceiver(ACTIVITY_SET_LEARN_MODE, HANDLER_TYPE_SET_LEARN_MODE, event_cb)
        self.driver.sendRequest(z.API_ZW_SET_LEARN_MODE, [z.LEARN_MODE_NWI], timeout=self._pairing_timeout_sec, callback=cb)

    def StopSetLearnMode(self, _):
        self.driver.sendRequest(z.API_ZW_SET_LEARN_MODE, [z.LEARN_MODE_DISABLE])

    def ChangeController(self, event_cb):
        cb = self.MakeFancyReceiver(ACTIVITY_CHANGE_CONTROLLER, HANDLER_TYPE_ADD_NODE, event_cb)
        self.driver.sendRequest(z.API_ZW_CONTROLLER_CHANGE, [z.CONTROLLER_CHANGE_START], timeout=self._pairing_timeout_sec, callback=cb)

    def StopChangeController(self, _):
        self.driver.sendRequest(z.API_ZW_CONTROLLER_CHANGE, [z.CONTROLLER_CHANGE_STOP])

    # ============================================================

    def ApplNodeInformation(self):
        """Advertise/change the features of this node"""

        def handler(callbackReason, data):
            if callbackReason == Transaction.CallbackReason.TIMED_OUT: return
            logging.warning("controller is now initialized")
            self._state = CONTROLLER_STATE_INITIALIZED

        self.driver.sendRequest(z.API_SERIAL_API_APPL_NODE_INFORMATION,
                                [_APPLICATION_NODEINFO_LISTENING,
                                 2,  # generic
                                 1,  # specific
                                 0,  # rest: size + data
                                 ],
                                callback=handler)

    def SendNodeInformation(self, dst_node: int, xmit: int, cb):
        def handler(callbackReason, data):
            if callbackReason == Transaction.CallbackReason.TIMED_OUT: return
            cb(data)

        self.driver.sendRequest(z.API_ZW_SEND_NODE_INFORMATION, [dst_node, xmit], callback=handler)

    def SetDefault(self):
        """Factory reset the controller"""

        def handler(callbackReason, data):
            if callbackReason == Transaction.CallbackReason.TIMED_OUT: return
            logging.warning("set default response %s", data)

        self.driver.sendRequest(z.API_ZW_SET_DEFAULT, [], callback=handler)

    def SoftReset(self):
        def handler(callbackReason, data):
            if callbackReason == Transaction.CallbackReason.TIMED_OUT: return
            logging.warning("soft reset response %s", data)

        self.driver.sendRequest(z.API_SERIAL_API_SOFT_RESET, [], timeout=2.0, callback=handler)


    def Initialize(self):
        self.UpdateVersion()
        self.UpdateId()
        self.UpdateControllerCapabilities()
        self.UpdateSerialApiGetCapabilities()
        self.UpdateSerialApiGetInitData()
        self.SetTimeouts(1000, 150)
        self.UpdateSucNodeId()
        self.ApplNodeInformation()  # sets isInitialised

    def WaitUntilInitialized(self, max_wait=5):
        logging.info("Waiting for the controller to initialise...")
        deadline = time.time() + max_wait
        while self._state != CONTROLLER_STATE_INITIALIZED:
            logging.warning("wait - current Controller state is: %s", self._state)
            time.sleep(0.5)
            if time.time() > deadline:
                return False
        return True

    def TriggerNodesUpdate(self):
        logging.info("trigger nodes update")
        for n in self.nodes:
            if n == self.props.node_id:
                continue
            self.RequestNodeInfo(n)

    def GetNodeId(self):
        return self.props.node_id

    def Update(self):
        logging.warning("Update")
        # self._event_cb(ACTIVITY_CONTROLLER_UPDATE, EVENT_UPDATE_STARTED)
        self.UpdateId()
        self.UpdateControllerCapabilities()
        self.UpdateSerialApiGetCapabilities()
        self.UpdateSerialApiGetInitData()
        for n in self.nodes:
            self.UpdateFailedNode(n)
