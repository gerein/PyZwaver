from enum import Enum

import pyzwaver.zwave as z
import pyzwaver.command

# FIXME: this shouldn't be copied here from transaction
# TODO: find a more elegant way to determine what frame we should be expecting
COMMAND_RES_REQ_MULTIREQ = {
    z.API_SERIAL_API_APPL_NODE_INFORMATION: (False, False, False),
    z.API_SERIAL_API_GET_CAPABILITIES:      (True,  False, False),
    z.API_SERIAL_API_GET_INIT_DATA:         (True,  False, False),
    z.API_SERIAL_API_SET_TIMEOUTS:          (True,  False, False),
    z.API_SERIAL_API_SOFT_RESET:            (True,  False, False),
    z.API_ZW_ADD_NODE_TO_NETWORK:           (False, True,  True ),
    z.API_ZW_CONTROLLER_CHANGE:             (False, True,  True ),
    z.API_ZW_ENABLE_SUC:                    (True,  False, False),
    z.API_ZW_GET_CONTROLLER_CAPABILITIES:   (True,  False, False),
    z.API_ZW_GET_NODE_PROTOCOL_INFO:        (True,  False, False),
    z.API_ZW_GET_RANDOM:                    (True,  False, False),
    z.API_ZW_GET_ROUTING_INFO:              (True,  False, False),
    z.API_ZW_GET_SUC_NODE_ID:               (True,  False, False),
    z.API_ZW_GET_VERSION:                   (True,  False, False),
    z.API_ZW_IS_FAILED_NODE_ID:             (True,  False, False),
    z.API_ZW_MEMORY_GET_ID:                 (True,  False, False),
    z.API_ZW_READ_MEMORY:                   (True,  False, False),
    z.API_ZW_REMOVE_FAILED_NODE_ID:         (True,  True,  False),
    z.API_ZW_REMOVE_NODE_FROM_NETWORK:      (False, True,  True ),
    z.API_ZW_REPLICATION_SEND_DATA:         (True,  True,  False),
    z.API_ZW_REQUEST_NODE_INFO:             (True,  False, False),
    z.API_ZW_REQUEST_NODE_NEIGHBOR_UPDATE:  (False, True,  True ),
    z.API_ZW_SEND_DATA:                     (True,  True,  False),
    z.API_ZW_SEND_DATA_MULTI:               (True,  True,  False),
    z.API_ZW_SEND_NODE_INFORMATION:         (True,  True,  False),
    z.API_ZW_SET_DEFAULT:                   (False, True,  False),
    z.API_ZW_SET_LEARN_MODE:                (False, True,  True ),
    z.API_ZW_SET_PROMISCUOUS_MODE:          (False, False, False),
    z.API_ZW_SET_SUC_NODE_ID:               (True,  False, False)
}


class SerialFrame:
    def __init__(self, data):
        self.data = data

    def parseData(self, data):
        return True

    @classmethod
    def fromDeviceData(cls, data):
        return SerialFrame(data)

    def toDeviceData(self):
        return self.data

    def toString(self):
        return ["%02x" % i for i in self.data]


class ConfirmationFrame(SerialFrame):
    FrameType = Enum('FrameType', {'ACK':z.ACK, 'NAK':z.NAK, 'CAN':z.CAN})
    FRAMETYPE_LOOKUP = {frameType.value: frameType for frameType in FrameType}

    def __init__(self, frameType: FrameType):
        super().__init__([frameType.value])
        self.frameType = frameType

    def parseData(self, data):
        if data not in ConfirmationFrame.FRAMETYPE_LOOKUP: return False
        self.frameType = ConfirmationFrame.FRAMETYPE_LOOKUP[data]
        return True

    @classmethod
    def fromDeviceData(cls, data):
        frame = ConfirmationFrame(ConfirmationFrame.FrameType.ACK)
        return frame if frame.parseData(data) else None

    def toString(self):
        return self.frameType.name


class DataFrame(SerialFrame):
    FrameType = Enum('FrameType', {'REQUEST':z.REQUEST, 'RESPONSE':z.RESPONSE})
    FRAMETYPE_LOOKUP = {frameType.value: frameType for frameType in FrameType}

    def __init__(self, serialCommand, serialCommandParameters=None):
        if not serialCommandParameters: serialCommandParameters = []
        super().__init__([DataFrame.FrameType.REQUEST.value, serialCommand] + serialCommandParameters)
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
        frame = DataFrame(None)
        if not frame.parseData(data): return None
        return frame

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

    def __init__(self, serialCommand, serialCommandParameters=None, callbackId=None):
        super().__init__(serialCommand, serialCommandParameters)
        self.callbackId = callbackId if callbackId else CallbackRequest.createCallbackId()

    def parseData(self, data):
        if not super().parseData(data): return False
        if self.frameType != DataFrame.FrameType.REQUEST: return False
        if not self.serialCommandParameters: return False
        if self.serialCommand not in COMMAND_RES_REQ_MULTIREQ or not COMMAND_RES_REQ_MULTIREQ[self.serialCommand][1]: return False
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


class NodeCommandFrame(CallbackRequest):
    TXoptions = Enum('TXoptions', {'ACK': z.TRANSMIT_OPTION_ACK, 'AUTO_ROUTE': z.TRANSMIT_OPTION_AUTO_ROUTE, 'EXPLORE': z.TRANSMIT_OPTION_EXPLORE, 'LOW_POWER': z.TRANSMIT_OPTION_EXPLORE, 'NO_ROUTE': z.TRANSMIT_OPTION_NO_ROUTE})
    standardTX = (TXoptions.ACK, TXoptions.AUTO_ROUTE, TXoptions.EXPLORE)

    def __init__(self, nodes, nodeCommand: tuple, nodeCommandValues: dict, txOptions:tuple=standardTX):
        super().__init__(z.API_ZW_SEND_DATA)
        self.nodes = nodes if isinstance(nodes, list) else [nodes]
        if len(self.nodes) > 1: self.serialCommand = z.API_ZW_SEND_DATA_MULTI
        self.nodeCommand = nodeCommand
        self.nodeCommandValues = nodeCommandValues
        self.txOptions = txOptions

    def parseData(self, data):
        if not super(CallbackRequest, self).parseData(data): return False
        if self.frameType != DataFrame.FrameType.REQUEST: return False
        if self.serialCommand != z.API_APPLICATION_COMMAND_HANDLER: return False
        if len(self.serialCommandParameters) < 5: return False
        if len(self.serialCommandParameters) < self.serialCommandParameters[2] + 3: return False
        self.rxStatus = self.serialCommandParameters[0]
        self.nodes = [self.serialCommandParameters[1]]
        self.nodeCommand = (self.serialCommandParameters[3], self.serialCommandParameters[4])
        self.nodeCommandParameters = self.serialCommandParameters[5:3 + self.serialCommandParameters[2]]

        if self.nodeCommand == z.MultiChannel_CmdEncap:
            self.nodes = [(self.nodes[0] << 8) + self.nodeCommandParameters[0]]
            self.nodeCommand = (self.nodeCommandParameters[2], self.nodeCommandParameters[3])
            self.nodeCommandParameters = self.nodeCommandParameters[4:]

        self.nodeCommandValues = pyzwaver.command.ParseCommand(self.nodeCommand, self.nodeCommandParameters)
        if self.nodeCommandValues is None: return False

        # We're ignoring rxSSIVal & securitykey
        return True

    @classmethod
    def fromDeviceData(cls, data):
        frame = NodeCommandFrame(None, (), {})
        return frame if frame.parseData(data) else None

    def toDeviceData(self):
        nodeListData = [n if n <= 255 else n >> 8 for n in self.nodes]
        if len(nodeListData) > 1: nodeListData = [len(nodeListData)] + nodeListData
        nodeCommandData = pyzwaver.command.AssembleCommand(self.nodeCommand, self.nodeCommandValues) # FIXME: we're ignoring potential ValueError here
        if len(self.nodes) == 1 and self.nodes[0] > 255:
            nodeCommandData = list(z.MultiChannel_CmdEncap) + [0, self.nodes[0] & 0xff] + nodeCommandData
        length = len(nodeListData) + len(nodeCommandData) + 6
        txData = 0
        for tx in self.txOptions: txData |= tx.value
        out = [length, self.frameType.value, self.serialCommand] + nodeListData + [len(nodeCommandData)] + nodeCommandData + [txData, self.callbackId]
        return [z.SOF] + out + [self.checksum(out)]

    def toString(self):
        txData = 0
        for tx in self.txOptions: txData |= tx.value
        txrx = "(TX:%02x" % txData + ")" if self.serialCommand != z.API_APPLICATION_COMMAND_HANDLER else \
               "(RX:%02x" % self.rxStatus + ")"
        nodesOut = "SrcNode: %02x " % self.nodes[0] if self.serialCommand == z.API_APPLICATION_COMMAND_HANDLER else \
                   "DstNodes:[" + " ".join(["%02x" % n for n in self.nodes]) + "]"
        prefix = super(CallbackRequest, self).outPrefix() if self.serialCommand == z.API_APPLICATION_COMMAND_HANDLER else \
                 super().outPrefix()
        return prefix + " " + txrx + " " + nodesOut + " " + z.SUBCMD_TO_STRING[(self.nodeCommand[0] << 8) + self.nodeCommand[1]] + " " + \
               str(self.nodeCommandValues)

