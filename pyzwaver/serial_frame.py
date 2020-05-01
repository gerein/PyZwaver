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

from enum import Enum

import pyzwaver.zwave as z
from pyzwaver.command import NodeCommand


class SerialFrame:
    @classmethod
    def fromDeviceData(cls, data):
        return None

    def toDeviceData(self):
        return []

    def toString(self):
        return ""


class ConfirmationFrame(SerialFrame):
    FrameType = Enum('FrameType', {'ACK':z.ACK, 'NAK':z.NAK, 'CAN':z.CAN})
    FRAMETYPE_LOOKUP = {frameType.value: frameType for frameType in FrameType}

    def __init__(self, frameType: FrameType):
        self.frameType = frameType

    def parseData(self, data):
        if data not in ConfirmationFrame.FRAMETYPE_LOOKUP: return False
        self.frameType = ConfirmationFrame.FRAMETYPE_LOOKUP[data]
        return True

    @classmethod
    def fromDeviceData(cls, data):
        frame = ConfirmationFrame(None)
        return frame if frame.parseData(data) else None

    def toDeviceData(self):
        return [self.frameType.value]

    def toString(self):
        return self.frameType.name


class DataFrame(SerialFrame):
    FrameType = Enum('FrameType', {'REQUEST':z.REQUEST, 'RESPONSE':z.RESPONSE})
    FRAMETYPE_LOOKUP = {frameType.value: frameType for frameType in FrameType}

    def __init__(self, serialCommand, serialCommandParameters=None):
        if not serialCommandParameters: serialCommandParameters = []
        self.frameType: Enum = DataFrame.FrameType.REQUEST
        self.serialCommand = serialCommand
        self.serialCommandParameters = serialCommandParameters

    def checksum(self, data):
        checksum = 0xff
        for b in data: checksum = checksum ^ b
        return checksum

    def parseData(self, data):
        if len(data) < 5: return False
        if data[0] != z.SOF: return False
        if data[1] != len(data) - 2: return False
        if data[2] not in DataFrame.FRAMETYPE_LOOKUP: return False
        if data[3] not in z.API_TO_STRING: return False
        if data[-1] != self.checksum(data[1:-1]): return False
        self.frameType = DataFrame.FRAMETYPE_LOOKUP[data[2]]
        self.serialCommand = data[3]
        self.serialCommandParameters = data[4:-1]
        return True

    @classmethod
    def fromDeviceData(cls, data):
        frame = cls(None)
        return frame if frame.parseData(data) else None

    def toDeviceData(self):
        out = [len(self.serialCommandParameters) + 3, self.frameType.value, self.serialCommand] + self.serialCommandParameters
        return [z.SOF] + out + [self.checksum(out)]

    def outPrefix(self):
        return self.frameType.name + " " + z.API_TO_STRING[self.serialCommand]

    def toString(self):
        return self.outPrefix() + " [ " + " ".join(["%02x" % i for i in self.serialCommandParameters]) + " ]"


class CallbackRequest(DataFrame):
    CALLBACK_ID = 0

    @staticmethod
    def createCallbackId():
        CallbackRequest.CALLBACK_ID = (CallbackRequest.CALLBACK_ID + 1) % 256
        return CallbackRequest.CALLBACK_ID

    def __init__(self, serialCommand, serialCommandParameters=None):
        super().__init__(serialCommand, serialCommandParameters)
        self.commandParameters = serialCommandParameters
        self.callbackId = CallbackRequest.createCallbackId()

    def parseData(self, data):
        if not super().parseData(data): return False
        if self.frameType != DataFrame.FrameType.REQUEST: return False
        if not self.serialCommandParameters: return False
        #if not Transaction.hasRequests(self.serialCommand): return False
        self.callbackId = self.serialCommandParameters[0]
        self.commandParameters = self.serialCommandParameters[1:]
        return True

    @classmethod
    def fromDeviceData(cls, data):
        frame = CallbackRequest(None)
        return frame if frame.parseData(data) else None

    def toDeviceData(self):
        out = [len(self.commandParameters) + 4, self.frameType.value, self.serialCommand] + self.commandParameters + [self.callbackId]
        return [z.SOF] + out + [self.checksum(out)]

    def outPrefix(self):
        return super().outPrefix() + " (callback:%02x" % self.callbackId + ")"

    def toString(self):
        return self.outPrefix() + " [ " + " ".join(["%02x" % i for i in self.commandParameters]) + " ]"


class AppCommandFrame(DataFrame):

    def parseData(self, data):
        if not super().parseData(data): return False
        if self.frameType != DataFrame.FrameType.REQUEST: return False
        if self.serialCommand != z.API_APPLICATION_COMMAND_HANDLER: return False
        if len(self.serialCommandParameters) < 5: return False
        if len(self.serialCommandParameters) < self.serialCommandParameters[2] + 3: return False
        self.rxStatus = self.serialCommandParameters[0]
        self.srcNode = self.serialCommandParameters[1]
        self.nodeCommand = NodeCommand.fromDeviceData(self.serialCommandParameters[3:])
        if self.nodeCommand is None: return False

        if self.nodeCommand.endpoint is not None:
            self.srcNode = (self.srcNode << 8) + self.nodeCommand.endPoint

        # We're ignoring rxSSIVal & securitykey
        return True

    def toDeviceData(self):
        assert False, "AppCommandFrame cannot be written to device"

    def toString(self):
        return super().outPrefix() + " (RX:%02x" % self.rxStatus + ") SrcNode:%02x " % self.srcNode + \
               z.SUBCMD_TO_STRING[(self.nodeCommand.command[0] << 8) + self.nodeCommand.command[1]] + " " + str(self.nodeCommand.commandValues)

