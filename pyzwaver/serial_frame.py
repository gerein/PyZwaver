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
from pyzwaver.command import SerialRequest


class SerialFrame:
    def parseData(self, data):
        return False

    @classmethod
    def fromDeviceData(cls, data):
        frame = cls()
        return frame if frame.parseData(data) else None

    def toDeviceData(self):
        return []

    def toString(self):
        return ""


class ConfirmationFrame(SerialFrame):
    FrameType = Enum('FrameType', {'ACK':z.ACK, 'NAK':z.NAK, 'CAN':z.CAN})
    FRAMETYPE_LOOKUP = {frameType.value: frameType for frameType in FrameType}

    def __init__(self, frameType:FrameType=None):
        self.frameType = frameType

    def parseData(self, data):
        if data not in ConfirmationFrame.FRAMETYPE_LOOKUP: return False
        self.frameType = ConfirmationFrame.FRAMETYPE_LOOKUP[data]
        return True

    def toDeviceData(self):
        return [self.frameType.value]

    def toString(self):
        return self.frameType.name


class DataFrame(SerialFrame):
    FrameType = Enum('FrameType', {'REQUEST':z.REQUEST, 'RESPONSE':z.RESPONSE})
    FRAMETYPE_LOOKUP = {frameType.value: frameType for frameType in FrameType}

    def __init__(self, serialRequest:SerialRequest=None):
        self.frameType: Enum = DataFrame.FrameType.REQUEST
        self.serialRequest = serialRequest

    def checksum(self, data):
        checksum = 0xff
        for b in data: checksum = checksum ^ b
        return checksum

    def parseData(self, data):
        if len(data) < 5: return False
        if data[0] != z.SOF: return False
        if data[1] != len(data) - 2: return False
        if data[-1] != self.checksum(data[1:-1]): return False

        self.frameType = DataFrame.FRAMETYPE_LOOKUP.get(data[2])
        if self.frameType is None: return False

        self.serialRequest = SerialRequest.fromDeviceData(data[2:-1])
        if self.serialRequest is None: return False
        return True

    def toDeviceData(self):
        serialRequestOut = self.serialRequest.toDeviceData()
        if serialRequestOut is None:   # REMOVEME
            print("XXX: serialrequest not serializable: " + str(self.serialRequest.serialCommand) + ": " + str(self.serialRequest.serialCommandValues))
        if serialRequestOut is None: return None

        out = [len(serialRequestOut) + 2, self.frameType.value] + serialRequestOut
        return [z.SOF] + out + [self.checksum(out)]

    def toString(self):
        return self.frameType.name + " " + self.serialRequest.toString()

