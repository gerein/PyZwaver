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
import threading
import time
import queue
import collections

from .serial_frame import ConfirmationFrame
from .serial_frame import DataFrame
from . import zwave as z


class ProcessingThread(threading.Thread):
    def __init__(self, frequency=0):
        super().__init__()
        self.frequency = frequency
        self.shutdownFlag = threading.Event()
        self.lastIsAliveTimestamp = time.time()

    def clean_up(self): pass
    def get_data(self): return None
    def process_data(self, data): pass

    def shutdown(self):
        self.shutdownFlag.set()
        if self.is_alive(): self.join()

    def run(self):
        logging.info("Starting thread '" + self.__class__.__name__ + "'")
        while not self.shutdownFlag.is_set():
            #REMOVEME print("Is-Alive: " + self.__class__.__name__)

            if time.time() - self.lastIsAliveTimestamp > 60 * 60:
                logging.info("Is-Alive ping: thread '" + self.__class__.__name__ + "'")
                self.lastIsAliveTimestamp = time.time()

            try:
                data = self.get_data()
                if isinstance(data, bytes) and len(data) == 0: data = None
            except:
                data = None

            if data is not None:
                self.process_data(data)
            else:
                if self.frequency > 0: self.shutdownFlag.wait(self.frequency)
        self.clean_up()
        logging.info("Stopped thread '" + self.__class__.__name__ + "'")


class MessageQueueOut:
    """
    MessageQueue for outbound messages. Tries to support
    priorities and fairness.
    """
    PRIO_HIGHEST = 1
    PRIO_HIGH    = 2
    PRIO_LOW     = 3
    PRIO_LOWEST  = 1000

    def __init__(self):
        self._q = queue.PriorityQueue()
        self._lo_counts = collections.defaultdict(int)
        self._hi_counts = collections.defaultdict(int)
        self._lo_min = 0
        self._hi_min = 0
        self._counter = 0

    def put(self, prio, queueObject, q=-1):
        if self._q.empty():
            self._lo_counts = collections.defaultdict(int)
            self._hi_counts = collections.defaultdict(int)
            self._lo_min = 0
            self._hi_min = 0

        if prio == self.PRIO_HIGH:
            count = max(self._hi_counts[q] + 1, self._hi_min)
            self._hi_counts[q] = count
        elif prio == self.PRIO_LOW:
            count = max(self._lo_counts[q] + 1, self._lo_min)
            self._lo_counts[q] = count
        else:
            count = self._counter
            self._counter += 1

        self._q.put(((prio, count, q), queueObject))

    def get(self, block=True):
        priority, message = self._q.get(block=block)
        prio, count, _ = priority
        if   prio == self.PRIO_HIGH: self._hi_min = count
        elif prio == self.PRIO_LOW:  self._lo_min = count
        return message


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


class TransactionProcessor(ProcessingThread):
    TransactionStatus = Enum('TransactionStatus', 'CREATED WAIT_FOR_CONFIRMATION WAIT_FOR_RESPONSE WAIT_FOR_REQUEST COMPLETED TIMED_OUT ABORTED')
    TRANSACTION_ENDED  = [TransactionStatus.COMPLETED, TransactionStatus.TIMED_OUT, TransactionStatus.ABORTED]
    TRANSACTION_FAILED = [TransactionStatus.TIMED_OUT, TransactionStatus.ABORTED]

    CallbackReason = Enum('CallbackReason', 'REQUEST_SENT RESPONSE_RECEIVED REQUEST_RECEIVED TIMED_OUT')

    def __init__(self, driver, frequency=0.5):
        super().__init__(frequency)
        self.driver = driver

        self.requestQueue = MessageQueueOut()       # queued requests to be send to the ZWave device

        self.noLiveTransaction = threading.Event()
        self.noLiveTransaction.set()
        self.transactionId = 0
        self.transactionRequest = None
        self.transactionCallback = None
        self.transactionStatus = None
        self.transactionRetransmissions = 0

#        self.ongoingTransaction = None              # singular live transaction being processed
        self.confirmationTimeoutThread = None       # confirmation receipt timeout thread
        self.transactionTimeoutThread = None        # overall single transaction timeout thread

        self.transactionLock = threading.RLock()    # synchronizes any changes to ongoing/open transactions
#        self.transactionClearedEvent = threading.Event()   # flags that the live transaction has finished and a new one can be send

    @staticmethod
    def hasRequests(serialCommand):
        return serialCommand in COMMAND_RES_REQ_MULTIREQ and COMMAND_RES_REQ_MULTIREQ[serialCommand][1]

    # This will handle retransmission of requests if we do not receive an ACK,
    # mostly due to timeouts (called by the Timer object attached to ongoingTransaction),
    # but also when we receive a NAK/CAN
    def confirmationIssueHandler(self, transactionId, timeout=True):
        with self.transactionLock:
            # break if we have a stray timer
            if self.noLiveTransaction.is_set(): return
            if not self.transactionId == transactionId: return

            # break if we're not actually waiting for a confirmation anymore
            if self.transactionStatus != TransactionProcessor.TransactionStatus.WAIT_FOR_CONFIRMATION: return

            if timeout:
                logging.error("X<<: ACK timed out")

            # have we tried re-transmitting already?
            if self.transactionRetransmissions == 3:
                # cancel confirmation timer immediately - we only need one timeout
                self.transactionTimeoutThread.cancel()

                self.transactionStatus = TransactionProcessor.TransactionStatus.ABORTED
                logging.error("XXX: Already re-transmitted 3 times - discarding transaction")
                # TODO: need to call callback

                self.noLiveTransaction.set()
                return

            time.sleep(0.1 + self.transactionRetransmissions * 1)
            # TODO: while waiting, the transmission might time out but we're retransmitting one more time anyway...

            logging.info("X>>: Retransmitting request for current transaction (retry %d)", self.transactionRetransmissions + 1)

            self.confirmationTimeoutThread = threading.Timer(1.5, self.confirmationIssueHandler, [self.transactionId])
            self.driver.writeToDevice(DataFrame(self.transactionRequest))
            self.confirmationTimeoutThread.start()

            self.transactionRetransmissions += 1


    # This will cancel a transaction if it times out (called by Timer object attached to
    # each transaction). Any responses/requests related to this transaction received after
    # cancellation are ignored
    def transactionTimeoutHandler(self, transactionId):
        with self.transactionLock:
            # break if we have a stray timer
            if self.noLiveTransaction.is_set(): return
            if not self.transactionId == transactionId: return

            # break if transaction already ended (e.g., in case we were stuck on the
            # transactionLock while the transaction ended)
            if self.transactionStatus in TransactionProcessor.TRANSACTION_ENDED: return

            # cancel confirmation timer immediately - we only need one timeout
            self.confirmationTimeoutThread.cancel()

            self.status = TransactionProcessor.TransactionStatus.TIMED_OUT
            logging.error("XXX: Transaction timed out: %s", self.transactionRequest.toString())
            if self.transactionCallback: self.transactionCallback(self.CallbackReason.TIMED_OUT, None)

            self.noLiveTransaction.set()

    def get_data(self): return self.requestQueue.get(block=False)

    def process_data(self, data):
        serialRequest, timeout, callback = data

        if not self.noLiveTransaction.is_set():
            # we have a new outgoing request but need to wait for the previous transaction to finish
            self.noLiveTransaction.wait()
            # slowing down send-rate a bit
            time.sleep(0.2)

        with self.transactionLock:
            self.noLiveTransaction.clear()

            # create new transaction
            self.transactionId = (self.transactionId + 1) % 10000
            self.transactionRequest = serialRequest
            self.transactionCallback = callback
            self.transactionStatus = TransactionProcessor.TransactionStatus.CREATED
            self.transactionRetransmissions = 0
            self.transactionTimeoutThread = threading.Timer(timeout, self.transactionTimeoutHandler, [self.transactionId])
            self.confirmationTimeoutThread = threading.Timer(1.5, self.confirmationIssueHandler, [self.transactionId])

            self.driver.writeToDevice(DataFrame(self.transactionRequest))

            self.transactionStatus = TransactionProcessor.TransactionStatus.WAIT_FOR_CONFIRMATION
            self.confirmationTimeoutThread.start()
            self.transactionTimeoutThread.start()

            logging.info("==>: Transaction started %s", self.transactionRequest.toString())


    def addRequest(self, priorityValue, requestObject, q):
        self.requestQueue.put(priorityValue, requestObject, q)


    def processIncomingFrame(self, frame):
        if isinstance(frame, DataFrame) and frame.frameType == DataFrame.FrameType.REQUEST:
            if  frame.serialRequest.serialCommand == z.API_ZW_APPLICATION_UPDATE or \
                frame.serialRequest.serialCommand == z.API_APPLICATION_COMMAND_HANDLER:
                # we're not handling application updates --> reject
                return False

        with self.transactionLock:
            if self.noLiveTransaction.is_set():
                # we're not expecting anything --> reject
                return False

            if isinstance(frame, ConfirmationFrame):
                if not self.transactionStatus == TransactionProcessor.TransactionStatus.WAIT_FOR_CONFIRMATION:
                    # we're not expecting a confirmation --> reject
                    return False

                # we received a confirmation, so can cancel the confirmation time-out timer
                self.confirmationTimeoutThread.cancel()

                if frame.frameType == ConfirmationFrame.FrameType.ACK:
                    # we received an ACK to our message, great --> let's decide what's next

                    if COMMAND_RES_REQ_MULTIREQ[self.transactionRequest.serialCommand][0]:
                        self.transactionStatus = TransactionProcessor.TransactionStatus.WAIT_FOR_RESPONSE
                    elif COMMAND_RES_REQ_MULTIREQ[self.transactionRequest.serialCommand][1]:
                        self.transactionStatus = TransactionProcessor.TransactionStatus.WAIT_FOR_REQUEST
                    else:
                        # the transactions is already done --> clean-up, ready for next one
                        self.transactionStatus = self.TransactionStatus.COMPLETED
                        self.transactionTimeoutThread.cancel()
                        logging.info("<==: Transaction completed %s", self.transactionRequest.toString())

                        if self.transactionCallback: self.transactionCallback(self.CallbackReason.REQUEST_SENT, None)

                        self.noLiveTransaction.set()

                else:
                    # we received a NAK or a CAN; we treat both the same, i.e.,
                    # as a confirmation time-out --> retransmit up to 3 times
                    self.confirmationIssueHandler(self.transactionId, timeout=False)

            elif frame.frameType == DataFrame.FrameType.RESPONSE:
                if  self.transactionStatus != TransactionProcessor.TransactionStatus.WAIT_FOR_RESPONSE or \
                    self.transactionRequest.serialCommand != frame.serialRequest.serialCommand:
                    # we're not expecting a response or command doesn't match --> reject
                    return False

                if COMMAND_RES_REQ_MULTIREQ[self.transactionRequest.serialCommand][1]:
                    self.transactionStatus = self.TransactionStatus.WAIT_FOR_REQUEST
                else:
                    # the transactions is already done --> clean-up, ready for next one
                    self.transactionStatus = self.TransactionStatus.COMPLETED
                    self.transactionTimeoutThread.cancel()
                    logging.info("<==: Transaction completed %s", self.transactionRequest.toString())

                    if self.transactionCallback: self.transactionCallback(self.CallbackReason.RESPONSE_RECEIVED, frame.serialRequest.serialCommandValues)

                    self.noLiveTransaction.set()

            elif frame.frameType == DataFrame.FrameType.REQUEST:
                if  self.transactionStatus != TransactionProcessor.TransactionStatus.WAIT_FOR_REQUEST or \
                    self.transactionRequest.serialCommand != frame.serialRequest.serialCommand or \
                    self.transactionRequest.serialCommandValues["callback"] != frame.serialRequest.serialCommandValues.get("callback"):
                    # we're not expecting a request or command/callback doesn't match --> reject
                    return False

                more = False
                if self.transactionCallback:
                    more = not self.transactionCallback(self.CallbackReason.REQUEST_RECEIVED, frame.serialRequest.serialCommandValues)
                    more = more and COMMAND_RES_REQ_MULTIREQ[self.transactionRequest.serialCommand][2]

                if not more:
                    # the transactions is already done --> clean-up, ready for next one
                    self.transactionStatus = self.TransactionStatus.COMPLETED
                    self.transactionTimeoutThread.cancel()
                    logging.info("<==: Transaction completed %s", self.transactionRequest.toString())

                    self.noLiveTransaction.set()

            return True
