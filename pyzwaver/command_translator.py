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

"""

import logging
import struct
import time

from pyzwaver import command
from pyzwaver import zwave as z
# from pyzwaver import zsecurity
from pyzwaver.driver import Driver
from pyzwaver.driver import MessageQueueOut
from pyzwaver.transaction import Transaction
from pyzwaver.serial_frame import SendDataFrame
from pyzwaver.command import NodeCommand


def Hexify(t): return ["%02x" % i for i in t]

def IsMultichannelNode(n): return n > 255
def SplitMultiChannelNode(n): return n >> 8, n & 0xff
def _NodeName(n): return "%d.%d" % SplitMultiChannelNode(n) if IsMultichannelNode(n) else str(n)


class CommandTranslator(object):
    """
    The CommandTranslator class registers itself as a listener to the driver.
    It will convert these raw commands received from the driver to the dictionary
    representation and send them to all the listeners that have been registered
    via AddListener().
    Certain non-command message are forwarded to the listeners as custom
    (pseudo) commands.
    The CommandTranslator performs wrapping/unwrapping for MultiChannel commands.
    The CommandTranslator is bi-directional. Commands in dictionary can be sent
    via  SendCommand()/SendMultiCommand() which will convert them to the wire
    representation before forwarding them to the driver.
    """

    def __init__(self, driver: Driver):
        self._driver = driver
        self._listeners = []
        driver.addListener(self)

    def AddListener(self, l):
        self._listeners.append(l)

    def _PushToListeners(self, n, ts, key, value):
        for l in self._listeners:
            l.put(n, ts, key, value)


    def SendCommand(self, n: int, key: tuple, values: dict, priority:tuple, txOptions:tuple=SendDataFrame.standardTX):
        self._driver.sendNodeCommand(n, key, values, priority, txOptions) # FIXME: this is int, not Enum


    def _RequestNodeInfo(self, n, retries):
        """This usually triggers send "API_ZW_APPLICATION_UPDATE:"""

        def handler(callbackReason, data):
            if callbackReason == Transaction.CallbackReason.TIMED_OUT or data[0] == 0:
                logging.warning("==X: RequestNodeInfo (node: %s) failed: %s", _NodeName(n), data)
                self._RequestNodeInfo(n, retries - 1)

        if retries == 0:
            logging.error("==X: RequestNodeInfo (node: %s) failed permanently", _NodeName(n))
            return

        logging.warning("===: RequestNodeInfo (node: %s, retries left: %d)", _NodeName(n), retries)
        self._driver.sendRequest(z.API_ZW_REQUEST_NODE_INFO, [n], MessageQueueOut.CONTROLLER_PRIORITY, callback=handler)


    def GetNodeProtocolInfo(self, n):
        def handler(callbackReason, data):
            if callbackReason == Transaction.CallbackReason.TIMED_OUT:
                logging.error("==X: GetNodeProtocolInfo (node: %s) failed", _NodeName(n))
                return
            if len(data) < 5:
                logging.error("==X: GetNodeProtocolInfo (node: %s): bad payload: %s", _NodeName(n), data)
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
            self._PushToListeners(n, time.time(), command.CUSTOM_COMMAND_PROTOCOL_INFO, out)


        logging.info("===: GetNodeProtocolInfo (node: %s)", _NodeName(n))
        self._driver.sendRequest(z.API_ZW_GET_NODE_PROTOCOL_INFO, [n], MessageQueueOut.CONTROLLER_PRIORITY, callback=handler)


    def Ping(self, n, retries, reason):
        logging.info("===: Ping (node: %s): reason %s, retries %d", _NodeName(n), reason, retries)

        if IsMultichannelNode(n):
            n, endpoint = SplitMultiChannelNode(n)
            self._driver.sendNodeCommand(n, z.MultiChannel_CapabilityGet, {"endpoint": endpoint}, MessageQueueOut.CONTROLLER_PRIORITY)
            return

        self.GetNodeProtocolInfo(n)

        def handler(callbackReason, data):
            if callbackReason == Transaction.CallbackReason.TIMED_OUT: return
            failed = data[0] != 0
            logging.info("===: Pong (node: %s) is-failed: %s, %s", _NodeName(n), failed, data)
            self._PushToListeners(n, time.time(), command.CUSTOM_COMMAND_FAILED_NODE, {"failed": failed})
            if not failed: self._RequestNodeInfo(n, retries)

        self._driver.sendRequest(z.API_ZW_IS_FAILED_NODE_ID, [n], MessageQueueOut.CONTROLLER_PRIORITY, callback=handler)


    def _HandleMessageApplicationCommand(self, node, nodeCommand:NodeCommand):
        nodeCommandValues = nodeCommand.commandValues
        if nodeCommand == z.MultiChannel_CapabilityReport:
            node = (node << 8) + nodeCommandValues["endpoint"]
            nodeCommandValues["commands"] = nodeCommandValues["classes"]
            nodeCommandValues["controls"] = []
            self._PushToListeners(node, time.time(), command.CUSTOM_COMMAND_APPLICATION_UPDATE, nodeCommandValues) # FIXME: do we need to reintroduce timestamp
            return

        self._PushToListeners(node, time.time(), nodeCommand, nodeCommandValues)


    def _HandleMessageApplicationUpdate(self, commandParameters):
        if commandParameters[0] == z.UPDATE_STATE_NODE_INFO_REQ_FAILED:
            n = commandParameters[1]
            if n != 0:
                logging.error("XXX: Application update request failed")

        elif commandParameters[0] == z.UPDATE_STATE_NODE_INFO_RECEIVED:
            # the node is awake now and/or has changed values
            n = commandParameters[1]
            length = commandParameters[2]
            data = commandParameters[3: 3 + length]
            commands = []
            controls = []
            seen_marker = False
            for i in data[3:]:
                if i == z.Mark:   seen_marker = True
                elif seen_marker: controls.append(i)
                else:             commands.append(i)
            value = {
                "basic": data[0],
                "generic": data[1],
                "specific": data[2],
                "commands": commands,
                "controls": controls,
            }
            self._PushToListeners(n, time.time(), command.CUSTOM_COMMAND_APPLICATION_UPDATE, value)  #FIXME: do we need to reintroduce timestamp

        elif commandParameters[0] == z.UPDATE_STATE_SUC_ID:
            logging.warning("===: Application updated: succ id updated: needs work")

        else:
            logging.error("XXX: Application update: unknown type (%x) - ignore", commandParameters[0])

    def put(self, command, commandParameters):
        """ this is how the CommandTranslator receives its input. output is send to its listeners"""
        if   command == z.API_APPLICATION_COMMAND_HANDLER: self._HandleMessageApplicationCommand(*commandParameters)
        elif command == z.API_ZW_APPLICATION_UPDATE:       self._HandleMessageApplicationUpdate(commandParameters)
        else:
            logging.error("unhandled message: %s %s", command, commandParameters)
