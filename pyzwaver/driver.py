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
driver.py contains the code interacting directly with serial device
"""

import collections
import logging
import queue
import serial
import threading
import time

from pyzwaver.serial_frame import *
from pyzwaver.transaction import Transaction
from pyzwaver import zwave as z


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

    def get(self):
        priority, message = self._q.get()
        prio, count, _ = priority
        if   prio == self.PRIO_HIGH: self._hi_min = count
        elif prio == self.PRIO_LOW: self._lo_min = count
        return message


class Driver(object):
    """
    Driver is responsible for sending and receiving raw
    Z-Wave message to/from a serial Z-Wave device. This includes
    messages for nodes and local communication with the Z-Wave
    device.

    The Driver object encapsulates all transmission related
    logic, i.e., confirmation, timeouts, queueing, etc.

    It only understands serial requests. Higher-level node
    commands are handled elsewhere
    """

    TERMINATE = 0xFFFF

    @staticmethod
    def MakeSerialDevice(port="/dev/ttyUSB0"):
        return serial.Serial(port=port, baudrate=115200, parity=serial.PARITY_NONE, stopbits=serial.STOPBITS_ONE,
                             bytesize=serial.EIGHTBITS, timeout=0.1)

    def __init__(self, serialDevice):
        self.device:serial.Serial = serialDevice

        # This synchronizes writing to the ZWave device. Likely not necessary, since Transactions
        # are also synced, but avoids unlikely race conditions.
        self.writeLock = threading.Lock()

        # Make sure we flush old stuff
        self.writeToDevice(ConfirmationFrame(ConfirmationFrame.FrameType.NAK))
        self.writeToDevice(ConfirmationFrame(ConfirmationFrame.FrameType.NAK))
        self.writeToDevice(ConfirmationFrame(ConfirmationFrame.FrameType.NAK))
        self.device.flushInput()
        self.device.flushOutput()

        self.writeToDevice(ConfirmationFrame(ConfirmationFrame.FrameType.NAK))
        self.writeToDevice(ConfirmationFrame(ConfirmationFrame.FrameType.NAK))
        self.writeToDevice(ConfirmationFrame(ConfirmationFrame.FrameType.NAK))
        self.device.flushInput()
        self.device.flushOutput()

        self._terminate = False  # flag for Threads to shut-down

        # Step 1: Set up CallBackThread to inform listeners of incoming messages
        self.listeners = []                         # callBack listeners for CallBackThread
        self.callBackQueue = queue.Queue()          # requests coming from the stick unrelated to an ongoing transaction to be distributed
        self.callBackThread = threading.Thread(target=self.CallbackThread, name="CallbackThread")
        self.callBackThread.start()

        # Step 2: Start listening to ZWave device
        self.ongoingTransaction:Transaction = None  # singular live transaction being processed
        self.confirmationTimeoutThread = None       # timeout thread related to the singular live transaction
        self.openTransactions = []                  # backlog of transactions that might still received requests

        self.transactionLock = threading.RLock()    # synchronizes any changes to ongoing/open transcations
        self.transactionClearedEvent = threading.Event()   # flags that the live transaction has finished and a new one can be send

        self.deviceProcessingThread = threading.Thread(target=self.DeviceProcessingThread, name="DeviceProcessingThread")
        self.deviceProcessingThread.start()

        # Step 3: Start processing requests to send to stick
        self.newRequestQueue = MessageQueueOut()    # queued requests to be send to the ZWave device
        self.newRequestProcessingThread = threading.Thread(target=self.NewRequestProcessingThread, name="NewRequestProcessingThread")
        self.newRequestProcessingThread.start()


    def addListener(self, l):
        self.listeners.append(l)


    RequestPriority = Enum("Priority", {"HIGHEST": MessageQueueOut.PRIO_HIGHEST, "HIGH_FAIR": MessageQueueOut.PRIO_HIGH, \
                                        "LOW_FAIR": MessageQueueOut.PRIO_LOW, "LOWEST": MessageQueueOut.PRIO_LOWEST})

    def sendRequest(self, command, commandParameters=None, requestPriority=RequestPriority.LOWEST, timeout=0.0, callback=None):
        priority, q = requestPriority, -1
        if type(requestPriority) == tuple: priority, q = requestPriority

        dataFrame = CallbackRequest(command, commandParameters) if Transaction.hasRequests(command) else DataFrame(command, commandParameters)
        self.newRequestQueue.put(priority.value, (dataFrame, timeout, callback), q)


    # Shut down all threads and hence Driver object
    def terminate(self):
        # DeviceProcessingThread is non-blocking and will shut down with this flag
        self._terminate = True

        # send listeners signal to shutdown
        self.callBackQueue.put((self.TERMINATE, None))
        self.newRequestQueue.put(MessageQueueOut.PRIO_HIGHEST, (None, 0, None))

        logging.info("Driver terminated")


    def writeToDevice(self, dataFrame: SerialFrame):
        with self.writeLock:
            logging.info(">>>: %s", dataFrame.toString())
            logging.debug(">>>: [ %s ]", " ".join(["%02x" % i for i in dataFrame.toDeviceData()]))
            self.device.write(dataFrame.toDeviceData())
            self.device.flush()


    # This will handle retransmission of requests if we do not receive an ACK,
    # mostly due to timeouts (called by the Timer object attached to ongoingTransaction),
    # but also when we receive a NAK/CAN
    def confirmationIssueHandler(self, timeout=True):
        with self.transactionLock:
            if not self.ongoingTransaction: return
            if self.ongoingTransaction.status != Transaction.TransactionStatus.WAIT_FOR_CONFIRMATION: return

            if timeout:
                logging.error("X<<: ACK timed out")

            if self.ongoingTransaction.retransmissions == 3:
                logging.error("XXX: Already re-transmitted 3 times - discarding transaction")
                self.ongoingTransaction.transactionTimeoutThread.cancel()
                self.ongoingTransaction.status = Transaction.TransactionStatus.ABORTED
                self.ongoingTransaction = None
                self.transactionClearedEvent.set()
                # TODO: need to call callback, probably via ProcessTimeout
                return

            time.sleep(0.1 + self.ongoingTransaction.retransmissions * 1)
            # TODO: while waiting, the transmission might time out but we're retransmitting one more time anyway...

            logging.info("X>>: Retransmitting request for current transaction (retry %d)", self.ongoingTransaction.retransmissions + 1)

            self.confirmationTimeoutThread = threading.Timer(1.5, self.confirmationIssueHandler)
            self.writeToDevice(self.ongoingTransaction.request)
            self.confirmationTimeoutThread.start()
            self.ongoingTransaction.retransmissions += 1


    # This will cancel a transaction if it times out (called by Timer object attached to
    # each transaction). Any responses/requests related to this transcation received after
    # cancellation are ignored
    def transactionTimeoutHandler(self, transaction):
        with self.transactionLock:
            if transaction.ended(): return   # double-check in case we were stuck on the transactionLock while the transaction ended

            if transaction == self.ongoingTransaction:   # cancel confirmation timer immediately - we only need one timeout
                self.confirmationTimeoutThread.cancel()

            transaction.processTimeout()
            if transaction in self.openTransactions: self.openTransactions.remove(transaction)
            if transaction == self.ongoingTransaction:
                self.ongoingTransaction = None
                self.transactionClearedEvent.set()


    # This Thread manages a queue of new Transactions to be send. It is triggered by the
    # transactionClearedEvent (previous live Transaction finished) and transmits the next one
    def NewRequestProcessingThread(self):
        while not self._terminate:
            dataFrame, timeout, callback = self.newRequestQueue.get()
            if dataFrame is None: break

            with self.transactionLock:
                self.transactionClearedEvent.clear()
                self.ongoingTransaction = Transaction(dataFrame, callback=callback)

                if timeout == 0.0:
                    timeout = 2.0 if not self.ongoingTransaction.hasRequests(dataFrame.serialCommand) else 2.5
                self.confirmationTimeoutThread = threading.Timer(1.5, self.confirmationIssueHandler, [self.ongoingTransaction])
                transactionTimeoutThread = threading.Timer(timeout, self.transactionTimeoutHandler, [self.ongoingTransaction])

                self.ongoingTransaction.start(transactionTimeoutThread)
                self.writeToDevice(self.ongoingTransaction.request)
                self.confirmationTimeoutThread.start()

            self.transactionClearedEvent.wait()

        logging.info("NewRequestProcessingThread terminated")


    # This Thread processes all incoming communication from the ZWave device. It manages all
    # transmission logic (timeouts, sending confirmations, retransmissions) and hands over
    # received messages to the appropriate handler (ongoing Transaction, command_translator, etc)
    def DeviceProcessingThread(self):
        while not self._terminate:
            b = self.device.read()  # this is non-blocking (see MakeSerialDevice)
            if not b: continue
            r = ord(b)  # we store everything as int

            if r not in z.FIRST_TO_STRING: continue   # we're looking for the start of a valid frame (ACK/NAK/CAN/SOF)

            if r == z.SOF:   # we are receiving a data frame - let's read it
                # we are expecting [ SOF, length, data, checksum]

                length = checksum = -1
                data = []

                timestamp = time.time()   # we have 1.5 seconds to read the data frame
                while time.time() - timestamp < 1.5 and checksum == -1 and not self._terminate:
                    b = self.device.read()   # this is non-blocking (see MakeSerialDevice)
                    if not b: continue
                    r = ord(b)  # we store everything as int

                    if length == -1: length = r
                    elif len(data) < length - 1: data.append(r)
                    else: checksum = r

                if self._terminate: continue

                frameData = [z.SOF, length] + data + [checksum]

                # ok, we received a full data frame - let's check it decide what to do with it
                dataFrame = AppCommandFrame.fromDeviceData(frameData)                     # is it a valid AppCommandFrame?
                if not dataFrame: dataFrame = CallbackRequest.fromDeviceData(frameData)   # nope! Is it a valid CallbackRequest?
                if not dataFrame: dataFrame = DataFrame.fromDeviceData(frameData)         # nope! Is it at least a valid generic DataFrame?

                logging.debug("<<<: [ %s ]: %s", " ".join(["%02x" % i for i in frameData]), dataFrame.__class__ if dataFrame else "invalid")

                if not dataFrame:   # nope! This frame is invalid --> let's send a NAK
                    logging.error("X<<: Received invalid frame [ %s ]", " ".join(["%02x" % i for i in frameData]))
                    self.writeToDevice(ConfirmationFrame(ConfirmationFrame.FrameType.NAK))
                    continue

                # good so far - let's send an ACK
                logging.info("<<<: %s", dataFrame.toString())
                self.writeToDevice(ConfirmationFrame(ConfirmationFrame.FrameType.ACK))

                with self.transactionLock:
                    if dataFrame.frameType == DataFrame.FrameType.RESPONSE:
                        if not self.ongoingTransaction or not self.ongoingTransaction.processResponse(dataFrame):
                            # we did not expect a response here
                            logging.warning("X<<: Received non-matching response - ignore")
                            continue

                        if self.ongoingTransaction.ended():
                            # this response has concluded our ongoing transaction
                            self.ongoingTransaction.transactionTimeoutThread.cancel()
                            self.ongoingTransaction = None
                            self.transactionClearedEvent.set()
                        elif self.ongoingTransaction.status == Transaction.TransactionStatus.WAIT_FOR_REQUEST:
                            # we're still expecting follow-on requests and could move this transaction to the
                            # back-book to be able to send a new one already
                            # FIXME: de-activated for now, seems to annoy the ZWave device
                            # self.openTransactions.append(self.ongoingTransaction)
                            # self.ongoingTransaction = None
                            # self.transactionClearedEvent.set()
                            pass

                        continue

                    if dataFrame.frameType == DataFrame.FrameType.REQUEST:

                        # distribute unsolicited requests
                        if dataFrame.serialCommand == z.API_ZW_APPLICATION_UPDATE:
                            # application updates are handed to the listeners (asynchronously)
                            logging.info("===: Received application update: %s", dataFrame.toString())
                            self.callBackQueue.put((dataFrame.serialCommand, dataFrame.serialCommandParameters))
                            continue

                        if dataFrame.serialCommand == z.API_APPLICATION_COMMAND_HANDLER:
                            # application commands are handed to the listeners (asynchronously)
                            if not isinstance(dataFrame, AppCommandFrame):
                                # we couldn't parse this one --> ignore
                                logging.error("==X: Received malformed device-request: %s", dataFrame.toString())
                                continue
                            logging.info("===: Received device-request: %s", dataFrame.toString())
                            self.callBackQueue.put((dataFrame.serialCommand, (dataFrame.srcNode, dataFrame.nodeCommand)))
                            continue

                        # this request should relate to the ongoing or an open back-book transaction, let's find the right one
                        matchingTransaction = None
                        if self.ongoingTransaction and self.ongoingTransaction.processRequest(dataFrame):
                            matchingTransaction = self.ongoingTransaction

                        if not matchingTransaction:  # it's not the ongoing one, let's check other open ones
                            for transaction in self.openTransactions:
                                if transaction.processRequest(dataFrame):
                                    matchingTransaction = transaction
                                    break

                        if not matchingTransaction:  # nope, don't know what this is
                            logging.warning("X<<: Received non-matching request - ignore: %s", dataFrame.toString())
                            continue

                        if matchingTransaction.ended():
                            # this request has concluded a transaction
                            matchingTransaction.transactionTimeoutThread.cancel()   # cancel the timeout thread

                            if self.ongoingTransaction == matchingTransaction:
                                self.ongoingTransaction = None
                                self.transactionClearedEvent.set()
                            else:
                                self.openTransactions.remove(matchingTransaction)

                            continue

                        continue

            else:
                # we received an ACK/NAK/CAN - let's decide what to do with it
                serialFrame = ConfirmationFrame.fromDeviceData(r)
                logging.info("<<<: %s", serialFrame.toString())

                with self.transactionLock:
                    if not self.ongoingTransaction or not self.ongoingTransaction.status == Transaction.TransactionStatus.WAIT_FOR_CONFIRMATION:
                        # we didn't actually expect a confirmation frame - ignore
                        logging.warning("X<<: Received %s without requiring message confirmation - ignore", serialFrame.toString())
                        continue

                    if serialFrame.frameType == serialFrame.FrameType.ACK:
                        # we received an ACK to our message, great --> cancel time-out time and decide what's next
                        self.confirmationTimeoutThread.cancel()
                        self.ongoingTransaction.processACK()

                        if self.ongoingTransaction.ended():
                            # the transactions is already done --> clean-up, ready for next one
                            self.ongoingTransaction.transactionTimeoutThread.cancel()
                            self.ongoingTransaction = None
                            self.transactionClearedEvent.set()

                        elif self.ongoingTransaction.status == Transaction.TransactionStatus.WAIT_FOR_REQUEST:
                            # no response but maybe add'l requests expected --> move transaction to back-book
                            # FIXME: de-activated for now, seems to annoy the ZWave device
                            # self.openTransactions.append(self.ongoingTransaction)
                            # self.ongoingTransaction = None
                            # self.transactionClearedEvent.set()
                            pass
                        continue

                    if serialFrame.frameType == serialFrame.FrameType.NAK or \
                       serialFrame.frameType == serialFrame.FrameType.CAN:
                        # we treat NAK and CAN the same as a confirmation time-out --> retransmit up to 3 times
                        self.confirmationTimeoutThread.cancel()
                        self.confirmationIssueHandler(timeout=False)
                        continue

        logging.info("DeviceProcessingThread terminated")


    # This Thread distributes incoming commands (APPLICATION_COMMAND_HANDLER, APPLICATION_UPDATE) not
    # related to a live transaction to registered listeners (command_translator) for asynchronous processing
    def CallbackThread(self):
        while not self._terminate:
            command, commandParameters = self.callBackQueue.get()
            if command == self.TERMINATE: break

            for listener in self.listeners: listener.put(command, commandParameters)

        logging.info("CallbackThread terminated")
