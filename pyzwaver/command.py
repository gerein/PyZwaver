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
command.py contain code for parsing and assembling API_APPLICATION_COMMAND
requests.

It also defines a bunch if `custom` commands that are used for administrative
purposes and have no wire representation.
"""

import logging

from . import zwave as z

# The command below prefixed with CUSTOM_COMMAND_* are made up
CUSTOM_COMMAND_PROTOCOL_INFO      = (256, 2)   # protocol info messages are transformed into this
CUSTOM_COMMAND_ACTIVE_SCENE       = (256, 3)   # unused
CUSTOM_COMMAND_FAILED_NODE        = (256, 4)   # if API_ZW_IS_FAILED_NODE_ID reports a failed node

_CUSTOM_COMMAND_STRINGS = {
    CUSTOM_COMMAND_PROTOCOL_INFO:      "_ProtocolInfo",
    CUSTOM_COMMAND_ACTIVE_SCENE:       "_Active_Scene",
    CUSTOM_COMMAND_FAILED_NODE:        "_FailedNode"
}

def IsCustom(key):
    return key in _CUSTOM_COMMAND_STRINGS

def StringifyCommand(key):
    if key in _CUSTOM_COMMAND_STRINGS: return _CUSTOM_COMMAND_STRINGS[key]
    if (key[0] << 8) + key[1] in z.SUBCMD_TO_STRING: return z.SUBCMD_TO_STRING[(key[0] << 8) + key[1]]
    return "Unknown:%02x:%02x" % (key[0], key[1])

def NodeDescription(basic_generic_specific):
    v = z.GENERIC_SPECIFIC_DB.get(basic_generic_specific[1] * 256 + basic_generic_specific[2])
    if v is None:
        logging.error("unknown generic device : %s", str(basic_generic_specific))
        return "unknown device_description: %s" % str(basic_generic_specific)
    return v[0]


class ParserAssembler:

    def __init__(self, formatTable):
        self.formatTable = formatTable

    def parse(self, data):
        if self.formatTable is None: return None
        values = {}

        for valueFormat in self.formatTable:
            dividerIndex = valueFormat.index("{")
            format, valueName = valueFormat[0:dividerIndex], valueFormat[dividerIndex + 1:-1]

            try:
                if   format == "A": value, data = bytes(data[1:1 + data[0]]), data[1 + data[0]:]
                elif format == "B": value, data = data[0], data[1:]
                elif format == "b": value, data = data[0], data[1:]
                elif format == "C": value, data = [(data[0] << 8) + data[1]] + data[2:7], data[7:]
                elif format == "F": value, data = {"encoding": data[0] >> 5, "text": data[1:1 + (data[0] & 0x1f)]}, data[1 + (data[0] & 0x1f):]
                elif format == "G": value, data = [(data[i + 0], (data[i + 2] << 8) + data[i + 3], (data[i + 5] << 8) + data[i + 6]) for i in range(0, len(data), 7)], []  # num, profile, event
                elif format == "L": value, data = data, []
                elif format == "N": value, data = data, []
                elif format == "O": value, data = data[0:8], data[8:]
                elif format == "R": value, data = {"size": len(data), "value": int.from_bytes(data, 'little', signed=False)}, []
                elif format == "T": value, data = {"size": data[0], "value": int.from_bytes(data[1:1 + data[0]], 'little', signed=False)}, data[1 + data[0]:]
                elif format == "V": value, data = {"size": data[0] & 0x7, "value": int.from_bytes(data[1:1 + (data[0] & 0x7)], 'big', signed=False)}, data[1 + (data[0] & 0x7):]
                elif format == "W": value, data = (data[0] << 8) + data[1], data[2:]
                elif format == "t": value, data = [(data[i] << 8) + data[i + 1] for i in range(1, data[0] * 2 + 1, 2)], data[data[0] * 2:]
                elif format == "E":
                    extensions = []
                    unencrypted = [data[0]]
                    has_unencypted_extension = (data[0] & 1) != 0
                    i = 1
                    while has_unencypted_extension:
                        extensions.append((data[i + 1], data[i + 2: i + data[i]]))
                        unencrypted += data[i: i + data[i]]
                        i += data[i]
                        has_unencypted_extension = (data[i + 1] & 128) != 0
                    value, data = {"mode": data[0], "extensions": extensions, "ciphertext": data[i:], "_plaintext": unencrypted, "_message_size": len(data)}, []
                elif format == "M":
                    rate =  (data[0] & 0b01100000) >> 5
                    kind =   data[0] & 0b00011111
                    unit = ((data[0] & 0b10000000) >> 5) | \
                           ((data[1] & 0b00011000) >> 3)
                    exp  =  (data[1] & 0b11100000) >> 5
                    size =   data[1] & 0b00000111

                    value = {
                        "type": kind,
                        "unit": unit,
                        "rate": rate,
                        "meterValue": int.from_bytes(data[2:2 + size], 'big', signed=True) / (10 ** exp)
                    }
                    data = data[2 + size:]

                    if len(data) > 0:
                        value["deltaTime"], data = (data[0] << 8) + data[1], data[2:]
                        if value["deltaTime"] != 0:
                            value["prevValue"] = int.from_bytes(data[0:size], 'big', signed=True) / (10 ** exp)
                            data = data[size:]

                elif format == "X":
                    exp = (data[0] & 0b11100000) >> 5
                    valueX = int.from_bytes(data[1: 1 + (data[0] & 0x7)], 'big', signed=True) / (10 ** exp)
                    value, data = {"unit": (data[0] >> 3) & 0x3, "sensorValue": valueX}, data[1 + (data[0] & 0x7):]

                elif format == "SZCMD":  value, data = NodeCommand.fromDeviceData(data[1:1 + data[0]]), data[1 + data[0]:]
                elif format == "CMD":    value, data = NodeCommand.fromDeviceData(data), []
                elif format == "NDMASK":
                    value = set()
                    for i in range(8 * 29):
                        if (data[i // 8] & (1 << (i % 8))) == 1: value.add(i + 1)
                    data = data[29:]
                else:
                    assert False, "unknown format"
            except:
                value = None

            if value is None:
                if format.islower(): break   # lower case are optional components
                logging.error("XXX: Error parsing incoming data-frame [ %s ] - partly parsed: %s", " ".join(["%02x" % i for i in data]), values)
                return None

            values[valueName] = value

        return values

    @staticmethod
    def _createMantissa(value, exp):
        v = int(value * pow(10, exp))
        return v.to_bytes(max((v.bit_length() + 7) // 8, 1), 'big', signed=True)

    def assemble(self, values):
        if self.formatTable is None: return None
        parameters = []
        for t in self.formatTable:
            mark = t.index("{")
            format, name = t[0:mark], t[mark + 1:-1]
            value = values.get(name)

            try:
                if value is None:
                    if format.islower(): continue  # lower case are optional components
                    else: raise Exception
                if   format == "A": data = [len(value)] + [int(x) for x in value]
                elif format == "B": data = [value]
                elif format == "b": data = [value]
                elif format == "C": data = [value[0] // 256, value[0] % 256, value[1], value[2], value[3], value[4], value[5]]
                elif format == "F": data = [(value["encoding"] << 5) | len(value["text"])] + value["text"]
                elif format == "G":
                    data = []
                    for num, profile, event in value: data += [num, 0, (profile >> 8) & 255, profile & 255, 0, (event >> 8) & 255, event & 255]
                elif format == "L": data = value
                elif format == "N": data = value
                elif format == "O": data = value
                elif format == "R": data = list(value["value"].to_bytes(value["size"], 'little', signed=False))
                elif format == "V": data = [value["size]"]] + list(value["value"].to_bytes(value["size"], 'big', signed=False))
                elif format == "W": data = [(value >> 8) & 0xff, value & 0xff]
                elif format == "t":
                    data = [len(value)]
                    for w in value:
                        data += [(w >> 8) & 0xff, w & 0xff]
                elif format == "E":
                    data = [value["mode"]]
                    for kind, datax in value["extensions"]: data += [len(value) + 2, kind, datax]
                    data += value["ciphertext"]
                elif format == "M":
                    m = self._createMantissa(value["meterValue"], value["exp"])
                    data = [(value["unit"] & 4) << 7 | value["rate"] << 5 | (value["type"] & 0x1f),
                              value["exp"] << 5 | (value["unit"] & 3) << 3 | len(m), m]
                    if "deltaTime" in value and "prevValue" in value:
                        m = self._createMantissa(value["prevValue"], value["exp"])
                        data += [value["deltaTime"] >> 8, value["deltaTime"] & 0xff] + m
                elif format == "X":
                    m = self._createMantissa(value["sensorValue"], value["exp"])
                    data = [value["exp"] << 5 | value["unit"] << 3 | len(m)] + m
                elif format == "SZCMD":
                    commandOut = value.toDeviceData()
                    data = [len(commandOut)] + commandOut
                elif format == "CMD": data = value.toDeviceData()
                else:
                    assert False, "unknown format"
            except:
                data = None

            if data is None:
                logging.error("XXX: Error assembling outgoing data-frame %s", values)
                return None

            parameters += data

        return parameters


class SerialRequest:
    CALLBACK_ID = 0

    @staticmethod
    def createCallbackId():
        SerialRequest.CALLBACK_ID = (SerialRequest.CALLBACK_ID + 1) % 256
        return SerialRequest.CALLBACK_ID

    def __init__(self, serialCommand:int=None, serialCommandValues:dict=None):
        if serialCommandValues is None: serialCommandValues = {}
        self.serialCommand = serialCommand
        self.serialCommandValues = serialCommandValues

        formatTable = z.SERIALCMD_TO_CONTROLLERREQUEST_PARSE_TABLE.get(self.serialCommand)
        if formatTable is not None and "B{callback}" in formatTable:
            self.serialCommandValues["callback"] = SerialRequest.createCallbackId()

    def parseData(self, data:list):
        if len(data) < 2: return False
        formatTable = z.SERIALCMD_TO_DEVICERESPONSE_PARSE_TABLE if data[0] == z.RESPONSE else z.SERIALCMD_TO_NODEREQUEST_PARSE_TABLE
        self.serialCommand = data[1]
        self.serialCommandValues = ParserAssembler(formatTable.get(self.serialCommand)).parse(data[2:])
        return self.serialCommandValues is not None

    @classmethod
    def fromDeviceData(cls, data):
        frame = SerialRequest()
        return frame if frame.parseData(data) else None

    def toDeviceData(self):
        formatTable = z.SERIALCMD_TO_CONTROLLERREQUEST_PARSE_TABLE.get(self.serialCommand)
        if formatTable is None: return None

        serialCommandParameters = ParserAssembler(formatTable).assemble(self.serialCommandValues)
        if serialCommandParameters is None: return None

        return [self.serialCommand] + serialCommandParameters

    def toString(self):
        valuesString = ""
        if self.serialCommandValues:
            for value in self.serialCommandValues:
                valuesString += ": " if valuesString == "" else ", "
                if   value == "classes":  valuesString += "classes: [" + " ".join([z.CMD_TO_STRING[commandClass] for commandClass in self.serialCommandValues[value]]) + "]"
                elif value == "command":  valuesString += "command: (" + (self.serialCommandValues["command"].toString() if self.serialCommandValues["command"] else "None") + ")"
                else:                     valuesString += value + ": " + str(self.serialCommandValues[value])

        return z.API_TO_STRING[self.serialCommand] + valuesString



class NodeCommand:

    def __init__(self, command:tuple=None, commandValues:dict=None):
        if commandValues is None: commandValues = {}
        self.command = command
        self.commandValues = commandValues

    def parseData(self, data):
        if len(data) < 2: return False
        self.command = (data[0], data[1])
        formatTable = z.SUBCMD_TO_PARSE_TABLE.get((self.command[0] << 8) + self.command[1])
        self.commandValues = ParserAssembler(formatTable).parse(data[2:])
        return self.commandValues is not None

    @classmethod
    def fromDeviceData(cls, data):
        frame = NodeCommand()
        return frame if frame.parseData(data) else None

    def toDeviceData(self):
        formatTable = z.SUBCMD_TO_PARSE_TABLE.get((self.command[0] << 8) + self.command[1])
        if formatTable is None: return None

        commandParameters = ParserAssembler(formatTable).assemble(self.commandValues)
        if commandParameters is None: return None

        return list(self.command) + commandParameters

    def toString(self):
        valuesString = ""
        if self.commandValues:
            for value in self.commandValues:
                valuesString += ": " if valuesString == "" else ", "
                if value == "command":  valuesString += "command: (" + self.commandValues["command"].toString() + ")"
                else:                   valuesString += value + ": " + str(self.commandValues[value])

        return z.SUBCMD_TO_STRING[(self.command[0] << 8) + self.command[1]] + valuesString
