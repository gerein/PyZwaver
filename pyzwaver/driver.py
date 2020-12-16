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

import logging
import queue
import serial
import threading
import time

from enum import Enum

from .serial_frame import SerialFrame
from .serial_frame import ConfirmationFrame
from .serial_frame import DataFrame
from .command import SerialRequest
from .transactionProcessor import ProcessingThread
from .transactionProcessor import TransactionProcessor
from .transactionProcessor import MessageQueueOut
from . import zwave as z


# This processor distributes incoming commands (APPLICATION_COMMAND_HANDLER, APPLICATION_UPDATE) not
# related to a live transaction to registered listeners for asynchronous processing
class IncomingRequestProcessor(ProcessingThread):
    def __init__(self, frequency=0.2):
        super().__init__(frequency)
        self.requestQueue = queue.Queue()
        self.listeners = []

    def addListener(self, listener): self.listeners.append(listener)

    def get_data(self): return self.requestQueue.get(block=False)

    def process_data(self, serialRequest):
        for listener in self.listeners: listener.put(serialRequest)

    def processIncomingFrame(self, frame):
        if  not isinstance(frame, DataFrame) or \
            not frame.frameType == DataFrame.FrameType.REQUEST or \
            not (frame.serialRequest.serialCommand == z.API_ZW_APPLICATION_UPDATE or
                 frame.serialRequest.serialCommand == z.API_APPLICATION_COMMAND_HANDLER):
            # we're only handling application updates --> reject
            return False

        self.requestQueue.put(frame.serialRequest)
        return True


# This Thread processes all incoming communication from the ZWave device. It manages all
# transmission logic (timeouts, sending confirmations, retransmissions) and hands over
# received messages to the appropriate handler (ongoing Transaction, command_translator, etc)
class DeviceReader(ProcessingThread):
    def __init__(self, driver, frequency=0.1):
        super().__init__(frequency)
        self.driver = driver
        self.currentFrame = None
        self.currentFrameTimestamp = 0

    def get_data(self): return self.driver.device.read()

    def process_data(self, data):
        r = ord(data) # we store everything as int

        if self.currentFrame is None:
            # we're waiting for a new frame to start
            if r == z.SOF:
                self.currentFrame = [r]
                self.currentFrameTimestamp = time.time()

            elif r in z.FIRST_TO_STRING:
                # we received an ACK/NAK/CAN - let's decide what to do with it
                serialFrame = ConfirmationFrame.fromDeviceData(r)
                logging.info("<<<: %s", serialFrame.toString())

                if not self.driver.transactionProcessor.processIncomingFrame(serialFrame):
                    logging.warning("X<<: Received %s without requiring message confirmation - ignore", serialFrame.toString())

            else:
                return   # not a valid frame start --> ignore

        else:
            if time.time() - self.currentFrameTimestamp > 1.5:
                # timeout --> abandon frame
                self.currentFrame = None
                return

            self.currentFrame.append(r)
            if len(self.currentFrame) < self.currentFrame[1] + 2: return

            # we received a full data frame - let's read it
            dataFrame = DataFrame.fromDeviceData(self.currentFrame)
            logging.debug("<<<: [ %s ]: %s", " ".join(["%02x" % i for i in self.currentFrame]), "invalid" if dataFrame is None else "valid")

            tmp = self.currentFrame
            self.currentFrame = None

            if not dataFrame:   # nope! This frame is invalid --> let's send a NAK
                logging.warning("X<<: Received invalid frame [ %s ]", " ".join(["%02x" % i for i in tmp]))
                self.driver.writeToDevice(ConfirmationFrame(ConfirmationFrame.FrameType.NAK))
                return

            # good so far - let's send an ACK
            logging.info("<<<: %s", dataFrame.toString())
            self.driver.writeToDevice(ConfirmationFrame(ConfirmationFrame.FrameType.ACK))

            if not self.driver.transactionProcessor.processIncomingFrame(dataFrame):
                if not self.driver.incomingRequestProcessor.processIncomingFrame(dataFrame):
                    logging.warning("X<<: Received non-matching dataFrame - ignore: %s", dataFrame.toString())



class Driver():
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

    def __init__(self, port="/dev/ttyUSB0"):
        self.device = serial.Serial(port=port, baudrate=115200, timeout=0)

        # This synchronizes writing to the ZWave device. Likely not necessary, since Transactions
        # are also synced, but should avoid unlikely race conditions.
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

        # Step 1: Set up processor for incoming application updates
        self.incomingRequestProcessor = IncomingRequestProcessor()
        self.incomingRequestProcessor.start()

        # Step 2: Start listening to ZWave device
        self.deviceReader = DeviceReader(self)
        self.deviceReader.start()

        # Step 3: Start processing outgoing requests and transactions
        self.transactionProcessor = TransactionProcessor(self)
        self.transactionProcessor.start()


    def addListener(self, l):
        self.incomingRequestProcessor.addListener(l)


    RequestPriority = Enum("Priority", {"HIGHEST"  : MessageQueueOut.PRIO_HIGHEST,
                                        "HIGH_FAIR": MessageQueueOut.PRIO_HIGH   ,
                                        "LOW_FAIR" : MessageQueueOut.PRIO_LOW    ,
                                        "LOWEST"   : MessageQueueOut.PRIO_LOWEST })

    def sendRequest(self, serialRequest:SerialRequest, requestPriority=RequestPriority.LOWEST, timeout=None, callback=None):
        priority, q = requestPriority, -1
        if type(requestPriority) == tuple: priority, q = requestPriority

        if timeout is None:
            # default transaction timeouts 2.0/2.5 seconds (depending if we're expecting requests back)
            timeout = 2.5 if TransactionProcessor.hasRequests(serialRequest.serialCommand) else 2.0

        self.transactionProcessor.addRequest(priority.value, (serialRequest, timeout, callback), q)


    def shutdown(self):
        logging.warning("Terminating driver")
        self.transactionProcessor.shutdown()
        self.incomingRequestProcessor.shutdown()
        self.deviceReader.shutdown()


    def writeToDevice(self, serialFrame:SerialFrame):
        with self.writeLock:
            logging.info(">>>: %s", serialFrame.toString())
            logging.debug(">>>: [ %s ]", " ".join(["%02x" % i for i in serialFrame.toDeviceData()]))
            self.device.write(serialFrame.toDeviceData())
            self.device.flush()

