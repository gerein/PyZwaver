from enum import Enum

from pyzwaver import zwave as z
from pyzwaver.serial_frame import DataFrame


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


class Transaction:
    TransactionStatus = Enum('TransactionStatus', 'CREATED WAIT_FOR_CONFIRMATION WAIT_FOR_RESPONSE WAIT_FOR_REQUEST COMPLETED TIMED_OUT ABORTED')
    TRANSACTION_ENDED  = [TransactionStatus.COMPLETED, TransactionStatus.TIMED_OUT, TransactionStatus.ABORTED]
    TRANSACTION_FAILED = [TransactionStatus.TIMED_OUT, TransactionStatus.ABORTED]

    CallbackReason = Enum('CallbackReason', 'REQUEST_SENT RESPONSE_RECEIVED REQUEST_RECEIVED TIMED_OUT')

    def __init__(self, dataFrame:DataFrame, callback=None):
        self.request = dataFrame
        self.callback = callback
        self.status = self.TransactionStatus.CREATED

        # TODO: these are transmission logic and should be moved to driver...
        self.retransmissions = 0
        self.transactionTimeoutThread = None


    def start(self, transactionTimeoutThread):
        self.status = self.TransactionStatus.WAIT_FOR_CONFIRMATION
        self.transactionTimeoutThread = transactionTimeoutThread
        self.transactionTimeoutThread.start()

    @staticmethod
    def hasRequests(serialCommand):
        if serialCommand in COMMAND_RES_REQ_MULTIREQ:
            return COMMAND_RES_REQ_MULTIREQ[serialCommand][1]
        return False

    def ended(self):  return self.status in Transaction.TRANSACTION_ENDED
    def failed(self): return self.status in Transaction.TRANSACTION_FAILED

    def processACK(self):
        if self.status != self.TransactionStatus.WAIT_FOR_CONFIRMATION: return False

        if COMMAND_RES_REQ_MULTIREQ[self.request.serialCommand][0]:
            self.status = self.TransactionStatus.WAIT_FOR_RESPONSE
        else:
            self.status = self.TransactionStatus.COMPLETED
            if self.callback: self.callback(self.CallbackReason.REQUEST_SENT, [])

        return True

    def processResponse(self, response:DataFrame):
        if self.status != self.TransactionStatus.WAIT_FOR_RESPONSE: return False

        if COMMAND_RES_REQ_MULTIREQ[self.request.serialCommand][1]:
            self.status = self.TransactionStatus.WAIT_FOR_REQUEST
        else:
            self.status = self.TransactionStatus.COMPLETED

        if self.callback: self.callback(self.CallbackReason.RESPONSE_RECEIVED, response.serialCommandParameters)   # FIXME: make asynchronous

        return True

    def processRequest(self, request):
        if self.status != self.TransactionStatus.WAIT_FOR_REQUEST: return False
        if not self.hasRequests(self.request.serialCommand): return False

        if self.request.callbackId != request.callbackId: return False

        if self.callback:
            more = self.callback(self.CallbackReason.REQUEST_RECEIVED, request.commandParameters)
            if not COMMAND_RES_REQ_MULTIREQ[self.request.serialCommand][2] or not more:
                self.status = self.TransactionStatus.COMPLETED
        else:
            self.status = self.TransactionStatus.COMPLETED

        return True

    def processTimeout(self):
        self.status = Transaction.TransactionStatus.TIMED_OUT
        if self.callback: self.callback(self.CallbackReason.TIMED_OUT, None)
