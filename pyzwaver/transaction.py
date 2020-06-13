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

import logging

from pyzwaver import zwave as z
from pyzwaver.command import SerialRequest


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

# This class handles all state transition and callback logic for request/response/request transactions
# It does not handle any transmission logic (timeouts, confirmations, etc)
class Transaction:
    TransactionStatus = Enum('TransactionStatus', 'CREATED WAIT_FOR_CONFIRMATION WAIT_FOR_RESPONSE WAIT_FOR_REQUEST COMPLETED TIMED_OUT ABORTED')
    TRANSACTION_ENDED  = [TransactionStatus.COMPLETED, TransactionStatus.TIMED_OUT, TransactionStatus.ABORTED]
    TRANSACTION_FAILED = [TransactionStatus.TIMED_OUT, TransactionStatus.ABORTED]

    CallbackReason = Enum('CallbackReason', 'REQUEST_SENT RESPONSE_RECEIVED REQUEST_RECEIVED TIMED_OUT')

    def __init__(self, serialRequest:SerialRequest, callback=None):
        self.serialRequest = serialRequest
        self.callback = callback
        self.status = self.TransactionStatus.CREATED

        # TODO: these are transmission logic and should be moved to driver...
        self.retransmissions = 0


    def start(self):
        self.status = self.TransactionStatus.WAIT_FOR_CONFIRMATION
        logging.info("==>: Transaction started %s", self.serialRequest.toString())

    @staticmethod
    def hasRequests(serialCommand):
        if serialCommand in COMMAND_RES_REQ_MULTIREQ:
            return COMMAND_RES_REQ_MULTIREQ[serialCommand][1]
        return False

    def ended(self):  return self.status in Transaction.TRANSACTION_ENDED
    def failed(self): return self.status in Transaction.TRANSACTION_FAILED

    def processACK(self):
        if self.status != self.TransactionStatus.WAIT_FOR_CONFIRMATION: return False

        if COMMAND_RES_REQ_MULTIREQ[self.serialRequest.serialCommand][0]:
            self.status = self.TransactionStatus.WAIT_FOR_RESPONSE
        else:
            self.status = self.TransactionStatus.COMPLETED
            logging.info("<==: Transaction completed %s", self.serialRequest.toString())
            if self.callback: self.callback(self.CallbackReason.REQUEST_SENT, None)

        return True

    def processResponse(self, response:SerialRequest):
        if self.status != self.TransactionStatus.WAIT_FOR_RESPONSE: return False
        if self.serialRequest.serialCommand != response.serialCommand: return False

        if COMMAND_RES_REQ_MULTIREQ[self.serialRequest.serialCommand][1]:
            self.status = self.TransactionStatus.WAIT_FOR_REQUEST
        else:
            self.status = self.TransactionStatus.COMPLETED
            logging.info("<==: Transaction completed %s", self.serialRequest.toString())

        if self.callback: self.callback(self.CallbackReason.RESPONSE_RECEIVED, response.serialCommandValues)

        return True

    def processRequest(self, request:SerialRequest):
        if self.status != self.TransactionStatus.WAIT_FOR_REQUEST: return False
        if self.serialRequest.serialCommand != request.serialCommand: return False

        if not self.hasRequests(self.serialRequest.serialCommand): return False
        if self.serialRequest.serialCommandValues["callback"] != request.serialCommandValues.get("callback"): return False

        more = False
        if self.callback:
            more = self.callback(self.CallbackReason.REQUEST_RECEIVED, request.serialCommandValues)
            more = more and COMMAND_RES_REQ_MULTIREQ[self.serialRequest.serialCommand][2]

        if not more:
            self.status = self.TransactionStatus.COMPLETED
            logging.info("<==: Transaction completed %s", self.serialRequest.toString())

        return True

    def processTimeout(self):
        self.status = Transaction.TransactionStatus.TIMED_OUT
        logging.error("XXX: Transaction timed out: %s", self.serialRequest.toString())
        if self.callback: self.callback(self.CallbackReason.TIMED_OUT, None)
